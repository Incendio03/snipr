"""The job worker: one job at a time, with retries and the processing window.

Jobs live in the ``jobs`` table. ``process_vod`` takes a stream from link to
clips waiting for review. ``render_clip`` re-renders one clip after an edit.
Only one job runs at a time, so transcription and encoding never compete for
the GPU.
"""

from __future__ import annotations

import shutil
import threading
import time as time_module
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from . import config, db, llm, notify, pipeline, platforms, publish, render, scoring, selection, transcribe
from .platforms import VodInfo

HEAVY = {"process_vod"}          # held for the processing window; render_clip is short and user-triggered
RETRY_DELAYS = [30, 120, 600]
MIN_FREE_GB = 5.0

STAGE_LABELS = {"download audio": "Downloading audio", "loudness": "Measuring loudness",
                "chat": "Reading chat replay", "transcribe": "Transcribing"}


class Permanent(Exception):
    """A failure that retrying cannot fix. The job stops at once with this message."""


# --------------------------------------------------------------------------- queue

def enqueue(kind: str, ref_id: int, label: str, force: bool = False) -> int:
    existing = db.row("SELECT id FROM jobs WHERE kind = ? AND ref_id = ? AND status IN ('queued', 'running')",
                      (kind, ref_id))
    if existing:
        if force:
            db.update("jobs", existing["id"], {"force": 1})
        return existing["id"]
    stamp = db.now()
    return db.insert("jobs", {"kind": kind, "ref_id": ref_id, "label": label, "status": "queued",
                              "force": int(force), "created_at": stamp, "updated_at": stamp})


def window_is_open(settings: dict | None = None, now: datetime | None = None) -> bool:
    processing = (settings or config.load_settings())["processing"]
    return publish.window_open((now or datetime.now()).time(), processing["window_start"], processing["window_end"])


def next_job(settings: dict, now: datetime | None = None) -> dict | None:
    """The next job that may run now, oldest first; short jobs before heavy ones."""
    now = now or datetime.now()
    open_now = window_is_open(settings, now)
    candidates = db.rows("SELECT * FROM jobs WHERE status = 'queued' AND (run_after IS NULL OR run_after <= ?) "
                         "ORDER BY CASE kind WHEN 'render_clip' THEN 0 ELSE 1 END, id",
                         (now.replace(microsecond=0).isoformat(),))
    for job in candidates:
        if job["kind"] in HEAVY and not (open_now or job["force"]):
            continue
        return job
    return None


def job_view(job: dict, settings: dict, now: datetime | None = None) -> dict:
    """A job as the Activity screen shows it."""
    status = job["status"]
    if status == "queued" and job["kind"] in HEAVY and not job["force"] and not window_is_open(settings, now):
        status = "waiting"
    return {"id": job["id"], "kind": job["kind"], "label": job["label"], "status": status,
            "stage": job["stage"], "progress": job["progress"], "attempts": job["attempts"],
            "max_attempts": int(settings["processing"]["max_retries"]), "error": job["error"],
            "created_at": job["created_at"], "updated_at": job["updated_at"]}


# --------------------------------------------------------------------------- handlers

class JobProgress:
    """Writes stage and progress to the job row, at most once a second."""

    def __init__(self, job_id: int) -> None:
        self.job_id = job_id
        self.last = 0.0
        self.stage = ""

    def __call__(self, stage: str, fraction: float | None) -> None:
        label = STAGE_LABELS.get(stage, stage)
        now = time_module.monotonic()
        if label != self.stage or now - self.last >= 1.0 or (fraction is not None and fraction >= 1.0):
            db.update("jobs", self.job_id, {"stage": label, "progress": fraction, "updated_at": db.now()})
            self.stage, self.last = label, now


def _vod_info(vod: dict) -> VodInfo:
    return VodInfo(platform=vod["platform"], vod_id=vod["vod_key"], url=vod["source_url"], title=vod["title"],
                   duration=vod["duration"], chapters=vod["chapters"] or [])


def streamer_layout(streamer: dict) -> dict:
    return {"type": streamer["layout"], "crop": streamer["crop_rect"],
            "facecam": streamer["facecam_rect"], "gameplay": streamer["gameplay_rect"],
            "caption_style": streamer.get("caption_style") or "box",
            "show_headline": bool(streamer.get("show_headline", 1))}


def credit_line(streamer: dict) -> str:
    link = streamer["channel_url"].replace("https://", "").replace("http://", "").replace("www.", "")
    return f"Credit: {streamer['name']}, {link}"


def build_request(streamer: dict, vod: dict, analysis, settings: dict) -> selection.SelectionRequest:
    sel = settings["selection"]
    return selection.SelectionRequest(
        mode=streamer["mode"], instructions=streamer["instructions"],
        max_clips=int(streamer["max_clips_per_vod"]), clip_min_s=float(streamer["clip_min_s"]),
        clip_max_s=float(streamer["clip_max_s"]), min_score=float(sel["min_score"]),
        duration=vod["duration"], span_start=analysis.span[0], ranges=analysis.ranges,
        transcript=analysis.transcript, loudness=analysis.loudness, chat_rate=analysis.chat_rate,
        nominations=int(sel["highlight_nominations"]), window_s=int(sel["window_s"]), step_s=int(sel["step_s"]),
        chat_weight=float(sel["chat_weight"]), audio_weight=float(sel["audio_weight"]),
        streamer=streamer["name"], vod_title=vod["title"])


def _free_gb() -> float:
    return shutil.disk_usage(config.data_dir()).free / 1e9


def _record_usage(vod_id: int, provider) -> None:
    if provider is None:
        return
    total = provider.total_tokens()
    db.execute("UPDATE vods SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
               "llm_requests = llm_requests + ? WHERE id = ?",
               (total["input_tokens"], total["output_tokens"], total["requests"], vod_id))
    provider.usage.clear()


def render_one(clip: dict, vod: dict, analysis_audio: Path | None, transcript: dict, settings: dict,
               progress=None) -> None:
    """Fetch the footage for a clip if needed, render it, and save a thumbnail."""
    clip_id = clip["id"]
    pad = float(settings["ingest"]["source_pad_s"])
    source = Path(clip["source_path"]) if clip.get("source_path") else None
    covered = (source is not None and source.exists() and clip.get("source_start") is not None
               and clip["source_start"] <= clip["start_s"] + 0.05 and clip["end_s"] <= clip["source_end"] + 0.05)
    if not covered:
        if progress:
            progress("Downloading footage", None)
        info = _vod_info(vod)
        target = pipeline.vod_dir(info) / "sources" / f"clip{clip_id}"
        lo = max(0.0, clip["start_s"] - pad)
        if analysis_audio is not None and analysis_audio.exists():
            source, source_start = pipeline.fetch_source(
                info, analysis_audio, vod["span_start"], clip["start_s"], clip["end_s"], target,
                pad_s=pad, height=int(settings["ingest"]["video_height"]))
        else:
            # The analysis audio was cleaned up, so the cut cannot be aligned; use the requested time.
            source = platforms.download_video(info.url, target, lo, clip["end_s"] + pad,
                                              int(settings["ingest"]["video_height"]))
            source_start = lo
        source_end = source_start + platforms.probe_duration(source)
        db.update("clips", clip_id, {"source_path": str(source), "source_start": source_start,
                                     "source_end": source_end})
        clip = {**clip, "source_path": str(source), "source_start": source_start, "source_end": source_end}
    if progress:
        progress("Rendering", None)
    out = config.subdir("clips") / f"{clip_id}.mp4"
    pipeline.produce(Path(clip["source_path"]), clip["source_start"], clip["start_s"], clip["end_s"],
                     transcript, clip["headline"], clip["layout"], out)
    render.grab_frame(out, min(2.0, (clip["end_s"] - clip["start_s"]) / 2),
                      config.subdir("thumbs") / f"{clip_id}.jpg", width=360)
    db.execute("UPDATE clips SET file_path = ?, render_state = 'ready', render_error = NULL, "
               "version = version + 1 WHERE id = ?", (str(out), clip_id))


def process_vod(job: dict, settings: dict) -> None:
    vod = db.get("vods", job["ref_id"])
    if vod is None:
        raise Permanent("This stream is no longer in the database.")
    streamer = db.get("streamers", vod["streamer_id"])
    progress = JobProgress(job["id"])
    db.update("vods", vod["id"], {"status": "processing", "error": None, "updated_at": db.now()})

    progress("Reading stream details", None)
    info = platforms.vod_info(vod["source_url"])
    if info.live_status == "is_live":
        raise RuntimeError("The stream is still live. It will be processed once it has finished.")
    published = datetime.fromtimestamp(info.published_at).replace(microsecond=0).isoformat() if info.published_at else None
    db.update("vods", vod["id"], {"title": info.title, "duration": info.duration, "chapters": info.chapters,
                                  "published_at": published or vod["published_at"]})
    vod = db.get("vods", vod["id"])
    db.update("jobs", job["id"], {"label": f"{streamer['name']}: {info.title}"})

    ranges = scoring.game_ranges(scoring.parse_games(streamer["games"]), info.chapters, info.title, info.duration)
    if ranges is None:
        db.update("vods", vod["id"], {"status": "skipped", "updated_at": db.now()})
        db.event(f"Skipped \"{info.title}\" from {streamer['name']}: no part of it matches the game filter "
                 f"({streamer['games']}).", vod_id=vod["id"])
        return
    if _free_gb() < MIN_FREE_GB:
        raise RuntimeError(f"Less than {MIN_FREE_GB:.0f} GB is free on the data drive. Free up space to continue.")

    analysis = pipeline.analyze(info, ranges, settings, progress)
    transcribe.release_models()                   # free the GPU before encoding
    db.update("vods", vod["id"], {"ranges": [list(r) for r in analysis.ranges], "span_start": analysis.span[0],
                                  "audio_path": str(analysis.audio_path), "stats": analysis.stats})
    vod = db.get("vods", vod["id"])

    progress("Choosing clips", None)
    provider = llm.get_provider(settings)
    try:
        picks = selection.select(build_request(streamer, vod, analysis, settings), provider)
    except selection.NeedsLLM as exc:
        raise Permanent(str(exc)) from exc
    except llm.LLMError as exc:
        _record_usage(vod["id"], provider)
        raise RuntimeError(str(exc)) from exc

    existing = db.rows("SELECT start_s, end_s FROM clips WHERE vod_id = ?", (vod["id"],))
    picks = [p for p in picks if not any(p.start < e["end_s"] and e["start_s"] < p.end for e in existing)]
    layout = streamer_layout(streamer)
    default_destinations = [streamer["default_destination_id"]] if streamer["default_destination_id"] else []
    credit = credit_line(streamer) if settings["publishing"]["append_streamer_credit"] else ""

    ready = 0
    for index, pick in enumerate(picks, 1):
        text = transcribe.text_between(analysis.transcript, pick.start, pick.end)
        game = next((c["title"] for c in info.chapters if c["start"] <= pick.start < c["end"]), "")
        progress(f"Writing post text for clip {index} of {len(picks)}", (index - 1) / len(picks))
        try:
            meta = (provider.write_metadata(text, streamer["name"], info.title, pick.title, game)
                    if provider else llm.fallback_metadata(text, info.title, game))
        except llm.LLMError:
            meta = llm.fallback_metadata(text, info.title, game)
        clip_id = db.insert("clips", {
            "vod_id": vod["id"], "start_s": pick.start, "end_s": pick.end, "score": pick.score,
            "reason": pick.reason, "flags": list(pick.flags), "layout": layout, "headline": meta["headline"],
            "title": meta["title"], "description": meta["description"], "credit": credit,
            "hashtags": meta["hashtags"], "working_title": pick.title, "status": "candidate",
            "render_state": "rendering", "destination_ids": default_destinations, "created_at": db.now()})
        try:
            render_one(db.get("clips", clip_id), vod, analysis.audio_path, analysis.transcript, settings,
                       lambda stage, f, i=index: progress(f"{stage}: clip {i} of {len(picks)}", (i - 1) / len(picks)))
            db.update("clips", clip_id, {"status": "pending_review"})
            ready += 1
        except Exception as exc:  # noqa: BLE001 - one bad clip must not sink the rest of the stream
            db.update("clips", clip_id, {"status": "pending_review", "render_state": "failed",
                                         "render_error": _short(exc)})
            db.event(f"A clip from \"{info.title}\" could not be rendered: {_short(exc)}", "error",
                     vod_id=vod["id"], clip_id=clip_id)
    _record_usage(vod["id"], provider)
    db.update("vods", vod["id"], {"status": "done", "updated_at": db.now()})

    if ready:
        noun = "clip" if ready == 1 else "clips"
        db.event(f"{ready} {noun} ready for review from {streamer['name']}: \"{info.title}\".", vod_id=vod["id"])
        notify.toast("Snipr: clips ready for review", f"{ready} {noun} from {streamer['name']}.", "#/review")
    elif not picks:
        db.event(f"No moment in \"{info.title}\" from {streamer['name']} reached the quality bar.", vod_id=vod["id"])


def render_clip(job: dict, settings: dict) -> None:
    clip = db.get("clips", job["ref_id"])
    if clip is None:
        raise Permanent("This clip is no longer in the database.")
    vod = db.get("vods", clip["vod_id"])
    transcript_path = pipeline.vod_dir(_vod_info(vod)) / "transcript.json"
    if not transcript_path.exists():
        raise Permanent("The transcript for this stream has been cleaned up, so the clip cannot be re-rendered.")
    audio_path = Path(vod["audio_path"]) if vod["audio_path"] else None
    try:
        render_one(clip, vod, audio_path, transcribe.load(transcript_path), settings, JobProgress(job["id"]))
    except Exception as exc:
        db.update("clips", clip["id"], {"render_state": "failed", "render_error": _short(exc)})
        raise


HANDLERS = {"process_vod": process_vod, "render_clip": render_clip}


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text if len(text) <= 400 else text[:400] + "…"


def run_job(job: dict, settings: dict) -> None:
    """Run one job and record the outcome, including the retry schedule."""
    db.update("jobs", job["id"], {"status": "running", "error": None, "updated_at": db.now()})
    limit = int(settings["processing"]["max_retries"])
    try:
        HANDLERS[job["kind"]](job, settings)
        db.update("jobs", job["id"], {"status": "done", "stage": "", "progress": 1.0, "updated_at": db.now()})
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
        attempts = job["attempts"] + 1
        permanent = isinstance(exc, Permanent)
        message = _short(exc)
        (config.data_dir() / "last_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        if permanent or attempts >= limit:
            db.update("jobs", job["id"], {"status": "failed", "attempts": attempts, "error": message,
                                          "progress": None, "updated_at": db.now()})
            db.event(f"{job['label'] or job['kind']} failed: {message}", "error",
                     vod_id=job["ref_id"] if job["kind"] == "process_vod" else None)
            if job["kind"] == "process_vod":
                db.update("vods", job["ref_id"], {"status": "failed", "error": message, "updated_at": db.now()})
        else:
            delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
            retry_at = (datetime.now() + timedelta(seconds=delay)).replace(microsecond=0).isoformat()
            db.update("jobs", job["id"], {"status": "queued", "attempts": attempts, "error": message,
                                          "run_after": retry_at, "progress": None, "updated_at": db.now()})
            db.event(f"{job['label'] or job['kind']} hit a problem and will be retried "
                     f"(attempt {attempts} of {limit}): {message}", "warn")


class Worker(threading.Thread):
    """Runs queued jobs one at a time until stopped."""

    def __init__(self) -> None:
        super().__init__(name="snipr-worker", daemon=True)
        self._stop_event = threading.Event()
        self.current: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # A job left "running" belongs to a previous process that was closed mid-job.
        db.execute("UPDATE jobs SET status = 'queued', stage = '', progress = NULL WHERE status = 'running'")
        while not self._stop_event.is_set():
            try:
                settings = config.load_settings()
                job = next_job(settings)
            except Exception:  # noqa: BLE001 - never let the loop die on a transient database error
                job = None
            if job is None:
                self._stop_event.wait(2.0)
                continue
            self.current = job["label"] or job["kind"]
            try:
                run_job(job, settings)
            finally:
                self.current = None


def describe_current() -> str | None:
    job = db.row("SELECT label, stage FROM jobs WHERE status = 'running' ORDER BY id LIMIT 1")
    if not job:
        return None
    return f"{job['stage']}: {job['label']}" if job["stage"] else job["label"]
