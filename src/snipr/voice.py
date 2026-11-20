"""Voiceover: turn a narration script into speech on this PC, with word timings.

The voice model is Kokoro (open source, Apache 2.0), run through ONNX Runtime
on the CPU. Nothing is sent anywhere and there is no cost per clip.

Kokoro does not report word timings with the standard model, so the spoken
audio is transcribed with the same Whisper model used for streams and the
timings are mapped back onto the script's own words. Captions therefore show
the script exactly as written, timed to how it was actually spoken.
"""

from __future__ import annotations

import difflib
import re
import urllib.request
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from . import config

MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")

# A short list for the settings page: id, label.
VOICES = [
    ("am_michael", "Michael (US, male)"),
    ("am_fenrir", "Fenrir (US, male, deeper)"),
    ("am_puck", "Puck (US, male, lighter)"),
    ("af_heart", "Heart (US, female)"),
    ("af_bella", "Bella (US, female, brighter)"),
    ("bm_george", "George (UK, male)"),
    ("bf_emma", "Emma (UK, female)"),
]
DEFAULT_VOICE = "am_michael"
WORDS_PER_SECOND = 2.6          # Kokoro at normal speed, measured in words of a narration script


class VoiceError(Exception):
    pass


def model_dir() -> Path:
    return config.subdir("models") / "kokoro"


def model_ready() -> bool:
    return all((model_dir() / name).is_file() and (model_dir() / name).stat().st_size > 1_000_000
               for name in MODEL_FILES)


def ensure_model(progress: Callable[[float], None] | None = None) -> Path:
    """Download the voice model files once (about 340 MB)."""
    folder = model_dir()
    folder.mkdir(parents=True, exist_ok=True)
    for index, name in enumerate(MODEL_FILES):
        target = folder / name
        if target.is_file() and target.stat().st_size > 1_000_000:
            continue
        partial = target.with_suffix(target.suffix + ".part")
        try:
            with urllib.request.urlopen(MODEL_URL + name, timeout=60) as response, open(partial, "wb") as out:
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                while block := response.read(1 << 20):
                    out.write(block)
                    done += len(block)
                    if progress and total:
                        progress((index + done / total) / len(MODEL_FILES))
            partial.replace(target)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise VoiceError(f"The voice model could not be downloaded: {exc}") from exc
    return folder


_engine = None


def _kokoro():
    global _engine
    if _engine is None:
        from kokoro_onnx import Kokoro

        folder = ensure_model()
        _engine = Kokoro(str(folder / MODEL_FILES[0]), str(folder / MODEL_FILES[1]))
    return _engine


def voice_ids() -> list[str]:
    return [voice for voice, _ in VOICES]


def synthesize(text: str, out_wav: Path, voice: str = DEFAULT_VOICE, speed: float = 1.0) -> float:
    """Speak ``text`` into a 16-bit mono WAV. Returns its length in seconds."""
    text = " ".join(text.split())
    if not text:
        raise VoiceError("There is no narration text to speak.")
    if voice not in voice_ids():
        voice = DEFAULT_VOICE
    try:
        samples, rate = _kokoro().create(text, voice=voice, speed=max(0.7, min(1.4, speed)), lang="en-us")
    except VoiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - ONNX Runtime and the phonemizer raise plain exceptions
        raise VoiceError(f"The voice model could not speak the narration: {exc}") from exc
    pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())
    return len(pcm) / float(rate)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    return np.frombuffer(raw, dtype=np.int16), rate


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(np.asarray(samples, dtype=np.int16).tobytes())


def word_budget(clip_seconds: float, speed: float = 1.0) -> int:
    """How many words of narration fit a clip, leaving a short breath at each end."""
    return max(8, int((clip_seconds - 1.0) * WORDS_PER_SECOND * speed))


# --------------------------------------------------------------------------- script format (pure)

_CUE = re.compile(r"^\s*\[\s*(\d+(?:\.\d+)?)\s*s?\s*\]\s*")
_PAUSE = re.compile(r"\[\s*pause\s*\]", re.IGNORECASE)
_TAG = re.compile(r"\[[^\]]*\]")
PAUSE_S = 0.7          # what a [pause] mark is worth
LINE_GAP_S = 0.25      # breath between two lines that follow each other


def parse_narration(text: str) -> list[dict]:
    """Split a narration script into the pieces that are spoken one after another.

    A line may start with a time cue, ``[12.5]``, meaning "say this 12.5 seconds
    into the clip"; lines without one follow straight on. ``[pause]`` leaves a
    gap. Any other bracketed note is dropped, so nothing in brackets is ever
    spoken or captioned. Returns ``[{"at": seconds or None, "text": str,
    "pause_after": seconds}]``.
    """
    parts: list[dict] = []
    for line in str(text or "").splitlines():
        cue = _CUE.match(line)
        at = float(cue.group(1)) if cue else None
        rest = line[cue.end():] if cue else line
        pieces = _PAUSE.split(rest)
        for index, piece in enumerate(pieces):
            spoken = " ".join(_TAG.sub(" ", piece).split())
            last = index == len(pieces) - 1
            if spoken:
                parts.append({"at": at, "text": spoken, "pause_after": 0.0 if last else PAUSE_S})
                at = None                       # the cue belongs to the first piece of its line
            elif not last and parts:
                parts[-1]["pause_after"] += PAUSE_S
    return parts


def spoken_text(text: str) -> str:
    """The words a narration script actually says, without cues or bracketed notes."""
    return " ".join(part["text"] for part in parse_narration(text))


def schedule(parts: list[dict], lengths: list[float], lead_s: float) -> list[tuple[float, float]]:
    """When each spoken piece starts and ends on the clip's clock.

    A cued piece starts at its cue unless the one before is still talking, in
    which case it waits its turn; an uncued piece follows the previous one.
    """
    spans: list[tuple[float, float]] = []
    cursor = lead_s
    for part, length in zip(parts, lengths):
        start = max(cursor, part["at"]) if part["at"] is not None else cursor
        spans.append((round(start, 3), round(start + length, 3)))
        cursor = start + length + LINE_GAP_S + part["pause_after"]
    return spans


# --------------------------------------------------------------------------- timings (pure)

def _key(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def align_script(script: str, heard: list[list], total_seconds: float) -> list[list]:
    """Give every word of ``script`` a start and end time.

    ``heard`` is ``[[start, end, word], ...]`` from transcribing the spoken
    audio. Words the transcription got right take its timings directly. Words
    it heard differently ("6" spoken, "six" heard) or missed share the gap
    between their matched neighbours evenly, so no word is left untimed.
    """
    words = script.split()
    if not words:
        return []
    timed: list[list | None] = [None] * len(words)
    matcher = difflib.SequenceMatcher(a=[_key(w) for w in words], b=[_key(h[2]) for h in heard], autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            if _key(words[block.a + offset]):
                start, end, _ = heard[block.b + offset]
                timed[block.a + offset] = [float(start), float(end), words[block.a + offset]]

    index = 0
    while index < len(words):
        if timed[index] is not None:
            index += 1
            continue
        run_end = index
        while run_end < len(words) and timed[run_end] is None:
            run_end += 1
        left = timed[index - 1][1] if index > 0 else 0.0
        right = timed[run_end][0] if run_end < len(words) else max(total_seconds, left)
        right = max(right, left)
        step = (right - left) / (run_end - index)
        for slot in range(index, run_end):
            begin = left + step * (slot - index)
            timed[slot] = [round(begin, 3), round(begin + step, 3), words[slot]]
        index = run_end
    return [[round(t[0], 3), round(max(t[1], t[0]), 3), t[2]] for t in timed]  # type: ignore[index]
