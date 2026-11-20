"""Source platforms: recognise channel links, list finished streams, download.

Everything goes through yt-dlp. Twitch and YouTube are supported; Kick is
recognised so the form can say it is not built yet (milestone 7).

What each platform gives us, checked against real channels on 2026-11-20:

* Twitch VODs carry game chapters ("Just Chatting", "Fortnite", ...) and chat
  replay (see ``chat.py``).
* YouTube stream replays carry neither, so the game filter falls back to the
  title and Highlights mode runs on audio alone.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yt_dlp
from yt_dlp.utils import download_range_func

SUPPORTED = ("twitch", "youtube")

# Writing a JPEG from video: the encoder refuses "TV range" colour (which YouTube
# marks its video as) unless it is converted to full range first.
JPEG_FILTER = "scale=out_range=pc,format=yuvj420p"
JPEG_ARGS = ["-vf", JPEG_FILTER, "-q:v", "3"]
ProgressFn = Callable[[float], None]


class PlatformError(Exception):
    """A channel or VOD could not be read from its platform."""


@dataclass
class Channel:
    platform: str
    handle: str
    url: str


@dataclass
class VodInfo:
    platform: str
    vod_id: str
    url: str
    title: str
    duration: float
    published_at: int | None = None
    uploader: str = ""
    chapters: list[dict] = field(default_factory=list)  # {start, end, title}
    live_status: str = ""


_TWITCH_CHANNEL = re.compile(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,30})(?:[/?#].*)?$")
_TWITCH_VOD = re.compile(r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/videos/(\d+)")
_YOUTUBE_HANDLE = re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]+)")
_YOUTUBE_CHANNEL = re.compile(r"(?:https?://)?(?:www\.|m\.)?youtube\.com/(channel/UC[\w\-]{20,}|c/[\w.\-]+|user/[\w.\-]+)")
_YOUTUBE_VIDEO = re.compile(r"(?:youtube\.com/(?:watch\?.*?v=|live/|shorts/)|youtu\.be/)([\w\-]{11})")
_KICK_CHANNEL = re.compile(r"(?:https?://)?(?:www\.)?kick\.com/([A-Za-z0-9_\-]{2,40})(?:[/?#].*)?$")
_TWITCH_RESERVED = {"videos", "directory", "settings", "downloads", "p", "search"}


def detect_platform(url: str) -> str | None:
    text = url.strip().lower()
    if "twitch.tv" in text:
        return "twitch"
    if "youtube.com" in text or "youtu.be" in text:
        return "youtube"
    if "kick.com" in text:
        return "kick"
    return None


def parse_channel(url: str) -> Channel:
    """Turn a pasted channel link into a platform, handle and canonical URL."""
    text = url.strip()
    if m := _TWITCH_CHANNEL.match(text):
        handle = m.group(1).lower()
        if handle not in _TWITCH_RESERVED:
            return Channel("twitch", handle, f"https://www.twitch.tv/{handle}")
    if m := _YOUTUBE_HANDLE.search(text):
        return Channel("youtube", m.group(1), f"https://www.youtube.com/{m.group(1)}")
    if m := _YOUTUBE_CHANNEL.search(text):
        return Channel("youtube", m.group(1).split("/", 1)[1], f"https://www.youtube.com/{m.group(1)}")
    if m := _KICK_CHANNEL.match(text):
        handle = m.group(1).lower()
        return Channel("kick", handle, f"https://kick.com/{handle}")
    raise PlatformError(
        "That does not look like a channel link. Paste something like "
        "https://twitch.tv/name or https://youtube.com/@name."
    )


def parse_vod_url(url: str) -> tuple[str, str]:
    """Return (platform, vod id) for a link to a single finished stream."""
    if m := _TWITCH_VOD.search(url):
        return "twitch", m.group(1)
    if m := _YOUTUBE_VIDEO.search(url):
        return "youtube", m.group(1)
    raise PlatformError("That does not look like a link to a Twitch VOD or a YouTube video.")


def _ydl(extra: dict | None = None) -> yt_dlp.YoutubeDL:
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


def _extract(url: str, opts: dict | None = None) -> dict:
    try:
        with _ydl(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise PlatformError(str(exc).replace("ERROR: ", "")) from exc


def list_vods(channel: Channel, limit: int = 10) -> list[VodInfo]:
    """Most recent finished streams of a channel, newest first.

    The listing is shallow (no chapters). Call ``vod_info`` for the details of
    one VOD.
    """
    if channel.platform == "twitch":
        page = f"{channel.url}/videos?filter=archives&sort=time"
    elif channel.platform == "youtube":
        page = f"{channel.url}/streams"
    else:
        raise PlatformError(f"{channel.platform.title()} ingest is not built yet.")

    info = _extract(page, {"extract_flat": True, "playlistend": limit})
    vods: list[VodInfo] = []
    for entry in info.get("entries") or []:
        if not entry:
            continue
        status = entry.get("live_status") or ""
        if status in ("is_live", "is_upcoming"):
            continue  # only finished streams are processed
        raw_id = str(entry.get("id") or "")
        vod_id = raw_id[1:] if channel.platform == "twitch" and raw_id.startswith("v") else raw_id
        url = entry.get("url") or entry.get("webpage_url") or ""
        if channel.platform == "youtube" and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={vod_id}"
        vods.append(VodInfo(
            platform=channel.platform,
            vod_id=vod_id,
            url=url,
            title=(entry.get("title") or "").strip(),
            duration=float(entry.get("duration") or 0),
            published_at=entry.get("timestamp"),
            live_status=status,
        ))
    return vods


def vod_info(url: str) -> VodInfo:
    platform, vod_id = parse_vod_url(url)
    info = _extract(url)
    chapters = [
        {"start": float(c.get("start_time") or 0), "end": float(c.get("end_time") or 0),
         "title": (c.get("title") or "").strip()}
        for c in (info.get("chapters") or [])
    ]
    return VodInfo(
        platform=platform,
        vod_id=vod_id,
        url=info.get("webpage_url") or url,
        title=(info.get("title") or "").strip(),
        duration=float(info.get("duration") or 0),
        published_at=info.get("timestamp"),
        uploader=info.get("uploader") or "",
        chapters=chapters,
        live_status=info.get("live_status") or "",
    )


def _download(url: str, out_path: Path, fmt: str, ranges: tuple[float, float] | None,
              progress: ProgressFn | None, merge_ext: str | None) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stem = out_path.with_suffix("")

    def hook(status: dict) -> None:
        if progress and status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            if total:
                progress(min(1.0, (status.get("downloaded_bytes") or 0) / total))
            elif status.get("fragment_count"):
                progress(min(1.0, (status.get("fragment_index") or 0) / status["fragment_count"]))

    opts: dict = {
        "format": fmt,
        "outtmpl": str(stem) + ".%(ext)s",
        "overwrites": True,
        "concurrent_fragment_downloads": 8,
        "retries": 5,
        "fragment_retries": 5,
        "progress_hooks": [hook],
    }
    if merge_ext:
        opts["merge_output_format"] = merge_ext
    if ranges:
        opts["download_ranges"] = download_range_func(None, [ranges])
        opts["force_keyframes_at_cuts"] = False
    try:
        with _ydl(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            produced = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as exc:
        raise PlatformError(str(exc).replace("ERROR: ", "")) from exc
    if merge_ext:
        produced = produced.with_suffix("." + merge_ext)
    if not produced.exists():
        matches = sorted(stem.parent.glob(stem.name + ".*"))
        if not matches:
            raise PlatformError(f"Download finished but no file was written for {url}")
        produced = matches[0]
    return produced


def download_audio(url: str, out_path: Path, start: float | None = None,
                   end: float | None = None, progress: ProgressFn | None = None) -> Path:
    """Download only the audio of a VOD, optionally only ``start``..``end`` seconds."""
    ranges = (float(start or 0), float(end)) if end else None
    return _download(url, out_path, "ba/worst[acodec!=none]/b", ranges, progress, None)


def download_video(url: str, out_path: Path, start: float, end: float, height: int = 1080,
                   progress: ProgressFn | None = None) -> Path:
    """Download ``start``..``end`` seconds of a VOD with picture and sound.

    The cut lands on keyframes, so the file does not begin exactly at
    ``start``. ``audio.find_offset`` measures the real offset against the
    analysis audio.
    """
    # H.264 with AAC first: it sits natively in MP4 and decodes fastest. YouTube
    # otherwise hands out VP9, which works but is slower to render from.
    fmt = (f"bv*[height<={height}][vcodec^=avc1]+ba[acodec^=mp4a]/"
           f"bv*[height<={height}]+ba/b[height<={height}]/b")
    return _download(url, out_path, fmt, (max(0.0, start), end), progress, "mp4")


NOT_READY = ("is_live", "is_upcoming", "post_live")     # still live, or the platform is still processing it


def sample_frame(channel: Channel, out_path: Path, games: list[str] | None = None) -> dict:
    """Save one frame from a recent finished stream of the channel, for marking crops.

    With ``games`` set, the newest stream that matches the game filter is used
    and the frame is taken from inside the matching part, so the frame shows
    the game the streamer will actually be clipped in. When none of the recent
    streams matches, the latest stream is used and ``matched`` is False.

    Only a ten second section is downloaded. Returns the frame's size, the
    stream's title, ``matched`` (None when no filter was given) and how many
    streams were looked at. Raises ``PlatformError`` when the channel has no
    finished stream.
    """
    from .scoring import game_matches, game_ranges

    vods = [v for v in list_vods(channel, limit=8 if games else 4) if v.duration == 0 or v.duration > 120]
    if not vods:
        raise PlatformError("This channel has no finished stream to take a frame from yet. "
                            "You can still add the streamer; clips will use a centre crop.")
    details: dict[str, VodInfo] = {}

    def look(vod: VodInfo) -> VodInfo:
        if vod.vod_id not in details:
            details[vod.vod_id] = vod_info(vod.url)
        return details[vod.vod_id]

    # Streams to try, best first: (stream, time to take the frame at, does it match the filter).
    plan: list[tuple[VodInfo, float, bool | None]] = []
    if games:
        for vod in vods:
            # YouTube has no game markers, so the title decides and the listing already has it.
            if channel.platform == "youtube" and not any(game_matches(g, vod.title) for g in games):
                continue
            info = look(vod)
            ranges = game_ranges(games, info.chapters, info.title, info.duration)
            if ranges and info.live_status not in NOT_READY:
                lo, hi = ranges[0]
                plan.append((info, lo + min(max(60.0, (hi - lo) / 3), 1200.0), True))
                if len(plan) == 2:
                    break
    for vod in vods[:3]:
        if all(vod.vod_id != chosen.vod_id for chosen, _, _ in plan):
            info = look(vod)
            if info.live_status not in NOT_READY:
                # A third of the way in, capped at 20 minutes: past the "starting soon" screen.
                at = min(max(60.0, info.duration / 3), 1200.0) if info.duration else 300.0
                plan.append((info, at, False if games else None))

    info, matched, size = None, None, None
    for candidate, at, hit in plan:
        # Ten seconds, not two: a piece is cut between keyframes, and a short piece of a YouTube
        # stream (keyframes up to five seconds apart) can hold no frame that decodes on its own.
        try:
            section = download_video(candidate.url, out_path.with_name(out_path.stem + "_section"), at, at + 10.0)
        except PlatformError:
            continue
        try:
            subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(section), "-frames:v", "1",
                            *JPEG_ARGS, "-update", "1", str(out_path)], check=False, capture_output=True)
            if out_path.exists():
                info, matched, size = candidate, hit, probe_video(section)
                break
        finally:
            section.unlink(missing_ok=True)
    if info is None or size is None:
        raise PlatformError("A frame could not be read from this channel's recent streams. Try again in a "
                            "moment. You can still save; clips use a centre crop until an area is marked.")
    return {"width": size["width"], "height": size["height"], "vod_title": info.title,
            "matched": matched, "looked_at": len(vods)}


def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def probe_video(path: Path) -> dict:
    """Width, height, frame rate and duration of a local video file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate:format=duration",
         "-of", "default=nw=1", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    num, _, den = values.get("r_frame_rate", "30/1").partition("/")
    return {
        "width": int(values["width"]),
        "height": int(values["height"]),
        "fps": float(num) / float(den or 1),
        "duration": float(values.get("duration") or 0),
    }
