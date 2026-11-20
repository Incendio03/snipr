"""Cut and encode clips with ffmpeg, on the GPU encoder when it is available."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path


class RenderError(Exception):
    pass


@lru_cache(maxsize=1)
def nvenc_available() -> bool:
    """True when ffmpeg can actually open an NVENC session on this machine."""
    probe = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "color=black:s=256x256:d=0.2",
         "-c:v", "h264_nvenc", "-f", "null", "-"],
        capture_output=True, text=True)
    return probe.returncode == 0


def video_codec_args() -> list[str]:
    if nvenc_available():
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0",
                "-maxrate", "12M", "-bufsize", "24M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RenderError(result.stderr.strip()[-1500:] or "ffmpeg failed without a message")


def seek_args(seek: float) -> tuple[list[str], list[str]]:
    """Input and output seek options that land exactly on ``seek`` seconds.

    A fast seek alone starts the sound up to a second late on files assembled
    from stream segments (measured: 0.92 s of missing audio). Seeking fast to
    ten seconds early and decoding the rest gives a frame-exact start with
    sound from the first frame.
    """
    fast = max(0.0, seek - 10.0)
    before = ["-ss", f"{fast:.3f}"] if fast > 0 else []
    after = ["-ss", f"{seek - fast:.3f}"] if seek - fast > 0 else []
    return before, after


AUDIO_ARGS = ["-c:a", "aac", "-b:a", "160k", "-af", "aresample=async=1:first_pts=0"]


def cut_plain(source: Path, source_start: float, start: float, end: float, out: Path) -> Path:
    """Cut ``start``..``end`` (VOD time) out of a source file, keeping its framing."""
    out.parent.mkdir(parents=True, exist_ok=True)
    before, after = seek_args(max(0.0, start - source_start))
    run_ffmpeg([*before, "-i", str(source), *after, "-t", f"{end - start:.3f}",
                *video_codec_args(), *AUDIO_ARGS, "-movflags", "+faststart", str(out)])
    return out
