import numpy as np

from snipr import scoring


def _flat(n, base, spikes):
    """A per-second curve at ``base`` with (start, length, level) spikes."""
    rng = np.random.default_rng(7)
    curve = base + rng.normal(0, 0.05 * abs(base) + 0.1, n)
    for start, length, level in spikes:
        curve[start:start + length] = level
    return curve


# ---- spikes -----------------------------------------------------------------

def test_window_means_uses_sliding_windows():
    starts, means = scoring.window_means(np.arange(60, dtype=float), 30, 10)
    assert list(starts) == [0, 10, 20, 30]
    assert means[0] == np.mean(np.arange(0, 30))
    assert means[3] == np.mean(np.arange(30, 60))


def test_window_means_empty_when_curve_shorter_than_window():
    starts, means = scoring.window_means(np.ones(10), 30, 10)
    assert starts.size == 0 and means.size == 0


def test_spike_in_chat_and_audio_scores_highest():
    chat = _flat(3600, 2.0, [(1200, 30, 40.0)])
    loud = _flat(3600, -30.0, [(1200, 30, -8.0)])
    windows = scoring.score_windows(chat, loud)
    best = max(windows, key=lambda w: w.raw)
    assert best.start == 1200
    assert best.score > 0.9
    quiet = [w for w in windows if w.end < 1100 or w.start > 1300]
    assert max(w.score for w in quiet) < 0.6


def test_scores_are_relative_to_the_streams_own_baseline():
    # The same absolute chat rate is a spike in a quiet stream and normal in a busy one.
    quiet_stream = scoring.score_windows(_flat(1800, 1.0, [(600, 30, 20.0)]), _flat(1800, -30.0, []))
    busy_stream = scoring.score_windows(_flat(1800, 20.0, []), _flat(1800, -30.0, []))
    assert max(w.score for w in quiet_stream) > 0.8
    assert max(w.score for w in busy_stream) < 0.6


def test_without_chat_loudness_carries_the_score():
    loud = _flat(1800, -30.0, [(900, 30, -6.0)])
    for chat in (None, np.zeros(1800)):
        windows = scoring.score_windows(chat, loud)
        best = max(windows, key=lambda w: w.score)
        assert best.start == 900 and best.score > 0.9 and best.chat_z == 0


def test_offset_shifts_window_times():
    windows = scoring.score_windows(None, _flat(600, -30.0, [(100, 30, -5.0)]), offset=5000.0)
    assert max(windows, key=lambda w: w.score).start == 5100.0


def test_nominate_merges_neighbours_and_keeps_distinct_moments():
    chat = _flat(3600, 2.0, [(600, 45, 40.0), (2000, 30, 30.0), (3000, 30, 25.0)])
    windows = scoring.score_windows(chat, _flat(3600, -30.0, []))
    picks = scoring.nominate(windows, 3)
    assert len(picks) == 3
    assert [round(p.start / 100) for p in picks] == [6, 20, 30]
    assert picks[0].end - picks[0].start >= 40          # the long spike became one moment
    assert all(a.end <= b.start for a, b in zip(picks, picks[1:]))


def test_nominate_respects_count_and_ignores_flat_streams():
    chat = _flat(3600, 2.0, [(i * 300, 30, 40.0) for i in range(1, 10)])
    assert len(scoring.nominate(scoring.score_windows(chat, _flat(3600, -30.0, [])), 4)) == 4
    assert scoring.nominate([scoring.Window(0, 30, 0.0, 0, 0)], 5) == []


# ---- boundaries -------------------------------------------------------------

SENTENCES = [{"s": 95.0, "e": 101.0}, {"s": 101.5, "e": 118.0}, {"s": 118.4, "e": 131.0},
             {"s": 131.2, "e": 150.0}, {"s": 150.5, "e": 171.0}]


def test_snap_moves_edges_out_to_sentence_breaks():
    assert scoring.snap_to_sentences(103.0, 128.0, SENTENCES, 20, 60) == (101.5, 131.0)


def test_snap_leaves_edges_when_no_break_is_near():
    assert scoring.snap_to_sentences(500.0, 530.0, SENTENCES, 20, 60) == (500.0, 530.0)


def test_snap_enforces_length_limits():
    start, end = scoring.snap_to_sentences(96.0, 170.0, SENTENCES, 20, 40)
    assert start == 95.0 and end - start <= 40 and end == 131.0
    start, end = scoring.snap_to_sentences(119.0, 122.0, SENTENCES, 20, 60)
    assert end - start >= 20


# ---- LLM output -------------------------------------------------------------

def _validate(raw, **kw):
    args = dict(duration=3600.0, min_len=20, max_len=60, min_score=0.6, limit=5)
    args.update(kw)
    return scoring.validate_segments(raw, **args)


def test_validate_keeps_good_segments_best_first():
    out = _validate([
        {"start": 100, "end": 140, "score": 0.7, "title": "b", "reason": "r"},
        {"start": 500, "end": 545, "score": 0.9, "title": "a", "reason": "r"},
    ])
    assert [s.title for s in out] == ["a", "b"]


def test_validate_accepts_timestamps_as_text():
    out = _validate([{"start": "00:10:00", "end": "10:40", "score": "0.8"}])
    assert (out[0].start, out[0].end, out[0].score) == (600.0, 640.0, 0.8)


def test_validate_drops_bad_times_low_scores_and_short_clips():
    out = _validate([
        {"start": 140, "end": 100, "score": 0.9},        # ends before it starts
        {"start": 3590, "end": 3700, "score": 0.9},      # past the end of the VOD
        {"start": -5, "end": 30, "score": 0.9},
        {"start": 100, "end": 140, "score": 0.4},        # below the quality bar
        {"start": 100, "end": 110, "score": 0.9},        # too short
        {"start": "soon", "end": 140, "score": 0.9},
        {"start": 100, "end": 140},                      # no score
        "not a dict",
    ])
    assert out == []
    assert _validate({"start": 1}) == [] and _validate(None) == []


def test_validate_trims_slightly_long_and_drops_far_too_long():
    sentences = [{"s": 100.0, "e": 130.0}, {"s": 130.0, "e": 155.0}, {"s": 155.0, "e": 170.0}]
    out = _validate([{"start": 100, "end": 170, "score": 0.9}], transcript_segments=sentences)
    assert (out[0].start, out[0].end) == (100.0, 155.0)
    assert _validate([{"start": 100, "end": 400, "score": 0.9}]) == []


def test_validate_resolves_overlaps_by_score_and_caps_the_count():
    out = _validate([
        {"start": 100, "end": 140, "score": 0.7, "title": "weaker"},
        {"start": 120, "end": 160, "score": 0.9, "title": "stronger"},
    ])
    assert [s.title for s in out] == ["stronger"]
    many = [{"start": i * 100, "end": i * 100 + 30, "score": 0.9} for i in range(1, 20)]
    assert len(_validate(many, limit=5)) == 5


def test_validate_respects_allowed_ranges_and_known_flags():
    raw = [{"start": 100, "end": 140, "score": 0.9, "flags": ["Music", "made-up"]},
           {"start": 2000, "end": 2040, "score": 0.9}]
    out = _validate(raw, allowed=[(0, 600)])
    assert len(out) == 1 and out[0].start == 100 and out[0].flags == ("music",)


def test_validate_reads_percent_scores():
    assert _validate([{"start": 100, "end": 140, "score": 82}])[0].score == 0.82


# ---- game filter ------------------------------------------------------------

def test_game_names_match_across_spellings():
    assert scoring.game_matches("GTA 6", "Grand Theft Auto VI")
    assert scoring.game_matches("Grand Theft Auto VI", "PLAYING GTA 6 RIGHT NOW GET IN HERE!")
    assert scoring.game_matches("gta6", "GTA 6 early access")
    assert scoring.game_matches("Fortnite", "ONE PIECE IS IN FORTNITE!")
    assert scoring.game_matches("EA Sports FC 27", "EA Sports FC 27")
    assert not scoring.game_matches("GTA 6", "Grand Theft Auto V")
    assert not scoring.game_matches("GTA 6", "Just Chatting")
    assert not scoring.game_matches("", "anything")


def test_parse_games_splits_commas_and_lines():
    assert scoring.parse_games("GTA 6, Fortnite\nMinecraft ,") == ["GTA 6", "Fortnite", "Minecraft"]
    assert scoring.parse_games(None) == [] and scoring.parse_games("  ") == []


CHAPTERS = [{"start": 0, "end": 1535, "title": "Just Chatting"},
            {"start": 1535, "end": 7710, "title": "Fortnite"},
            {"start": 7710, "end": 10408, "title": "EA Sports FC 27"},
            {"start": 10408, "end": 11049, "title": "Just Chatting"}]


def test_game_ranges_keeps_only_matching_chapters():
    assert scoring.game_ranges(["Fortnite"], CHAPTERS, "x", 11049) == [(1535.0, 7710.0)]
    assert scoring.game_ranges(["Fortnite", "EA Sports FC 27"], CHAPTERS, "x", 11049) == [(1535.0, 10408.0)]
    assert scoring.game_ranges(["Just Chatting"], CHAPTERS, "x", 11049) == [(0.0, 1535.0), (10408.0, 11049.0)]


def test_game_ranges_skips_vods_without_the_game():
    assert scoring.game_ranges(["GTA 6"], CHAPTERS, "ONE PIECE IS IN FORTNITE", 11049) is None
    assert scoring.game_ranges(["GTA 6"], [], "REVEALING MY NEW FORTNITE SKIN", 9000) is None


def test_game_ranges_falls_back_to_title_without_chapters_and_passes_everything_unfiltered():
    assert scoring.game_ranges(["GTA 6"], [], "PLAYING GTA 6 EARLY ACCESS", 9000) == [(0.0, 9000.0)]
    assert scoring.game_ranges([], CHAPTERS, "x", 11049) == [(0.0, 11049.0)]
