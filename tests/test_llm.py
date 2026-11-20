import json
import re
from types import SimpleNamespace

import numpy as np
import pytest

from snipr import llm, selection


class StubStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class StubClient:
    """Stands in for the Anthropic client and records what was sent."""

    def __init__(self, answers, stop_reason="end_turn"):
        self.answers = list(answers)
        self.stop_reason = stop_reason
        self.calls = []
        self.messages = self

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        text = answer if isinstance(answer, str) else json.dumps(answer)
        return StubStream(SimpleNamespace(
            usage=SimpleNamespace(input_tokens=1200, output_tokens=300,
                                  cache_read_input_tokens=0, cache_creation_input_tokens=0),
            stop_reason=self.stop_reason,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        ))


def _transcript():
    lines = [(100.0, 104.0, "So here is how you set up OBS scenes."),
             (104.5, 118.0, "First you add a scene, then you add your sources to it."),
             (118.5, 131.0, "And that is the whole trick, it takes two minutes."),
             (400.0, 405.0, "Oh my god, five stars!"),
             (405.5, 431.0, "They are all on me, go go go, no way we made it out.")]
    return {"segments": [{"s": s, "e": e, "t": t, "w": []} for s, e, t in lines]}


def _request(mode="informative", instructions="", chat=True):
    return selection.SelectionRequest(
        mode=mode, instructions=instructions, max_clips=5, clip_min_s=20, clip_max_s=60, min_score=0.6,
        duration=600.0, span_start=0.0, ranges=[(0.0, 600.0)], transcript=_transcript(),
        loudness=np.full(600, -30.0, dtype=np.float32),
        chat_rate=np.full(600, 2.0, dtype=np.float32) if chat else None,
        streamer="pixelmoth", vod_title="GTA 6 day 2")


def _provider(answers, **kw):
    client = StubClient(answers, **kw)
    return llm.AnthropicProvider(model="claude-haiku-5-5", client=client), client


def test_transcript_lines_carry_second_timestamps_and_respect_ranges():
    text = llm.transcript_lines(_transcript()["segments"])
    assert text.splitlines()[0] == "[100.0] So here is how you set up OBS scenes."
    assert len(text.splitlines()) == 5
    assert llm.transcript_lines(_transcript()["segments"], 390, 440).splitlines()[0].startswith("[400.0]")


def test_informative_prompt_has_instruction_signals_and_whole_transcript():
    prompt = llm.build_select_prompt(_request(), None)
    assert selection.INFORMATIVE_INSTRUCTION in prompt
    assert "between 20 and 60 seconds" in prompt and "at most 5" in prompt
    assert "chat_messages" in prompt and "<transcript>" in prompt
    assert "[400.0] Oh my god, five stars!" in prompt
    assert "pixelmoth" in prompt and "GTA 6 day 2" in prompt


def test_custom_prompt_uses_the_users_words_instead_of_the_preset():
    prompt = llm.build_select_prompt(_request("custom", "Moments where he reacts to a jump scare"), None)
    assert "Moments where he reacts to a jump scare" in prompt
    assert selection.INFORMATIVE_INSTRUCTION not in prompt


def test_prompt_without_chat_omits_the_chat_column():
    prompt = llm.build_select_prompt(_request(chat=False), None)
    assert "chat_messages" not in prompt and "loudness_dbfs" in prompt


def test_highlights_prompt_shows_only_the_transcript_around_each_nomination():
    from snipr.scoring import Window
    prompt = llm.build_select_prompt(_request("highlights"), [Window(395.0, 425.0, 0.9, 6.0, 3.0, 5.0)])
    assert '<candidate index="1" flagged_from="395.0" flagged_to="425.0"' in prompt
    assert "[400.0] Oh my god, five stars!" in prompt
    assert "OBS" not in prompt                      # the far-away part of the stream is left out


HISTORY = [
    {"verdict": "approved", "reason": None, "title": "Five stars in a minute", "summary": "The wanted level climbs fast."},
    {"verdict": "rejected", "reason": "boring", "title": "Menu tour", "summary": "He scrolls through settings."},
    {"verdict": "rejected", "reason": "bad_cut", "title": "The heist", "summary": "Starts mid-sentence."},
    {"verdict": "rejected", "reason": "bad_framing", "title": "Crop problem", "summary": "Face cut off."},
]


def test_review_history_reaches_the_prompt_in_every_mode():
    from snipr.scoring import Window

    request = _request()
    request.history = HISTORY
    for prompt in (llm.build_select_prompt(request, None),
                   llm.build_select_prompt(request, [Window(395.0, 425.0, 0.9, 6.0, 3.0, 5.0)])):
        assert "<reviewer_history>" in prompt and "Approved:" in prompt and "Rejected:" in prompt
        assert "- Five stars in a minute: The wanted level climbs fast." in prompt
        assert "- Menu tour (they found it boring): He scrolls through settings." in prompt
        assert "started or ended in the wrong place" in prompt
        assert "Crop problem" not in prompt             # framing is about the crop, not the pick


def test_no_history_means_no_history_block():
    assert llm.history_block(None) == "" and llm.history_block([]) == ""
    assert llm.history_block([HISTORY[3]]) == ""        # only a framing rejection: nothing to learn from
    assert "<reviewer_history>" not in llm.build_select_prompt(_request(), None)
    only_approved = llm.history_block([HISTORY[0]])
    assert "Approved:" in only_approved and "Rejected:" not in only_approved


def test_select_segments_sends_schema_model_and_returns_raw_segments():
    answer = {"segments": [{"start": 100.0, "end": 131.0, "score": 0.8, "title": "OBS scenes",
                            "reason": "Complete how-to.", "flags": []}]}
    provider, client = _provider([answer])
    out = provider.select_segments(_request(), None)
    assert out == answer["segments"]
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["format"]["schema"] == llm.SEGMENT_SCHEMA
    assert "temperature" not in call and "thinking" not in call
    assert provider.total_tokens() == {"input_tokens": 1200, "output_tokens": 300, "requests": 1}


def test_selection_end_to_end_validates_what_the_model_returns():
    answer = {"segments": [
        {"start": 100.0, "end": 131.0, "score": 0.8, "title": "OBS scenes", "reason": "r", "flags": []},
        {"start": 400.0, "end": 431.0, "score": 0.4, "title": "weak", "reason": "r", "flags": []},
        {"start": 900.0, "end": 940.0, "score": 0.9, "title": "past the end", "reason": "r", "flags": []},
    ]}
    provider, _ = _provider([answer])
    picks = selection.select(_request(), provider)
    assert [p.title for p in picks] == ["OBS scenes"]


def test_refusal_and_cut_off_answers_raise_clear_errors():
    provider, _ = _provider([{"segments": []}], stop_reason="refusal")
    with pytest.raises(llm.LLMError, match="declined"):
        provider.select_segments(_request(), None)
    provider, _ = _provider(['{"segments": ['], stop_reason="max_tokens")
    with pytest.raises(llm.LLMError, match="cut off"):
        provider.select_segments(_request(), None)


def test_long_transcripts_are_split_and_merged(monkeypatch):
    monkeypatch.setattr(llm, "MAX_TRANSCRIPT_TOKENS", 30)
    a = {"segments": [{"start": 100.0, "end": 131.0, "score": 0.8, "title": "a", "reason": "r", "flags": []}]}
    b = {"segments": [{"start": 400.0, "end": 431.0, "score": 0.7, "title": "b", "reason": "r", "flags": []}]}
    provider, client = _provider([a, b, {"segments": []}, {"segments": []}])
    out = provider.select_segments(_request(), None)
    assert len(client.calls) >= 2
    assert {s["title"] for s in out} >= {"a", "b"}
    assert "five stars" not in client.calls[0]["messages"][0]["content"]


def test_write_metadata_cleans_the_answer():
    provider, client = _provider([{
        "headline": '"Getting 5 stars in GTA 6."', "title": "  Five stars in under a minute ",
        "description": "The wanted level climbs to five stars.", "hashtags": ["#GTA6", "gta6", "Gaming Clips", ""]}])
    meta = provider.write_metadata("Oh my god, five stars!", "pixelmoth", "GTA 6 day 2", game="GTA 6")
    assert meta == {"headline": "Getting 5 stars in GTA 6", "title": "Five stars in under a minute",
                    "description": "The wanted level climbs to five stars.", "hashtags": ["gta6", "gamingclips"]}
    assert "GTA 6" in client.calls[0]["messages"][0]["content"]


def test_write_narration_sends_frames_in_order_with_the_word_budget(tmp_path):
    frames = []
    for index in range(2):
        frame = tmp_path / f"frame{index}.jpg"
        frame.write_bytes(b"\xff\xd8jpeg-bytes-%d" % index)
        frames.append(frame)
    provider, client = _provider([{"script": '"Speed walks straight into the water." The crocodile does not wait.'}])
    script = provider.write_narration("Oh my god!", frames, 31.0, 78, "IShowSpeed", "GTA 6 day 2", game="GTA 6")
    assert script == "Speed walks straight into the water. The crocodile does not wait."
    content = client.calls[0]["messages"][0]["content"]
    kinds = [block["type"] for block in content]
    assert kinds == ["text", "image", "text", "image", "text"]
    assert content[1]["source"]["media_type"] == "image/jpeg" and content[0]["text"] == "Frame 1 of 2:"
    assert "Word budget: 78 words" in content[-1]["text"] and "Clip length: 31 seconds" in content[-1]["text"]
    assert client.calls[0]["output_config"]["format"]["schema"] == llm.NARRATION_SCHEMA
    assert provider.usage[-1]["purpose"] == "narration"


def test_narration_over_budget_is_cut_at_a_sentence_end():
    script = "One two three four five. Six seven eight nine ten. Eleven twelve thirteen fourteen fifteen."
    assert llm.clean_narration(script, 20) == script
    assert llm.clean_narration(script, 10) == "One two three four five. Six seven eight nine ten."
    assert llm.clean_narration("a b c d e f g h i j k l m n o p", 10) == "a b c d e f g h i j k."


def test_headline_is_capped_and_fallback_metadata_is_usable():
    assert len(llm.clean_metadata({"headline": "one two three four five six seven eight nine ten"})["headline"].split()) == 8
    meta = llm.fallback_metadata("Oh my god five stars they are all on me", "GTA 6 day 2", "GTA 6")
    assert meta["headline"] == ""                     # no plate is better than a nonsense hook
    assert meta["hashtags"][0] == "gta6" and meta["title"] == "GTA 6 day 2"
    assert meta["description"].startswith("Oh my god")


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


def _gemini(monkeypatch, response):
    """A Gemini provider whose HTTP call is replaced; returns the provider and the captured request."""
    provider = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    provider.rate_limit_waits_s = (0, 0)             # tests never wait out a real minute
    provider.unavailable_waits_s = (0, 0)
    provider.fallback_models = ()                    # one model only, unless a test is about fallback
    sent = {}

    def fake_post(body, model=None):
        sent["body"] = body
        return response

    monkeypatch.setattr(provider, "_post", fake_post)
    return provider, sent


def _gemini_answer(payload, finish="STOP"):
    return FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True},
                                               {"text": json.dumps(payload)}]}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120, "thoughtsTokenCount": 30}})


def test_gemini_request_shape_and_answer(monkeypatch):
    answer = {"segments": [{"start": 100.0, "end": 131.0, "score": 0.8, "title": "OBS scenes",
                            "reason": "Complete how-to.", "flags": []}]}
    provider, sent = _gemini(monkeypatch, _gemini_answer(answer))
    assert provider.select_segments(_request(), None) == answer["segments"]
    body = sent["body"]
    assert body["systemInstruction"]["parts"][0]["text"] == llm.SELECT_SYSTEM
    assert body["contents"][0]["role"] == "user" and "[400.0] Oh my god, five stars!" in body["contents"][0]["parts"][0]["text"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == llm.SEGMENT_SCHEMA
    assert provider.total_tokens() == {"input_tokens": 900, "output_tokens": 150, "requests": 1}


def test_gemini_sends_frames_as_inline_images(monkeypatch, tmp_path):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8jpeg")
    provider, sent = _gemini(monkeypatch, _gemini_answer({"script": "Speed walks into the water."}))
    assert provider.write_narration("Oh my god!", [frame], 31.0, 78, "IShowSpeed", "GTA 6") == "Speed walks into the water."
    parts = sent["body"]["contents"][0]["parts"]
    assert [list(p)[0] for p in parts] == ["text", "inlineData", "text"]
    assert parts[1]["inlineData"]["mimeType"] == "image/jpeg" and parts[1]["inlineData"]["data"]


def test_gemini_errors_are_explained(monkeypatch):
    cases = [
        (FakeResponse(400, {"error": {"message": "API key not valid. Please pass a valid API key."}}), "key was rejected"),
        (FakeResponse(403, {"error": {"message": "denied"}}), "key was rejected"),
        (FakeResponse(404, {"error": {"message": "model not found"}}), "was not found"),
        (FakeResponse(429, {"error": {"message": "quota"}}), "rate limit"),
        (FakeResponse(400, {"error": {"message": "Unknown name foo"}}), "rejected the request: Unknown name foo"),
        (FakeResponse(503, {}), "temporarily unavailable (503). This is on Google's side, not your key"),
        (FakeResponse(503, {"error": {"message": "The model is overloaded. Please try again later."}}),
         "Google says: The model is overloaded. Please try again later."),
        (FakeResponse(429, {"error": {"message": "Quota exceeded for input tokens per minute, limit 250000"}}),
         "Google says: Quota exceeded for input tokens per minute, limit 250000"),
        (FakeResponse(418, {"error": {"message": "teapot"}}), "returned an error (418). Google says: teapot"),
        (FakeResponse(200, {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}), "declined this request"),
        (FakeResponse(200, {"candidates": []}), "no answer"),
        (_gemini_answer({"segments": []}, finish="MAX_TOKENS"), "cut off"),
        (_gemini_answer({"segments": []}, finish="SAFETY"), "without a usable answer (safety)"),
    ]
    for response, expected in cases:
        provider, _ = _gemini(monkeypatch, response)
        with pytest.raises(llm.LLMError, match=re.escape(expected)):
            provider.select_segments(_request(), None)


def test_gemini_waits_out_a_rate_limit_and_sends_long_transcripts_in_parts(monkeypatch):
    provider = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    provider.rate_limit_waits_s = (0, 0)                        # no real waiting in the test
    answers = [FakeResponse(429, {"error": {"message": "quota"}}),
               _gemini_answer({"segments": []})]
    calls = []
    monkeypatch.setattr(provider, "_post", lambda body, model=None: (calls.append(body), answers.pop(0))[1])
    assert provider.select_segments(_request(), None) == []
    assert len(calls) == 2                                      # tried again after the "slow down"

    # Three refusals in a row is a real failure, reported as a rate limit.
    stuck = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    stuck.rate_limit_waits_s = (0, 0)
    monkeypatch.setattr(stuck, "_post", lambda body, model=None: FakeResponse(429, {"error": {"message": "quota"}}))
    with pytest.raises(llm.LLMError, match="rate limit"):
        stuck.select_segments(_request(), None)

    assert llm.GeminiProvider.max_transcript_tokens < llm.MAX_TRANSCRIPT_TOKENS
    assert llm.AnthropicProvider(model="m", client=object())._transcript_limit() == llm.MAX_TRANSCRIPT_TOKENS


def test_gemini_retries_when_google_is_briefly_unavailable(monkeypatch):
    provider = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    provider.rate_limit_waits_s = provider.unavailable_waits_s = (0, 0)
    answers = [FakeResponse(503, {}), FakeResponse(500, {}), _gemini_answer({"segments": []})]
    calls = []
    monkeypatch.setattr(provider, "_post", lambda body, model=None: (calls.append(1), answers.pop(0))[1])
    assert provider.select_segments(_request(), None) == []
    assert len(calls) == 3                                      # two "unavailable" answers were waited out

    down = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    down.rate_limit_waits_s = down.unavailable_waits_s = (0, 0)
    tries = []
    down.fallback_models = ()
    monkeypatch.setattr(down, "_post", lambda body, model=None: (tries.append(1), FakeResponse(503, {}))[1])
    with pytest.raises(llm.LLMError, match="temporarily unavailable"):
        down.select_segments(_request(), None)
    assert len(tries) == 3                                      # gives up after the two waits


def test_gemini_falls_back_to_a_lighter_model_when_the_chosen_one_is_overloaded(monkeypatch):
    provider = llm.GeminiProvider(model="gemini-3.8-flash", api_key="AIza-test-key-123456789")
    provider.rate_limit_waits_s = provider.unavailable_waits_s = (0, 0)
    asked = []

    def fake_post(body, model=None):
        asked.append(model)
        if model == "gemini-3.8-flash":
            return FakeResponse(503, {"error": {"message": "This model is currently experiencing high demand."}})
        return _gemini_answer({"segments": []})

    monkeypatch.setattr(provider, "_post", fake_post)
    assert provider.select_segments(_request(), None) == []
    assert asked == ["gemini-3.8-flash"] * 3 + ["gemini-3.5-flash-lite"]      # patient with the first, then moved on
    assert provider.fallbacks_used == ["gemini-3.5-flash-lite"]
    assert provider.usage[-1]["model"] == "gemini-3.5-flash-lite"             # usage is logged against what answered

    # When every model is overloaded, the error is the chosen model's, in Google's words.
    monkeypatch.setattr(provider, "_post", lambda body, model=None: FakeResponse(
        503, {"error": {"message": "This model is currently experiencing high demand."}}))
    with pytest.raises(llm.LLMError, match="Google says: This model is currently experiencing high demand"):
        provider.select_segments(_request(), None)

    # A wrong key or a bad request is not a reason to try other models.
    asked.clear()
    monkeypatch.setattr(provider, "_post", lambda body, model=None: (asked.append(model), FakeResponse(403, {}))[1])
    with pytest.raises(llm.LLMError, match="key was rejected"):
        provider.select_segments(_request(), None)
    assert asked == ["gemini-3.8-flash"]


def test_gemini_reads_how_long_google_asks_it_to_wait():
    asked = FakeResponse(429, {"error": {"message": "quota", "details": [
        {"@type": "type.googleapis.com/google.rpc.Help"},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}]}})
    assert llm.GeminiProvider._retry_delay(asked) == 34.0
    assert llm.GeminiProvider._retry_delay(FakeResponse(429, {"error": {"message": "quota"}})) is None
    assert llm.GeminiProvider._retry_delay(FakeResponse(429, {"error": {"details": [{"retryDelay": "soon"}]}})) is None


def test_provider_follows_the_setting_and_its_own_key(monkeypatch):
    keys = {"gemini_api_key": "AIza-test-key-123456789"}
    monkeypatch.setattr(llm.secrets, "get", lambda name: keys.get(name))
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    gemini = {"llm": {"provider": "gemini", "gemini_model": "gemini-3.5-flash-lite"}}
    provider = llm.get_provider(gemini)
    assert isinstance(provider, llm.GeminiProvider) and provider.model == "gemini-3.5-flash-lite"
    assert llm.api_key(gemini) == "AIza-test-key-123456789"
    # Claude is selected but only a Gemini key exists: nothing is available, and the keys do not mix.
    claude = {"llm": {"provider": "anthropic", "model": "claude-haiku-5-5"}}
    assert llm.get_provider(claude) is None and llm.api_key(claude) is None
    assert llm.model_for({"llm": {}}, "gemini") == "gemini-3.5-flash-lite"


def test_no_key_means_no_provider(monkeypatch):
    monkeypatch.setattr(llm.secrets, "get", lambda name: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert llm.get_provider({"llm": {"provider": "anthropic", "model": "claude-haiku-5-5"}}) is None
    with pytest.raises(llm.LLMError):
        llm.get_provider({"llm": {"provider": "something-else"}})
