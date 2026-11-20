"""Paths and global settings.

Settings live in ``data/settings.yaml``. Anything missing there falls back to
``DEFAULTS``, which mirrors section 13 of ``docs/design.md``.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "transcription": {
        "model": "medium",
        "compute_type": "int8_float16",
        "batch_size": 8,
    },
    "llm": {
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
    },
    "selection": {
        "min_score": 0.6,
        "highlight_nominations": 15,
        "window_s": 30,
        "step_s": 10,
        "chat_weight": 0.6,
        "audio_weight": 0.4,
    },
    "processing": {
        "window_start": "01:00",
        "window_end": "08:00",
        "max_retries": 3,
        "poll_minutes": 15,
    },
    "ingest": {
        "video_height": 1080,
        "source_pad_s": 20,
    },
    "publishing": {
        "require_approval": True,
        "default_post_slots": ["12:00", "18:00"],
        "append_streamer_credit": True,
    },
    "storage": {
        "source_max_age_days": 5,
        "keep_rejected_clips_days": 7,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
    },
}


def data_dir() -> Path:
    """Where the database, downloads and rendered clips live.

    ``SNIPR_DATA`` overrides it. The default is ``data/`` next to the source
    tree, which keeps large files on the same drive as the project.
    """
    override = os.environ.get("SNIPR_DATA")
    root = Path(override) if override else Path(__file__).resolve().parents[2] / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def subdir(name: str) -> Path:
    path = data_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def settings_path() -> Path:
    return data_dir() / "settings.yaml"


def load_settings() -> dict[str, Any]:
    merged = copy.deepcopy(DEFAULTS)
    path = settings_path()
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            _merge(merged, loaded)
    return merged


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    """Merge ``changes`` into the saved settings file and return the result."""
    path = settings_path()
    current: dict[str, Any] = {}
    if path.exists():
        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    _merge(current, changes)
    path.write_text(yaml.safe_dump(current, sort_keys=False), encoding="utf-8")
    return load_settings()
