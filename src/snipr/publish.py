"""Posting slots, the processing window, and moving posts through their states.

A destination has posting slots (times of day). An approved clip takes the
destination's next free slot, one clip per slot. When the slot arrives, a post
on an Export destination becomes due and the user uploads it by hand.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from . import db, notify

_SLOT = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
OPEN_STATES = ("scheduled", "due", "posting", "failed")


class SlotError(ValueError):
    pass


def parse_time(text: str) -> time:
    match = _SLOT.match(str(text).strip())
    if not match:
        raise SlotError(f"'{text}' is not a time. Use 24-hour HH:MM, for example 18:00.")
    return time(int(match.group(1)), int(match.group(2)))


def clean_slots(slots) -> list[str]:
    """Validate, de-duplicate and sort a destination's posting slots."""
    if not isinstance(slots, list) or not slots:
        raise SlotError("A destination needs at least one posting slot.")
    return [t.strftime("%H:%M") for t in sorted({parse_time(s) for s in slots})]


def next_free_slot(slots: list[str], taken: set[datetime], now: datetime) -> datetime:
    """The first slot after ``now`` that no other post of the destination holds."""
    times = [parse_time(s) for s in clean_slots(slots)]
    day = now.date()
    for _ in range(366):
        for slot in times:
            candidate = datetime.combine(day, slot)
            if candidate > now and candidate not in taken:
                return candidate
        day += timedelta(days=1)
    raise SlotError("No free posting slot in the next year.")


def window_open(now: time, start: str, end: str) -> bool:
    """Is ``now`` inside the processing window? The window may cross midnight."""
    lo, hi = parse_time(start), parse_time(end)
    if lo == hi:
        return True                      # same start and end means always open
    if lo < hi:
        return lo <= now < hi
    return now >= lo or now < hi


def taken_slots(destination_id: int) -> set[datetime]:
    found = db.rows("SELECT scheduled_at FROM posts WHERE destination_id = ? AND status IN ('scheduled', 'due', 'posting')",
                    (destination_id,))
    return {datetime.fromisoformat(r["scheduled_at"]) for r in found}


def schedule(clip_id: int, destination_ids: list[int], post_now: bool, now: datetime | None = None) -> list[int]:
    """Create one post per destination for an approved clip. Returns the post ids."""
    now = now or datetime.now().replace(microsecond=0)
    created = []
    for dest_id in destination_ids:
        dest = db.get("destinations", dest_id)
        if dest is None:
            raise SlotError("One of the chosen destinations no longer exists.")
        existing = db.row("SELECT id FROM posts WHERE clip_id = ? AND destination_id = ? AND status != 'posted'",
                          (clip_id, dest_id))
        if existing:
            created.append(existing["id"])
            continue
        when = now if post_now else next_free_slot(dest["post_slots"], taken_slots(dest_id), now)
        created.append(db.insert("posts", {
            "clip_id": clip_id, "destination_id": dest_id, "method": dest["post_method"],
            "status": "scheduled", "scheduled_at": when.isoformat(), "created_at": db.now()}))
    release_due(now)
    return created


def release_due(now: datetime | None = None) -> int:
    """Move scheduled posts whose slot has arrived to ``due`` and remind the user."""
    now = now or datetime.now()
    due = db.rows(
        "SELECT p.id, p.method, d.name AS destination, d.platform, c.headline, c.title "
        "FROM posts p JOIN destinations d ON d.id = p.destination_id JOIN clips c ON c.id = p.clip_id "
        "WHERE p.status = 'scheduled' AND p.scheduled_at <= ?", (now.replace(microsecond=0).isoformat(),))
    for post in due:
        # Automatic upload is not built for any platform yet, so every post is handed to the user.
        db.update("posts", post["id"], {"status": "due"})
        label = post["headline"] or post["title"] or "A clip"
        db.event(f"Time to post \"{label}\" to {post['destination']} ({post['platform'].title()}).")
        notify.toast("Snipr: a clip is due",
                     f"{label} is ready to post to {post['destination']}.", "#/publishing")
    return len(due)
