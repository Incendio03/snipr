"""Command line entry point: ``snipr <command>``."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, config


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _printer():
    last = {"stage": "", "at": 0.0}

    def progress(stage: str, fraction: float) -> None:
        now = time.monotonic()
        if stage != last["stage"] or fraction >= 1.0 or now - last["at"] > 5:
            print(f"  {stage}: {fraction * 100:3.0f}%", flush=True)
            last.update(stage=stage, at=now)

    return progress


def cmd_clip(args: argparse.Namespace) -> int:
    """VOD link in, transcript and candidate clips out (milestone 1)."""
    from . import pipeline, platforms, render, selection

    settings = config.load_settings()
    print(f"Reading {args.url}")
    vod = platforms.vod_info(args.url)
    print(f"  {vod.uploader}: {vod.title}  ({_clock(vod.duration)})")
    for chapter in vod.chapters:
        print(f"    {_clock(chapter['start'])}-{_clock(chapter['end'])}  {chapter['title']}")

    ranges = [(0.0, vod.duration)]
    if args.game:
        from . import scoring
        found = scoring.game_ranges(scoring.parse_games(args.game), vod.chapters, vod.title, vod.duration)
        if found is None:
            print(f"No part of this VOD matches the game filter '{args.game}'. Nothing to do.")
            return 0
        ranges = found
    if args.start is not None or args.end is not None:
        lo, hi = args.start or 0.0, args.end or vod.duration
        ranges = [(max(a, lo), min(b, hi)) for a, b in ranges if min(b, hi) - max(a, lo) > 30]
        if not ranges:
            print("The --start/--end range does not overlap the part of the VOD to process.")
            return 1
    print("Processing " + ", ".join(f"{_clock(a)}-{_clock(b)}" for a, b in ranges))

    analysis = pipeline.analyze(vod, ranges, settings, _printer())
    stats = analysis.stats
    if stats:
        print(f"Transcribed {_clock(stats['audio_seconds'])} of audio in {_clock(stats['wall_seconds'])} "
              f"({stats['speed']}x real time, {stats['model']} on {stats['device']}, {stats['compute_type']})")
    print(f"Transcript: {len(analysis.transcript['segments'])} segments. "
          f"Chat replay: {'yes' if analysis.chat_rate is not None else 'not available'}.")

    sel = settings["selection"]
    request = selection.SelectionRequest(
        mode=args.mode, instructions=args.instructions or "", max_clips=args.count,
        clip_min_s=args.min_len, clip_max_s=args.max_len,
        min_score=sel["min_score"] if args.min_score is None else args.min_score,
        duration=vod.duration, span_start=analysis.span[0], ranges=analysis.ranges,
        transcript=analysis.transcript, loudness=analysis.loudness, chat_rate=analysis.chat_rate,
        nominations=sel["highlight_nominations"], window_s=sel["window_s"], step_s=sel["step_s"],
        chat_weight=sel["chat_weight"], audio_weight=sel["audio_weight"],
        streamer=vod.uploader, vod_title=vod.title)

    llm = None
    if not args.no_llm:
        from . import llm as llm_module
        llm = llm_module.get_provider(settings)
    if llm is None:
        print("No LLM configured: picking on chat and loudness spikes only.")
    picks = selection.select(request, llm)
    if not picks:
        print("No moment reached the quality bar.")
        return 0

    out_dir = Path(args.out) if args.out else analysis.workdir / "candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    report = []
    for index, seg in enumerate(picks, 1):
        print(f"Clip {index}: {_clock(seg.start)}-{_clock(seg.end)}  score {seg.score:.2f}  {seg.title}")
        print(f"    {seg.reason}")
        source, source_start = pipeline.fetch_source(
            vod, analysis.audio_path, analysis.span[0], seg.start, seg.end,
            analysis.workdir / "sources" / f"cand{index}",
            pad_s=settings["ingest"]["source_pad_s"], height=settings["ingest"]["video_height"])
        clip_path = render.cut_plain(source, source_start, seg.start, seg.end, out_dir / f"clip{index}.mp4")
        print(f"    {clip_path}")
        report.append({"file": str(clip_path), "start": seg.start, "end": seg.end, "score": seg.score,
                       "title": seg.title, "reason": seg.reason, "flags": list(seg.flags)})
    (out_dir / "candidates.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {len(report)} clips to {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="snipr", description="Streamer VODs in, short clips out.")
    parser.add_argument("--version", action="version", version=f"snipr {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    clip = sub.add_parser("clip", help="process one VOD link into candidate clips")
    clip.add_argument("url", help="link to a Twitch VOD or a YouTube stream replay")
    clip.add_argument("--mode", choices=["highlights", "informative", "custom"], default="highlights")
    clip.add_argument("--instructions", help="what to look for (custom mode)")
    clip.add_argument("--game", help="only process parts of the stream in these games, comma separated")
    clip.add_argument("--count", type=int, default=5, help="most clips to produce (default 5)")
    clip.add_argument("--min-len", type=float, default=20.0)
    clip.add_argument("--max-len", type=float, default=60.0)
    clip.add_argument("--min-score", type=float, default=None)
    clip.add_argument("--start", type=float, help="only process the VOD from this second")
    clip.add_argument("--end", type=float, help="only process the VOD up to this second")
    clip.add_argument("--no-llm", action="store_true", help="pick on chat and loudness alone")
    clip.add_argument("--out", help="folder for the clips (default: the VOD's working folder)")
    clip.set_defaults(func=cmd_clip)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
