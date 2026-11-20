"""Audio helpers built on ffmpeg: loudness curve, and aligning a clip to its source."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


def _pcm_stream(path: Path, start: float | None, duration: float | None) -> subprocess.Popen:
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def energy_curve(path: Path, hop_s: float = 1.0, start: float | None = None,
                 duration: float | None = None) -> np.ndarray:
    """RMS level in dBFS for every ``hop_s`` seconds of a media file.

    The file is streamed through ffmpeg in small blocks, so a 24 hour VOD does
    not have to fit in memory.
    """
    hop = int(SAMPLE_RATE * hop_s)
    block_bytes = hop * 2
    proc = _pcm_stream(path, start, duration)
    levels: list[float] = []
    assert proc.stdout is not None
    while True:
        raw = proc.stdout.read(block_bytes)
        if len(raw) < 2:
            break
        samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        levels.append(20.0 * np.log10(max(rms, 1e-5)))
    err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    if proc.wait() != 0 and not levels:
        raise RuntimeError(f"ffmpeg could not read audio from {path}: {err.strip()[:300]}")
    return np.asarray(levels, dtype=np.float32)


def to_wav(src: Path, dst: Path, start: float | None = None, duration: float | None = None) -> Path:
    """Write a 16 kHz mono WAV of ``src`` (optionally only a range of it)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


def measure_offset(reference: Path, ref_start: float, segment: Path, max_lag_s: float = 8.0,
                   probe_s: float = 30.0, segment_skip: float = 0.0) -> tuple[float, float]:
    """Where ``segment`` really starts on the timeline of ``reference``.

    A downloaded section is cut on keyframes, so it can begin a little before
    or after the time that was asked for. Both files hold the same sound, so
    the lag that best lines up their loudness envelopes gives the true start.

    ``segment_skip`` starts the comparison that many seconds into the segment,
    for a segment that begins before the reference does. Returns
    ``(start time on the reference timeline, match quality 0..1)``; the quality
    is 0 when the two could not be compared and ``ref_start`` is returned as is.
    """
    hop = 0.01
    seg = energy_curve(segment, hop_s=hop, start=segment_skip or None, duration=probe_s)
    anchor = ref_start + segment_skip              # reference time the probe should match
    lead = max(0.0, anchor - max_lag_s)
    ref = energy_curve(reference, hop_s=hop, start=lead or None, duration=probe_s + 2 * max_lag_s)
    if seg.size < 200 or ref.size < seg.size:
        return ref_start, 0.0
    seg_n = seg - seg.mean()
    ref_n = ref - ref.mean()
    corr = np.correlate(ref_n, seg_n, mode="valid")
    if corr.size == 0:
        return ref_start, 0.0
    # Normalise by the local energy of the reference so a loud stretch cannot win by volume.
    window = np.ones(seg_n.size, dtype=np.float32)
    local = np.sqrt(np.maximum(np.convolve(ref_n * ref_n, window, mode="valid"), 1e-6))
    quality = corr / (local * max(float(np.linalg.norm(seg_n)), 1e-6))
    best = int(np.argmax(quality))
    return lead + best * hop - segment_skip, float(quality[best])


def find_offset(reference: Path, ref_start: float, segment: Path, max_lag_s: float = 8.0,
                probe_s: float = 30.0, segment_skip: float = 0.0) -> float:
    """``measure_offset`` that falls back to ``ref_start`` on a poor match."""
    offset, quality = measure_offset(reference, ref_start, segment, max_lag_s, probe_s, segment_skip)
    return offset if quality >= 0.5 else ref_start
