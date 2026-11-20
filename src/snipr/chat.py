"""Chat replay for finished Twitch VODs, reduced to messages per second.

Twitch's own web player reads chat replay from its GraphQL endpoint. Paging by
cursor is refused for anonymous clients, so this walks the VOD by time offset
instead: ask for the comments at second T, take the last comment's offset as
the next T. The VOD is split into slices that are walked in parallel.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

import httpx
import numpy as np

_GQL = "https://gql.twitch.tv/gql"
_CLIENT_ID = "kd1unb4b3q4t58fwlpcbzcbnm76a8fp"  # the public web player client
_QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


class ChatError(Exception):
    pass


def _page(client: httpx.Client, video_id: str, offset: int) -> list[tuple[str, int]]:
    body = [{
        "operationName": "VideoCommentsByOffsetOrCursor",
        "variables": {"videoID": video_id, "contentOffsetSeconds": offset},
        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": _QUERY_HASH}},
    }]
    last_error: Exception | None = None
    for _ in range(4):
        try:
            response = client.post(_GQL, json=body)
            response.raise_for_status()
            video = response.json()[0]["data"]["video"]
            if video is None or video.get("comments") is None:
                return []
            return [(edge["node"]["id"], int(edge["node"]["contentOffsetSeconds"]))
                    for edge in video["comments"]["edges"]]
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            last_error = exc
    raise ChatError(f"Twitch chat replay request failed at {offset}s: {last_error!r}")


def _walk(video_id: str, start: int, end: int, counts: np.ndarray, base: int) -> int:
    """Count comments in [start, end) into ``counts``. Returns requests made."""
    requests = 0
    seen: set[str] = set()
    offset = start
    with httpx.Client(headers={"Client-ID": _CLIENT_ID}, timeout=30) as client:
        while offset < end:
            page = _page(client, video_id, offset)
            requests += 1
            if not page:
                break
            for comment_id, at in page:
                if comment_id in seen or at < start or at >= end:
                    continue
                seen.add(comment_id)
                counts[at - base] += 1
            last = page[-1][1]
            # A second with more comments than one page holds would repeat forever.
            offset = last if last > offset else offset + 1
            if len(seen) > 4000:
                seen = set(list(seen)[-1000:])
    return requests


def twitch_chat_rate(video_id: str, duration: float, start: float = 0.0, end: float | None = None,
                     workers: int = 12, progress: Callable[[float], None] | None = None) -> np.ndarray:
    """Messages per second for ``start``..``end`` of a Twitch VOD.

    Index 0 of the result is second ``start`` of the VOD.
    """
    lo = int(start)
    hi = int(min(duration, end) if end else duration)
    if hi <= lo:
        return np.zeros(0, dtype=np.float32)
    counts = np.zeros(hi - lo, dtype=np.float32)
    slice_len = max(120, (hi - lo) // max(1, workers * 4))
    slices = [(s, min(hi, s + slice_len)) for s in range(lo, hi, slice_len)]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(lambda sl: _walk(video_id, sl[0], sl[1], counts, lo), slices):
            done += 1
            if progress:
                progress(done / len(slices))
    return counts
