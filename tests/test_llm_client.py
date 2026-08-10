from __future__ import annotations

import json

import httpx
import pytest
import respx

from reel_pipeline.config import LlmConfig, Settings
from reel_pipeline.llm_client import LlmCallError, call_llm


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

    sent_body = route.calls.last.request.content
    assert json.loads(sent_body)["response_format"] == {"type": "json_object"}


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
