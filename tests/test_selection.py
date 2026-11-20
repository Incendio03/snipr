import numpy as np
import pytest

from snipr import selection


def _request(mode="highlights", **kw):
    rng = np.random.default_rng(3)
    chat = 2.0 + rng.normal(0, 0.5, 1800)
    chat[900:930] = 30.0
    loud = -30.0 + rng.normal(0, 1.0, 1800)
    sentences = [{"s": float(t), "e": float(t + 9), "t": f"sentence at {t}", "w": []}
                 for t in range(0, 1800, 10)]
    args = dict(mode=mode, instructions="", max_clips=3, clip_min_s=20, clip_max_s=60, min_score=0.5,
                duration=1800.0, span_start=0.0, ranges=[(0.0, 1800.0)],
                transcript={"segments": sentences}, loudness=loud, chat_rate=chat)
    args.update(kw)
    return selection.SelectionRequest(**args)


def test_highlights_without_llm_picks_the_spike_and_cuts_on_sentences():
    picks = selection.select(_request(), None)
    assert len(picks) == 1
    clip = picks[0]
    assert clip.start <= 900 and clip.end >= 929
    assert clip.start % 10 == 0                       # snapped to a sentence start
    assert 20 <= clip.end - clip.start <= 60
    assert "chat ran" in clip.reason


def test_highlights_stays_inside_the_allowed_ranges():
    assert selection.select(_request(ranges=[(0.0, 600.0)]), None) == []


def test_highlights_hands_nominations_to_the_llm():
    class Recorder:
        def select_segments(self, req, nominated):
            self.nominated = nominated
            return [{"start": 895.0, "end": 935.0, "score": 0.9, "title": "t", "reason": "r", "flags": []}]

    llm = Recorder()
    picks = selection.select(_request(), llm)
    assert llm.nominated and any(w.start <= 900 <= w.end for w in llm.nominated)
    assert [(p.start, p.end) for p in picks] == [(895.0, 935.0)]


def test_informative_and_custom_need_the_llm():
    with pytest.raises(selection.NeedsLLM):
        selection.select(_request("informative"), None)
    with pytest.raises(selection.NeedsLLM):
        selection.select(_request("custom", instructions="anything funny"), None)


def test_custom_needs_instructions_and_unknown_modes_are_rejected():
    class Never:
        def select_segments(self, req, nominated):
            raise AssertionError("should not be called")

    with pytest.raises(ValueError, match="instructions"):
        selection.select(_request("custom"), Never())
    with pytest.raises(ValueError, match="Unknown"):
        selection.select(_request("sideways"), Never())


def test_per_minute_sums_chat_and_averages_loudness():
    chat = np.concatenate([np.full(60, 2.0), np.full(60, 5.0)])
    assert selection.per_minute(chat, 120.0, "chat") == [(2, 120.0), (3, 300.0)]
    assert selection.per_minute(np.full(90, -20.0), 0.0, "loudness") == [(0, -20.0), (1, -20.0)]
    assert selection.per_minute(None, 0.0, "chat") == []
