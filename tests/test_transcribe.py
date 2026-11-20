from snipr import transcribe


def _words(*items):
    return [[s, e, t] for s, e, t in items]


def test_sentences_end_at_punctuation():
    words = _words((0.0, 0.4, "Look"), (0.5, 0.9, "at"), (1.0, 1.4, "this."),
                   (1.5, 1.9, "What"), (2.0, 2.4, "is"), (2.5, 2.9, "that?"),
                   (3.0, 3.4, "No"), (3.5, 3.9, "way!"))
    out = transcribe.split_sentences(words)
    assert [s["t"] for s in out] == ["Look at this.", "What is that?", "No way!"]
    assert (out[0]["s"], out[0]["e"]) == (0.0, 1.4)
    assert out[1]["w"][0] == [1.5, 1.9, "What"]


def test_sentences_end_at_long_pauses():
    words = _words((0.0, 0.4, "hold"), (0.5, 0.9, "on"), (3.0, 3.4, "okay"), (3.5, 3.9, "go"))
    assert [s["t"] for s in transcribe.split_sentences(words)] == ["hold on", "okay go"]


def test_long_run_without_punctuation_is_broken_up():
    words = _words(*[(i * 0.5, i * 0.5 + 0.4, "go") for i in range(120)])   # one minute of speech
    out = transcribe.split_sentences(words, max_len_s=25.0)
    assert len(out) >= 2
    assert all(s["e"] - s["s"] <= 25.5 for s in out)
    assert sum(len(s["w"]) for s in out) == 120


def test_fragments_are_joined_to_the_word_before():
    words = _words((0.0, 0.2, "y"), (0.2, 0.5, "'all"), (0.6, 0.9, "suck"), (0.9, 0.95, "!"), (1.2, 1.5, "bro"))
    assert transcribe.merge_fragments(words) == [[0.0, 0.5, "y'all"], [0.6, 0.95, "suck!"], [1.2, 1.5, "bro"]]
    # A fragment after a long pause is left alone, as is one with nothing before it.
    assert transcribe.merge_fragments(_words((0.0, 0.2, "'cause"), (5.0, 5.2, "'em"))) == \
        [[0.0, 0.2, "'cause"], [5.0, 5.2, "'em"]]


def test_empty_transcript():
    assert transcribe.split_sentences([]) == []


def test_words_between_uses_word_midpoints():
    transcript = {"segments": transcribe.split_sentences(
        _words((9.6, 10.2, "edge"), (10.3, 10.8, "inside"), (19.7, 20.5, "tail"), (20.6, 21.0, "out")))}
    assert [w[2] for w in transcribe.words_between(transcript, 10.0, 20.0)] == ["inside"]
    assert transcribe.text_between(transcript, 9.0, 22.0) == "edge inside tail out"
