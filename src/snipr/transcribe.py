"""Speech to text with word timings, using faster-whisper on the GPU.

Long VODs are transcribed in chunks cut with ffmpeg, so memory use stays flat
and progress can be reported. Times in the result are seconds on the timeline
of the audio file that was passed in, plus ``time_offset``.
"""

from __future__ import annotations

import json
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import audio, gpu

CHUNK_S = 1800.0


@dataclass
class TranscribeStats:
    audio_seconds: float
    wall_seconds: float
    device: str
    model: str
    compute_type: str

    @property
    def speed(self) -> float:
        """How many seconds of audio are transcribed per second of waiting."""
        return self.audio_seconds / self.wall_seconds if self.wall_seconds else 0.0


_model_cache: dict[tuple[str, str, str], object] = {}


def _load_model(model_name: str, compute_type: str):
    """Load the model on the GPU, falling back to the CPU if CUDA is unusable."""
    from faster_whisper import WhisperModel

    gpu.enable_cuda_dlls()
    attempts = [("cuda", compute_type), ("cpu", "int8")]
    last: Exception | None = None
    for device, ctype in attempts:
        key = (model_name, device, ctype)
        if key in _model_cache:
            return _model_cache[key], device, ctype
        try:
            model = WhisperModel(model_name, device=device, compute_type=ctype)
            _model_cache[key] = model
            return model, device, ctype
        except Exception as exc:  # noqa: BLE001 - CTranslate2 raises plain RuntimeError/ValueError
            last = exc
    raise RuntimeError(f"Could not load the Whisper model '{model_name}': {last!r}")


def release_models() -> None:
    """Drop loaded models so the GPU memory is free for encoding."""
    _model_cache.clear()


def _run_chunk(model, wav: Path, batch_size: int, language: str) -> list[dict]:
    from faster_whisper import BatchedInferencePipeline

    def collect(segments) -> list[dict]:
        out = []
        for seg in segments:
            words = [[round(w.start, 3), round(w.end, 3), w.word.strip()] for w in (seg.words or [])
                     if w.word.strip()]
            text = seg.text.strip()
            if text:
                out.append({"s": round(seg.start, 3), "e": round(seg.end, 3), "t": text, "w": words})
        return out

    # The samples are passed in directly. Letting faster-whisper open the file
    # goes through PyAV, whose current release rejects an argument it passes.
    samples = _read_wav(wav)
    size = batch_size
    while size > 1:
        try:
            pipeline = BatchedInferencePipeline(model=model)
            segments, _ = pipeline.transcribe(samples, batch_size=size, language=language,
                                              word_timestamps=True)
            return collect(segments)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower():
                raise
            size //= 2  # not enough VRAM for this batch; try a smaller one
    segments, _ = model.transcribe(samples, language=language, word_timestamps=True,
                                   vad_filter=True, beam_size=5)
    return collect(segments)


def _read_wav(path: Path):
    """Load a 16 kHz mono 16-bit WAV as float32 samples in -1..1."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != audio.SAMPLE_RATE or handle.getnchannels() != 1:
            raise ValueError(f"{path} is not 16 kHz mono")
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe(path: Path, model_name: str = "medium", compute_type: str = "int8_float16",
               batch_size: int = 8, language: str = "en",
               ranges: list[tuple[float, float]] | None = None, time_offset: float = 0.0,
               progress: Callable[[float], None] | None = None) -> tuple[dict, TranscribeStats]:
    """Transcribe ``path`` (or only ``ranges`` of it, in file seconds).

    Returns ``({"segments": [{"s", "e", "t", "w": [[start, end, word], ...]}]}, stats)``.
    """
    total = audio_duration(path)
    spans = ranges or [(0.0, total)]
    chunks: list[tuple[float, float]] = []
    for lo, hi in spans:
        lo, hi = max(0.0, lo), min(total, hi)
        at = lo
        while at < hi - 0.5:
            chunks.append((at, min(hi, at + CHUNK_S)))
            at += CHUNK_S
    audio_seconds = sum(hi - lo for lo, hi in chunks)

    model, device, ctype = _load_model(model_name, compute_type)
    segments: list[dict] = []
    started = time.perf_counter()
    done = 0.0
    with tempfile.TemporaryDirectory(prefix="snipr-asr-") as tmp:
        for index, (lo, hi) in enumerate(chunks):
            wav = audio.to_wav(path, Path(tmp) / f"chunk{index}.wav", start=lo, duration=hi - lo)
            for seg in _run_chunk(model, wav, batch_size, language):
                shift = lo + time_offset
                segments.append({
                    "s": round(seg["s"] + shift, 3),
                    "e": round(seg["e"] + shift, 3),
                    "t": seg["t"],
                    "w": [[round(w[0] + shift, 3), round(w[1] + shift, 3), w[2]] for w in seg["w"]],
                })
            wav.unlink(missing_ok=True)
            done += hi - lo
            if progress:
                progress(done / audio_seconds if audio_seconds else 1.0)
    wall = time.perf_counter() - started
    segments.sort(key=lambda s: s["s"])
    words = [w for seg in segments for w in seg["w"]]
    stats = TranscribeStats(audio_seconds, wall, device, model_name, ctype)
    return {"segments": split_sentences(words)}, stats


def split_sentences(words: list[list], max_gap_s: float = 1.0, max_len_s: float = 25.0) -> list[dict]:
    """Regroup words into sentences.

    The batched model returns blocks of up to 30 seconds, which are useless as
    clip boundaries. A sentence ends at closing punctuation, at a pause longer
    than ``max_gap_s``, or once it has run for ``max_len_s``.
    """
    sentences: list[dict] = []
    current: list[list] = []

    def flush() -> None:
        if current:
            sentences.append({"s": current[0][0], "e": current[-1][1],
                              "t": " ".join(w[2] for w in current), "w": list(current)})
            current.clear()

    for index, word in enumerate(words):
        if current and word[0] - current[-1][1] > max_gap_s:
            flush()
        current.append(word)
        nxt = words[index + 1] if index + 1 < len(words) else None
        ends_sentence = word[2].rstrip('"\')').endswith((".", "?", "!"))
        too_long = word[1] - current[0][0] >= max_len_s
        if ends_sentence or too_long or nxt is None:
            flush()
    return sentences


def audio_duration(path: Path) -> float:
    from .platforms import probe_duration

    return probe_duration(path)


def save(transcript: dict, path: Path) -> None:
    path.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def words_between(transcript: dict, start: float, end: float) -> list[list]:
    """All words whose midpoint falls inside ``start``..``end``."""
    out = []
    for seg in transcript["segments"]:
        if seg["e"] < start or seg["s"] > end:
            continue
        for word in seg["w"]:
            mid = (word[0] + word[1]) / 2
            if start <= mid <= end:
                out.append(word)
    return out


def text_between(transcript: dict, start: float, end: float) -> str:
    return " ".join(w[2] for w in words_between(transcript, start, end))
