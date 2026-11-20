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
    clip = picks[0]                                   # best first
    assert clip.start <= 900 and clip.end >= 929
    assert clip.start % 10 == 0                       # snapped to a sentence start
    assert 20 <= clip.end - clip.start <= 60
    assert "chat ran" in clip.reason
    assert all(p.score >= selection.SIGNAL_ONLY_MIN_SCORE for p in picks)


def test_without_llm_a_modest_spike_still_clears_the_bar_set_for_the_llm():
    # In a busy chat a real spike is only a few deviations above baseline, which
    # scores under the 0.6 bar meant for the LLM's own scores.
    rng = np.random.default_rng(5)
    chat = 20.0 + rng.normal(0, 4.0, 1800)
    chat[900:930] += 3.6
    loud = np.full(1800, -30.0)                       # flat sound: chat alone carries the score
    picks = selection.select(_request(chat_rate=chat, loudness=loud, min_score=0.6), None)
    spike = [p for p in picks if p.start <= 915 <= p.end]
    assert spike and selection.SIGNAL_ONLY_MIN_SCORE <= spike[0].score < 0.6


def test_highlights_stays_inside_the_allowed_ranges():
    picks = selection.select(_request(ranges=[(0.0, 600.0)]), None)
    assert all(0 <= p.start and p.end <= 601 for p in picks)       # the spike at 900 is out of bounds


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


def _talk(start, seconds, words_per_second):
    """Sentences of evenly spaced words, with word timings, like a real transcript."""
    words = [[start + i / words_per_second, start + (i + 0.8) / words_per_second, f"w{i}"]
             for i in range(int(seconds * words_per_second))]
    return [{"s": chunk[0][0], "e": chunk[-1][1], "t": " ".join(w[2] for w in chunk), "w": chunk}
            for chunk in (words[i:i + 12] for i in range(0, len(words), 12))]


def test_words_per_second_counts_what_is_spoken_inside_a_stretch():
    transcript = {"segments": _talk(100, 40, 2.5) + [{"s": 500.0, "e": 502.0, "t": "are you a bot", "w": []}]}
    assert 2.3 < selection.words_per_second(transcript, 100, 140) < 2.6
    assert selection.words_per_second(transcript, 300, 340) == 0.0
    assert selection.words_per_second(transcript, 495, 530) == 4 / 35          # untimed sentence counted by its words
    assert selection.words_per_second(transcript, 10, 10) == 0.0


def test_a_pick_with_almost_no_talking_is_replaced_by_the_next_best():
    # The model's top pick points at 35 near-silent seconds; its second pick is a real explanation.
    transcript = {"segments": _talk(1000, 45, 2.5) + [{"s": 300.0, "e": 302.0, "t": "bro are you a bot", "w": []}]}

    class Model:
        def select_segments(self, req, nominated):
            return [{"start": 297.0, "end": 332.0, "score": 0.85, "title": "empty", "reason": "r", "flags": []},
                    {"start": 1000.0, "end": 1040.0, "score": 0.7, "title": "explains", "reason": "r", "flags": []}]

    picks = selection.select(_request("informative", transcript=transcript, max_clips=1, min_score=0.6), Model())
    assert [p.title for p in picks] == ["explains"]
    # Custom allows quieter moments (reactions), but not near-silence either.
    custom = selection.select(_request("custom", instructions="reactions", transcript=transcript, max_clips=2,
                                       min_score=0.6), Model())
    assert [p.title for p in custom] == ["explains"]


def test_per_minute_sums_chat_and_averages_loudness():
    chat = np.concatenate([np.full(60, 2.0), np.full(60, 5.0)])
    assert selection.per_minute(chat, 120.0, "chat") == [(2, 120.0), (3, 300.0)]
    assert selection.per_minute(np.full(90, -20.0), 0.0, "loudness") == [(0, -20.0), (1, -20.0)]
    assert selection.per_minute(None, 0.0, "chat") == []
