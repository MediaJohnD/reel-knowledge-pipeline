from __future__ import annotations

import httpx
import pytest
import respx

from reel_pipeline.config import LlmConfig, Settings
from reel_pipeline.llm_client import LlmCallError, call_llm


def test_call_llm_dispatches_to_ollama(tmp_path):
    settings = Settings(
        project_root=tmp_path, llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434")
    )

    with respx.mock:
        respx.post("http://localhost:11434/api/generate").mock(
            return_value=httpx.Response(200, json={"response": "hello from ollama"})
        )
        result = call_llm(settings, "prompt text", model="qwen2.5:14b", max_tokens=100)

    assert result == "hello from ollama"


def test_call_llm_ollama_raises_clear_error_on_failure(tmp_path):
    settings = Settings(
        project_root=tmp_path, llm=LlmConfig(provider="ollama", ollama_host="http://localhost:11434")
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


def test_call_llm_raises_for_unknown_provider(tmp_path):
    settings = Settings(project_root=tmp_path, llm=LlmConfig(provider="not-a-real-provider"))

    with pytest.raises(ValueError, match="not-a-real-provider"):
        call_llm(settings, "prompt text", model="whatever", max_tokens=100)
