"""LLM provider: choosing segments and writing post text.

The rest of Snipr only knows two functions, ``select_segments`` and
``write_metadata``, so another provider can be swapped in by adding a class
with the same two methods and returning it from ``get_provider``.

Only text is sent: transcript lines with timestamps, plus per-minute chat and
loudness figures. Never audio or video.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import secrets, selection

SEGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "score": {"type": "number"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "flags": {"type": "array", "items": {"type": "string", "enum": ["slurs", "sexual", "music"]}},
                },
                "required": ["start", "end", "score", "title", "reason", "flags"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}

METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["headline", "title", "description", "hashtags"],
    "additionalProperties": False,
}

SELECT_SYSTEM = """\
You pick moments from a livestream recording to turn into short vertical clips for \
YouTube Shorts, TikTok and Facebook Reels. You are given the stream's transcript with a \
timestamp in seconds at the start of every line, and you return the segments worth clipping.

A good clip stands on its own. Someone who never saw the stream must understand it from its \
first sentence, and it must end at a natural stopping point, not in the middle of a thought. \
Start at the beginning of the sentence that sets the moment up, and end right after the payoff.

For every segment you return:
- "start" and "end" are times in seconds taken from the transcript's timestamps. Use the \
timestamp of the line where the clip should begin, and the timestamp of the first line that \
should NOT be in the clip as the end.
- "score" is your confidence from 0 to 1 that this would do well as a short clip. Use the \
whole range: 0.9 is a moment you would post without hesitation, 0.6 is decent, below 0.5 is \
filler. Scores are used to drop weak picks, so do not inflate them.
- "title" is a short working title for the moment.
- "reason" is one or two sentences on why this moment works, written for the person \
reviewing the clip.
- "flags" lists any of these that apply inside the segment, and is empty otherwise: \
"slurs" (slurs or hate speech are said), "sexual" (sexually explicit talk), "music" \
(copyrighted music is clearly playing or being sung along to, as far as the transcript shows).

Respect the length limits you are given. Never return overlapping segments. Returning fewer \
segments, or none, is the right answer when the material is weak. Skip ads, sponsor reads, \
raid and subscriber thank-yous, technical difficulties and dead air."""

METADATA_SYSTEM = """\
You write the text that goes on and around a short vertical clip cut from a livestream. \
You get the transcript of the clip and some context.

Return:
- "headline": the hook burned onto the video for its whole length. At most 6 words, no \
hashtags, no emoji, no quotation marks, no full stop. It tells a scrolling viewer what they \
are about to see, for example "Getting 5 stars in GTA 6" or "How to fix your bitrate".
- "title": the post title, at most 70 characters. Specific and honest about what happens. \
No clickbait that the clip does not pay off.
- "description": one or two plain sentences describing the moment. Do not add a credit \
line, links or hashtags here; those are appended separately.
- "hashtags": 3 to 5 hashtags without the # sign, lowercase, no spaces. Lead with the \
game or topic.

Write in plain English, in the register of the clip. Do not use emoji."""


class LLMError(Exception):
    """The provider could not produce a usable answer."""


def seconds_label(value: float) -> str:
    return f"{value:.1f}"


def transcript_lines(segments: list[dict], start: float | None = None, end: float | None = None) -> str:
    """Transcript as one ``[seconds] sentence`` line per sentence."""
    lines = []
    for seg in segments:
        if start is not None and seg["e"] < start:
            continue
        if end is not None and seg["s"] > end:
            break
        lines.append(f"[{seconds_label(seg['s'])}] {seg['t']}")
    return "\n".join(lines)


def _signals_block(req: "selection.SelectionRequest") -> str:
    chat = dict(selection.per_minute(req.chat_rate, req.span_start, "chat"))
    loud = dict(selection.per_minute(req.loudness, req.span_start, "loudness"))
    if not loud:
        return ""
    rows = ["minute | start_seconds | chat_messages | loudness_dbfs" if chat
            else "minute | start_seconds | loudness_dbfs"]
    for minute in sorted(loud):
        if chat:
            rows.append(f"{minute} | {minute * 60} | {chat.get(minute, 0):.0f} | {loud[minute]:.1f}")
        else:
            rows.append(f"{minute} | {minute * 60} | {loud[minute]:.1f}")
    return "\n".join(rows)


def build_select_prompt(req: "selection.SelectionRequest", nominated: list | None) -> str:
    """The user message for one selection request."""
    segments = req.transcript.get("segments", [])
    limits = (f"Each clip must be between {req.clip_min_s:.0f} and {req.clip_max_s:.0f} seconds long. "
              f"Return at most {req.max_clips} segments.")
    header = f"Streamer: {req.streamer or 'unknown'}\nStream title: {req.vod_title or 'unknown'}\n"

    if nominated is not None:
        parts = [header,
                 "These stretches of the stream were flagged because chat activity or loudness "
                 "jumped well above the stream's normal level. A jump only tells you something "
                 "happened, not that it is worth watching: decide for each one whether there is a "
                 "funny, hype or surprising moment that works as a clip, and where exactly it "
                 "starts and ends. You may pick a segment that starts before or ends after the "
                 "flagged window, as long as it stays inside the transcript shown for it. Drop the "
                 "ones that are noise.", limits, ""]
        for index, win in enumerate(nominated, 1):
            lo = max(req.span_start, win.start - 45)
            hi = win.end + 30
            parts.append(f"<candidate index=\"{index}\" flagged_from=\"{seconds_label(win.start)}\" "
                         f"flagged_to=\"{seconds_label(win.end)}\" chat_spike=\"{win.chat_z:.1f}\" "
                         f"loudness_spike=\"{win.audio_z:.1f}\">")
            parts.append(transcript_lines(segments, lo, hi) or "(no speech in the transcript here)")
            parts.append("</candidate>")
        return "\n".join(parts)

    if req.mode == "custom":
        wanted = req.instructions.strip()
    else:
        wanted = selection.INFORMATIVE_INSTRUCTION
        if req.instructions.strip():
            wanted += "\n\nAdditional guidance from the reviewer: " + req.instructions.strip()
    parts = [header, "What to look for:", f"<instructions>\n{wanted}\n</instructions>", limits]
    signals = _signals_block(req)
    if signals:
        has_chat = "chat_messages" in signals
        explain = ("chat_messages counts chat messages in that minute and loudness_dbfs is the "
                   "average sound level." if has_chat else
                   "loudness_dbfs is the average sound level in that minute. This stream has no "
                   "chat replay.")
        parts += ["",
                  "Per-minute signals for the stream. Use them when the instructions are about "
                  "reactions, hype or how the audience responded. " + explain,
                  f"<signals>\n{signals}\n</signals>"]
    parts += ["", "<transcript>", transcript_lines(segments), "</transcript>"]
    return "\n".join(parts)


@dataclass
class AnthropicProvider:
    """Claude through the Anthropic API."""

    model: str
    api_key: str | None = None
    client: object = None
    usage: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic

            # With no explicit key the SDK resolves credentials from the environment.
            self.client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()

    def _ask(self, purpose: str, system: str, prompt: str, schema: dict, max_tokens: int) -> dict:
        import anthropic

        try:
            # Streamed so a long transcript cannot run into the HTTP timeout.
            with self.client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": prompt}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise LLMError("The Anthropic API key was rejected. Check it in settings.") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"The Anthropic API key is not allowed to use {self.model}.") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"The model '{self.model}' was not found. Check the model name in settings.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("The Anthropic API rate limit was hit. The job will be retried.") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"The Anthropic API rejected the request: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"The Anthropic API returned an error ({exc.status_code}).") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Could not reach the Anthropic API. Check the internet connection.") from exc

        used = message.usage
        self.usage.append({
            "purpose": purpose, "model": self.model,
            "input_tokens": (used.input_tokens or 0) + (getattr(used, "cache_read_input_tokens", 0) or 0)
                            + (getattr(used, "cache_creation_input_tokens", 0) or 0),
            "output_tokens": used.output_tokens or 0,
        })
        if message.stop_reason == "refusal":
            raise LLMError("The model declined this request, so nothing was selected.")
        if message.stop_reason == "max_tokens":
            raise LLMError("The model's answer was cut off before it finished.")
        text = next((block.text for block in message.content if block.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("The model's answer was not valid JSON.") from exc

    def select_segments(self, req: "selection.SelectionRequest", nominated: list | None) -> list[dict]:
        """Raw segment dicts for one VOD. Validation happens in ``scoring``."""
        segments = req.transcript.get("segments", [])
        if nominated is None and _estimated_tokens(segments) > MAX_TRANSCRIPT_TOKENS:
            return self._select_in_parts(req)
        answer = self._ask("select", SELECT_SYSTEM, build_select_prompt(req, nominated), SEGMENT_SCHEMA, 32000)
        return answer.get("segments", [])

    def _select_in_parts(self, req: "selection.SelectionRequest") -> list[dict]:
        """Split a transcript that is too long for one request and merge the answers."""
        import copy

        segments = req.transcript["segments"]
        parts = max(2, -(-_estimated_tokens(segments) // MAX_TRANSCRIPT_TOKENS))
        size = -(-len(segments) // parts)
        found: list[dict] = []
        for index in range(0, len(segments), size):
            part = copy.copy(req)
            part.transcript = {"segments": segments[index:index + size]}
            answer = self._ask("select", SELECT_SYSTEM, build_select_prompt(part, None), SEGMENT_SCHEMA, 32000)
            found.extend(answer.get("segments", []))
        return found

    def write_metadata(self, clip_text: str, streamer: str, vod_title: str, working_title: str = "",
                       game: str = "") -> dict:
        """Headline, title, description and hashtags for one clip."""
        prompt = "\n".join([
            f"Streamer: {streamer or 'unknown'}",
            f"Stream title: {vod_title or 'unknown'}",
            f"Game or topic: {game or 'unknown'}",
            f"Working title from selection: {working_title or 'none'}",
            "", "<clip_transcript>", clip_text.strip() or "(no speech)", "</clip_transcript>",
        ])
        answer = self._ask("metadata", METADATA_SYSTEM, prompt, METADATA_SCHEMA, 8000)
        return clean_metadata(answer)

    def total_tokens(self) -> dict:
        return {"input_tokens": sum(u["input_tokens"] for u in self.usage),
                "output_tokens": sum(u["output_tokens"] for u in self.usage),
                "requests": len(self.usage)}


MAX_TRANSCRIPT_TOKENS = 600_000   # leaves room in a 1M-token context for the answer


def _estimated_tokens(segments: list[dict]) -> int:
    return int(sum(len(s["t"]) + 12 for s in segments) / 3.2)


def clean_metadata(raw: dict) -> dict:
    """Trim and tidy the LLM's post text so it is safe to render and post."""
    headline = " ".join(str(raw.get("headline") or "").replace('"', "").split()).rstrip(".")
    words = headline.split()
    if len(words) > 8:
        headline = " ".join(words[:8])
    tags = []
    for tag in raw.get("hashtags") or []:
        tag = "".join(ch for ch in str(tag).lower() if ch.isalnum())
        if tag and tag not in tags:
            tags.append(tag)
    return {
        "headline": headline,
        "title": " ".join(str(raw.get("title") or "").split())[:100],
        "description": str(raw.get("description") or "").strip(),
        "hashtags": tags[:5],
    }


def fallback_metadata(clip_text: str, vod_title: str, game: str = "") -> dict:
    """Post text when no LLM is configured: plain, clearly a placeholder to edit."""
    words = clip_text.split()
    headline = " ".join(words[:6]).rstrip(".,!?") if words else (vod_title or "Clip")
    tags = ["".join(ch for ch in game.lower() if ch.isalnum())] if game else []
    return clean_metadata({
        "headline": headline,
        "title": (vod_title or headline)[:70],
        "description": " ".join(words[:30]),
        "hashtags": [t for t in tags if t] + ["gaming", "clips"],
    })


def api_key() -> str | None:
    return secrets.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or None


def get_provider(settings: dict):
    """Return the configured provider, or None when no API key is stored."""
    cfg = settings.get("llm", {})
    if cfg.get("provider", "anthropic") != "anthropic":
        raise LLMError(f"Unknown LLM provider '{cfg.get('provider')}'. Only 'anthropic' is built.")
    key = api_key()
    if not key and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return None
    return AnthropicProvider(model=cfg.get("model", "claude-haiku-5-5"), api_key=key)
