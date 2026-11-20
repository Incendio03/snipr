"""Choose clips from an analysed VOD, by mode.

Highlights nominates moments from chat and loudness spikes, then lets the LLM
filter and trim them. Informative and Custom read the whole transcript. With
no LLM configured, Highlights still works on the spike scores alone; the other
two modes need the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import scoring
from .scoring import Segment

INFORMATIVE_INSTRUCTION = (
    "Find self-contained segments where the streamer explains something: how to do a thing, "
    "why something works, advice, or a clear opinion with reasons. A viewer who has not seen "
    "the rest of the stream must be able to follow it from the first sentence."
)


# The bar when no LLM is configured: a spike about two baseline deviations high.
# On a real 45 minute stretch the best windows scored 0.41 to 0.53 and ordinary
# chatter stayed under 0.4.
SIGNAL_ONLY_MIN_SCORE = 0.4


class NeedsLLM(Exception):
    """Raised when a mode cannot run without an LLM provider."""


@dataclass
class SelectionRequest:
    mode: str                              # highlights | informative | custom
    instructions: str
    max_clips: int
    clip_min_s: float
    clip_max_s: float
    min_score: float
    duration: float                        # VOD length
    span_start: float                      # VOD time of index 0 of the curves
    ranges: list[tuple[float, float]]      # parts of the VOD that may be clipped
    transcript: dict
    loudness: np.ndarray
    chat_rate: np.ndarray | None
    nominations: int = 15
    window_s: int = 30
    step_s: int = 10
    chat_weight: float = 0.6
    audio_weight: float = 0.4
    streamer: str = ""
    vod_title: str = ""


def _in_ranges(start: float, end: float, ranges: list[tuple[float, float]]) -> bool:
    return any(lo - 1 <= start and end <= hi + 1 for lo, hi in ranges)


def nominate_highlights(req: SelectionRequest) -> list[scoring.Window]:
    windows = scoring.score_windows(req.chat_rate, req.loudness, req.window_s, req.step_s,
                                    req.chat_weight, req.audio_weight, offset=req.span_start)
    windows = [w for w in windows if _in_ranges(w.start, w.end, req.ranges)]
    return scoring.nominate(windows, req.nominations, max_len_s=max(req.clip_max_s, 60.0))


def per_minute(curve: np.ndarray | None, span_start: float, kind: str) -> list[tuple[int, float]]:
    """(VOD minute, value) pairs: messages per minute for chat, mean dBFS for loudness."""
    if curve is None or len(curve) == 0:
        return []
    out = []
    for i in range(0, len(curve), 60):
        block = curve[i:i + 60]
        value = float(np.sum(block)) if kind == "chat" else float(np.mean(block))
        out.append((int((span_start + i) // 60), round(value, 1)))
    return out


def _spike_reason(win: scoring.Window, has_chat: bool) -> str:
    parts = []
    if has_chat and win.chat_z >= 1:
        parts.append(f"chat ran {win.chat_z:.0f} deviations above this stream's baseline")
    if win.audio_z >= 1:
        parts.append(f"loudness ran {win.audio_z:.0f} deviations above baseline")
    return ("Picked on signals alone: " + " and ".join(parts) + ".") if parts else "Picked on signals alone."


def select(req: SelectionRequest, llm=None) -> list[Segment]:
    """Return the clips for one VOD, best first, at most ``req.max_clips``."""
    segments = req.transcript.get("segments", [])
    common = dict(duration=req.duration, min_len=req.clip_min_s, max_len=req.clip_max_s,
                  min_score=req.min_score, limit=req.max_clips, allowed=req.ranges,
                  transcript_segments=segments)

    if req.mode == "highlights":
        nominated = nominate_highlights(req)
        if not nominated:
            return []
        if llm is not None:
            raw = llm.select_segments(req, nominated)
            return scoring.validate_segments(raw, **common)
        # No LLM: keep the strongest spikes, cut on sentence breaks. Spike scores
        # are not on the LLM's scale, so the quality bar set for the LLM would
        # reject nearly everything; a bar measured for spikes is used instead.
        common["min_score"] = min(req.min_score, SIGNAL_ONLY_MIN_SCORE)
        has_chat = req.chat_rate is not None and float(np.sum(req.chat_rate)) > 0
        picks = []
        for win in nominated:
            start, end = scoring.snap_to_sentences(win.start, win.end, segments,
                                                   req.clip_min_s, req.clip_max_s)
            picks.append({"start": start, "end": end, "score": win.score,
                          "title": "", "reason": _spike_reason(win, has_chat)})
        return scoring.validate_segments(picks, **common)

    if req.mode in ("informative", "custom"):
        if llm is None:
            raise NeedsLLM(f"{req.mode.title()} mode reads the transcript with the LLM. "
                           "Add an Anthropic API key in settings to use it.")
        if req.mode == "custom" and not req.instructions.strip():
            raise ValueError("Custom mode needs instructions describing what to look for.")
        raw = llm.select_segments(req, None)
        return scoring.validate_segments(raw, **common)

    raise ValueError(f"Unknown selection mode: {req.mode}")
