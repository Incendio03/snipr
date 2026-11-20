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
        target = out_dir / f"clip{index}.mp4"
        meta = {}
        if args.layout == "source":
            clip_path = render.cut_plain(source, source_start, seg.start, seg.end, target)
        else:
            from . import llm as llm_module, transcribe
            text = transcribe.text_between(analysis.transcript, seg.start, seg.end)
            game = next((c["title"] for c in vod.chapters if c["start"] <= seg.start < c["end"]), "")
            meta = (llm.write_metadata(text, vod.uploader, vod.title, seg.title, game) if llm
                    else llm_module.fallback_metadata(text, vod.title, game))
            clip_path = pipeline.produce(source, source_start, seg.start, seg.end, analysis.transcript,
                                         meta["headline"], {"type": "fill", "crop": None}, target)
            print(f"    headline: {meta['headline']}")
        print(f"    {clip_path}")
        report.append({"file": str(clip_path), "start": seg.start, "end": seg.end, "score": seg.score,
                       "title": seg.title, "reason": seg.reason, "flags": list(seg.flags), **meta})
    (out_dir / "candidates.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {len(report)} clips to {out_dir}")
    return 0


def _streamer_or_defaults(streamer_id: int | None) -> dict:
    from . import db

    db.init()
    if streamer_id is None:
        return {"id": None, "name": "", "games": "", "clip_min_s": 20.0, "clip_max_s": 60.0}
    streamer = db.get("streamers", streamer_id)
    if streamer is None:
        raise SystemExit(f"There is no streamer with id {streamer_id}.")
    return streamer


def cmd_nominate(args: argparse.Namespace) -> int:
    """Write a stream's candidate moments, with their transcript, to a file for a person or an assistant to read.

    This is the first half of picking clips by hand: Snipr finds where chat and
    loudness spike and transcribes only those stretches. The reader chooses and
    the picks come back in through ``import-picks``.
    """
    from . import pipeline, platforms, scoring, selection

    settings = config.load_settings()
    streamer = _streamer_or_defaults(args.streamer_id)
    vod = platforms.vod_info(args.url)
    games = scoring.parse_games(args.games if args.games is not None else streamer["games"])
    ranges = scoring.game_ranges(games, vod.chapters, vod.title, vod.duration)
    if ranges is None:
        print(f"No part of this stream matches the game filter ({', '.join(games)}). Nothing to nominate.")
        return 1
    print(f"{vod.uploader}: {vod.title}  ({_clock(vod.duration)})")
    signals = pipeline.analyze(vod, ranges, settings, _printer(), transcribe_speech=False)
    sel = settings["selection"]
    request = selection.SelectionRequest(
        mode="highlights", instructions="", max_clips=args.count, clip_min_s=float(streamer["clip_min_s"]),
        clip_max_s=float(streamer["clip_max_s"]), min_score=0.0, duration=vod.duration, span_start=0.0,
        ranges=signals.ranges, transcript={"segments": []}, loudness=signals.loudness,
        chat_rate=signals.chat_rate, nominations=args.count, window_s=sel["window_s"], step_s=sel["step_s"],
        chat_weight=sel["chat_weight"], audio_weight=sel["audio_weight"])
    nominated = selection.nominate_highlights(request)
    if not nominated:
        print("Nothing stood out in chat or loudness.")
        return 1
    analysis = pipeline.analyze(vod, ranges, settings, _printer(),
                                speech_ranges=selection.speech_ranges(nominated, vod.duration))
    from . import llm as llm_module

    lines = [f"Stream: {vod.title}", f"Streamer: {vod.uploader}", f"Length: {_clock(vod.duration)}",
             f"Clip length allowed: {streamer['clip_min_s']:.0f} to {streamer['clip_max_s']:.0f} seconds",
             f"Chat replay: {'yes' if analysis.chat_rate is not None else 'no (loudness only)'}",
             "", "Each candidate is a stretch where loudness or chat jumped. Lines start with the time in seconds.",
             ""]
    for index, win in enumerate(sorted(nominated, key=lambda w: w.raw, reverse=True), 1):
        lo, hi = max(0.0, win.start - selection.SPEECH_BEFORE_S), win.end + selection.SPEECH_AFTER_S
        lines += [f"=== Candidate {index} | flagged {win.start:.0f}-{win.end:.0f} s ({_clock(win.start)}) | "
                  f"loudness spike {win.audio_z:.1f} | chat spike {win.chat_z:.1f}",
                  llm_module.transcript_lines(analysis.transcript["segments"], lo, hi) or "(no speech here)", ""]
    out = Path(args.out) if args.out else analysis.workdir / "nominations.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {len(nominated)} candidates to {out}")
    return 0


def cmd_import_picks(args: argparse.Namespace) -> int:
    """Put hand-picked clips into the review queue and queue them for rendering.

    The picks file is a JSON list. Each item needs ``start``, ``end`` and
    ``score`` and may carry ``title``, ``reason``, ``flags``, ``headline``,
    ``description``, ``hashtags`` and ``narration``.
    """
    from . import db, pipeline, platforms, scoring, transcribe, worker

    settings = config.load_settings()
    streamer = _streamer_or_defaults(args.streamer_id)
    if streamer["id"] is None:
        raise SystemExit("import-picks needs --streamer-id so the clips get that streamer's layout and limits.")
    raw = json.loads(Path(args.picks).read_text(encoding="utf-8"))
    platform, key = platforms.parse_vod_url(args.url)
    vod = db.row("SELECT * FROM vods WHERE streamer_id = ? AND platform = ? AND vod_key = ?", (streamer["id"], platform, key))
    info = platforms.vod_info(args.url)
    folder = pipeline.vod_dir(info)
    if not (folder / "transcript.json").exists():
        raise SystemExit("This stream has not been analysed yet. Run 'snipr nominate' on it first.")
    audio_files = sorted(p for p in folder.glob("audio.*") if not p.name.endswith(".part"))
    fields = {"title": info.title, "duration": info.duration, "chapters": info.chapters, "status": "done",
              "error": None, "span_start": 0.0, "ranges": [[0.0, info.duration]],
              "audio_path": str(audio_files[0]) if audio_files else None, "updated_at": db.now()}
    if vod is None:
        vod_id = db.insert("vods", {"streamer_id": streamer["id"], "platform": platform, "vod_key": key,
                                    "source_url": info.url, "created_at": db.now(), **fields})
    else:
        vod_id = vod["id"]
        db.update("vods", vod_id, fields)

    transcript = transcribe.load(folder / "transcript.json")
    picks = scoring.validate_segments(raw, duration=info.duration, min_len=float(streamer["clip_min_s"]),
                                      max_len=float(streamer["clip_max_s"]), min_score=0.0, limit=len(raw),
                                      transcript_segments=transcript["segments"])
    extras = {round(float(item["start"]), 1): item for item in raw
              if isinstance(item, dict) and isinstance(item.get("start"), (int, float))}
    existing = db.rows("SELECT start_s, end_s FROM clips WHERE vod_id = ? AND status != 'rejected'", (vod_id,))
    credit = worker.credit_line(streamer) if settings["publishing"]["append_streamer_credit"] else ""
    added = 0
    for pick in sorted(picks, key=lambda p: p.start):
        if any(pick.start < e["end_s"] and e["start_s"] < pick.end for e in existing):
            print(f"  skipped {_clock(pick.start)}: overlaps a clip already in the queue")
            continue
        extra = extras.get(round(pick.start, 1), {})
        tags = ["".join(ch for ch in str(t).lower() if ch.isalnum()) for t in extra.get("hashtags") or []]
        clip_id = db.insert("clips", {
            "vod_id": vod_id, "start_s": pick.start, "end_s": pick.end, "score": pick.score,
            "reason": pick.reason, "flags": list(pick.flags), "layout": worker.streamer_layout(streamer),
            "headline": " ".join(str(extra.get("headline") or "").split()), "title": pick.title or info.title,
            "description": str(extra.get("description") or "").strip(), "credit": credit,
            "hashtags": [t for t in tags if t][:5], "working_title": pick.title,
            "narration": " ".join(str(extra.get("narration") or "").split()),
            "status": "pending_review", "render_state": "rendering",
            "destination_ids": [streamer["default_destination_id"]] if streamer["default_destination_id"] else [],
            "created_at": db.now()})
        worker.enqueue("render_clip", clip_id, f"Render: {pick.title or 'clip'}", force=True)
        added += 1
        print(f"  queued {_clock(pick.start)}-{_clock(pick.end)}  {pick.title}")
    dropped = len(raw) - len(picks)
    if dropped:
        print(f"  {dropped} pick(s) were dropped: bad times, or outside the streamer's clip length limits")
    print(f"Added {added} clip(s) to the review queue. They render as soon as Snipr is running.")
    return 0


def cmd_set_key(args: argparse.ArgumentParser) -> int:
    """Store the Anthropic API key in Windows Credential Manager."""
    import getpass

    from . import secrets

    if args.clear:
        secrets.delete("anthropic_api_key")
        print("Removed the stored Anthropic API key.")
        return 0
    key = getpass.getpass("Paste the Anthropic API key (it will not be shown): ").strip()
    if not key:
        print("Nothing entered; the stored key is unchanged.")
        return 1
    secrets.put("anthropic_api_key", key)
    print("Saved to Windows Credential Manager under 'snipr'.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the web page, the worker and the scheduler."""
    import uvicorn

    server = config.load_settings()["server"]
    host, port = server["host"], int(args.port or server["port"])
    url = f"http://{host}:{port}/"
    print(f"Snipr {__version__} is running at {url}  (Ctrl+C to stop)")
    if args.open:
        import threading
        import webbrowser
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run("snipr.web.app:app", host=host, port=port, log_level="warning")
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
    clip.add_argument("--layout", choices=["fill", "source"], default="fill",
                      help="fill: 9:16 centre crop with headline and captions; source: original framing")
    clip.add_argument("--out", help="folder for the clips (default: the VOD's working folder)")
    clip.set_defaults(func=cmd_clip)

    serve = sub.add_parser("serve", help="run the web page, the worker and the scheduler")
    serve.add_argument("--port", type=int, help="port to listen on (default 8765)")
    serve.add_argument("--open", action="store_true", help="open the page in the browser")
    serve.set_defaults(func=cmd_serve)

    nominate = sub.add_parser("nominate", help="write a stream's candidate moments and their transcript to a file")
    nominate.add_argument("url", help="link to a Twitch VOD or a YouTube stream replay")
    nominate.add_argument("--streamer-id", type=int, help="use this streamer's games and clip length limits")
    nominate.add_argument("--games", help="game filter, comma separated (default: the streamer's)")
    nominate.add_argument("--count", type=int, default=40, help="how many candidates to write (default 40)")
    nominate.add_argument("--out", help="file to write (default: nominations.txt in the stream's working folder)")
    nominate.set_defaults(func=cmd_nominate)

    picks = sub.add_parser("import-picks", help="put hand-picked clips into the review queue")
    picks.add_argument("url", help="link to the stream the picks are from")
    picks.add_argument("picks", help="JSON file with the picks")
    picks.add_argument("--streamer-id", type=int, required=True, help="the streamer the clips belong to")
    picks.set_defaults(func=cmd_import_picks)

    key = sub.add_parser("set-key", help="store the Anthropic API key in Windows Credential Manager")
    key.add_argument("--clear", action="store_true", help="remove the stored key")
    key.set_defaults(func=cmd_set_key)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
