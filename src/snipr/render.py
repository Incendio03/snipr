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
        # Capped at 8 Mbit/s: a one minute clip stays around 60 MB, well inside upload limits.
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0",
                "-maxrate", "8M", "-bufsize", "16M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "8M",
            "-bufsize", "16M", "-pix_fmt", "yuv420p"]


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


OUT_W, OUT_H = 1080, 1920
STACK_TOP_H = 608                       # stacked layout: facecam 1080 x 608, gameplay 1080 x 1312
STACK_BOTTOM_H = OUT_H - STACK_TOP_H


def aspect_fit(rect: list | None, src_w: int, src_h: int, ratio: float) -> tuple[int, int, int, int]:
    """The largest crop of aspect ``ratio`` (width / height) centred in ``rect``.

    ``rect`` is ``[x, y, w, h]`` as fractions of the source frame, or None for
    the whole frame. Returns even pixel values ``(x, y, w, h)`` inside the frame,
    so a rectangle drawn slightly off-ratio in the form still renders undistorted.
    """
    if rect and len(rect) == 4 and rect[2] > 0 and rect[3] > 0:
        rx, ry, rw, rh = (max(0.0, min(1.0, float(v))) for v in rect)
        rw, rh = min(rw, 1.0 - rx), min(rh, 1.0 - ry)
    else:
        rx, ry, rw, rh = 0.0, 0.0, 1.0, 1.0
    px, py, pw, ph = rx * src_w, ry * src_h, rw * src_w, rh * src_h
    if pw / ph > ratio:
        w, h = ph * ratio, ph
    else:
        w, h = pw, pw / ratio
    w = max(2, int(w) // 2 * 2)
    h = max(2, int(h) // 2 * 2)
    x = int(px + (pw - w) / 2) // 2 * 2
    y = int(py + (ph - h) / 2) // 2 * 2
    return max(0, min(x, src_w - w)), max(0, min(y, src_h - h)), w, h


def is_stacked(layout: dict | None) -> bool:
    return bool(layout and layout.get("type") == "stacked" and layout.get("facecam") and layout.get("gameplay"))


def frame_filters(layout: dict | None, src_w: int, src_h: int) -> str:
    """The filter chain that turns the source picture ``[src]`` into a 1080 x 1920 ``[base]``."""
    if is_stacked(layout):
        fx, fy, fw, fh = aspect_fit(layout["facecam"], src_w, src_h, OUT_W / STACK_TOP_H)
        gx, gy, gw, gh = aspect_fit(layout["gameplay"], src_w, src_h, OUT_W / STACK_BOTTOM_H)
        return (f"[src]split=2[fa][ga];"
                f"[fa]crop={fw}:{fh}:{fx}:{fy},scale={OUT_W}:{STACK_TOP_H}:flags=lanczos[top];"
                f"[ga]crop={gw}:{gh}:{gx}:{gy},scale={OUT_W}:{STACK_BOTTOM_H}:flags=lanczos[bottom];"
                f"[top][bottom]vstack=inputs=2,setsar=1[base]")
    x, y, w, h = aspect_fit((layout or {}).get("crop"), src_w, src_h, OUT_W / OUT_H)
    return f"[src]crop={w}:{h}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1[base]"


NARRATION_LEAD_S = 0.4            # the narrator starts this long after the clip does
DUCKED_VOLUME = 0.16              # stream sound under a voiceover: about 16 dB down


def audio_filters(rest: float, duration: float, narration_input: int | None, mix: str,
                  narration_len: float | None = None, gain: float = 1.0) -> str:
    """The audio half of the filter graph, ending in ``[a]``.

    Without a voiceover the stream's own sound is kept. With one, the stream
    sound is dropped ("mute") or lowered while the narrator speaks ("duck").
    When the narration is shorter than the clip, the lowered stream sound
    comes back up once the narrator has finished.
    """
    stream = (f"[0:a]atrim=start={rest:.3f}:duration={duration:.3f},asetpts=PTS-STARTPTS,"
              f"aresample=async=1:first_pts=0")
    if narration_input is None:
        return f"{stream}[a]"
    lead_ms = int(NARRATION_LEAD_S * 1000)
    voice = (f"[{narration_input}:a]aresample=48000,volume={gain:.3f},adelay={lead_ms}:all=1,"
             f"apad=whole_dur={duration:.3f}")
    if mix == "mute":
        return f"{voice},alimiter=limit=0.95[a]"
    done = NARRATION_LEAD_S + (narration_len or duration) + 0.2
    if done < duration - 1.0:
        low, rise = DUCKED_VOLUME, 0.6
        level = (f"volume='if(lt(t,{done:.3f}),{low},min(1,{low}+(t-{done:.3f})/{rise}*{1 - low:.2f}))'"
                 f":eval=frame")
    else:
        level = f"volume={DUCKED_VOLUME}"
    return (f"{stream},aresample=48000,{level}[bed];{voice}[vo];"
            f"[bed][vo]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[a]")


def render_clip(source: Path, source_start: float, start: float, end: float, out: Path,
                layout: dict | None, overlays=None, src_info: dict | None = None,
                narration: Path | None = None, mix: str = "duck",
                narration_len: float | None = None, narration_gain: float = 1.0) -> Path:
    """Render ``start``..``end`` (VOD time) as a 1080 x 1920 clip with headline and captions.

    ``overlays`` is a ``captions.Overlays``; None renders the picture alone.
    ``narration`` is a voiceover audio file laid over the clip (see ``audio_filters``).
    """
    from .platforms import probe_video

    out.parent.mkdir(parents=True, exist_ok=True)
    info = src_info or probe_video(source)
    duration = end - start
    seek = max(0.0, start - source_start)
    fast = max(0.0, seek - 10.0)            # see seek_args: fast seek short of the target, then trim
    rest = seek - fast
    fps = min(60.0, max(24.0, round(info["fps"])))

    args: list[str] = []
    if fast > 0:
        args += ["-ss", f"{fast:.3f}"]
    args += ["-i", str(source)]
    graph = [f"[0:v]trim=start={rest:.3f}:duration={duration:.3f},setpts=PTS-STARTPTS,fps={fps:g}[src]",
             frame_filters(layout, info["width"], info["height"])]
    last = "base"
    index = 1
    if overlays is not None and overlays.headline_png is not None:
        args += ["-i", str(overlays.headline_png)]
        x, y = overlays.headline_xy
        graph.append(f"[{last}][{index}:v]overlay={x}:{y}:format=auto[withhead]")
        last, index = "withhead", index + 1
    if overlays is not None and overlays.captions_list is not None:
        args += ["-f", "concat", "-safe", "0", "-i", str(overlays.captions_list)]
        graph.append(f"[{last}][{index}:v]overlay=0:{overlays.captions_y}:format=auto[withcaps]")
        last, index = "withcaps", index + 1
    graph.append(f"[{last}]format=yuv420p[v]")
    narration_input = None
    if narration is not None:
        args += ["-i", str(narration)]
        narration_input = index
    graph.append(audio_filters(rest, duration, narration_input, mix, narration_len, narration_gain))
    args += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]",
             "-t", f"{duration:.3f}", *video_codec_args(), "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", str(out)]
    run_ffmpeg(args)
    return out


def grab_frame(source: Path, at: float, out: Path, width: int | None = None) -> Path:
    """Save one frame of a local video as an image."""
    out.parent.mkdir(parents=True, exist_ok=True)
    args = ["-ss", f"{max(0.0, at):.3f}", "-i", str(source), "-frames:v", "1"]
    if width:
        args += ["-vf", f"scale={width}:-2"]
    run_ffmpeg([*args, "-update", "1", str(out)])
    return out


def cut_plain(source: Path, source_start: float, start: float, end: float, out: Path) -> Path:
    """Cut ``start``..``end`` (VOD time) out of a source file, keeping its framing."""
    out.parent.mkdir(parents=True, exist_ok=True)
    before, after = seek_args(max(0.0, start - source_start))
    run_ffmpeg([*before, "-i", str(source), *after, "-t", f"{end - start:.3f}",
                *video_codec_args(), *AUDIO_ARGS, "-movflags", "+faststart", str(out)])
    return out
