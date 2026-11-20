"""Headline and word-by-word captions, drawn to the Designer's clip format spec.

Text is drawn with Pillow using the bundled fonts and handed to ffmpeg as
images: one for the headline, and one per caption state stitched together by
an ffconcat list that carries the timing. That keeps the look pixel-exact
(rounded highlight box, outline) instead of approximating it with subtitles.

Sizes are in pixels on the 1080 x 1920 output frame.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
FRAME_W, FRAME_H = 1080, 1920
STACK_SEAM = 608                      # stacked layout: facecam above this line, gameplay below


@dataclass(frozen=True)
class Style:
    # Headline: Chakra Petch Bold 64/72, white on a #0A0A0F plate.
    headline_font: str = "ChakraPetch-700.ttf"
    headline_size: int = 64
    headline_line: int = 72
    headline_pad_x: int = 32
    headline_pad_y: int = 20
    headline_radius: int = 8
    headline_max_width: int = 920
    headline_top: int = 150
    headline_max_lines: int = 2
    headline_min_size: int = 44
    # Captions: Inter ExtraBold 76, white with an 8 px outline, current word on a purple box.
    caption_font: str = "Inter-800.ttf"
    caption_size: int = 76
    caption_center_y: int = 1340
    caption_outline: int = 8
    caption_max_words: int = 3
    caption_gap: int = 36
    side_margin: int = 60
    box_pad_x: int = 16
    box_pad_y: int = 4
    box_radius: int = 10
    ink: tuple = (255, 255, 255, 255)
    plate: tuple = (10, 10, 15, 255)          # #0A0A0F
    accent: tuple = (124, 58, 237, 255)       # #7C3AED
    # How the current word is marked: "box" puts it on an accent box, "color"
    # keeps the outline and paints the word itself in the accent colour.
    highlight: str = "box"
    uppercase: bool = False


STYLE = Style()

# "box" is the Designer's spec. "bold" is the plain creator look: one or two
# heavy capitals at a time, the spoken word in yellow, no box.
STYLES: dict[str, Style] = {
    "box": STYLE,
    "bold": Style(caption_font="Montserrat-900.ttf", caption_size=104, caption_center_y=1400,
                  caption_outline=12, caption_max_words=2, caption_gap=30, highlight="color",
                  uppercase=True, accent=(255, 214, 10, 255)),
}


def get_style(name: str | None) -> Style:
    return STYLES.get(name or "box", STYLE)


@lru_cache(maxsize=32)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / name), size)


# --------------------------------------------------------------------------- caption timing (pure)

@dataclass
class Line:
    words: list[tuple[float, float, str]]     # (start, end, text), clip-relative seconds

    @property
    def start(self) -> float:
        return self.words[0][0]

    @property
    def end(self) -> float:
        return self.words[-1][1]


@dataclass
class State:
    start: float
    end: float
    line: int | None      # index into the lines, None for no caption on screen
    word: int = 0         # which word of the line is highlighted


def clean_word(text: str) -> str:
    """Caption spelling of a transcript word: no trailing commas or full stops."""
    text = text.strip().strip('"“”')
    while len(text) > 1 and text[-1] in ".,;:":
        text = text[:-1]
    return text


def group_words(words: list, max_words: int = 3, max_gap_s: float = 0.6,
                measure: Callable[[list[str]], float] | None = None,
                max_width: float = 960.0) -> list[Line]:
    """Split clip-relative words into caption lines.

    A line holds up to ``max_words`` words and breaks early at a pause, at the
    end of a sentence, or when it would be wider than ``max_width``.
    """
    lines: list[Line] = []
    current: list[tuple[float, float, str]] = []
    sentence_ended = False
    for start, end, raw in words:
        text = clean_word(raw)
        if not text:
            continue
        too_wide = bool(current and measure and measure([w[2] for w in current] + [text]) > max_width)
        paused = bool(current and start - current[-1][1] > max_gap_s)
        if current and (len(current) >= max_words or paused or sentence_ended or too_wide):
            lines.append(Line(current))
            current = []
        current.append((float(start), float(end), text))
        sentence_ended = raw.strip().rstrip('"”').endswith((".", "?", "!"))
    if current:
        lines.append(Line(current))
    return lines


def caption_states(lines: list[Line], clip_len: float, hold_s: float = 0.25) -> list[State]:
    """The caption timeline: which line is up and which word is lit, with no gaps.

    A line appears when its first word starts. The highlight moves when the
    next word starts. The line stays for ``hold_s`` after its last word unless
    the next line begins sooner. Blank states fill the silences, and the states
    cover 0..``clip_len`` exactly.
    """
    states: list[State] = []
    cursor = 0.0

    def add(start: float, end: float, line: int | None, word: int = 0) -> None:
        nonlocal cursor
        start, end = max(start, cursor), min(end, clip_len)
        if end - start < 1e-3:
            return
        if start - cursor > 1e-3:
            states.append(State(cursor, start, None))
        states.append(State(start, end, line, word))
        cursor = end

    for index, line in enumerate(lines):
        if line.start >= clip_len:
            break
        next_start = lines[index + 1].start if index + 1 < len(lines) else clip_len
        line_end = min(line.end + hold_s, next_start, clip_len)
        for w, (start, _end, _text) in enumerate(line.words):
            word_end = line.words[w + 1][0] if w + 1 < len(line.words) else line_end
            add(start, word_end, index, w)
    if clip_len - cursor > 1e-3:
        states.append(State(cursor, clip_len, None))
    return states


# --------------------------------------------------------------------------- drawing

def measure_line(texts: list[str], style: Style = STYLE, size: int | None = None) -> float:
    font = _font(style.caption_font, size or style.caption_size)
    return sum(font.getlength(t) for t in texts) + style.caption_gap * (len(texts) - 1)


def caption_strip_height(style: Style = STYLE) -> int:
    return int(style.caption_size * 1.2) + 2 * (style.box_pad_y + style.caption_outline + 6)


def draw_caption(texts: list[str], active: int | None, style: Style = STYLE) -> Image.Image:
    """One caption line as a transparent strip the width of the frame."""
    height = caption_strip_height(style)
    strip = Image.new("RGBA", (FRAME_W, height), (0, 0, 0, 0))
    if not texts:
        return strip
    size = style.caption_size
    usable = FRAME_W - 2 * style.side_margin
    while measure_line(texts, style, size) > usable and size > 36:
        size -= 4                                  # a single very long word still has to fit
    font = _font(style.caption_font, size)
    draw = ImageDraw.Draw(strip)
    ascent, descent = font.getmetrics()
    text_h = ascent + descent
    top = (height - text_h) // 2
    x = (FRAME_W - measure_line(texts, style, size)) / 2
    for index, text in enumerate(texts):
        width = font.getlength(text)
        if index == active and style.highlight == "color":
            draw.text((x, top), text, font=font, fill=style.accent,
                      stroke_width=style.caption_outline, stroke_fill=style.plate)
        elif index == active:
            draw.rounded_rectangle(
                (x - style.box_pad_x, top - style.box_pad_y, x + width + style.box_pad_x,
                 top + text_h + style.box_pad_y),
                radius=style.box_radius, fill=style.accent)
            draw.text((x, top), text, font=font, fill=style.ink)
        else:
            draw.text((x, top), text, font=font, fill=style.ink,
                      stroke_width=style.caption_outline, stroke_fill=style.plate)
        x += width + style.caption_gap
    return strip


def wrap_headline(text: str, style: Style = STYLE) -> tuple[list[str], int]:
    """Break the headline into at most two lines, shrinking the type if needed."""
    words = " ".join(text.split()).split(" ")
    inner = style.headline_max_width - 2 * style.headline_pad_x
    size = style.headline_size
    while True:
        font = _font(style.headline_font, size)
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if current and font.getlength(trial) > inner:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)
        fits = len(lines) <= style.headline_max_lines and all(font.getlength(l) <= inner for l in lines)
        if fits or size <= style.headline_min_size:
            break
        size -= 4
    if len(lines) > style.headline_max_lines:
        lines = lines[: style.headline_max_lines]
        last = lines[-1]
        while last and font.getlength(last + "…") > inner:
            last = last[:-1].rstrip()
        lines[-1] = last + "…"
    return lines, size


def draw_headline(text: str, style: Style = STYLE) -> Image.Image | None:
    """The headline plate, cropped tight. None when there is no headline."""
    if not text.strip():
        return None
    lines, size = wrap_headline(text, style)
    font = _font(style.headline_font, size)
    line_h = round(style.headline_line * size / style.headline_size)
    text_w = max(font.getlength(line) for line in lines)
    width = int(text_w + 2 * style.headline_pad_x)
    height = line_h * len(lines) + 2 * style.headline_pad_y
    plate = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(plate)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=style.headline_radius, fill=style.plate)
    ascent, descent = font.getmetrics()
    for index, line in enumerate(lines):
        x = (width - font.getlength(line)) / 2
        y = style.headline_pad_y + index * line_h + (line_h - (ascent + descent)) / 2
        draw.text((x, y), line, font=font, fill=style.ink)
    return plate


# --------------------------------------------------------------------------- overlays for ffmpeg

@dataclass
class Overlays:
    headline_png: Path | None
    headline_xy: tuple[int, int]
    captions_list: Path | None          # ffconcat file, None when the clip has no speech
    captions_y: int
    lines: list[Line] = field(default_factory=list)


def build_overlays(words: list, headline: str, clip_len: float, out_dir: Path,
                   stacked: bool = False, style: Style = STYLE) -> Overlays:
    """Write the headline image and the caption image sequence for one clip.

    ``words`` are ``[start, end, text]`` in clip-relative seconds.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    headline_png: Path | None = None
    headline_xy = (0, 0)
    plate = draw_headline(headline, style)
    if plate is not None:
        headline_png = out_dir / "headline.png"
        plate.save(headline_png)
        # Fill frame: top edge 150 px down. Stacked: centred on the seam so it never covers the face.
        y = STACK_SEAM - plate.height // 2 if stacked else style.headline_top
        headline_xy = ((FRAME_W - plate.width) // 2, y)

    usable = FRAME_W - 2 * style.side_margin
    if style.uppercase:
        words = [[w[0], w[1], str(w[2]).upper()] for w in words]
    lines = group_words(words, style.caption_max_words, measure=lambda t: measure_line(t, style),
                        max_width=usable)
    states = caption_states(lines, clip_len)
    captions_y = style.caption_center_y - caption_strip_height(style) // 2
    captions_list: Path | None = None
    if lines:
        rendered: dict[tuple[int | None, int], str] = {}
        entries: list[str] = ["ffconcat version 1.0"]
        last_name = ""
        for state in states:
            key = (state.line, state.word if state.line is not None else 0)
            if key not in rendered:
                name = "blank.png" if state.line is None else f"l{state.line:04d}w{state.word}.png"
                texts = [] if state.line is None else [w[2] for w in lines[state.line].words]
                draw_caption(texts, None if state.line is None else state.word, style).save(out_dir / name)
                rendered[key] = name
            last_name = rendered[key]
            entries += [f"file '{last_name}'", f"duration {state.end - state.start:.3f}"]
        entries.append(f"file '{last_name}'")       # the concat demuxer needs the last frame repeated
        captions_list = out_dir / "captions.ffconcat"
        captions_list.write_text("\n".join(entries) + "\n", encoding="utf-8")
    return Overlays(headline_png, headline_xy, captions_list, captions_y, lines)
