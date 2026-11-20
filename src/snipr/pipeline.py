"""The pipeline stages as plain functions on files.

``analyze`` turns a VOD into a transcript plus chat and loudness curves.
``fetch_source`` downloads the picture for one clip. The database-backed
worker and the command line both call these.

Per-VOD working files live in ``data/vods/<platform>-<id>/``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, chat, config, platforms, transcribe
from .platforms import VodInfo

Progress = Callable[[str, float], None]


def _noop(stage: str, fraction: float) -> None:
    pass


@dataclass
class Analysis:
    vod: VodInfo
    workdir: Path
    audio_path: Path
    span: tuple[float, float]            # the stretch of the VOD that was analysed
    ranges: list[tuple[float, float]]    # the parts of the span that were transcribed
    loudness: np.ndarray                 # dBFS per second, index 0 = span[0]
    chat_rate: np.ndarray | None         # messages per second, index 0 = span[0]
    transcript: dict
    stats: dict = field(default_factory=dict)


def vod_dir(vod: VodInfo) -> Path:
    return config.subdir("vods") / f"{vod.platform}-{vod.vod_id}"


def analyze(vod: VodInfo, ranges: list[tuple[float, float]] | None = None,
            settings: dict | None = None, progress: Progress = _noop) -> Analysis:
    """Download the audio of a VOD and derive everything selection needs.

    ``ranges`` limits the work to parts of the VOD (the game filter). Each step
    saves its result in the VOD's working folder and is skipped on a rerun, so
    a failure late in the stage does not repeat the download or transcription.
    """
    settings = settings or config.load_settings()
    work = vod_dir(vod)
    work.mkdir(parents=True, exist_ok=True)
    ranges = [(max(0.0, lo), min(vod.duration, hi)) for lo, hi in (ranges or [(0.0, vod.duration)])]
    # The audio is always downloaded whole. A full download runs many pieces in
    # parallel and gives a file whose clock is exactly the VOD's; a partial one
    # goes through ffmpeg piece by piece and starts at an offset that is only
    # known to within a stream segment. Only the costly steps, chat and
    # transcription, are limited to the wanted ranges.
    span = (0.0, vod.duration)
    busy = (int(min(lo for lo, _ in ranges)), int(max(hi for _, hi in ranges)))
    range_key = [[round(a), round(b)] for a, b in ranges]
    state_path = work / "analysis.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    if state.get("v") != 4:            # bump when the format of a saved step changes
        state = {"v": 4}

    def remember(**values) -> None:
        state.update(values)
        state_path.write_text(json.dumps(state), encoding="utf-8")

    # 1. audio
    audio_path = Path(state["audio"]) if state.get("audio") and Path(state["audio"]).exists() else None
    if audio_path is None:
        progress("download audio", 0.0)
        audio_path = platforms.download_audio(vod.url, work / "audio",
                                              progress=lambda f: progress("download audio", f))
        remember(audio=str(audio_path), loudness=False)
    progress("download audio", 1.0)

    # 2. loudness
    loud_path = work / "loudness.npy"
    if state.get("loudness") and loud_path.exists():
        loudness = np.load(loud_path)
    else:
        progress("loudness", 0.0)
        loudness = audio.energy_curve(audio_path)
        np.save(loud_path, loudness)
        remember(loudness=True)
    progress("loudness", 1.0)

    # 3. chat (Twitch only; other platforms have no replay we can read)
    chat_path = work / "chat.npy"
    chat_rate: np.ndarray | None = None
    covered = state.get("chat_span")
    if state.get("chat") == "none":
        chat_rate = None
    elif covered and covered[0] <= busy[0] and busy[1] <= covered[1] and chat_path.exists():
        chat_rate = np.load(chat_path)
    elif vod.platform == "twitch":
        progress("chat", 0.0)
        try:
            part = chat.twitch_chat_rate(vod.vod_id, vod.duration, busy[0], busy[1],
                                         progress=lambda f: progress("chat", f))
            chat_rate = np.zeros(max(len(loudness), busy[0] + len(part)), dtype=np.float32)
            chat_rate[busy[0]:busy[0] + len(part)] = part
            np.save(chat_path, chat_rate)
            remember(chat=True, chat_span=list(busy))
        except chat.ChatError as exc:
            remember(chat="none", chat_error=str(exc))
            chat_rate = None
    else:
        remember(chat="none")
    progress("chat", 1.0)

    # 4. transcript
    transcript_path = work / "transcript.json"
    if state.get("transcript_ranges") == range_key and transcript_path.exists():
        transcript = transcribe.load(transcript_path)
        stats = state.get("stats", {})
    else:
        progress("transcribe", 0.0)
        cfg = settings["transcription"]
        whole = len(ranges) == 1 and ranges[0][0] <= 1.0 and ranges[0][1] >= vod.duration - 1.0
        transcript, tstats = transcribe.transcribe(
            audio_path, cfg["model"], cfg["compute_type"], int(cfg.get("batch_size", 8)),
            ranges=None if whole else ranges, progress=lambda f: progress("transcribe", f))
        transcribe.save(transcript, transcript_path)
        stats = {"audio_seconds": round(tstats.audio_seconds, 1), "wall_seconds": round(tstats.wall_seconds, 1),
                 "speed": round(tstats.speed, 2), "device": tstats.device, "model": tstats.model,
                 "compute_type": tstats.compute_type}
        remember(transcript_ranges=range_key, stats=stats)
    progress("transcribe", 1.0)

    return Analysis(vod, work, audio_path, span, ranges, loudness, chat_rate, transcript, stats)


def produce(source: Path, source_start: float, start: float, end: float, transcript: dict,
            headline: str, layout: dict | None, out_path: Path) -> Path:
    """Render one clip: reframe to 9:16, burn in the headline and the captions."""
    import shutil
    import tempfile

    from . import captions, render

    words = [[w[0] - start, w[1] - start, w[2]] for w in transcribe.words_between(transcript, start, end)]
    overlay_dir = Path(tempfile.mkdtemp(prefix="snipr-overlay-"))
    options = layout or {}
    try:
        overlays = captions.build_overlays(
            words, headline if options.get("show_headline", True) else "", end - start, overlay_dir,
            stacked=render.is_stacked(layout), style=captions.get_style(options.get("caption_style")))
        return render.render_clip(source, source_start, start, end, out_path, layout, overlays)
    finally:
        shutil.rmtree(overlay_dir, ignore_errors=True)


def fetch_source(vod: VodInfo, analysis_audio: Path, span_start: float, start: float, end: float,
                 out_path: Path, pad_s: float = 20.0, height: int = 1080,
                 progress: Callable[[float], None] | None = None) -> tuple[Path, float]:
    """Download the picture for ``start``..``end`` plus padding on both sides.

    Returns the file and the VOD time at which it really begins. The padding is
    what lets a clip be extended in review without another download.
    """
    lo = max(0.0, start - pad_s)
    hi = min(vod.duration, end + pad_s) if vod.duration else end + pad_s
    path = platforms.download_video(vod.url, out_path, lo, hi, height, progress)
    # A segment that begins before the analysed span has nothing to match at
    # its start, so compare from the point where the analysis audio begins.
    skip = max(0.0, span_start - lo + 1.0) if lo < span_start else 0.0
    true_start = audio.find_offset(analysis_audio, lo - span_start, path, segment_skip=skip) + span_start
    return path, true_start
