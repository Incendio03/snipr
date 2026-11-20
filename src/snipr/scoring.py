"""Pure selection logic: spike scoring, nominations, boundaries, validation, game filter.

Nothing here touches the network, the GPU or the disk, so it is all unit tested.
Times are seconds on the VOD timeline.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np


# --------------------------------------------------------------------------- spikes

@dataclass
class Window:
    start: float
    end: float
    score: float      # 0..1, for display and the quality bar
    chat_z: float
    audio_z: float
    raw: float = 0.0  # unsquashed spike size, used for ranking so huge spikes do not tie


def robust_z(values: np.ndarray) -> np.ndarray:
    """Distance from the stream's own baseline, in robust standard deviations.

    Median and MAD are used so a handful of huge spikes do not raise the bar
    for every other moment in the stream.
    """
    values = np.asarray(values, dtype=np.float64)
    if values.size == 0:
        return values
    median = np.median(values)
    mad = np.median(np.abs(values - median))
    scale = 1.4826 * mad
    if scale < 1e-9:
        scale = float(np.std(values)) or 1.0
    return (values - median) / scale


def window_means(curve: np.ndarray, window_s: int, step_s: int) -> tuple[np.ndarray, np.ndarray]:
    """Mean of ``curve`` (one value per second) over sliding windows."""
    curve = np.asarray(curve, dtype=np.float64)
    if curve.size < window_s:
        return np.zeros(0), np.zeros(0)
    starts = np.arange(0, curve.size - window_s + 1, step_s)
    sums = np.concatenate([[0.0], np.cumsum(curve)])
    means = (sums[starts + window_s] - sums[starts]) / window_s
    return starts.astype(np.float64), means


def unit_score(raw: float) -> float:
    """Squash a spike size (in baseline deviations) into 0..1."""
    return 1.0 - math.exp(-max(0.0, raw) / 4.0)


def score_windows(chat_rate: np.ndarray | None, loudness: np.ndarray, window_s: int = 30,
                  step_s: int = 10, chat_weight: float = 0.6, audio_weight: float = 0.4,
                  offset: float = 0.0) -> list[Window]:
    """Score every sliding window by how far chat and loudness rise above baseline.

    ``chat_rate`` may be None (YouTube has no chat replay); loudness then
    carries the whole score. ``offset`` is the VOD time of index 0.
    """
    starts, loud_means = window_means(loudness, window_s, step_s)
    if starts.size == 0:
        return []
    audio_z = np.maximum(robust_z(loud_means), 0.0)
    has_chat = chat_rate is not None and len(chat_rate) >= window_s and float(np.sum(chat_rate)) > 0
    if has_chat:
        _, chat_means = window_means(np.asarray(chat_rate)[: len(loudness)], window_s, step_s)
        count = min(chat_means.size, audio_z.size)
        chat_z = np.zeros_like(audio_z)
        chat_z[:count] = np.maximum(robust_z(chat_means)[:count], 0.0)
        total = chat_weight + audio_weight
        raw = (chat_weight * chat_z + audio_weight * audio_z) / total
    else:
        chat_z = np.zeros_like(audio_z)
        raw = audio_z
    return [
        Window(float(s) + offset, float(s) + offset + window_s, unit_score(float(r)), float(c),
               float(a), float(r))
        for s, r, c, a in zip(starts, raw, chat_z, audio_z)
    ]


def nominate(windows: list[Window], count: int, merge_gap_s: float = 10.0,
             max_len_s: float = 90.0) -> list[Window]:
    """Pick the ``count`` best non-overlapping moments.

    Windows are taken best first. One that overlaps or sits within
    ``merge_gap_s`` of an already chosen moment never becomes a second
    nomination of it. It widens the chosen moment (up to ``max_len_s``) only
    when it is nearly as strong, which means the moment really lasts that
    long; the weaker shoulders of a short spike are simply dropped.
    """
    chosen: list[Window] = []
    for win in sorted(windows, key=lambda w: (w.raw, w.score), reverse=True):
        if win.score <= 0:
            break
        merged = False
        for i, kept in enumerate(chosen):
            if win.start <= kept.end + merge_gap_s and win.end >= kept.start - merge_gap_s:
                start, end = min(kept.start, win.start), max(kept.end, win.end)
                if win.raw >= 0.75 * kept.raw and end - start <= max_len_s:
                    chosen[i] = Window(start, end, kept.score, max(kept.chat_z, win.chat_z),
                                       max(kept.audio_z, win.audio_z), kept.raw)
                merged = True
                break
        if merged:
            continue
        if len(chosen) >= count:
            continue
        chosen.append(win)
    return sorted(chosen, key=lambda w: w.start)


# --------------------------------------------------------------------------- boundaries

def snap_to_sentences(start: float, end: float, segments: list[dict], min_len: float,
                      max_len: float, reach_s: float = 8.0) -> tuple[float, float]:
    """Move a clip's edges to nearby sentence breaks in the transcript.

    The start moves back to the beginning of the sentence it lands in, the end
    moves forward to the end of its sentence, each by at most ``reach_s``. The
    result always respects ``min_len``..``max_len``.
    """
    new_start, new_end = start, end
    starts = [s["s"] for s in segments if start - reach_s <= s["s"] <= start + reach_s / 2]
    if starts:
        earlier = [s for s in starts if s <= start]
        new_start = max(earlier) if earlier else min(starts)
    ends = [s["e"] for s in segments if end - reach_s / 2 <= s["e"] <= end + reach_s]
    if ends:
        later = [e for e in ends if e >= end]
        new_end = min(later) if later else max(ends)

    if new_end - new_start > max_len:
        limit = new_start + max_len
        inside = [s["e"] for s in segments if new_start + min_len <= s["e"] <= limit]
        new_end = max(inside) if inside else limit
    if new_end - new_start < min_len:
        new_end = new_start + min_len
    return round(max(0.0, new_start), 3), round(new_end, 3)


# --------------------------------------------------------------------------- LLM output

@dataclass
class Segment:
    start: float
    end: float
    score: float
    title: str = ""
    reason: str = ""
    flags: tuple[str, ...] = ()


KNOWN_FLAGS = ("slurs", "sexual", "music")


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return float(text)
        if m := re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)", text):
            hours = int(m.group(1) or 0)
            return hours * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def validate_segments(raw: list, *, duration: float, min_len: float, max_len: float,
                      min_score: float, limit: int,
                      allowed: list[tuple[float, float]] | None = None,
                      transcript_segments: list[dict] | None = None) -> list[Segment]:
    """Turn the LLM's JSON into trustworthy clips.

    Drops anything with unusable times, outside the VOD or the allowed ranges,
    below the quality bar or shorter than ``min_len``. A segment a little over
    ``max_len`` is trimmed at a sentence break; one far over it is dropped.
    Overlapping picks keep the higher score. Returns at most ``limit``, best first.
    """
    cleaned: list[Segment] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        start, end = _number(item.get("start")), _number(item.get("end"))
        score = _number(item.get("score"))
        if start is None or end is None or score is None:
            continue
        if start < 0 or end > duration + 1.0 or end <= start:
            continue
        end = min(end, duration)
        score = min(1.0, score if score <= 1.0 else score / 100.0 if score <= 100 else 1.0)
        if score < min_score:
            continue
        if allowed is not None and not any(lo - 1 <= start and end <= hi + 1 for lo, hi in allowed):
            continue
        length = end - start
        if length < min_len:
            continue
        if length > max_len:
            if length > max_len * 1.25:
                continue
            limit_end = start + max_len
            breaks = [s["e"] for s in (transcript_segments or []) if start + min_len <= s["e"] <= limit_end]
            end = max(breaks) if breaks else limit_end
        raw_flags = item.get("flags") or []
        flags = tuple(f for f in KNOWN_FLAGS if isinstance(raw_flags, list) and f in
                      {str(x).strip().lower() for x in raw_flags})
        cleaned.append(Segment(round(start, 3), round(end, 3), round(score, 3),
                               str(item.get("title") or "").strip()[:120],
                               str(item.get("reason") or "").strip()[:500], flags))

    cleaned.sort(key=lambda s: s.score, reverse=True)
    kept: list[Segment] = []
    for seg in cleaned:
        if any(seg.start < other.end and other.start < seg.end for other in kept):
            continue
        kept.append(seg)
        if len(kept) >= limit:
            break
    return kept


# --------------------------------------------------------------------------- game filter

_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
          "ix": "9", "x": "10", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14",
          "xv": "15", "xvi": "16"}


def _forms(name: str) -> set[str]:
    """Spellings a game name can be matched by: joined words and its acronym."""
    tokens = [_ROMAN.get(t, t) for t in re.findall(r"[a-z0-9]+", name.lower())]
    if not tokens:
        return set()
    joined = "".join(tokens)
    forms = {joined}
    if len(tokens) > 1:
        acronym = "".join(t if t.isdigit() else t[0] for t in tokens)
        if len(acronym) >= 3:
            forms.add(acronym)
    return forms


def game_matches(game: str, text: str) -> bool:
    """Does ``text`` (a chapter name or stream title) refer to ``game``?

    "GTA 6" matches "Grand Theft Auto VI" and "PLAYING GTA 6 RIGHT NOW".
    """
    wanted = _forms(game)
    if not wanted:
        return False
    tokens = [_ROMAN.get(t, t) for t in re.findall(r"[a-z0-9]+", text.lower())]
    joined = "".join(tokens)
    if any(len(form) >= 3 and form in joined for form in wanted):
        return True
    # Acronym of any run of words in the text, e.g. "grand theft auto 6" -> "gta6".
    for size in range(2, min(len(tokens), 8) + 1):
        for i in range(len(tokens) - size + 1):
            run = tokens[i:i + size]
            if "".join(t if t.isdigit() else t[0] for t in run) in wanted:
                return True
    return False


def parse_games(text: str | None) -> list[str]:
    """Split the form's comma or newline separated game list."""
    return [part.strip() for part in re.split(r"[,\n]", text or "") if part.strip()]


def game_ranges(games: list[str], chapters: list[dict], title: str,
                duration: float) -> list[tuple[float, float]] | None:
    """Which parts of a VOD to process for a streamer's game filter.

    Returns the whole VOD when no filter is set, the matching chapter ranges
    when the platform marks game changes, and the whole VOD when there are no
    chapters but the title matches. Returns None when nothing matches, which
    means the VOD is skipped.
    """
    whole = [(0.0, float(duration))]
    if not games:
        return whole
    if chapters:
        hits = [(float(c["start"]), float(c["end"])) for c in chapters
                if any(game_matches(g, c.get("title", "")) for g in games)]
        if not hits:
            return None
        merged: list[tuple[float, float]] = []
        for lo, hi in sorted(hits):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        return merged
    if any(game_matches(g, title) for g in games):
        return whole
    return None
