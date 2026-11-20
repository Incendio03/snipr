"""Timed background work: find new streams, release due posts, clean up files."""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, db, pipeline, platforms, publish, worker
from .platforms import PlatformError, VodInfo


def _published(vod: VodInfo) -> str | None:
    if not vod.published_at:
        return None
    return datetime.fromtimestamp(vod.published_at).replace(microsecond=0).isoformat()


def add_vod(streamer: dict, vod: VodInfo, status: str = "queued", force: bool = False) -> tuple[int, int | None]:
    """Record a stream for a streamer and, unless it is ignored, queue it. Returns (vod id, job id)."""
    vod_id = db.insert("vods", {
        "streamer_id": streamer["id"], "platform": vod.platform, "vod_key": vod.vod_id, "source_url": vod.url,
        "title": vod.title, "duration": vod.duration, "published_at": _published(vod), "status": status,
        "chapters": vod.chapters, "created_at": db.now(), "updated_at": db.now()})
    job_id = None
    if status == "queued":
        job_id = worker.enqueue("process_vod", vod_id, f"{streamer['name']}: {vod.title}", force=force)
    return vod_id, job_id


def poll_streamer(streamer: dict) -> int:
    """Look for finished streams we have not seen. Returns how many were queued.

    The first time a streamer is checked only their latest stream is queued;
    the older ones are recorded as ignored so a new streamer does not start
    with a backlog of days of processing.
    """
    channel = platforms.parse_channel(streamer["channel_url"])
    listed = platforms.list_vods(channel, limit=5)
    known = {r["vod_key"] for r in db.rows("SELECT vod_key FROM vods WHERE streamer_id = ?", (streamer["id"],))}
    first_time = not known
    queued = 0
    fresh = [v for v in listed if v.vod_id not in known]
    for position, vod in enumerate(fresh):
        if first_time and position > 0:
            add_vod(streamer, vod, status="ignored")
            continue
        details = platforms.vod_info(vod.url)
        if details.live_status in ("is_live", "is_upcoming", "post_live"):
            continue                    # still being recorded or processed; seen again on the next check
        add_vod(streamer, details)
        queued += 1
    db.update("streamers", streamer["id"], {"last_checked_at": db.now()})
    return queued


def poll_all() -> None:
    for streamer in db.rows("SELECT * FROM streamers WHERE enabled = 1"):
        try:
            queued = poll_streamer(streamer)
            if queued:
                noun = "stream" if queued == 1 else "streams"
                db.event(f"Found {queued} new {noun} from {streamer['name']}.")
        except PlatformError as exc:
            db.event(f"Could not check {streamer['name']} for new streams: {exc}", "warn")
        except Exception as exc:  # noqa: BLE001 - one streamer must not stop the others
            db.event(f"Could not check {streamer['name']} for new streams: {exc!r}", "warn")


def cleanup() -> None:
    """Delete footage that is no longer needed. Transcripts and curves are kept (they are small)."""
    storage = config.load_settings()["storage"]
    now = datetime.now()
    source_cutoff = (now - timedelta(days=float(storage["source_max_age_days"]))).isoformat()
    rejected_cutoff = (now - timedelta(days=float(storage["keep_rejected_clips_days"]))).isoformat()

    # Rendered files of clips rejected long enough ago.
    for clip in db.rows("SELECT id, file_path FROM clips WHERE status = 'rejected' AND file_path IS NOT NULL "
                        "AND reviewed_at < ?", (rejected_cutoff,)):
        Path(clip["file_path"]).unlink(missing_ok=True)
        (config.subdir("thumbs") / f"{clip['id']}.jpg").unlink(missing_ok=True)
        db.update("clips", clip["id"], {"file_path": None})

    # Source footage and audio, once every clip of the stream is reviewed or the stream is old.
    for vod in db.rows("SELECT * FROM vods WHERE status IN ('done', 'failed', 'skipped') AND audio_path IS NOT NULL"):
        waiting = db.scalar("SELECT COUNT(*) FROM clips WHERE vod_id = ? AND status IN ('candidate', 'pending_review')",
                            (vod["id"],))
        if waiting and vod["updated_at"] > source_cutoff:
            continue
        folder = pipeline.vod_dir(VodInfo(vod["platform"], vod["vod_key"], vod["source_url"], vod["title"],
                                          vod["duration"]))
        shutil.rmtree(folder / "sources", ignore_errors=True)
        Path(vod["audio_path"]).unlink(missing_ok=True)
        db.update("vods", vod["id"], {"audio_path": None})
        db.execute("UPDATE clips SET source_path = NULL, source_start = NULL, source_end = NULL WHERE vod_id = ?",
                   (vod["id"],))

    # Sample frames that no streamer uses.
    used = {r["sample_frame"] for r in db.rows("SELECT sample_frame FROM streamers WHERE sample_frame IS NOT NULL")}
    for frame in config.subdir("frames").glob("*.jpg"):
        age = now - datetime.fromtimestamp(frame.stat().st_mtime)
        if frame.stem not in used and age > timedelta(days=1):
            frame.unlink(missing_ok=True)


def start() -> BackgroundScheduler:
    settings = config.load_settings()
    scheduler = BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300})
    # The first check runs a few seconds after start: it catches up on streams published while the PC was off.
    scheduler.add_job(poll_all, "interval", minutes=max(1, int(settings["processing"]["poll_minutes"])),
                      next_run_time=datetime.now() + timedelta(seconds=10), id="poll")
    scheduler.add_job(publish.release_due, "interval", seconds=30, id="release")
    scheduler.add_job(cleanup, "interval", hours=6, next_run_time=datetime.now() + timedelta(minutes=2), id="cleanup")
    scheduler.start()
    return scheduler
