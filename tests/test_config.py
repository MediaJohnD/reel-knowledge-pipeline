from __future__ import annotations

import pytest

from reel_pipeline.config import DEFAULT_SETTINGS_PATH, load_settings


def test_loads_defaults_from_settings_yaml():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert settings.transcription.backend == "local"
    assert settings.webhook.port == 8787
    assert "instagram.com" in settings.download.allowed_domains
    assert "youtube.com" in settings.download.allowed_domains
    assert "facebook.com" in settings.download.allowed_domains
    assert "linkedin.com" in settings.download.allowed_domains
    assert settings.webhook_secret is None
    assert settings.anthropic_api_key is None
    assert settings.instagram_cookies_file is None
    assert settings.instagram_cookies_browser is None
    assert settings.ytdlp_cookies_file is None
    assert settings.ytdlp_cookies_browser is None


def test_env_overrides_take_precedence(tmp_path):
    env = {
        "REEL_VAULT_DIR": str(tmp_path / "custom-vault"),
        "REEL_TRANSCRIPTION_BACKEND": "openai",
        "REEL_WEBHOOK_PORT": "9999",
        "REEL_WEBHOOK_SECRET": "shh",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "OPENAI_API_KEY": "sk-openai-test",
    }
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env=env)

    assert settings.paths.vault_dir == str(tmp_path / "custom-vault")
    assert settings.transcription.backend == "openai"
    assert settings.webhook.port == 9999
    assert settings.webhook_secret == "shh"
    assert settings.anthropic_api_key == "sk-ant-test"
    assert settings.openai_api_key == "sk-openai-test"


def test_missing_config_file_falls_back_to_model_defaults(tmp_path):
    settings = load_settings(config_path=tmp_path / "does-not-exist.yaml", env={})

    assert settings.transcription.backend == "local"
    assert settings.webhook.port == 8787


def test_require_webhook_secret_raises_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    with pytest.raises(RuntimeError, match="REEL_WEBHOOK_SECRET"):
        settings.require_webhook_secret()


def test_require_anthropic_api_key_raises_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        settings.require_anthropic_api_key()


def test_require_groq_api_key_raises_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        settings.require_groq_api_key()


def test_require_gemini_api_key_raises_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        settings.require_gemini_api_key()


def test_require_instagram_cookies_raises_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    with pytest.raises(RuntimeError, match="REEL_INSTAGRAM_COOKIES"):
        settings.require_instagram_cookies()


def test_require_instagram_cookies_prefers_file_over_browser():
    settings = load_settings(
        config_path=DEFAULT_SETTINGS_PATH,
        env={
            "REEL_INSTAGRAM_COOKIES_FILE": "/tmp/cookies.txt",
            "REEL_INSTAGRAM_COOKIES_BROWSER": "chrome",
        },
    )

    assert settings.require_instagram_cookies() == ("file", "/tmp/cookies.txt")


def test_require_instagram_cookies_falls_back_to_browser():
    settings = load_settings(
        config_path=DEFAULT_SETTINGS_PATH,
        env={"REEL_INSTAGRAM_COOKIES_BROWSER": "chrome"},
    )

    assert settings.require_instagram_cookies() == ("browser", "chrome")


def test_optional_ytdlp_cookies_returns_none_when_unset():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert settings.optional_ytdlp_cookies() is None


def test_optional_ytdlp_cookies_prefers_file_over_browser():
    settings = load_settings(
        config_path=DEFAULT_SETTINGS_PATH,
        env={
            "REEL_YTDLP_COOKIES_FILE": "/tmp/ytdlp-cookies.txt",
            "REEL_YTDLP_COOKIES_BROWSER": "chrome",
        },
    )

    assert settings.optional_ytdlp_cookies() == ("file", "/tmp/ytdlp-cookies.txt")


def test_optional_ytdlp_cookies_falls_back_to_browser():
    settings = load_settings(
        config_path=DEFAULT_SETTINGS_PATH,
        env={"REEL_YTDLP_COOKIES_BROWSER": "chrome"},
    )

    assert settings.optional_ytdlp_cookies() == ("browser", "chrome")


def test_resolved_paths_are_absolute_under_project_root(tmp_path):
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})
    settings.project_root = tmp_path

    assert settings.queue_file == tmp_path / "data" / "inbox" / "queue.txt"
    assert settings.vault_dir == tmp_path / "data" / "notes"


def test_loads_text_capture_defaults_from_settings_yaml():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert settings.text_capture.blocked_domains == []
    assert settings.enrich_text_capture_prompt.name == "enrich_text_capture.md"
    assert settings.text_capture.render_fallback.enabled is True
    assert settings.text_capture.render_fallback.timeout_seconds == 20


def test_render_fallback_rejects_zero_timeout():
    """Playwright treats timeout=0 as "disabled", not "instant" - a
    0-or-negative timeout_seconds would silently defeat the whole point of
    a bounded fallback rather than erroring loudly (found by review,
    2026-08-10).
    """
    from pydantic import ValidationError

    from reel_pipeline.config import RenderFallbackConfig

    with pytest.raises(ValidationError):
        RenderFallbackConfig(timeout_seconds=0)


def test_loads_retry_defaults_from_settings_yaml():
    settings = load_settings(config_path=DEFAULT_SETTINGS_PATH, env={})

    assert settings.retry.max_attempts == 5
    assert settings.retry.backoff_schedule_minutes == [1, 5, 30, 120, 480]


def test_retry_config_overridable_from_yaml(tmp_path):
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(
        "retry:\n  max_attempts: 3\n  backoff_schedule_minutes: [2, 10]\n",
        encoding="utf-8",
    )

    settings = load_settings(config_path=config_path, env={})

    assert settings.retry.max_attempts == 3
    assert settings.retry.backoff_schedule_minutes == [2, 10]
