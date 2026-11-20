from datetime import datetime, time

import pytest

from snipr import db, publish


def test_slots_are_validated_sorted_and_deduplicated():
    assert publish.clean_slots(["18:00", "9:05", "18:00"]) == ["09:05", "18:00"]
    for bad in ([], None, ["25:00"], ["noon"], ["12:60"]):
        with pytest.raises(publish.SlotError):
            publish.clean_slots(bad)


def test_next_free_slot_takes_the_first_open_slot_after_now():
    slots = ["12:00", "18:00"]
    morning = datetime(2026, 11, 21, 9, 0)
    assert publish.next_free_slot(slots, set(), morning) == datetime(2026, 11, 21, 12, 0)
    assert publish.next_free_slot(slots, {datetime(2026, 11, 21, 12, 0)}, morning) == datetime(2026, 11, 21, 18, 0)
    taken = {datetime(2026, 11, 21, 12, 0), datetime(2026, 11, 21, 18, 0)}
    assert publish.next_free_slot(slots, taken, morning) == datetime(2026, 11, 22, 12, 0)


def test_a_slot_at_exactly_now_or_earlier_is_not_reused():
    noon = datetime(2026, 11, 21, 12, 0)
    assert publish.next_free_slot(["12:00"], set(), noon) == datetime(2026, 11, 22, 12, 0)
    assert publish.next_free_slot(["12:00"], set(), datetime(2026, 11, 21, 23, 59)) == datetime(2026, 11, 22, 12, 0)


def test_processing_window_handles_midnight():
    assert publish.window_open(time(3, 0), "01:00", "08:00")
    assert not publish.window_open(time(8, 0), "01:00", "08:00")
    assert not publish.window_open(time(0, 30), "01:00", "08:00")
    assert publish.window_open(time(23, 30), "22:00", "06:00")       # crosses midnight
    assert publish.window_open(time(2, 0), "22:00", "06:00")
    assert not publish.window_open(time(12, 0), "22:00", "06:00")
    assert publish.window_open(time(12, 0), "00:00", "00:00")        # same start and end: always open


def _destination(name="Funny clips", slots=("12:00", "18:00")):
    return db.insert("destinations", {"name": name, "platform": "youtube", "post_slots": list(slots),
                                      "created_at": db.now()})


def test_each_clip_gets_its_own_slot_per_destination(seeded):
    dest = _destination()
    now = datetime(2026, 11, 21, 9, 0)
    first = publish.schedule(seeded["clips"][0], [dest], False, now)
    second = publish.schedule(seeded["clips"][1], [dest], False, now)
    times = [db.get("posts", p)["scheduled_at"] for p in first + second]
    assert times == ["2026-11-21T12:00:00", "2026-11-21T18:00:00"]
    assert all(db.get("posts", p)["status"] == "scheduled" for p in first + second)


def test_scheduling_the_same_clip_twice_does_not_duplicate_the_post(seeded):
    dest = _destination()
    now = datetime(2026, 11, 21, 9, 0)
    assert publish.schedule(seeded["clips"][0], [dest], False, now) == publish.schedule(seeded["clips"][0], [dest], False, now)
    assert db.scalar("SELECT COUNT(*) FROM posts") == 1


def test_post_now_is_due_immediately_and_slots_release_on_time(seeded):
    dest = _destination()
    now = datetime(2026, 11, 21, 9, 0)
    (urgent,) = publish.schedule(seeded["clips"][0], [dest], True, now)
    (later,) = publish.schedule(seeded["clips"][1], [dest], False, now)
    assert db.get("posts", urgent)["status"] == "due"
    assert db.get("posts", later)["status"] == "scheduled"
    assert publish.release_due(datetime(2026, 11, 21, 11, 59)) == 0
    assert publish.release_due(datetime(2026, 11, 21, 12, 0)) == 1
    assert db.get("posts", later)["status"] == "due"
    assert db.scalar("SELECT COUNT(*) FROM events WHERE message LIKE 'Time to post%'") == 2
