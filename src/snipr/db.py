"""SQLite storage. One short-lived connection per operation, WAL mode.

The web server, the worker and the scheduler run on different threads, so
nothing here keeps a connection open between calls.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    channel_url TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'en',
    mode TEXT NOT NULL DEFAULT 'highlights',
    instructions TEXT NOT NULL DEFAULT '',
    games TEXT NOT NULL DEFAULT '',
    max_clips_per_vod INTEGER NOT NULL DEFAULT 5,
    clip_min_s REAL NOT NULL DEFAULT 20,
    clip_max_s REAL NOT NULL DEFAULT 60,
    layout TEXT NOT NULL DEFAULT 'fill',
    crop_rect TEXT,
    facecam_rect TEXT,
    gameplay_rect TEXT,
    caption_style TEXT NOT NULL DEFAULT 'box',
    show_headline INTEGER NOT NULL DEFAULT 1,
    default_destination_id INTEGER,
    enabled INTEGER NOT NULL DEFAULT 1,
    sample_frame TEXT,
    sample_w INTEGER,
    sample_h INTEGER,
    created_at TEXT NOT NULL,
    last_checked_at TEXT
);

CREATE TABLE IF NOT EXISTS destinations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_handle TEXT NOT NULL DEFAULT '',
    post_method TEXT NOT NULL DEFAULT 'export',
    token_ref TEXT,
    post_slots TEXT NOT NULL DEFAULT '["12:00", "18:00"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vods (
    id INTEGER PRIMARY KEY,
    streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
    platform TEXT NOT NULL,
    vod_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    duration REAL NOT NULL DEFAULT 0,
    published_at TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT,
    chapters TEXT NOT NULL DEFAULT '[]',
    ranges TEXT,
    span_start REAL NOT NULL DEFAULT 0,
    audio_path TEXT,
    stats TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    llm_requests INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (streamer_id, platform, vod_key)
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    vod_id INTEGER NOT NULL REFERENCES vods(id) ON DELETE CASCADE,
    start_s REAL NOT NULL,
    end_s REAL NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    flags TEXT NOT NULL DEFAULT '[]',
    layout TEXT NOT NULL DEFAULT '{}',
    file_path TEXT,
    source_path TEXT,
    source_start REAL,
    source_end REAL,
    headline TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    credit TEXT NOT NULL DEFAULT '',
    hashtags TEXT NOT NULL DEFAULT '[]',
    working_title TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',
    render_state TEXT NOT NULL DEFAULT 'rendering',
    render_error TEXT,
    reject_reason TEXT,
    destination_ids TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    destination_id INTEGER NOT NULL REFERENCES destinations(id) ON DELETE CASCADE,
    method TEXT NOT NULL DEFAULT 'export',
    status TEXT NOT NULL DEFAULT 'scheduled',
    scheduled_at TEXT NOT NULL,
    posted_at TEXT,
    post_url TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT '',
    progress REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    force INTEGER NOT NULL DEFAULT 0,
    run_after TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    vod_id INTEGER,
    clip_id INTEGER
);

CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS posts_status ON posts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""

JSON_COLUMNS = {"crop_rect", "facecam_rect", "gameplay_rect", "post_slots", "chapters", "ranges", "stats",
                "flags", "layout", "hashtags", "destination_ids"}

_path_override: Path | None = None


def use_path(path: Path | None) -> None:
    """Point the module at another database file (tests)."""
    global _path_override
    _path_override = path


def db_path() -> Path:
    return _path_override or config.data_dir() / "snipr.db"


def now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release. An existing database gets them on start.
ADDED_COLUMNS = [
    ("streamers", "caption_style", "TEXT NOT NULL DEFAULT 'box'"),
    ("streamers", "show_headline", "INTEGER NOT NULL DEFAULT 1"),
]


def init() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        for table, column, ddl in ADDED_COLUMNS:
            present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _decode(row: sqlite3.Row) -> dict:
    # "layout" is JSON on clips but a plain word on streamers, so only values
    # that are JSON objects or arrays are decoded.
    out = dict(row)
    for key in out.keys() & JSON_COLUMNS:
        if isinstance(out[key], str) and out[key][:1] in "[{":
            out[key] = json.loads(out[key])
    return out


def _encode(values: dict) -> dict:
    return {k: (json.dumps(v) if k in JSON_COLUMNS and isinstance(v, (dict, list)) else
                int(v) if isinstance(v, bool) else v) for k, v in values.items()}


def rows(sql: str, params: tuple | dict = ()) -> list[dict]:
    with connect() as conn:
        return [_decode(r) for r in conn.execute(sql, params).fetchall()]


def row(sql: str, params: tuple | dict = ()) -> dict | None:
    found = rows(sql, params)
    return found[0] if found else None


def scalar(sql: str, params: tuple | dict = ()) -> Any:
    with connect() as conn:
        found = conn.execute(sql, params).fetchone()
        return found[0] if found else None


def execute(sql: str, params: tuple | dict = ()) -> int:
    """Run one statement. Returns the new row id for an INSERT, else the rows changed."""
    with connect() as conn:
        cursor = conn.execute(sql, params)
        return cursor.lastrowid if sql.lstrip().upper().startswith("INSERT") else cursor.rowcount


def insert(table: str, values: dict) -> int:
    values = _encode(values)
    columns = ", ".join(values)
    marks = ", ".join(f":{k}" for k in values)
    return execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", values)


def update(table: str, row_id: int, values: dict) -> None:
    if not values:
        return
    values = _encode(values)
    sets = ", ".join(f"{k} = :{k}" for k in values)
    execute(f"UPDATE {table} SET {sets} WHERE id = :_id", {**values, "_id": row_id})


def get(table: str, row_id: int) -> dict | None:
    return row(f"SELECT * FROM {table} WHERE id = ?", (row_id,))


def event(message: str, level: str = "info", vod_id: int | None = None, clip_id: int | None = None) -> None:
    insert("events", {"at": now(), "level": level, "message": message, "vod_id": vod_id, "clip_id": clip_id})
