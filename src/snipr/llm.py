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


NARRATION_SCHEMA = {
    "type": "object",
    "properties": {"script": {"type": "string"}},
    "required": ["script"],
    "additionalProperties": False,
}

NARRATION_SYSTEM = """\
You write the voiceover for a short vertical clip cut from a livestream. A narrator reads \
your script over the footage while the stream's own sound plays quietly underneath, and the \
script is also shown on screen as captions, a couple of words at a time.

You are given still frames from the clip in the order they appear, the transcript of what \
the streamer says during it, and a word budget.

Write what a gaming channel's narrator would say over this moment: present tense, concrete, \
about what is happening on screen and why it is worth watching. The first sentence is the \
hook, so it must make someone stop scrolling. Follow the order of events in the frames so \
the words match the picture as it plays.

Only say what the frames and the transcript support. Do not invent facts about the game, \
such as features, numbers or release details, and do not claim to know what the streamer \
is thinking. Refer to the streamer by name where it reads naturally.

The script is read aloud at a fixed pace, so the word budget is a hard limit: a script over \
budget gets cut off before the clip ends. Aim for 85 to 100 percent of the budget.

Write plain sentences only: no stage directions, no speaker labels, no emoji, no hashtags, \
no quotation marks. Write numbers the way they should be spoken."""


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


REASON_NOTES = {
    "boring": "they found it boring",
    "wrong_topic": "not the kind of moment they want from this streamer",
    "bad_cut": "the moment was fine but the clip started or ended in the wrong place",
    "other": "no reason given",
    None: "no reason given",
}


def history_block(history: list[dict] | None) -> str:
    """Earlier review decisions as guidance for the next selection, or "" when there are none.

    This is how the reviewer's taste carries over: the model sees what they
    kept and what they turned down, and why. Rejections for bad framing are
    left out because they are about the crop, not about which moment was picked.
    """
    kept = [h for h in history or [] if h.get("verdict") == "approved"][:8]
    dropped = [h for h in history or [] if h.get("verdict") == "rejected" and h.get("reason") != "bad_framing"][:8]
    if not kept and not dropped:
        return ""

    def line(item: dict, with_reason: bool) -> str:
        title = " ".join(str(item.get("title") or "untitled").split())[:90]
        summary = " ".join(str(item.get("summary") or "").split())[:220]
        note = f" ({REASON_NOTES.get(item.get('reason'), 'no reason given')})" if with_reason else ""
        return f"- {title}{note}" + (f": {summary}" if summary else "")

    parts = ["<reviewer_history>",
             "The person who reviews these clips has already approved and rejected earlier picks from this "
             "streamer. Lean toward the kind of moment they approve and away from the kind they reject, and "
             "score accordingly. Treat this as their taste, not as rules: a strong moment is still worth "
             "returning even if it resembles something they once turned down. Where a rejection says the cut "
             "was wrong, take extra care that your segments start at the setup and end right after the payoff."]
    if kept:
        parts += ["Approved:"] + [line(h, False) for h in kept]
    if dropped:
        parts += ["Rejected:"] + [line(h, True) for h in dropped]
    parts.append("</reviewer_history>")
    return "\n".join(parts)


def build_select_prompt(req: "selection.SelectionRequest", nominated: list | None) -> str:
    """The user message for one selection request."""
    segments = req.transcript.get("segments", [])
    limits = (f"Each clip must be between {req.clip_min_s:.0f} and {req.clip_max_s:.0f} seconds long. "
              f"Return at most {req.max_clips} segments.")
    header = f"Streamer: {req.streamer or 'unknown'}\nStream title: {req.vod_title or 'unknown'}\n"
    taste = history_block(req.history)
    if taste:
        limits = limits + "\n\n" + taste

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
class Provider:
    """What every provider shares: the prompts, and how the answers are tidied.

    A provider only has to implement ``_ask``: send one request with a system
    prompt, a user message and a JSON schema, and return the parsed answer.
    The user message is text, or a list of blocks (``{"type": "text", ...}`` and
    base64 ``{"type": "image", ...}``) when still frames go along.
    """

    model: str
    api_key: str | None = None
    client: object = None
    usage: list[dict] = field(default_factory=list)

    def _ask(self, purpose: str, system: str, prompt: str | list, schema: dict, max_tokens: int) -> dict:
        raise NotImplementedError

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

    def write_narration(self, clip_text: str, frames: list, clip_seconds: float, word_budget: int,
                        streamer: str, vod_title: str, working_title: str = "", game: str = "") -> str:
        """A voiceover script for one clip, sized to ``word_budget`` words.

        ``frames`` are JPEG files from the clip in order. They are the one place
        where pictures, not only text, are sent to the provider.
        """
        import base64
        from pathlib import Path

        content: list = []
        for index, frame in enumerate(frames, 1):
            data = base64.standard_b64encode(Path(frame).read_bytes()).decode("ascii")
            content.append({"type": "text", "text": f"Frame {index} of {len(frames)}:"})
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
        content.append({"type": "text", "text": "\n".join([
            f"Streamer: {streamer or 'unknown'}",
            f"Stream title: {vod_title or 'unknown'}",
            f"Game or topic: {game or 'unknown'}",
            f"Working title from selection: {working_title or 'none'}",
            f"Clip length: {clip_seconds:.0f} seconds",
            f"Word budget: {word_budget} words",
            "", "<clip_transcript>", clip_text.strip() or "(no speech)", "</clip_transcript>",
        ])})
        answer = self._ask("narration", NARRATION_SYSTEM, content, NARRATION_SCHEMA, 8000)
        return clean_narration(answer.get("script", ""), word_budget)

    def total_tokens(self) -> dict:
        return {"input_tokens": sum(u["input_tokens"] for u in self.usage),
                "output_tokens": sum(u["output_tokens"] for u in self.usage),
                "requests": len(self.usage)}


@dataclass
class AnthropicProvider(Provider):
    """Claude through the Anthropic API."""

    def __post_init__(self) -> None:
        if self.client is None:
            import anthropic

            # With no explicit key the SDK resolves credentials from the environment.
            self.client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()

    def _ask(self, purpose: str, system: str, prompt: str | list, schema: dict, max_tokens: int) -> dict:
        """One request. ``prompt`` is text, or a list of content blocks when images are included."""
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


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_DEFAULT_MODEL = "gemini-3.8-flash"


@dataclass
class GeminiProvider(Provider):
    """Google's Gemini through its REST API (``generateContent``).

    Checked against Google's documentation on 2026-11-20: JSON output is asked
    for with ``responseMimeType`` and ``responseJsonSchema``; the key goes in the
    ``x-goog-api-key`` header. On Gemini's free tier Google uses what is sent to
    improve its products; on the paid tier it does not.
    """

    def _parts(self, prompt: str | list) -> list[dict]:
        if isinstance(prompt, str):
            return [{"text": prompt}]
        parts = []
        for block in prompt:
            if block.get("type") == "image":
                source = block["source"]
                parts.append({"inlineData": {"mimeType": source["media_type"], "data": source["data"]}})
            else:
                parts.append({"text": block.get("text", "")})
        return parts

    def _post(self, body: dict):
        import httpx

        return httpx.post(f"{GEMINI_URL}/{self.model}:generateContent", json=body,
                          headers={"x-goog-api-key": self.api_key or "", "Content-Type": "application/json"},
                          timeout=httpx.Timeout(600.0, connect=20.0))

    def _ask(self, purpose: str, system: str, prompt: str | list, schema: dict, max_tokens: int) -> dict:
        import httpx

        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": self._parts(prompt)}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema,
                                 "maxOutputTokens": max_tokens},
        }
        try:
            response = self._post(body)
        except httpx.HTTPError as exc:
            raise LLMError("Could not reach the Gemini API. Check the internet connection.") from exc
        if response.status_code != 200:
            raise LLMError(self._error_text(response))
        data = response.json()

        used = data.get("usageMetadata") or {}
        self.usage.append({"purpose": purpose, "model": self.model,
                           "input_tokens": int(used.get("promptTokenCount") or 0),
                           "output_tokens": int(used.get("candidatesTokenCount") or 0)
                                            + int(used.get("thoughtsTokenCount") or 0)})
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise LLMError(f"Gemini declined this request ({blocked.lower().replace('_', ' ')}), "
                           "so nothing was selected.")
        candidates = data.get("candidates") or []
        if not candidates:
            raise LLMError("Gemini returned no answer.")
        reason = candidates[0].get("finishReason") or "STOP"
        if reason == "MAX_TOKENS":
            raise LLMError("The model's answer was cut off before it finished.")
        if reason not in ("STOP", "FINISH_REASON_UNSPECIFIED"):
            raise LLMError(f"Gemini stopped without a usable answer ({reason.lower().replace('_', ' ')}).")
        text = "".join(part.get("text", "") for part in (candidates[0].get("content") or {}).get("parts") or []
                       if not part.get("thought"))
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("The model's answer was not valid JSON.") from exc

    def _error_text(self, response) -> str:
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = ""
        status = response.status_code
        if status in (401, 403) or "API key not valid" in detail or "API_KEY_INVALID" in detail:
            return "The Gemini API key was rejected. Check it in settings."
        if status == 404:
            return f"The model '{self.model}' was not found. Check the Gemini model name in settings."
        if status == 429:
            return ("The Gemini rate limit was reached. On the free tier that usually means today's or this "
                    "minute's allowance is used up. The job will be retried.")
        if status == 400:
            return f"The Gemini API rejected the request: {detail[:300] or 'no reason given'}"
        return f"The Gemini API returned an error ({status})."


PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "secret": "anthropic_api_key", "env": "ANTHROPIC_API_KEY",
                  "model_setting": "model", "default_model": "claude-haiku-5-5", "cls": AnthropicProvider},
    "gemini": {"label": "Gemini (Google)", "secret": "gemini_api_key", "env": "GEMINI_API_KEY",
               "model_setting": "gemini_model", "default_model": GEMINI_DEFAULT_MODEL, "cls": GeminiProvider},
}

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


def clean_narration(script: str, word_budget: int) -> str:
    """Tidy a narration script and cut it at a sentence end if it runs over budget."""
    text = " ".join(str(script).replace('"', "").replace("“", "").replace("”", "").split())
    words = text.split()
    limit = int(word_budget * 1.1)                  # a little over is spoken slightly faster instead
    if len(words) <= limit:
        return text
    kept = words[:limit]
    for index in range(len(kept) - 1, max(0, len(kept) // 2), -1):
        if kept[index].endswith((".", "!", "?")):
            return " ".join(kept[: index + 1])
    return " ".join(kept).rstrip(",;:") + "."


def fallback_metadata(clip_text: str, vod_title: str, game: str = "") -> dict:
    """Post text when no LLM is configured: plain, clearly a placeholder to edit.

    The headline is left empty. It is burned into the video, and the first
    words of the transcript ("But I know me yeah, I") make a worse hook than
    no plate at all; the reviewer can type one in the edit screen.
    """
    words = clip_text.split()
    tags = ["".join(ch for ch in game.lower() if ch.isalnum())] if game else []
    return clean_metadata({
        "headline": "",
        "title": (vod_title or "Clip")[:70],
        "description": " ".join(words[:30]),
        "hashtags": [t for t in tags if t] + ["gaming", "clips"],
    })


def provider_name(settings: dict | None = None) -> str:
    """Which provider is selected in settings."""
    from . import config

    name = ((settings or config.load_settings()).get("llm") or {}).get("provider", "anthropic")
    if name not in PROVIDERS:
        raise LLMError(f"Unknown AI provider '{name}'. Choose Claude or Gemini in settings.")
    return name


def key_for(name: str) -> str | None:
    """The stored API key for a provider (Credential Manager first, then the environment)."""
    spec = PROVIDERS[name]
    return secrets.get(spec["secret"]) or os.environ.get(spec["env"]) or None


def model_for(settings: dict, name: str) -> str:
    spec = PROVIDERS[name]
    return (settings.get("llm") or {}).get(spec["model_setting"]) or spec["default_model"]


def api_key(settings: dict | None = None) -> str | None:
    """The key of the provider that is selected. None means no AI is available."""
    return key_for(provider_name(settings))


def get_provider(settings: dict):
    """Return the selected provider ready to use, or None when it has no API key stored."""
    name = provider_name(settings)
    key = key_for(name)
    if not key and not (name == "anthropic" and os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    return PROVIDERS[name]["cls"](model=model_for(settings, name), api_key=key)


def check_provider(settings: dict) -> dict:
    """Send one tiny request to the selected provider and say whether it works."""
    provider = get_provider(settings)
    if provider is None:
        raise LLMError("No API key is stored for the selected provider.")
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
              "additionalProperties": False}
    provider._ask("check", "You are a connection test. Answer with the JSON asked for and nothing else.",
                  'Reply with {"ok": true}.', schema, 2000)
    return {"ok": True, "provider": provider_name(settings), "model": provider.model}
