from PIL import Image

from snipr import captions, render
from snipr.captions import Line, group_words, caption_states


def _w(*items):
    return [[s, e, t] for s, e, t in items]


# ---- grouping ---------------------------------------------------------------

def test_lines_hold_at_most_three_words():
    words = _w(*[(i * 0.3, i * 0.3 + 0.25, f"w{i}") for i in range(7)])
    lines = group_words(words)
    assert [[w[2] for w in l.words] for l in lines] == [["w0", "w1", "w2"], ["w3", "w4", "w5"], ["w6"]]


def test_lines_break_at_pauses_and_sentence_ends():
    words = _w((0.0, 0.3, "hold"), (0.4, 0.7, "on"), (2.0, 2.3, "okay"), (2.4, 2.7, "go."), (2.8, 3.1, "Now"))
    lines = group_words(words)
    assert [[w[2] for w in l.words] for l in lines] == [["hold", "on"], ["okay", "go"], ["Now"]]


def test_lines_break_when_too_wide_and_drop_trailing_punctuation():
    words = _w((0.0, 0.3, "extraordinarily,"), (0.4, 0.7, "unbelievably"), (0.8, 1.1, "long"))
    lines = group_words(words, measure=lambda texts: sum(len(t) for t in texts) * 50, max_width=900)
    assert [[w[2] for w in l.words] for l in lines] == [["extraordinarily"], ["unbelievably", "long"]]
    assert captions.clean_word('"what?"') == "what?" and captions.clean_word("yes!") == "yes!"
    assert captions.clean_word("okay.") == "okay" and captions.clean_word(".") == "."


def test_real_font_keeps_lines_inside_the_safe_width():
    words = _w(*[(i * 0.3, i * 0.3 + 0.25, "congratulations") for i in range(6)])
    usable = captions.FRAME_W - 2 * captions.STYLE.side_margin
    for line in group_words(words, measure=captions.measure_line, max_width=usable):
        assert captions.measure_line([w[2] for w in line.words]) <= usable


# ---- timeline ---------------------------------------------------------------

def test_states_cover_the_clip_without_gaps_or_overlaps():
    lines = [Line([(1.0, 1.3, "that"), (1.4, 1.7, "is")]), Line([(1.8, 2.2, "five"), (2.3, 2.6, "stars")]),
             Line([(6.0, 6.4, "wow")])]
    states = caption_states(lines, 8.0)
    assert states[0].start == 0.0 and states[-1].end == 8.0
    assert all(abs(a.end - b.start) < 1e-9 for a, b in zip(states, states[1:]))
    assert all(s.end > s.start for s in states)


def test_highlight_moves_when_the_next_word_starts():
    lines = [Line([(1.0, 1.3, "that"), (1.4, 1.7, "is"), (1.8, 2.2, "five")])]
    states = [s for s in caption_states(lines, 5.0) if s.line is not None]
    assert [(s.start, s.end, s.word) for s in states] == [(1.0, 1.4, 0), (1.4, 1.8, 1), (1.8, 2.45, 2)]


def test_line_holds_briefly_then_clears_unless_the_next_line_starts():
    lines = [Line([(1.0, 1.3, "a")]), Line([(1.4, 1.7, "b")]), Line([(4.0, 4.3, "c")])]
    states = caption_states(lines, 6.0)
    shown = [(s.line, round(s.start, 2), round(s.end, 2)) for s in states]
    assert (0, 1.0, 1.4) in shown                 # cut short by the next line
    assert (1, 1.4, 1.95) in shown                # held 0.25 s after its last word
    assert (None, 1.95, 4.0) in shown             # blank during the silence


def test_words_past_the_end_of_the_clip_are_dropped():
    lines = [Line([(1.0, 1.3, "in")]), Line([(9.0, 9.4, "late")]), Line([(12.0, 12.3, "out")])]
    states = caption_states(lines, 9.2)
    assert states[-1].end == 9.2 and all(s.line != 2 for s in states)
    assert caption_states([], 3.0) == [captions.State(0.0, 3.0, None)]


# ---- drawing ----------------------------------------------------------------

def test_caption_strip_is_frame_wide_and_highlights_only_the_active_word():
    plain = captions.draw_caption(["that", "is", "five"], None)
    lit = captions.draw_caption(["that", "is", "five"], 2)
    assert plain.size == (captions.FRAME_W, captions.caption_strip_height())
    accent = captions.STYLE.accent
    count = lambda image: sum(n for n, colour in image.getcolors(maxcolors=1 << 20) if colour == accent)
    assert count(plain) == 0 and count(lit) > 2000
    left_margin = plain.getbbox()[0]
    assert left_margin >= captions.STYLE.side_margin - captions.STYLE.caption_outline
    assert captions.draw_caption([], None).getbbox() is None           # blank state is fully transparent


def test_highlight_does_not_shift_the_other_words():
    first = captions.draw_caption(["that", "is", "five"], 0)
    second = captions.draw_caption(["that", "is", "five"], 2)
    assert first.getbbox()[1] == second.getbbox()[1]                   # same baseline
    # The middle word is drawn identically in both, so its column of pixels is unchanged.
    mid = captions.FRAME_W // 2
    column = lambda image: [image.getpixel((mid, y)) for y in range(image.height)]
    assert column(first) == column(second)


def test_headline_wraps_to_two_lines_and_shrinks_before_truncating():
    lines, size = captions.wrap_headline("Getting 5 stars in GTA 6")
    assert lines == ["Getting 5 stars in GTA 6"] and size == 64
    lines, size = captions.wrap_headline("Getting five stars in GTA 6 with one bullet left")
    assert len(lines) == 2 and size <= 64
    lines, size = captions.wrap_headline(" ".join(["extraordinary"] * 12))
    assert len(lines) == 2 and size == captions.STYLE.headline_min_size and lines[-1].endswith("…")


def test_headline_plate_respects_max_width_and_empty_text():
    plate = captions.draw_headline("Getting five stars in GTA 6 with one bullet left")
    assert plate.width <= captions.STYLE.headline_max_width
    assert plate.getpixel((plate.width // 2, 3)) == captions.STYLE.plate
    assert captions.draw_headline("   ") is None


def test_build_overlays_writes_images_and_a_timed_list(tmp_path):
    words = _w((0.5, 0.8, "that"), (0.9, 1.2, "is"), (1.3, 1.7, "five"), (1.8, 2.2, "stars."), (5.0, 5.4, "Wow"))
    out = captions.build_overlays(words, "Getting 5 stars in GTA 6", 7.0, tmp_path)
    assert out.headline_png.exists() and out.headline_xy[1] == 150
    assert out.headline_xy[0] == (1080 - Image.open(out.headline_png).width) // 2      # centred
    text = out.captions_list.read_text(encoding="utf-8").splitlines()
    assert text[0] == "ffconcat version 1.0"
    durations = [float(line.split()[1]) for line in text if line.startswith("duration")]
    assert abs(sum(durations) - 7.0) < 0.01
    files = [line.split("'")[1] for line in text if line.startswith("file")]
    assert files[0] == "blank.png" and files[-1] == files[-2]
    assert all((tmp_path / name).exists() for name in files)
    assert out.captions_y == 1340 - captions.caption_strip_height() // 2


def test_stacked_headline_sits_on_the_seam_and_silent_clips_have_no_caption_list(tmp_path):
    out = captions.build_overlays([], "Clutch", 5.0, tmp_path, stacked=True)
    height = Image.open(out.headline_png).height
    assert out.headline_xy[1] == 608 - height // 2
    assert out.captions_list is None
    assert captions.build_overlays([], "", 5.0, tmp_path).headline_png is None


# ---- layout geometry --------------------------------------------------------

def test_default_fill_crop_is_the_centre_nine_by_sixteen():
    assert render.aspect_fit(None, 1920, 1080, 9 / 16) == (656, 0, 606, 1080)


def test_crop_rect_is_fitted_to_ratio_inside_the_marked_area():
    x, y, w, h = render.aspect_fit([0.0, 0.5, 0.4, 0.5], 1920, 1080, 9 / 16)
    assert (w, h) == (302, 540) and y == 540 and 0 <= x and x + w <= 768
    assert abs(w / h - 9 / 16) < 0.01
    # Out-of-range values are clamped instead of producing a crop outside the frame.
    x, y, w, h = render.aspect_fit([0.9, 0.9, 0.5, 0.5], 1920, 1080, 9 / 16)
    assert x + w <= 1920 and y + h <= 1080 and w % 2 == 0 and h % 2 == 0


def test_frame_filters_for_fill_and_stacked():
    fill = render.frame_filters({"type": "fill", "crop": None}, 1920, 1080)
    assert fill == "[src]crop=606:1080:656:0,scale=1080:1920:flags=lanczos,setsar=1[base]"
    stacked = render.frame_filters({"type": "stacked", "facecam": [0, 0.5, 0.4, 0.5],
                                    "gameplay": [0.2, 0, 0.6, 1]}, 1920, 1080)
    assert "scale=1080:608" in stacked and "scale=1080:1312" in stacked and "vstack" in stacked
    # A stacked layout with a rectangle missing falls back to fill.
    assert "vstack" not in render.frame_filters({"type": "stacked", "facecam": None, "gameplay": None}, 1920, 1080)
