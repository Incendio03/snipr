import pytest


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """An empty Snipr data folder with a fresh database, no background threads, no toasts."""
    monkeypatch.setenv("SNIPR_DATA", str(tmp_path))
    monkeypatch.setenv("SNIPR_NO_BACKGROUND", "1")
    monkeypatch.setenv("SNIPR_NO_NOTIFY", "1")
    from snipr import db, llm, worker

    monkeypatch.setattr(worker, "is_online", lambda: True)        # tests never depend on the real connection
    monkeypatch.setattr(llm.secrets, "get", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    db.init()
    return tmp_path


@pytest.fixture
def client(data_dir):
    from fastapi.testclient import TestClient

    from snipr.web.app import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded(data_dir):
    """One streamer, one processed stream and two rendered clips waiting for review."""
    from snipr import config, db

    streamer = db.insert("streamers", {"name": "pixelmoth", "platform": "twitch",
                                       "channel_url": "https://www.twitch.tv/pixelmoth", "created_at": db.now()})
    vod = db.insert("vods", {"streamer_id": streamer, "platform": "twitch", "vod_key": "111",
                             "source_url": "https://www.twitch.tv/videos/111", "title": "GTA 6 day 2",
                             "duration": 9000.0, "status": "done", "created_at": db.now(), "updated_at": db.now()})
    clips = []
    for start, score in ((8048.0, 0.82), (3000.0, 0.7)):
        clip = db.insert("clips", {
            "vod_id": vod, "start_s": start, "end_s": start + 41.0, "score": score, "reason": "Chat spike.",
            "flags": ["music"], "layout": {"type": "fill", "crop": None}, "headline": "Getting 5 stars in GTA 6",
            "title": "Five stars", "description": "The wanted level climbs.", "credit": "Credit: pixelmoth",
            "hashtags": ["gta6", "gaming"], "status": "pending_review", "render_state": "ready",
            "source_start": start - 20, "source_end": start + 61, "created_at": db.now()})
        path = config.subdir("clips") / f"{clip}.mp4"
        path.write_bytes(b"not really a video")
        db.update("clips", clip, {"file_path": str(path)})
        clips.append(clip)
    return {"streamer": streamer, "vod": vod, "clips": clips}
