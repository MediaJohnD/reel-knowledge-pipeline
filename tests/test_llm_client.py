from __future__ import annotations

import json
import time

import httpx
import pytest
import respx

from reel_pipeline.config import LlmConfig, Settings
from reel_pipeline.llm_client import (
    LlmCallError,
    _rate_limit_wait,
    _seconds_to_wait,
    call_llm,
    describe_images,
)


def test_call_llm_dispatches_to_ollama(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "hello from ollama"})
        )
        result = call_llm(settings, "prompt text", model="qwen2.5:14b", max_tokens=100)

    assert result == "hello from ollama"


def test_call_llm_ollama_raises_clear_error_on_failure(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(500, json={"error": "model not found"})
        )
        with pytest.raises(LlmCallError, match="Ollama"):
            call_llm(settings, "prompt text", model="missing-model", max_tokens=100)


def test_call_llm_dispatches_to_anthropic(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="anthropic"))
    settings.anthropic_api_key = "sk-ant-test"

    with respx.mock:
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "text", "text": "hello from claude"}]}
            )
        )
        result = call_llm(settings, "prompt text", model="claude-sonnet-4-5", max_tokens=100)

    assert result == "hello from claude"


def test_call_llm_anthropic_static_prefix_sets_cached_system_block(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="anthropic"))
    settings.anthropic_api_key = "sk-ant-test"

    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "text", "text": "hello from claude"}]}
            )
        )
        call_llm(
            settings,
            "prompt text",
            model="claude-sonnet-4-5",
            max_tokens=100,
            static_prefix="shared instructions",
        )

    sent = json.loads(route.calls.last.request.content)
    assert sent["system"] == [
        {
            "type": "text",
            "text": "shared instructions",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert sent["messages"] == [{"role": "user", "content": "prompt text"}]


def test_call_llm_anthropic_omits_system_without_static_prefix(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="anthropic"))
    settings.anthropic_api_key = "sk-ant-test"

    with respx.mock:
        route = respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200, json={"content": [{"type": "text", "text": "hello from claude"}]}
            )
        )
        call_llm(settings, "prompt text", model="claude-sonnet-4-5", max_tokens=100)

    assert "system" not in json.loads(route.calls.last.request.content)


def test_call_llm_groq_static_prefix_precedes_prompt(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
        )
        call_llm(
            settings,
            "variable part",
            model="llama-3.1-8b-instant",
            max_tokens=100,
            static_prefix="shared instructions",
        )

    sent = json.loads(route.calls.last.request.content)
    content = sent["messages"][0]["content"]
    assert content.startswith("shared instructions")
    assert content.endswith("variable part")


def test_call_llm_dispatches_to_groq(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"

    with respx.mock:
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "hello from groq"}}]}
            )
        )
        result = call_llm(settings, "prompt text", model="llama-3.1-8b-instant", max_tokens=100)

    assert result == "hello from groq"


def test_call_llm_groq_never_sends_reasoning_format(tmp_path):
    """Groq 400s on reasoning_format for non-reasoning models, and on "raw"
    specifically for gpt-oss-120b (verified live 2026-08-15) - sending it at
    all made the whole provider unusable. Covers the vision path too, which
    used to send it as well.
    """
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8\xff\xdb-not-a-real-jpeg")

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )
        call_llm(settings, "prompt", model="openai/gpt-oss-120b", max_tokens=100)
        call_llm(settings, "prompt", model="openai/gpt-oss-120b", max_tokens=100, json_mode=True)
        describe_images(settings, "prompt", [image], model="qwen/qwen3.6-27b", max_tokens=100)

    assert route.call_count == 3
    for call in route.calls:
        assert "reasoning_format" not in json.loads(call.request.content)


def test_call_llm_groq_strips_think_block_from_reasoning_models(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"
    raw_content = "\n<think>\nreasoning about the answer\n</think>\n\nhello from qwen"

    with respx.mock:
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": raw_content}}]}
            )
        )
        result = call_llm(settings, "prompt text", model="qwen/qwen3.6-27b", max_tokens=100)

    # Still inlined by qwen/qwen3.6-27b as of 2026-08-15 with no
    # reasoning_format sent at all - see test_call_llm_groq_never_sends_reasoning_format.
    assert result == "hello from qwen"


def test_call_llm_groq_truncated_reasoning_raises_instead_of_leaking_fragment(tmp_path):
    # max_tokens exhausted mid-reasoning, before a closing </think> ever
    # appeared - found live 2026-08-12. There's no confirmed answer text, so
    # this must be a real, diagnosable failure, not a silently "successful"
    # call returning a chopped-off reasoning fragment as the answer.
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"
    truncated = "\n<think>\nStill reasoning and ran out of budget"

    with respx.mock:
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": truncated}}]}
            )
        )
        with pytest.raises(LlmCallError, match="no text content"):
            call_llm(settings, "prompt text", model="qwen/qwen3.6-27b", max_tokens=50)


def test_call_llm_dispatches_to_cerebras(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="cerebras"))
    settings.cerebras_api_key = "csk-test"

    with respx.mock:
        respx.post("https://api.cerebras.ai/v1/chat/completions").mock(
            return_value=httpx.Response(
                200, json={"choices": [{"message": {"content": "hello from cerebras"}}]}
            )
        )
        result = call_llm(settings, "prompt text", model="gpt-oss-120b", max_tokens=100)

    assert result == "hello from cerebras"


def test_call_llm_cerebras_requires_api_key(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="cerebras"))

    with pytest.raises(RuntimeError, match="CEREBRAS_API_KEY"):
        call_llm(settings, "prompt text", model="gpt-oss-120b", max_tokens=100)


def test_gemini_key_goes_in_a_header_and_never_into_an_error_message(tmp_path):
    """A ?key= query param leaks into httpx's HTTPStatusError message, which
    this module wraps into LlmCallError -> record.error ->
    data/inbox/needs-attention.txt. That wrote the real key to disk twice on
    2026-08-12. Covers the vision path too, and asserts on the failure path
    specifically - that is the one that actually leaked.
    """
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="gemini"))
    settings.gemini_api_key = "gemini-secret-value"
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8\xff\xdb-not-a-real-jpeg")
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    )

    with respx.mock:
        route = respx.post(endpoint).mock(return_value=httpx.Response(503))
        for call in (
            lambda: call_llm(settings, "p", model="gemini-2.5-flash", max_tokens=10),
            lambda: describe_images(
                settings, "p", [image], model="gemini-2.5-flash", max_tokens=10
            ),
        ):
            with pytest.raises(LlmCallError) as excinfo:
                call()
            assert "gemini-secret-value" not in str(excinfo.value)

    assert route.call_count == 2
    for sent in route.calls:
        assert "key=" not in str(sent.request.url)
        assert sent.request.headers["x-goog-api-key"] == "gemini-secret-value"


def test_call_llm_dispatches_to_gemini(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="gemini"))
    settings.gemini_api_key = "gemini-test-key"

    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "hello from gemini"}]}}]},
            )
        )
        result = call_llm(settings, "prompt text", model="gemini-2.5-flash", max_tokens=100)

    assert result == "hello from gemini"


def test_call_llm_gemini_max_tokens_exhaustion_raises_diagnosable_error(tmp_path):
    # Gemini 2.5's internal "thinking" can consume the whole max_tokens budget
    # before any answer text - finishReason="MAX_TOKENS" with empty parts.
    # Found live 2026-08-12 testing the vision path; the error should say what
    # actually happened instead of dumping the raw response payload.
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="gemini"))
    settings.gemini_api_key = "gemini-test-key"

    with respx.mock:
        respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"candidates": [{"content": {"role": "model"}, "finishReason": "MAX_TOKENS"}]},
            )
        )
        with pytest.raises(LlmCallError, match="max_tokens was exhausted"):
            call_llm(settings, "prompt text", model="gemini-2.5-flash", max_tokens=50)


def test_call_llm_groq_json_mode_sets_response_format(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
        )
        call_llm(
            settings, "prompt text", model="llama-3.1-8b-instant", max_tokens=100, json_mode=True
        )

    sent = json.loads(route.calls.last.request.content)
    assert sent["response_format"] == {"type": "json_object"}
    # "raw" reasoning_format isn't supported alongside JSON mode.
    assert "reasoning_format" not in sent


def test_call_llm_groq_omits_response_format_by_default(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-test"

    with respx.mock:
        route = respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})
        )
        call_llm(settings, "prompt text", model="llama-3.1-8b-instant", max_tokens=100)

    assert "response_format" not in json.loads(route.calls.last.request.content)


def test_call_llm_gemini_json_mode_sets_response_mime_type(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="gemini"))
    settings.gemini_api_key = "gemini-test-key"

    with respx.mock:
        route = respx.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        ).mock(
            return_value=httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]},
            )
        )
        call_llm(settings, "prompt text", model="gemini-2.5-flash", max_tokens=100, json_mode=True)

    sent = json.loads(route.calls.last.request.content)
    assert sent["generationConfig"]["response_mime_type"] == "application/json"


def test_call_llm_raises_for_unknown_provider(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="not-a-real-provider"))

    with pytest.raises(ValueError, match="not-a-real-provider"):
        call_llm(settings, "prompt text", model="whatever", max_tokens=100)


def test_seconds_to_wait_is_zero_on_first_call():
    assert _seconds_to_wait(min_interval=2.0, elapsed_since_last_call=None) == 0.0


def test_seconds_to_wait_is_zero_when_interval_disabled():
    assert _seconds_to_wait(min_interval=0.0, elapsed_since_last_call=0.0) == 0.0


def test_seconds_to_wait_returns_remaining_gap():
    assert _seconds_to_wait(min_interval=2.0, elapsed_since_last_call=0.5) == 1.5


def test_seconds_to_wait_is_zero_once_interval_has_elapsed():
    assert _seconds_to_wait(min_interval=2.0, elapsed_since_last_call=5.0) == 0.0


def test_call_llm_throttles_consecutive_calls_to_same_provider(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="ollama", min_interval_seconds={"ollama": 0.2}),
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "ok"})
        )
        start = time.monotonic()
        call_llm(settings, "first", model="m", max_tokens=10)
        call_llm(settings, "second", model="m", max_tokens=10)
        elapsed = time.monotonic() - start

    assert elapsed >= 0.2


def test_rate_limit_wait_honours_retry_after_header():
    response = httpx.Response(429, headers={"retry-after": "7"})
    assert _rate_limit_wait(response, attempt=0) == 7.0


def test_rate_limit_wait_backs_off_without_a_usable_retry_after():
    # An HTTP-date Retry-After (also legal) is unparseable as seconds, so it
    # must fall back to the schedule rather than blow up.
    response = httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert _rate_limit_wait(response, attempt=0) == 5.0
    assert _rate_limit_wait(response, attempt=2) == 20.0


def test_call_llm_retries_a_429_and_succeeds(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="cerebras", min_interval_seconds={"cerebras": 0.0}),
    )
    settings.cerebras_api_key = "csk-test"
    monkeypatch.setattr("reel_pipeline.llm_client.time.sleep", lambda _seconds: None)

    with respx.mock:
        route = respx.post("https://api.cerebras.ai/v1/chat/completions").mock(
            side_effect=[
                httpx.Response(429, headers={"retry-after": "1"}),
                httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]}),
            ]
        )
        result = call_llm(settings, "prompt", model="gpt-oss-120b", max_tokens=100)

    assert result == "recovered"
    assert route.call_count == 2


def test_call_llm_gives_up_on_a_persistent_429(tmp_path, monkeypatch):
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="cerebras", min_interval_seconds={"cerebras": 0.0}),
    )
    settings.cerebras_api_key = "csk-test"
    monkeypatch.setattr("reel_pipeline.llm_client.time.sleep", lambda _seconds: None)

    with respx.mock:
        route = respx.post("https://api.cerebras.ai/v1/chat/completions").mock(
            return_value=httpx.Response(429)
        )
        with pytest.raises(LlmCallError, match="429"):
            call_llm(settings, "prompt", model="gpt-oss-120b", max_tokens=100)

    assert route.call_count == 1 + 3  # initial attempt plus _RATE_LIMIT_RETRIES


def test_call_llm_does_not_sleep_out_a_quota_length_retry_after(tmp_path, monkeypatch):
    """A daily-quota Retry-After must fail fast for the item-level retry to
    handle, not stall the whole run_once() pass.
    """
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="cerebras", min_interval_seconds={"cerebras": 0.0}),
    )
    settings.cerebras_api_key = "csk-test"
    slept: list[float] = []
    monkeypatch.setattr("reel_pipeline.llm_client.time.sleep", slept.append)

    with respx.mock:
        route = respx.post("https://api.cerebras.ai/v1/chat/completions").mock(
            return_value=httpx.Response(429, headers={"retry-after": "3600"})
        )
        with pytest.raises(LlmCallError, match="429"):
            call_llm(settings, "prompt", model="gpt-oss-120b", max_tokens=100)

    assert route.call_count == 1
    assert slept == []


def test_http_error_message_includes_the_provider_response_body(tmp_path):
    """httpx's HTTPStatusError string is only "Client error '400 Bad Request'
    for url ..." - the body, which is the part that says *why*, is dropped. The
    Ollama vision path then appended its own guess ("Is Ollama running, and is
    the model pulled?"), so needs-attention.txt recorded a cause that was not
    merely vague but wrong: verified 2026-08-21 that Ollama was running and the
    model was pulled, while the real error was a context overflow on a 20-image
    carousel. Every provider goes through _post_json, so this is asserted once.
    """
    settings = Settings(
        project_root=tmp_path,
        llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434"),
    )
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"fake-jpg-bytes")

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(
                400,
                json={
                    "error": {
                        "code": 400,
                        "message": (
                            "request (20667 tokens) exceeds the available "
                            "context size (16384 tokens), try increasing it"
                        ),
                        "type": "exceed_context_size_error",
                    }
                },
            )
        )
        with pytest.raises(LlmCallError, match="exceeds the available context size"):
            describe_images(settings, "p", [image], model="mistral-small3.1", max_tokens=100)


def test_http_error_body_does_not_leak_an_api_key_the_provider_echoed_back(tmp_path):
    """Including the response body re-opens the leak path closed on 2026-08-12
    (LlmCallError -> record.error -> needs-attention.txt on disk) if a provider
    quotes the credential back in its error text. Auth always travels as a
    request header here, so any header value appearing in the body is scrubbed.
    """
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="groq"))
    settings.groq_api_key = "gsk-secret-value-1234"

    with respx.mock:
        respx.post("https://api.groq.com/openai/v1/chat/completions").mock(
            return_value=httpx.Response(
                401,
                json={"error": {"message": "Invalid API key: gsk-secret-value-1234"}},
            )
        )
        with pytest.raises(LlmCallError) as excinfo:
            call_llm(settings, "prompt text", model="openai/gpt-oss-120b", max_tokens=10)

    message = str(excinfo.value)
    assert "gsk-secret-value-1234" not in message
    # The body still has to arrive - proving it was included, then scrubbed,
    # rather than dropped wholesale (which would pass the leak check trivially).
    assert "Invalid API key" in message
