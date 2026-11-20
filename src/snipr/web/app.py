"""FastAPI application: JSON API under /api, rendered files under /media, the page under /.

Single user, localhost only, no login. Requests from other sites are refused
(see ``guard``), because a page open in the same browser could otherwise call
this API.
"""

from __future__ import annotations

import mimetypes
import os
import re
import secrets as token_source
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .. import (__version__, config, db, llm, pipeline, platforms, publish, scheduler, scoring, secrets, selection,
                transcribe, voice, worker)
from ..platforms import PlatformError, VodInfo

mimetypes.add_type("font/ttf", ".ttf")      # Windows has no entry, so fonts went out as octet-stream
mimetypes.add_type("text/javascript", ".js")  # and a registry override can break ES modules

STATIC_DIR = Path(__file__).parent / "static"
FONT_DIR = Path(__file__).parent.parent / "assets" / "fonts"

MODES = ("highlights", "informative", "custom")
LAYOUTS = ("fill", "stacked")
CAPTION_STYLES = ("box", "bold")
MIXES = ("duck", "mute")
STACKED_NEEDS_BOXES = ("Stacked puts the facecam above the gameplay, so Snipr needs to know where each one is. "
                       "Click \"Get sample frame\", then drag one box over the facecam and the other over the "
                       "gameplay. Or switch Layout to Fill frame, which needs nothing marked.")
LAYOUT_DEFAULTS = {"type": "fill", "crop": None, "facecam": None, "gameplay": None,
                   "caption_style": "box", "show_headline": True,
                   "voiceover": False, "voice": voice.DEFAULT_VOICE, "mix": "duck"}
DEST_PLATFORMS = ("youtube", "tiktok", "facebook")
REJECT_REASONS = (None, "boring", "bad_cut", "wrong_topic", "bad_framing", "other")
WHISPER_MODELS = ("small", "medium", "large-v3")
API_NOTES = {"youtube": "Automatic upload to YouTube is not built yet.",
             "tiktok": "Automatic upload to TikTok is not built yet.",
             "facebook": "Automatic upload to Facebook is not built yet."}

_background: dict = {}


def bad(message: str, status: int = 422) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    if not os.environ.get("SNIPR_NO_BACKGROUND"):
        _background["worker"] = worker.Worker()
        _background["worker"].start()
        _background["scheduler"] = scheduler.start()
    yield
    if "scheduler" in _background:
        _background.pop("scheduler").shutdown(wait=False)
    if "worker" in _background:
        _background.pop("worker").stop()


app = FastAPI(title="Snipr", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    """Refuse requests that do not come from this machine's own Snipr page.

    The Host check stops DNS rebinding; the Origin check stops another website
    open in the browser from changing anything here.
    """
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse({"detail": "Snipr only answers on localhost."}, status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        origin_host = re.sub(r"^https?://", "", origin).split(":")[0].lower()
        if origin_host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
    return await call_next(request)


# --------------------------------------------------------------------------- helpers

def _window(settings: dict) -> dict:
    processing = settings["processing"]
    return {"mode": processing.get("mode", "manual"), "start": processing["window_start"],
            "end": processing["window_end"], "open_now": worker.window_is_open(settings)}


def _worker_state() -> dict:
    current = worker.describe_current()
    return {"busy": current is not None, "current": current}


def _rect(value, name: str):
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise bad(f"{name} must be four numbers: x, y, width and height as fractions of the frame.")
    x, y, w, h = (float(v) for v in value)
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 and 0 < h <= 1 and x + w <= 1.001 and y + h <= 1.001):
        raise bad(f"{name} must lie inside the frame.")
    return [round(x, 5), round(y, 5), round(min(w, 1 - x), 5), round(min(h, 1 - y), 5)]


def _frame_info(token: str | None, width, height) -> dict | None:
    if not token or not (config.subdir("frames") / f"{token}.jpg").exists():
        return None
    return {"url": f"/media/frames/{token}.jpg", "width": width, "height": height}


def streamer_out(s: dict) -> dict:
    counts = {r["status"]: r["n"] for r in db.rows(
        "SELECT c.status, COUNT(*) AS n FROM clips c JOIN vods v ON v.id = c.vod_id "
        "WHERE v.streamer_id = ? GROUP BY c.status", (s["id"],))}
    last = db.row("SELECT title, published_at, created_at FROM vods WHERE streamer_id = ? AND status != 'ignored' "
                  "ORDER BY COALESCE(published_at, created_at) DESC LIMIT 1", (s["id"],))
    return {
        "id": s["id"], "name": s["name"], "platform": s["platform"], "channel_url": s["channel_url"],
        "mode": s["mode"], "instructions": s["instructions"], "games": s["games"],
        "max_clips_per_vod": s["max_clips_per_vod"], "clip_min_s": s["clip_min_s"], "clip_max_s": s["clip_max_s"],
        "layout": s["layout"], "crop_rect": s["crop_rect"], "facecam_rect": s["facecam_rect"],
        "gameplay_rect": s["gameplay_rect"], "caption_style": s["caption_style"],
        "show_headline": bool(s["show_headline"]), "voiceover": bool(s["voiceover"]), "voice": s["voice"],
        "voiceover_mix": s["voiceover_mix"], "narration_style": s["narration_style"],
        "default_destination_id": s["default_destination_id"],
        "enabled": bool(s["enabled"]),
        "sample_frame": _frame_info(s["sample_frame"], s["sample_w"], s["sample_h"]),
        "stats": {"pending": counts.get("pending_review", 0), "approved": counts.get("approved", 0),
                  "rejected": counts.get("rejected", 0),
                  "last_vod_title": last["title"] if last else None,
                  "last_vod_at": (last["published_at"] or last["created_at"]) if last else None,
                  "last_checked_at": s["last_checked_at"]},
    }


def clip_out(c: dict) -> dict:
    vod = db.get("vods", c["vod_id"])
    streamer = db.get("streamers", vod["streamer_id"])
    ready = c["render_state"] == "ready" and c["file_path"] and Path(c["file_path"]).exists()
    layout = {**LAYOUT_DEFAULTS, **(c["layout"] or {})}
    source_range = ({"start_s": c["source_start"], "end_s": c["source_end"]}
                    if c["source_start"] is not None else {"start_s": c["start_s"], "end_s": c["end_s"]})
    return {
        "id": c["id"], "vod_id": c["vod_id"], "streamer_id": streamer["id"], "streamer_name": streamer["name"],
        "platform": vod["platform"], "vod_title": vod["title"],
        "vod_date": vod["published_at"] or vod["created_at"],
        "start_s": c["start_s"], "end_s": c["end_s"], "duration_s": round(c["end_s"] - c["start_s"], 2),
        "score": c["score"], "min_score": float(config.load_settings()["selection"]["min_score"]),
        "mode": streamer["mode"], "reason": c["reason"], "flags": c["flags"], "layout": layout,
        "headline": c["headline"], "title": c["title"], "description": c["description"], "credit": c["credit"],
        "hashtags": c["hashtags"], "narration": c["narration"],
        "narration_style": streamer["narration_style"],
        "status": "pending_review" if c["status"] == "candidate" else c["status"],
        "reject_reason": c["reject_reason"],
        "render_state": c["render_state"] if c["render_state"] != "ready" or ready else "failed",
        "render_error": c["render_error"] if c["render_state"] != "ready" or ready
                        else "The rendered file is missing. Edit and save the clip to render it again.",
        "video_url": f"/media/clips/{c['id']}.mp4?v={c['version']}" if ready else None,
        "thumb_url": f"/media/thumbs/{c['id']}.jpg?v={c['version']}" if ready else None,
        "destination_ids": c["destination_ids"], "source_range": source_range,
        "clip_min_s": streamer["clip_min_s"], "clip_max_s": streamer["clip_max_s"], "vod_duration_s": vod["duration"],
        "sample_frame": _frame_info(streamer["sample_frame"], streamer["sample_w"], streamer["sample_h"]),
    }


def destination_out(d: dict) -> dict:
    queued = db.scalar("SELECT COUNT(*) FROM posts WHERE destination_id = ? AND status IN ('scheduled', 'due', 'posting')",
                       (d["id"],))
    try:
        slot = publish.next_free_slot(d["post_slots"], publish.taken_slots(d["id"]), datetime.now()).isoformat()
    except publish.SlotError:
        slot = None
    return {"id": d["id"], "name": d["name"], "platform": d["platform"], "account_handle": d["account_handle"],
            "post_method": d["post_method"], "api_available": False, "api_note": API_NOTES.get(d["platform"], ""),
            "post_slots": d["post_slots"], "enabled": bool(d["enabled"]), "next_free_slot": slot, "queued": queued}


def post_out(p: dict) -> dict:
    clip = db.get("clips", p["clip_id"])
    dest = db.get("destinations", p["destination_id"])
    vod = db.get("vods", clip["vod_id"])
    streamer = db.get("streamers", vod["streamer_id"])
    exists = bool(clip["file_path"]) and Path(clip["file_path"]).exists()
    text = clip["description"].strip()
    if clip["credit"].strip():
        text = (text + "\n\n" if text else "") + clip["credit"].strip()
    return {"id": p["id"], "clip_id": p["clip_id"], "destination_id": p["destination_id"],
            "destination_name": dest["name"], "platform": dest["platform"], "method": p["method"],
            "status": p["status"], "scheduled_at": p["scheduled_at"], "posted_at": p["posted_at"],
            "post_url": p["post_url"], "error": p["error"],
            "clip": {"headline": clip["headline"], "title": clip["title"], "post_text": text,
                     "hashtags_text": " ".join(f"#{h}" for h in clip["hashtags"]),
                     "video_url": f"/media/clips/{clip['id']}.mp4?v={clip['version']}" if exists else None,
                     "thumb_url": f"/media/thumbs/{clip['id']}.jpg?v={clip['version']}" if exists else None,
                     "file_path": clip["file_path"] if exists else None,
                     "streamer_name": streamer["name"], "duration_s": round(clip["end_s"] - clip["start_s"], 2)}}


def _need(table: str, row_id: int, what: str) -> dict:
    found = db.get(table, row_id)
    if found is None:
        raise bad(f"That {what} no longer exists.", 404)
    return found


# --------------------------------------------------------------------------- state

@app.get("/api/state")
def get_state() -> dict:
    settings = config.load_settings()
    return {"version": __version__,
            "review_count": db.scalar("SELECT COUNT(*) FROM clips WHERE status IN ('candidate', 'pending_review')"),
            "due_posts": db.scalar("SELECT COUNT(*) FROM posts WHERE status = 'due'"),
            "failed_jobs": db.scalar("SELECT COUNT(*) FROM jobs WHERE status = 'failed'"),
            "llm_ready": llm.api_key(settings) is not None,
            "provider": llm.provider_name(settings),
            "model": llm.model_for(settings, llm.provider_name(settings)),
            "window": _window(settings), "worker": _worker_state()}


# --------------------------------------------------------------------------- streamers

def _streamer_fields(body: dict, current: dict | None) -> dict:
    """Validate the editable streamer fields in ``body`` against the existing row."""
    merged = dict(current or {"mode": "highlights", "instructions": "", "games": "", "max_clips_per_vod": 5,
                              "clip_min_s": 20, "clip_max_s": 60, "layout": "fill", "crop_rect": None,
                              "facecam_rect": None, "gameplay_rect": None, "caption_style": "box",
                              "show_headline": True, "voiceover": False, "voice": voice.DEFAULT_VOICE,
                              "voiceover_mix": "duck", "default_destination_id": None, "enabled": True})
    out: dict = {}
    for key in ("show_headline", "voiceover"):
        if key in body:
            out[key] = bool(body[key])
    if isinstance(body.get("narration_style"), str) and len(body["narration_style"]) > 1500:
        raise bad("The narration style is too long. Keep it under 1,500 characters.")
    for key in ("name", "mode", "instructions", "games", "layout", "caption_style", "voice", "voiceover_mix",
                "narration_style"):
        if key in body:
            if not isinstance(body[key], str):
                raise bad(f"{key} must be text.")
            out[key] = body[key].strip()
    if "name" in out and not out["name"]:
        raise bad("Give the streamer a name.")
    for key in ("crop_rect", "facecam_rect", "gameplay_rect"):
        if key in body:
            out[key] = _rect(body[key], key.replace("_", " ").capitalize())
    for key, low, high in (("max_clips_per_vod", 1, 30), ("clip_min_s", 5, 600), ("clip_max_s", 5, 600)):
        if key in body:
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise bad(f"{key.replace('_', ' ').capitalize()} must be a number from {low} to {high}.")
            out[key] = int(value) if key == "max_clips_per_vod" else float(value)
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    if "default_destination_id" in body:
        dest = body["default_destination_id"]
        if dest is not None and db.get("destinations", int(dest)) is None:
            raise bad("The chosen default destination no longer exists.")
        out["default_destination_id"] = int(dest) if dest is not None else None
    merged.update(out)
    if merged["mode"] not in MODES:
        raise bad("Mode must be Highlights, Informative or Custom.")
    if merged["layout"] not in LAYOUTS:
        raise bad("Layout must be Fill frame or Stacked.")
    if merged["caption_style"] not in CAPTION_STYLES:
        raise bad("Caption style must be Box or Bold.")
    if merged["voice"] not in voice.voice_ids():
        raise bad("Pick one of the listed voices.")
    if merged["voiceover_mix"] not in MIXES:
        raise bad("Stream sound under a voiceover must be Lowered or Muted.")
    if merged["mode"] == "custom" and not merged["instructions"].strip():
        raise bad("Custom mode needs instructions describing what to look for.")
    if merged["clip_min_s"] > merged["clip_max_s"]:
        raise bad("The shortest clip length cannot be longer than the longest.")
    if merged["layout"] == "stacked" and not (merged["facecam_rect"] and merged["gameplay_rect"]):
        raise bad(STACKED_NEEDS_BOXES)
    return out


def _resolve(url: str) -> tuple[platforms.Channel, dict | None]:
    try:
        channel = platforms.parse_channel(url)
    except PlatformError as exc:
        raise bad(str(exc), 400) from exc
    wanted = re.sub(r"[^a-z0-9]", "", channel.handle.lower())
    duplicate = None
    for other in db.rows("SELECT id, name, channel_url FROM streamers"):
        names = {re.sub(r"[^a-z0-9]", "", other["name"].lower()),
                 re.sub(r"[^a-z0-9]", "", other["channel_url"].rstrip("/").rsplit("/", 1)[-1].lower())}
        if wanted and wanted in names:
            duplicate = {"id": other["id"], "name": other["name"]}
            break
    return channel, duplicate


@app.get("/api/streamers")
def list_streamers() -> list[dict]:
    return [streamer_out(s) for s in db.rows("SELECT * FROM streamers ORDER BY name COLLATE NOCASE")]


@app.post("/api/streamers/resolve")
def resolve_streamer(body: dict = Body(...)) -> dict:
    channel, duplicate = _resolve(str(body.get("channel_url") or ""))
    supported = channel.platform in platforms.SUPPORTED
    return {"platform": channel.platform, "handle": channel.handle, "name": channel.handle.lstrip("@"),
            "channel_url": channel.url, "supported": supported,
            "message": None if supported else f"{channel.platform.title()} is not supported yet. "
                                              "Twitch and YouTube channels work today.",
            "duplicate_of": duplicate}


@app.post("/api/streamers/sample-frame")
def sample_frame(body: dict = Body(...)) -> dict:
    channel, _ = _resolve(str(body.get("channel_url") or ""))
    if channel.platform not in platforms.SUPPORTED:
        raise bad(f"{channel.platform.title()} is not supported yet.", 400)
    token = token_source.token_hex(8)
    games_text = str(body.get("games") or "").strip()
    games = scoring.parse_games(games_text)
    try:
        info = platforms.sample_frame(channel, config.subdir("frames") / f"{token}.jpg", games or None)
    except PlatformError as exc:
        raise bad(str(exc), 502) from exc
    except subprocess.CalledProcessError as exc:
        raise bad("A frame could not be read from the latest stream. Try again in a moment.", 502) from exc
    (config.subdir("frames") / f"{token}.json").write_text(f'{{"w": {info["width"]}, "h": {info["height"]}}}')
    note = None
    if info["matched"] is False:
        note = (f"None of this channel's last {info['looked_at']} streams is in {games_text}, so this frame is "
                f"from \"{info['vod_title']}\". It still works for marking where the facecam sits. Streams in "
                f"other games are skipped when processing, so if {games_text} is streamed on another platform, "
                "use that channel's link.")
    return {"frame_token": token, "url": f"/media/frames/{token}.jpg", "width": info["width"],
            "height": info["height"], "vod_title": info["vod_title"],
            "game_matched": info["matched"], "note": note}


def _apply_frame(values: dict, token) -> None:
    if not token:
        return
    token = str(token)
    meta = config.subdir("frames") / f"{token}.json"
    if re.fullmatch(r"[0-9a-f]{16}", token) and meta.exists():
        import json
        size = json.loads(meta.read_text())
        values.update(sample_frame=token, sample_w=size["w"], sample_h=size["h"])


@app.post("/api/streamers", status_code=201)
def create_streamer(body: dict = Body(...)) -> dict:
    channel, _ = _resolve(str(body.get("channel_url") or ""))
    if channel.platform not in platforms.SUPPORTED:
        raise bad(f"{channel.platform.title()} is not supported yet. Twitch and YouTube channels work today.")
    values = _streamer_fields(body, None)
    values.setdefault("name", channel.handle.lstrip("@"))
    values.update(platform=channel.platform, channel_url=channel.url, created_at=db.now())
    _apply_frame(values, body.get("frame_token"))
    return streamer_out(db.get("streamers", db.insert("streamers", values)))


@app.put("/api/streamers/{streamer_id}")
def update_streamer(streamer_id: int, body: dict = Body(...)) -> dict:
    current = _need("streamers", streamer_id, "streamer")
    values = _streamer_fields(body, current)
    if "channel_url" in body and body["channel_url"] != current["channel_url"]:
        channel, _ = _resolve(str(body["channel_url"]))
        if channel.platform not in platforms.SUPPORTED:
            raise bad(f"{channel.platform.title()} is not supported yet.")
        # A different channel counts as never checked, so only its latest stream is
        # queued first instead of everything on the new channel's front page.
        values.update(platform=channel.platform, channel_url=channel.url, last_checked_at=None)
    _apply_frame(values, body.get("frame_token"))
    db.update("streamers", streamer_id, values)
    return streamer_out(db.get("streamers", streamer_id))


@app.delete("/api/streamers/{streamer_id}", status_code=204)
def delete_streamer(streamer_id: int) -> Response:
    _need("streamers", streamer_id, "streamer")
    db.execute("DELETE FROM streamers WHERE id = ?", (streamer_id,))
    return Response(status_code=204)


@app.post("/api/streamers/{streamer_id}/check")
def check_streamer(streamer_id: int) -> dict:
    streamer = _need("streamers", streamer_id, "streamer")
    try:
        queued = scheduler.poll_streamer(streamer)
    except PlatformError as exc:
        raise bad(str(exc), 502) from exc
    noun = "stream" if queued == 1 else "streams"
    return {"queued": queued, "message": f"Queued {queued} new {noun}." if queued else "No new streams."}


@app.post("/api/streamers/{streamer_id}/test-selection")
def test_selection(streamer_id: int) -> dict:
    """Rerun selection only on the last analysed stream, with the saved settings."""
    streamer = _need("streamers", streamer_id, "streamer")
    settings = config.load_settings()
    for vod in db.rows("SELECT * FROM vods WHERE streamer_id = ? AND ranges IS NOT NULL ORDER BY updated_at DESC",
                       (streamer_id,)):
        folder = pipeline.vod_dir(VodInfo(vod["platform"], vod["vod_key"], vod["source_url"], vod["title"],
                                          vod["duration"]))
        if (folder / "transcript.json").exists() and (folder / "loudness.npy").exists():
            break
    else:
        raise bad("No stream of this streamer has been analysed yet. Process one first, then test here.", 409)
    chat_path = folder / "chat.npy"
    analysis = pipeline.Analysis(
        vod=VodInfo(vod["platform"], vod["vod_key"], vod["source_url"], vod["title"], vod["duration"]),
        workdir=folder, audio_path=Path(vod["audio_path"] or folder / "audio"),
        span=(vod["span_start"], vod["span_start"]), ranges=[tuple(r) for r in vod["ranges"]],
        loudness=np.load(folder / "loudness.npy"), chat_rate=np.load(chat_path) if chat_path.exists() else None,
        transcript=transcribe.load(folder / "transcript.json"))
    provider = llm.get_provider(settings)
    try:
        picks = selection.select(worker.build_request(streamer, vod, analysis, settings), provider)
    except (selection.NeedsLLM, llm.LLMError, ValueError) as exc:
        raise bad(str(exc), 409) from exc
    noun = "moment" if len(picks) == 1 else "moments"
    return {"vod_title": vod["title"],
            "message": f"{len(picks)} {noun} would be clipped from this stream with the current settings.",
            "picks": [{"start_s": p.start, "end_s": p.end, "score": p.score, "title": p.title,
                       "reason": p.reason, "flags": list(p.flags)} for p in picks]}


# --------------------------------------------------------------------------- clips

@app.get("/api/clips")
def list_clips(status: str = "pending_review", streamer_id: int | None = None) -> list[dict]:
    states = {"pending_review": ("candidate", "pending_review"), "approved": ("approved",),
              "rejected": ("rejected",), "all": ("candidate", "pending_review", "approved", "rejected")}
    if status not in states:
        raise bad("Unknown status filter.", 400)
    marks = ", ".join("?" for _ in states[status])
    sql = (f"SELECT c.* FROM clips c JOIN vods v ON v.id = c.vod_id WHERE c.status IN ({marks}) "
           + ("AND v.streamer_id = ? " if streamer_id else "")
           + "ORDER BY COALESCE(v.published_at, v.created_at) DESC, v.id DESC, c.score DESC")
    params = (*states[status], *((streamer_id,) if streamer_id else ()))
    return [clip_out(c) for c in db.rows(sql, params)]


@app.get("/api/clips/{clip_id}")
def get_clip(clip_id: int) -> dict:
    return clip_out(_need("clips", clip_id, "clip"))


@app.put("/api/clips/{clip_id}")
def update_clip(clip_id: int, body: dict = Body(...)) -> dict:
    clip = _need("clips", clip_id, "clip")
    vod = db.get("vods", clip["vod_id"])
    streamer = db.get("streamers", vod["streamer_id"])
    values: dict = {}
    for key in ("headline", "title", "description", "credit", "narration"):
        if key in body:
            if not isinstance(body[key], str):
                raise bad(f"{key.capitalize()} must be text.")
            values[key] = " ".join(body[key].split()) if key == "narration" else body[key].strip()
    if "hashtags" in body:
        if not isinstance(body["hashtags"], list):
            raise bad("Hashtags must be a list.")
        tags = []
        for tag in body["hashtags"]:
            tag = "".join(ch for ch in str(tag).lower() if ch.isalnum() or ch == "_")
            if tag and tag not in tags:
                tags.append(tag)
        values["hashtags"] = tags
    if "destination_ids" in body:
        values["destination_ids"] = _destination_ids(body["destination_ids"])
    start = body.get("start_s", clip["start_s"])
    end = body.get("end_s", clip["end_s"])
    if "start_s" in body or "end_s" in body:
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)):
            raise bad("Start and end must be times in seconds.")
        if end <= start:
            raise bad("The end must be after the start.")
        if start < 0 or (vod["duration"] and end > vod["duration"] + 0.5):
            raise bad("The clip must stay inside the stream.")
        length = end - start
        if not streamer["clip_min_s"] - 0.5 <= length <= streamer["clip_max_s"] + 0.5:
            raise bad(f"The clip would be {length:.0f} seconds long. This streamer's clips must be "
                      f"{streamer['clip_min_s']:.0f} to {streamer['clip_max_s']:.0f} seconds.")
        values.update(start_s=round(float(start), 3), end_s=round(float(end), 3))
    current_layout = {**LAYOUT_DEFAULTS, **(clip["layout"] or {})}
    if "layout" in body:
        layout = body["layout"] or {}
        if not isinstance(layout, dict) or layout.get("type") not in LAYOUTS:
            raise bad("Layout must be Fill frame or Stacked.")
        style = layout.get("caption_style", current_layout["caption_style"])
        if style not in CAPTION_STYLES:
            raise bad("Caption style must be Box or Bold.")
        speaker = layout.get("voice", current_layout["voice"])
        if speaker not in voice.voice_ids():
            raise bad("Pick one of the listed voices.")
        mix = layout.get("mix", current_layout["mix"])
        if mix not in MIXES:
            raise bad("Stream sound under a voiceover must be Lowered or Muted.")
        new_layout = {"type": layout["type"], "crop": _rect(layout.get("crop"), "Crop area"),
                      "facecam": _rect(layout.get("facecam"), "Facecam area"),
                      "gameplay": _rect(layout.get("gameplay"), "Gameplay area"),
                      "caption_style": style,
                      "show_headline": bool(layout.get("show_headline", current_layout["show_headline"])),
                      "voiceover": bool(layout.get("voiceover", current_layout["voiceover"])),
                      "voice": speaker, "mix": mix}
        if new_layout["type"] == "stacked" and not (new_layout["facecam"] and new_layout["gameplay"]):
            raise bad(STACKED_NEEDS_BOXES)
        values["layout"] = new_layout

    final_layout = values.get("layout", current_layout)
    narration = values.get("narration", clip["narration"])
    if final_layout["voiceover"]:
        if not narration:
            raise bad("Voiceover is on but there is no narration yet. Write one, or switch voiceover off.")
        length = values.get("end_s", clip["end_s"]) - values.get("start_s", clip["start_s"])
        budget = int(voice.word_budget(length) * 1.3)       # the narrator can speed up by about a third
        if len(narration.split()) > budget:
            raise bad(f"The narration is {len(narration.split())} words. About {budget} is the most that "
                      f"fits a {length:.0f} second clip. Shorten it or lengthen the clip.")
    rerender = (values.get("start_s", clip["start_s"]) != clip["start_s"]
                or values.get("end_s", clip["end_s"]) != clip["end_s"]
                or values.get("headline", clip["headline"]) != clip["headline"]
                or ("layout" in values and values["layout"] != current_layout)
                or (final_layout["voiceover"] and narration != clip["narration"])
                or clip["render_state"] == "failed")
    if rerender:
        values.update(render_state="rendering", render_error=None)
    db.update("clips", clip_id, values)
    if rerender:
        worker.enqueue("render_clip", clip_id, f"Re-render: {values.get('headline', clip['headline']) or 'clip'}",
                       force=True)
    return clip_out(db.get("clips", clip_id))


@app.post("/api/clips/{clip_id}/write-narration")
def write_clip_narration(clip_id: int, body: dict = Body(default={})) -> dict:
    """Have the LLM draft a voiceover script for a clip. The draft is returned, not saved.

    ``instructions`` in the body is the reviewer's own direction for how to narrate.
    """
    instructions = _instructions(body)
    clip = _need("clips", clip_id, "clip")
    vod = db.get("vods", clip["vod_id"])
    streamer = db.get("streamers", vod["streamer_id"])
    provider = llm.get_provider(config.load_settings())
    if provider is None:
        raise bad("Writing a narration needs a Claude or Gemini API key. Add one in Settings, or type the "
                  "narration yourself.", 409)
    folder = pipeline.vod_dir(VodInfo(vod["platform"], vod["vod_key"], vod["source_url"], vod["title"], vod["duration"]))
    if not clip["source_path"] or not Path(clip["source_path"]).exists() or not (folder / "transcript.json").exists():
        raise bad("The footage for this clip has been cleaned up, so a narration cannot be drafted from it. "
                  "You can still type one.", 409)
    try:
        script = worker.write_narration(clip, vod, streamer, transcribe.load(folder / "transcript.json"), provider,
                                        instructions=instructions)
    except llm.LLMError as exc:
        raise bad(str(exc), 502) from exc
    _count_tokens(provider, vod["id"])
    return {"narration": script, "words": len(script.split()),
            "word_budget": voice.word_budget(clip["end_s"] - clip["start_s"])}


def _instructions(body: dict) -> str:
    text = body.get("instructions") or ""
    if not isinstance(text, str):
        raise bad("The narration instructions must be text.")
    if len(text) > 1500:
        raise bad("The narration instructions are too long. Keep them under 1,500 characters.")
    return text.strip()


def _count_tokens(provider, vod_id: int) -> None:
    total = provider.total_tokens()
    db.execute("UPDATE vods SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, "
               "llm_requests = llm_requests + ? WHERE id = ?",
               (total["input_tokens"], total["output_tokens"], total["requests"], vod_id))


@app.post("/api/clips/{clip_id}/rewrite-narration")
def rewrite_clip_narration(clip_id: int, body: dict = Body(...)) -> dict:
    """Have the LLM rework a narration the reviewer typed. Returned, not saved."""
    instructions = _instructions(body)
    draft = " ".join(str(body.get("text") or "").split())
    if not draft:
        raise bad("Type a narration first; there is nothing to rewrite.")
    clip = _need("clips", clip_id, "clip")
    vod = db.get("vods", clip["vod_id"])
    streamer = db.get("streamers", vod["streamer_id"])
    provider = llm.get_provider(config.load_settings())
    if provider is None:
        raise bad("Rewriting needs a Claude or Gemini API key. Add one in Settings.", 409)
    start = float(body.get("start_s", clip["start_s"])) if isinstance(body.get("start_s"), (int, float)) else clip["start_s"]
    end = float(body.get("end_s", clip["end_s"])) if isinstance(body.get("end_s"), (int, float)) else clip["end_s"]
    length = max(5.0, end - start)
    folder = pipeline.vod_dir(VodInfo(vod["platform"], vod["vod_key"], vod["source_url"], vod["title"], vod["duration"]))
    spoken = ""
    if (folder / "transcript.json").exists():
        spoken = transcribe.text_between(transcribe.load(folder / "transcript.json"), start, end)
    budget = voice.word_budget(length)
    try:
        script = provider.rewrite_narration(draft, spoken, length, budget, instructions, streamer["name"])
    except llm.LLMError as exc:
        raise bad(str(exc), 502) from exc
    _count_tokens(provider, vod["id"])
    return {"narration": script, "words": len(script.split()), "word_budget": budget}


# --------------------------------------------------------------------------- narration templates

def _template_fields(body: dict) -> dict:
    name = " ".join(str(body.get("name") or "").split())
    prompt = str(body.get("prompt") or "").strip()
    if not name:
        raise bad("Give the template a name.")
    if len(name) > 60:
        raise bad("Keep the template name under 60 characters.")
    if not prompt:
        raise bad("Write the narration instructions to save.")
    if len(prompt) > 1500:
        raise bad("The narration instructions are too long. Keep them under 1,500 characters.")
    return {"name": name, "prompt": prompt}


@app.get("/api/narration-templates")
def list_templates() -> list[dict]:
    return db.rows("SELECT id, name, prompt FROM narration_templates ORDER BY name COLLATE NOCASE")


@app.post("/api/narration-templates", status_code=201)
def create_template(body: dict = Body(...)) -> dict:
    values = _template_fields(body)
    same = db.row("SELECT id FROM narration_templates WHERE name = ? COLLATE NOCASE", (values["name"],))
    if same:                                    # saving under an existing name updates it
        db.update("narration_templates", same["id"], values)
        return db.row("SELECT id, name, prompt FROM narration_templates WHERE id = ?", (same["id"],))
    new_id = db.insert("narration_templates", {**values, "created_at": db.now()})
    return db.row("SELECT id, name, prompt FROM narration_templates WHERE id = ?", (new_id,))


@app.delete("/api/narration-templates/{template_id}", status_code=204)
def delete_template(template_id: int) -> Response:
    _need("narration_templates", template_id, "template")
    db.execute("DELETE FROM narration_templates WHERE id = ?", (template_id,))
    return Response(status_code=204)


@app.get("/api/voices")
def list_voices() -> dict:
    return {"voices": [{"id": voice_id, "label": label} for voice_id, label in voice.VOICES],
            "default": voice.DEFAULT_VOICE, "model_ready": voice.model_ready(),
            "words_per_second": voice.WORDS_PER_SECOND}


def _destination_ids(value) -> list[int]:
    if not isinstance(value, list):
        raise bad("Destinations must be a list.")
    ids = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or db.get("destinations", item) is None:
            raise bad("One of the chosen destinations no longer exists.")
        if item not in ids:
            ids.append(item)
    return ids


def _cancel_unsent(clip_id: int) -> None:
    db.execute("DELETE FROM posts WHERE clip_id = ? AND status != 'posted'", (clip_id,))


@app.post("/api/clips/{clip_id}/approve")
def approve_clip(clip_id: int, body: dict = Body(default={})) -> dict:
    clip = _need("clips", clip_id, "clip")
    if clip["render_state"] != "ready":
        raise bad("This clip is still rendering. Approve it once the video is ready.", 409)
    ids = _destination_ids(body.get("destination_ids", clip["destination_ids"]))
    db.update("clips", clip_id, {"status": "approved", "reject_reason": None, "reviewed_at": db.now(),
                                 "destination_ids": ids})
    try:
        post_ids = publish.schedule(clip_id, ids, bool(body.get("post_now")))
    except publish.SlotError as exc:
        raise bad(str(exc)) from exc
    return {"clip": clip_out(db.get("clips", clip_id)),
            "posts": [post_out(db.get("posts", pid)) for pid in post_ids]}


@app.post("/api/clips/{clip_id}/reject")
def reject_clip(clip_id: int, body: dict = Body(default={})) -> dict:
    _need("clips", clip_id, "clip")
    reason = body.get("reason")
    if reason not in REJECT_REASONS:
        raise bad("Unknown rejection reason.")
    _cancel_unsent(clip_id)
    db.update("clips", clip_id, {"status": "rejected", "reject_reason": reason, "reviewed_at": db.now()})
    return clip_out(db.get("clips", clip_id))


@app.post("/api/clips/{clip_id}/restore")
def restore_clip(clip_id: int) -> dict:
    _need("clips", clip_id, "clip")
    _cancel_unsent(clip_id)
    db.update("clips", clip_id, {"status": "pending_review", "reject_reason": None, "reviewed_at": None})
    return clip_out(db.get("clips", clip_id))


# --------------------------------------------------------------------------- destinations

def _destination_fields(body: dict, current: dict | None) -> dict:
    out: dict = {}
    if "name" in body or current is None:
        name = str(body.get("name") or "").strip()
        if not name:
            raise bad("Give the destination a name.")
        out["name"] = name
    if "platform" in body or current is None:
        if body.get("platform") not in DEST_PLATFORMS:
            raise bad("Platform must be YouTube, TikTok or Facebook.")
        out["platform"] = body["platform"]
    if "account_handle" in body:
        out["account_handle"] = str(body["account_handle"] or "").strip()
    if "post_method" in body:
        if body["post_method"] == "api":
            raise bad("Automatic upload is not built yet. Use Export for now.")
        if body["post_method"] != "export":
            raise bad("Posting method must be Export or API upload.")
        out["post_method"] = "export"
    if "post_slots" in body or current is None:
        slots = body.get("post_slots") or config.load_settings()["publishing"]["default_post_slots"]
        try:
            out["post_slots"] = publish.clean_slots(slots)
        except publish.SlotError as exc:
            raise bad(str(exc)) from exc
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


@app.get("/api/destinations")
def list_destinations() -> list[dict]:
    return [destination_out(d) for d in db.rows("SELECT * FROM destinations ORDER BY name COLLATE NOCASE, platform")]


@app.post("/api/destinations", status_code=201)
def create_destination(body: dict = Body(...)) -> dict:
    values = _destination_fields(body, None)
    values["created_at"] = db.now()
    return destination_out(db.get("destinations", db.insert("destinations", values)))


@app.put("/api/destinations/{dest_id}")
def update_destination(dest_id: int, body: dict = Body(...)) -> dict:
    current = _need("destinations", dest_id, "destination")
    db.update("destinations", dest_id, _destination_fields(body, current))
    return destination_out(db.get("destinations", dest_id))


@app.delete("/api/destinations/{dest_id}", status_code=204)
def delete_destination(dest_id: int) -> Response:
    _need("destinations", dest_id, "destination")
    unsent = db.scalar("SELECT COUNT(*) FROM posts WHERE destination_id = ? AND status != 'posted'", (dest_id,))
    if unsent:
        noun = "post" if unsent == 1 else "posts"
        raise bad(f"This destination still has {unsent} unsent {noun}. Post or cancel them first.", 409)
    db.execute("UPDATE streamers SET default_destination_id = NULL WHERE default_destination_id = ?", (dest_id,))
    db.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))
    return Response(status_code=204)


# --------------------------------------------------------------------------- posts

@app.get("/api/posts")
def list_posts(status: str = "open") -> list[dict]:
    publish.release_due()
    if status == "open":
        found = db.rows("SELECT * FROM posts WHERE status IN ('scheduled', 'due', 'posting', 'failed') "
                        "ORDER BY CASE status WHEN 'due' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END, scheduled_at")
    elif status == "posted":
        found = db.rows("SELECT * FROM posts WHERE status = 'posted' ORDER BY posted_at DESC LIMIT 50")
    elif status == "all":
        found = db.rows("SELECT * FROM posts ORDER BY scheduled_at DESC LIMIT 200")
    else:
        raise bad("Unknown status filter.", 400)
    return [post_out(p) for p in found]


@app.post("/api/posts/{post_id}/post-now")
def post_now(post_id: int) -> dict:
    post = _need("posts", post_id, "post")
    if post["status"] != "scheduled":
        raise bad("Only a post that is waiting for its slot can be posted now.", 409)
    db.update("posts", post_id, {"scheduled_at": db.now()})
    publish.release_due()
    return post_out(db.get("posts", post_id))


@app.post("/api/posts/{post_id}/mark-posted")
def mark_posted(post_id: int, body: dict = Body(default={})) -> dict:
    _need("posts", post_id, "post")
    url = str(body.get("post_url") or "").strip() or None
    if url and not re.match(r"^https?://\S+$", url):
        raise bad("The post link must start with http:// or https://.")
    db.update("posts", post_id, {"status": "posted", "posted_at": db.now(), "post_url": url, "error": None})
    return post_out(db.get("posts", post_id))


@app.post("/api/posts/{post_id}/cancel", status_code=204)
def cancel_post(post_id: int) -> Response:
    post = _need("posts", post_id, "post")
    if post["status"] == "posted":
        raise bad("This post has already been marked as posted.", 409)
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    return Response(status_code=204)


@app.post("/api/posts/{post_id}/reveal", status_code=204)
def reveal_post(post_id: int) -> Response:
    post = _need("posts", post_id, "post")
    clip = db.get("clips", post["clip_id"])
    if not clip["file_path"] or not Path(clip["file_path"]).exists():
        raise bad("The clip file is no longer on disk.", 404)
    subprocess.Popen(["explorer", "/select,", str(Path(clip["file_path"]))])
    return Response(status_code=204)


# --------------------------------------------------------------------------- activity

@app.get("/api/activity")
def get_activity() -> dict:
    settings = config.load_settings()
    unfinished = db.rows("SELECT * FROM jobs WHERE status IN ('queued', 'running', 'failed') ORDER BY id")
    finished = db.rows("SELECT * FROM jobs WHERE status = 'done' ORDER BY id DESC LIMIT 20")
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    usage = db.row("SELECT COALESCE(SUM(tokens_in), 0) AS tokens_in, COALESCE(SUM(tokens_out), 0) AS tokens_out, "
                   "COALESCE(SUM(llm_requests), 0) AS requests FROM vods WHERE updated_at >= ?", (month_start,))
    return {"window": _window(settings), "worker": _worker_state(),
            "jobs": [worker.job_view(j, settings) for j in unfinished + finished],
            "events": db.rows("SELECT id, at, level, message FROM events ORDER BY id DESC LIMIT 100"),
            "usage": {**usage, "since": month_start}}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int) -> dict:
    job = _need("jobs", job_id, "job")
    if job["status"] != "failed":
        raise bad("Only a failed job can be retried.", 409)
    db.update("jobs", job_id, {"status": "queued", "attempts": 0, "error": None, "run_after": None,
                               "force": 1, "updated_at": db.now()})
    return worker.job_view(db.get("jobs", job_id), config.load_settings())


@app.post("/api/jobs/{job_id}/run-now")
def run_job_now(job_id: int) -> dict:
    _need("jobs", job_id, "job")
    db.update("jobs", job_id, {"force": 1, "run_after": None, "updated_at": db.now()})
    return worker.job_view(db.get("jobs", job_id), config.load_settings())


@app.post("/api/jobs/{job_id}/cancel", status_code=204)
def cancel_job(job_id: int) -> Response:
    """Take a job that is not running off the list, for good."""
    job = _need("jobs", job_id, "job")
    if job["status"] == "running":
        raise bad("This job is running right now. It can be cancelled once it stops.", 409)
    if job["status"] == "done":
        raise bad("This job has already finished.", 409)
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    if job["kind"] == "process_vod":
        # "ignored" keeps the scheduler from queueing the same stream again on its next check.
        # It can still be processed on purpose with "Process one stream".
        db.update("vods", job["ref_id"], {"status": "ignored", "error": None, "updated_at": db.now()})
    elif job["kind"] == "render_clip":
        db.update("clips", job["ref_id"], {"render_state": "failed",
                                           "render_error": "The render was cancelled. Save the clip again to render it."})
    db.event(f"Cancelled: {job['label'] or job['kind']}.")
    return Response(status_code=204)


@app.post("/api/run-now")
def run_all_now() -> dict:
    started = db.execute("UPDATE jobs SET force = 1, run_after = NULL WHERE status = 'queued' AND force = 0")
    return {"started": started}


@app.post("/api/vods")
def add_vod(body: dict = Body(...)) -> dict:
    streamer = _need("streamers", int(body.get("streamer_id") or 0), "streamer")
    try:
        platform, key = platforms.parse_vod_url(str(body.get("url") or ""))
        known = db.row("SELECT * FROM vods WHERE streamer_id = ? AND platform = ? AND vod_key = ? AND status != 'ignored'",
                       (streamer["id"], platform, key))
        if known:
            # Processing it again reuses the saved transcript and only adds moments not already clipped,
            # which is how new instructions or a newly added API key are applied to a stream.
            if known["status"] in ("queued", "processing"):
                raise bad("This stream is already being processed.", 409)
            db.update("vods", known["id"], {"status": "queued", "error": None, "updated_at": db.now()})
            job_id = worker.enqueue("process_vod", known["id"], f"{streamer['name']}: {known['title']}", force=True)
            return {"job_id": job_id, "message": "Queued again. Clips already made from this stream are kept."}
        info = platforms.vod_info(str(body["url"]))
    except PlatformError as exc:
        raise bad(str(exc), 400) from exc
    if info.live_status == "is_live":
        raise bad("This stream is still live. Add it once it has finished.", 409)
    db.execute("DELETE FROM vods WHERE streamer_id = ? AND platform = ? AND vod_key = ? AND status = 'ignored'",
               (streamer["id"], platform, key))
    _, job_id = scheduler.add_vod(streamer, info, force=True)
    return {"job_id": job_id, "message": "Queued. It starts as soon as the worker is free."}


# --------------------------------------------------------------------------- settings

def settings_out() -> dict:
    s = config.load_settings()
    root = config.data_dir()
    used = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return {"llm": {"provider": llm.provider_name(s),
                    "model": llm.model_for(s, "anthropic"), "key_set": llm.key_for("anthropic") is not None,
                    "gemini_model": llm.model_for(s, "gemini"), "gemini_key_set": llm.key_for("gemini") is not None,
                    "gemini_note": "On Gemini's free tier, Google uses what Snipr sends (stream transcripts and, "
                                   "for voiceover, still frames) to improve its products. On the paid tier it "
                                   "does not."},
            "processing": {"mode": s["processing"].get("mode", "manual"),
                           **{k: s["processing"][k] for k in ("window_start", "window_end", "poll_minutes", "max_retries")}},
            "selection": {"min_score": s["selection"]["min_score"]},
            "publishing": {"default_post_slots": s["publishing"]["default_post_slots"],
                           "append_streamer_credit": bool(s["publishing"]["append_streamer_credit"])},
            "transcription": {"model": s["transcription"]["model"]},
            "storage": {"source_max_age_days": s["storage"]["source_max_age_days"],
                        "keep_rejected_clips_days": s["storage"]["keep_rejected_clips_days"],
                        "data_dir": str(root), "used_gb": round(used / 1e9, 2),
                        "free_gb": round(shutil.disk_usage(root).free / 1e9, 1)}}


def _number(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise bad(f"{name} must be a number from {low:g} to {high:g}.")
    return value


@app.get("/api/settings")
def get_settings() -> dict:
    return settings_out()


@app.put("/api/settings")
def put_settings(body: dict = Body(...)) -> dict:
    changes: dict = {}
    try:
        chosen = body.get("llm") or {}
        if "provider" in chosen:
            if chosen["provider"] not in llm.PROVIDERS:
                raise bad("The AI provider must be Claude or Gemini.")
            changes.setdefault("llm", {})["provider"] = chosen["provider"]
        for key in ("model", "gemini_model"):
            if model := chosen.get(key):
                if not re.fullmatch(r"[A-Za-z0-9._\-]{3,80}", str(model)):
                    raise bad("That does not look like a model name.")
                changes.setdefault("llm", {})[key] = str(model)
        processing = body.get("processing") or {}
        if "mode" in processing:
            if processing["mode"] not in ("manual", "window"):
                raise bad("Processing must be Manual or Time window.")
            changes.setdefault("processing", {})["mode"] = processing["mode"]
        for key in ("window_start", "window_end"):
            if key in processing:
                changes.setdefault("processing", {})[key] = publish.parse_time(processing[key]).strftime("%H:%M")
        if "poll_minutes" in processing:
            changes.setdefault("processing", {})["poll_minutes"] = int(_number(processing["poll_minutes"], "The check interval", 1, 1440))
        if "max_retries" in processing:
            changes.setdefault("processing", {})["max_retries"] = int(_number(processing["max_retries"], "Retries", 1, 10))
        if "min_score" in (body.get("selection") or {}):
            changes["selection"] = {"min_score": float(_number(body["selection"]["min_score"], "The quality bar", 0, 1))}
        publishing = body.get("publishing") or {}
        if "default_post_slots" in publishing:
            changes.setdefault("publishing", {})["default_post_slots"] = publish.clean_slots(publishing["default_post_slots"])
        if "append_streamer_credit" in publishing:
            changes.setdefault("publishing", {})["append_streamer_credit"] = bool(publishing["append_streamer_credit"])
        if "model" in (body.get("transcription") or {}):
            if body["transcription"]["model"] not in WHISPER_MODELS:
                raise bad("Transcription model must be small, medium or large-v3.")
            changes["transcription"] = {"model": body["transcription"]["model"]}
        storage = body.get("storage") or {}
        for key, label in (("source_max_age_days", "Days to keep footage"), ("keep_rejected_clips_days", "Days to keep rejected clips")):
            if key in storage:
                changes.setdefault("storage", {})[key] = int(_number(storage[key], label, 1, 365))
    except publish.SlotError as exc:
        raise bad(str(exc)) from exc
    if changes:
        config.save_settings(changes)
        if "processing" in changes and "poll_minutes" in changes["processing"] and "scheduler" in _background:
            _background["scheduler"].reschedule_job("poll", trigger="interval",
                                                    minutes=changes["processing"]["poll_minutes"])
    return settings_out()


def _provider_arg(value) -> str:
    name = str(value or "anthropic")
    if name not in llm.PROVIDERS:
        raise bad("The AI provider must be Claude or Gemini.")
    return name


@app.post("/api/settings/api-key")
def set_api_key(body: dict = Body(...)) -> dict:
    """Store a provider's API key in Windows Credential Manager. ``provider`` defaults to Claude."""
    name = _provider_arg(body.get("provider"))
    key = str(body.get("key") or "").strip()
    if len(key) < 20 or " " in key:
        raise bad("That does not look like an API key.")
    secrets.put(llm.PROVIDERS[name]["secret"], key)
    return {"key_set": True, "provider": name}


@app.delete("/api/settings/api-key")
def delete_api_key(provider: str = "anthropic") -> dict:
    name = _provider_arg(provider)
    secrets.delete(llm.PROVIDERS[name]["secret"])
    return {"key_set": llm.key_for(name) is not None, "provider": name}


@app.post("/api/settings/test-ai")
def test_ai() -> dict:
    """Send one tiny request to the selected provider, to confirm the key and model work."""
    settings = config.load_settings()
    try:
        result = llm.check_provider(settings)
    except llm.LLMError as exc:
        raise bad(str(exc), 502) from exc
    label = llm.PROVIDERS[result["provider"]]["label"]
    return {**result, "message": f"{label} answered using {result['model']}. Snipr can use it."}


# --------------------------------------------------------------------------- files

def _file(path: Path, media_type: str) -> FileResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(path, media_type=media_type)


@app.get("/media/clips/{clip_id}.mp4")
def media_clip(clip_id: int) -> FileResponse:
    return _file(config.subdir("clips") / f"{clip_id}.mp4", "video/mp4")


@app.get("/media/thumbs/{clip_id}.jpg")
def media_thumb(clip_id: int) -> FileResponse:
    return _file(config.subdir("thumbs") / f"{clip_id}.jpg", "image/jpeg")


@app.get("/media/frames/{token}.jpg")
def media_frame(token: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{16}", token):
        raise HTTPException(status_code=404, detail="File not found.")
    return _file(config.subdir("frames") / f"{token}.jpg", "image/jpeg")


@app.get("/")
def index():
    page = STATIC_DIR / "index.html"
    if not page.is_file():
        return PlainTextResponse("The Snipr page has not been built yet.", status_code=503)
    return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-cache"})


app.mount("/static/fonts", StaticFiles(directory=FONT_DIR), name="fonts")
app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
