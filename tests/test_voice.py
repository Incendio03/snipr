from snipr import voice


def _heard(*items):
    return [[s, e, w] for s, e, w in items]


def test_matching_words_take_the_spoken_timings_and_keep_script_spelling():
    heard = _heard((0.2, 0.5, "gta"), (0.6, 0.9, "six"), (1.0, 1.2, "is"), (1.3, 1.9, "realistic"))
    out = voice.align_script("GTA six is realistic.", heard, 2.2)
    assert out == [[0.2, 0.5, "GTA"], [0.6, 0.9, "six"], [1.0, 1.2, "is"], [1.3, 1.9, "realistic."]]


def test_words_heard_differently_share_the_gap_between_their_neighbours():
    # The script says "6" and "20%"; the transcription heard "six" and "twenty percent".
    heard = _heard((0.0, 0.4, "GTA"), (0.5, 0.9, "six"), (1.0, 1.3, "is"), (1.4, 1.8, "twenty"),
                   (1.8, 2.2, "percent"), (2.3, 2.8, "bigger"))
    out = voice.align_script("GTA 6 is 20% bigger", heard, 3.0)
    assert [w[2] for w in out] == ["GTA", "6", "is", "20%", "bigger"]
    assert out[1][0] == 0.4 and out[1][1] == 1.0            # between "GTA" and "is"
    assert out[3][0] == 1.3 and out[3][1] == 2.3            # between "is" and "bigger"
    assert all(a[1] <= b[0] + 1e-9 for a, b in zip(out, out[1:]))


def test_unmatched_words_at_the_edges_and_with_nothing_heard():
    heard = _heard((1.0, 1.4, "world"))
    out = voice.align_script("Hello big world again", heard, 3.0)
    assert out[0][0] == 0.0 and out[1][1] == 1.0            # two leading words split 0..1
    assert out[2] == [1.0, 1.4, "world"]
    assert out[3] == [1.4, 3.0, "again"]                    # trailing word runs to the end
    spread = voice.align_script("one two three four", [], 2.0)
    assert [w[:2] for w in spread] == [[0.0, 0.5], [0.5, 1.0], [1.0, 1.5], [1.5, 2.0]]
    assert voice.align_script("   ", heard, 1.0) == []


def test_word_budget_scales_with_clip_length_and_speed():
    assert voice.word_budget(31) == 78
    assert voice.word_budget(31, speed=1.2) > voice.word_budget(31)
    assert voice.word_budget(2) == 8                        # never asks for an unusably short script
    assert voice.DEFAULT_VOICE in voice.voice_ids()
