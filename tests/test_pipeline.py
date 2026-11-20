import json

import numpy as np

from snipr import pipeline, transcribe, worker
from snipr.platforms import VodInfo


def test_merge_ranges_joins_overlaps_and_near_neighbours():
    assert pipeline.merge_ranges([(50, 80), (0, 30), (25, 40)]) == [(0, 40), (50, 80)]
    assert pipeline.merge_ranges([(0, 30), (35, 50)], gap=10) == [(0, 50)]
    assert pipeline.merge_ranges([]) == []


def test_subtract_ranges_returns_only_what_is_missing():
    assert pipeline.subtract_ranges([(0, 100)], []) == [(0, 100)]
    assert pipeline.subtract_ranges([(0, 100)], [(0, 100)]) == []
    assert pipeline.subtract_ranges([(0, 100)], [(20, 40), (60, 70)]) == [(0, 20), (40, 60), (70, 100)]
    assert pipeline.subtract_ranges([(10, 30), (50, 90)], [(0, 60)]) == [(60, 90)]
    assert pipeline.subtract_ranges([(0, 100)], [(1, 99)]) == []          # slivers are not worth a model run


def _fake_vod(tmp_path, monkeypatch, duration=7200.0):
    """A stream whose audio and loudness are already 'on disk', and a transcriber that records its calls."""
    monkeypatch.setenv("SNIPR_DATA", str(tmp_path))
    vod = VodInfo("youtube", "abc", "https://www.youtube.com/watch?v=abc", "A stream", duration)
    work = pipeline.vod_dir(vod)
    work.mkdir(parents=True)
    (work / "audio.webm").write_bytes(b"x")
    np.save(work / "loudness.npy", np.full(int(duration), -30.0, dtype=np.float32))
    (work / "analysis.json").write_text(json.dumps({"v": 4, "audio": str(work / "audio.webm"),
                                                    "loudness": True, "chat": "none"}))
    calls = []

    def fake_transcribe(path, model, compute, batch, ranges=None, progress=None, **kw):
        (lo, hi), = ranges
        calls.append((lo, hi))
        stats = transcribe.TranscribeStats(hi - lo, (hi - lo) / 30, "cuda", model, compute)
        return {"segments": [{"s": lo + 1, "e": lo + 2, "t": f"speech at {lo:.0f}", "w": []}]}, stats

    monkeypatch.setattr(pipeline.transcribe, "transcribe", fake_transcribe)
    return vod, work, calls


def test_only_missing_speech_is_transcribed_and_nothing_is_lost(tmp_path, monkeypatch):
    vod, work, calls = _fake_vod(tmp_path, monkeypatch)
    first = pipeline.analyze(vod, speech_ranges=[(100, 300), (1000, 1200)])
    assert calls == [(100, 300), (1000, 1200)]
    assert [s["t"] for s in first.transcript["segments"]] == ["speech at 100", "speech at 1000"]

    calls.clear()
    again = pipeline.analyze(vod, speech_ranges=[(100, 300)])               # already covered: no model run
    assert calls == [] and len(again.transcript["segments"]) == 2

    second = pipeline.analyze(vod, speech_ranges=[(250, 500), (5000, 5100)])
    assert calls == [(300, 500), (5000, 5100)]                              # only the new parts
    texts = [s["t"] for s in second.transcript["segments"]]
    assert texts == ["speech at 100", "speech at 300", "speech at 1000", "speech at 5000"]   # earlier work kept, in order
    state = json.loads((work / "analysis.json").read_text())
    assert state["transcript_ranges"] == [[100, 500], [1000, 1200], [5000, 5100]]


def test_long_transcription_is_saved_in_half_hour_steps(tmp_path, monkeypatch):
    vod, work, calls = _fake_vod(tmp_path, monkeypatch)
    saved_after = []
    real_save = transcribe.save

    def counting_save(transcript, path):
        saved_after.append(len(transcript["segments"]))
        real_save(transcript, path)

    monkeypatch.setattr(pipeline.transcribe, "save", counting_save)
    result = pipeline.analyze(vod)                                          # the whole two hours
    assert calls == [(0, 1800), (1800, 3600), (3600, 5400), (5400, 7200)]
    assert saved_after == [1, 2, 3, 4]                                      # written to disk after every step
    assert result.stats["audio_seconds"] == 7200.0 and result.stats["speed"] == 30.0

    # A run that stopped half way picks up where the saved file ends.
    state = json.loads((work / "analysis.json").read_text())
    state["transcript_ranges"] = [[0, 3600]]
    (work / "analysis.json").write_text(json.dumps(state))
    calls.clear()
    pipeline.analyze(vod)
    assert calls == [(3600, 5400), (5400, 7200)]


def test_signals_only_run_skips_transcription(tmp_path, monkeypatch):
    vod, _, calls = _fake_vod(tmp_path, monkeypatch)
    result = pipeline.analyze(vod, transcribe_speech=False)
    assert calls == [] and result.transcript == {"segments": []} and len(result.loudness) == 7200


def test_reading_modes_stop_at_once_without_an_ai_key(client, seeded, monkeypatch):
    from snipr import db, platforms

    db.update("streamers", seeded["streamer"], {"mode": "informative"})
    monkeypatch.setattr(platforms, "vod_info", lambda url: VodInfo("twitch", "111", url, "GTA 6 day 2", 9000.0))
    monkeypatch.setattr(pipeline, "analyze", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not analyse")))
    job_id = worker.enqueue("process_vod", seeded["vod"], "pixelmoth: GTA 6 day 2", force=True)
    worker.run_job(db.get("jobs", job_id), __import__("snipr.config", fromlist=["x"]).load_settings())
    job = db.get("jobs", job_id)
    assert job["status"] == "failed" and job["attempts"] == 1                # not retried
    assert "needs an AI" in job["error"] and "Gemini" in job["error"]
