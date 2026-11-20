import pytest

from snipr import platforms
from snipr.platforms import Channel, PlatformError, VodInfo


def test_channel_links_are_recognised():
    assert platforms.parse_channel("twitch.tv/IShowSpeed") == Channel("twitch", "ishowspeed", "https://www.twitch.tv/ishowspeed")
    assert platforms.parse_channel("https://www.youtube.com/@IShowSpeed/streams").url == "https://www.youtube.com/@IShowSpeed"
    assert platforms.parse_channel("https://kick.com/Someone").platform == "kick"
    for bad in ("hello", "https://www.twitch.tv/videos/123", "https://example.com/@name"):
        with pytest.raises(PlatformError):
            platforms.parse_channel(bad)


def test_stream_links_are_recognised():
    assert platforms.parse_vod_url("https://www.twitch.tv/videos/2921253522") == ("twitch", "2921253522")
    assert platforms.parse_vod_url("https://www.youtube.com/watch?v=qSyo5d-dVzA") == ("youtube", "qSyo5d-dVzA")
    assert platforms.parse_vod_url("https://youtu.be/qSyo5d-dVzA") == ("youtube", "qSyo5d-dVzA")
    with pytest.raises(PlatformError):
        platforms.parse_vod_url("https://www.twitch.tv/ishowspeed")


@pytest.fixture
def fake_channel(monkeypatch, tmp_path):
    """A Twitch channel with three finished streams; records which part a frame is taken from."""
    streams = {
        "1": VodInfo("twitch", "1", "https://www.twitch.tv/videos/1", "Fortnite night", 9000.0,
                     chapters=[{"start": 0, "end": 9000, "title": "Fortnite"}]),
        "2": VodInfo("twitch", "2", "https://www.twitch.tv/videos/2", "Variety", 12000.0,
                     chapters=[{"start": 0, "end": 3000, "title": "Just Chatting"},
                               {"start": 3000, "end": 12000, "title": "Grand Theft Auto VI"}]),
        "3": VodInfo("twitch", "3", "https://www.twitch.tv/videos/3", "Older", 5000.0, chapters=[]),
    }
    taken = {}

    def fake_download(url, out_path, start, end, height=1080, progress=None):
        taken.update(url=url, start=start)
        section = out_path.with_suffix(".mp4")
        section.write_bytes(b"x")
        return section

    monkeypatch.setattr(platforms, "list_vods", lambda channel, limit=10: list(streams.values()))
    monkeypatch.setattr(platforms, "vod_info", lambda url: streams[url.rsplit("/", 1)[-1]])
    monkeypatch.setattr(platforms, "download_video", fake_download)
    monkeypatch.setattr(platforms, "probe_video", lambda path: {"width": 1920, "height": 1080, "fps": 60, "duration": 2})

    def fake_ffmpeg(args, **kwargs):
        if taken.get("unreadable") != taken["url"]:
            (tmp_path / "frame.jpg").write_bytes(b"jpeg")       # ffmpeg writes the frame

    monkeypatch.setattr(platforms.subprocess, "run", fake_ffmpeg)
    taken["streams"] = streams
    return Channel("twitch", "someone", "https://www.twitch.tv/someone"), tmp_path / "frame.jpg", taken


def test_sample_frame_without_a_filter_uses_the_latest_stream(fake_channel):
    channel, out, taken = fake_channel
    info = platforms.sample_frame(channel, out)
    assert taken["url"].endswith("/1") and info["matched"] is None and info["vod_title"] == "Fortnite night"


def test_sample_frame_comes_from_inside_the_matching_game(fake_channel):
    channel, out, taken = fake_channel
    info = platforms.sample_frame(channel, out, ["GTA 6"])
    assert taken["url"].endswith("/2") and info["matched"] is True and info["vod_title"] == "Variety"
    assert 3000 < taken["start"] < 12000                 # inside the GTA part, not the chatting before it


def test_sample_frame_falls_back_and_says_so_when_no_stream_matches(fake_channel):
    channel, out, taken = fake_channel
    info = platforms.sample_frame(channel, out, ["Minecraft"])
    assert taken["url"].endswith("/1") and info["matched"] is False and info["looked_at"] == 3


def test_sample_frame_skips_a_stream_the_platform_is_still_processing(fake_channel):
    channel, out, taken = fake_channel
    taken["streams"]["1"].live_status = "post_live"          # just ended, not ready to read yet
    info = platforms.sample_frame(channel, out)
    assert taken["url"].endswith("/2") and info["vod_title"] == "Variety"


def test_sample_frame_moves_on_when_a_stream_gives_no_readable_frame(fake_channel):
    channel, out, taken = fake_channel
    taken["unreadable"] = "https://www.twitch.tv/videos/1"
    info = platforms.sample_frame(channel, out)
    assert taken["url"].endswith("/2") and info["vod_title"] == "Variety"


def test_sample_frame_needs_a_finished_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "list_vods", lambda channel, limit=10: [])
    with pytest.raises(PlatformError, match="no finished stream"):
        platforms.sample_frame(Channel("twitch", "x", "https://www.twitch.tv/xx"), tmp_path / "f.jpg")
