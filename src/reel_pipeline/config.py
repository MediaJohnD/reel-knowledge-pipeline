"""Typed configuration, loaded from config/settings.yaml with environment overrides.

Non-secret values (paths, model names, allowed domains) live in
config/settings.yaml so they're versioned and reviewable. Secrets (API keys,
webhook shared secret) are only ever read from environment variables /.env -
never hardcoded and never written to settings.yaml.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class PathsConfig(BaseModel):
    inbox_dir: str = "data/inbox"
    queue_file: str = "data/inbox/queue.txt"
    state_file: str = "data/inbox/state.json"
    needs_attention_file: str = "data/inbox/needs-attention.txt"
    logs_dir: str = "data/logs"
    tmp_dir: str = "data/tmp"
    vault_dir: str = "data/notes"
    skills_dir: str = "data/generated_skills"


class PromptsConfig(BaseModel):
    enrich_transcript: str = "config/prompts/enrich_transcript.md"
    enrich_text_capture: str = "config/prompts/enrich_text_capture.md"
    create_skill: str = "config/prompts/create_skill.md"
    describe_image_post: str = "config/prompts/describe_image_post.md"


class TranscriptionConfig(BaseModel):
    backend: str = "local"  # "local" | "openai"
    local_model_size: str = "base"
    openai_model: str = "whisper-1"
    # Passed straight through to faster-whisper's WhisperModel(device=..., compute_type=...) -
    # both libraries already support "auto"/"default" as an auto-select sentinel, so no
    # translation layer is needed here. Override either if auto-detection picks wrong on
    # a given machine. device: "auto"|"cpu"|"cuda". compute_type: "default"|"int8"|"float16"|...
    device: str = "auto"
    compute_type: str = "default"


class LlmConfig(BaseModel):
    provider: str = "anthropic"  # "anthropic" | "ollama" | "groq" | "gemini"
    ollama_host: str = "http://localhost:11434"
    # Ollama defaults every request to a 4096-token context window regardless of
    # the model's actual capacity, silently truncating (or, for vision requests,
    # erroring) on long transcripts or multi-image carousels. Set generously -
    # this only affects memory use for the duration of a request, not model choice.
    ollama_num_ctx: int = 16384


class EnrichmentConfig(BaseModel):
    # Model name meaning depends on llm.provider: an Anthropic model id (e.g.
    # "claude-sonnet-5") when provider is "anthropic", or a locally-pulled
    # Ollama model tag (e.g. "qwen2.5:14b") when provider is "ollama".
    model: str = "claude-sonnet-5"
    max_tokens: int = 4096


class SkillWriterConfig(BaseModel):
    model: str = "claude-sonnet-5"
    max_tokens: int = 2048
    enabled: bool = True


class ImageDescriptionConfig(BaseModel):
    # Must be a vision-capable model - e.g. an Ollama model tagged "vision" in
    # `ollama list` (mistral-small3.1, llava, qwen2-vl, ...), or any current
    # Claude model (all support image input) when llm.provider is "anthropic".
    model: str = "mistral-small3.1"
    max_tokens: int = 1024


class DownloadConfig(BaseModel):
    allowed_domains: list[str] = Field(default_factory=list)
    blocked_domains: list[str] = Field(default_factory=list)


class TextCaptureConfig(BaseModel):
    # Domains routed to TextFetcher instead of Downloader - see validators.classify_url_kind().
    # Kept deliberately separate from download.allowed_domains: these aren't media
    # platforms, and mixing the two lists would make classify_url_kind() ambiguous.
    allowed_domains: list[str] = Field(default_factory=list)


class WebhookConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787


class MaintenanceConfig(BaseModel):
    # Bounds disk/log growth without needing an external cron job - both sweeps run
    # at the top of every run_once(), which already fires on every webhook hit and
    # queue check. Set either to 0 to disable that sweep entirely.
    tmp_retention_days: int = 30
    needs_attention_retention_days: int = 30
    # state.json is fully reparsed and rewritten on every single mutation
    # (O(n) per stage transition, so O(n^2) over a full backlog pass) - a
    # deliberate, documented tradeoff at this project's "handful of reels"
    # scale (see docs/superpowers/specs/2026-07-29-state-reliability-design.md's
    # "Deferred" section: a SQLite/WAL migration would fix it but costs
    # state.json's human-readability). This threshold turns that into a
    # *monitored* deferral instead of a silent one - once item count reaches
    # it, run_once() logs a warning so growth beyond this project's original
    # assumptions gets noticed instead of just slowly degrading. Set to 0 to
    # disable the check.
    state_size_warning_threshold: int = 500


class RetryConfig(BaseModel):
    # Exponential backoff in minutes, indexed by (attempt_count - 1); clamped to
    # the last entry once attempt_count exceeds the schedule's length (only
    # possible if max_attempts > len(backoff_schedule_minutes)). min_length=1 is
    # required: worker.py computes schedule[min(attempt_count, len(schedule)) - 1],
    # which would raise IndexError on an empty list - inside the exception handler
    # itself, aborting the whole run_once() pass instead of just failing the one
    # item. Catch the misconfiguration at config-load time instead.
    backoff_schedule_minutes: list[int] = Field(
        default_factory=lambda: [1, 5, 30, 120, 480], min_length=1
    )
    # After this many failed attempts, an item's status becomes FAILED_PERMANENT
    # instead of FAILED, so run_once() stops retrying it automatically.
    max_attempts: int = 5


class Settings(BaseModel):
    """Root configuration object used throughout the pipeline."""

    project_root: Path = PROJECT_ROOT
    # Operational control, not a code change - flip to DEBUG via REEL_LOG_LEVEL when
    # diagnosing a stuck run, no restart-with-edited-source needed.
    log_level: str = "INFO"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    prompts: PromptsConfig = Field(default_factory=PromptsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    enrichment: EnrichmentConfig = Field(default_factory=EnrichmentConfig)
    skill_writer: SkillWriterConfig = Field(default_factory=SkillWriterConfig)
    image_description: ImageDescriptionConfig = Field(default_factory=ImageDescriptionConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    text_capture: TextCaptureConfig = Field(default_factory=TextCaptureConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)

    # Secrets - never sourced from settings.yaml, only environment variables.
    webhook_secret: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    groq_api_key: str | None = None
    gemini_api_key: str | None = None

    # Instagram requires an authenticated session to download reliably (see
    # docs/runbook.md). Treated like a secret - a cookies file/browser choice is as
    # sensitive as a session token - so it's env-only, never written to settings.yaml.
    instagram_cookies_file: str | None = None
    instagram_cookies_browser: str | None = None

    # Generic cookies for everything yt-dlp handles (YouTube, Facebook, LinkedIn,
    # TikTok, X/Twitter, Vimeo) - unlike Instagram's, these are optional: public
    # YouTube already works anonymously, but Facebook and LinkedIn generally gate
    # most content behind a login the same way Instagram does. Same env-only,
    # never-in-settings.yaml treatment as the Instagram cookies above.
    ytdlp_cookies_file: str | None = None
    ytdlp_cookies_browser: str | None = None

    model_config = {"arbitrary_types_allowed": True}

    def resolve(self, relative_path: str) -> Path:
        """Resolve a configured path against the project root (absolute paths pass through)."""
        path = Path(relative_path)
        return path if path.is_absolute() else self.project_root / path

    @property
    def inbox_dir(self) -> Path:
        return self.resolve(self.paths.inbox_dir)

    @property
    def queue_file(self) -> Path:
        return self.resolve(self.paths.queue_file)

    @property
    def state_file(self) -> Path:
        return self.resolve(self.paths.state_file)

    @property
    def needs_attention_file(self) -> Path:
        return self.resolve(self.paths.needs_attention_file)

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.paths.logs_dir)

    @property
    def tmp_dir(self) -> Path:
        return self.resolve(self.paths.tmp_dir)

    @property
    def vault_dir(self) -> Path:
        return self.resolve(self.paths.vault_dir)

    @property
    def skills_dir(self) -> Path:
        return self.resolve(self.paths.skills_dir)

    @property
    def enrich_transcript_prompt(self) -> Path:
        return self.resolve(self.prompts.enrich_transcript)

    @property
    def enrich_text_capture_prompt(self) -> Path:
        return self.resolve(self.prompts.enrich_text_capture)

    @property
    def create_skill_prompt(self) -> Path:
        return self.resolve(self.prompts.create_skill)

    @property
    def describe_image_post_prompt(self) -> Path:
        return self.resolve(self.prompts.describe_image_post)

    def require_webhook_secret(self) -> str:
        if not self.webhook_secret:
            raise RuntimeError(
                "REEL_WEBHOOK_SECRET is not set. Set it in your environment or .env "
                "before starting the webhook server."
            )
        return self.webhook_secret

    def require_anthropic_api_key(self) -> str:
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Set it in your environment or .env "
                "before running enrichment or skill generation."
            )
        return self.anthropic_api_key

    def require_groq_api_key(self) -> str:
        if not self.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Set it in your environment or .env "
                "before running enrichment or skill generation with llm.provider: groq."
            )
        return self.groq_api_key

    def require_gemini_api_key(self) -> str:
        if not self.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Set it in your environment or .env "
                "before running enrichment or skill generation with llm.provider: gemini."
            )
        return self.gemini_api_key

    def require_instagram_cookies(self) -> tuple[str, str]:
        """Returns (kind, value): ("file", path) or ("browser", browser_name)."""
        if self.instagram_cookies_file:
            return "file", self.instagram_cookies_file
        if self.instagram_cookies_browser:
            return "browser", self.instagram_cookies_browser
        raise RuntimeError(
            "Instagram downloads require a logged-in session. Set "
            "REEL_INSTAGRAM_COOKIES_FILE (a Netscape-format cookies.txt) or "
            "REEL_INSTAGRAM_COOKIES_BROWSER (e.g. 'chrome') in .env - see docs/runbook.md."
        )

    def optional_ytdlp_cookies(self) -> tuple[str, str] | None:
        """Returns (kind, value) if configured, else None - unlike Instagram's cookies,
        these are optional: public YouTube works without them, but gated platforms
        (Facebook, LinkedIn) will fail with a clear "log in" error from yt-dlp itself
        if content requires auth and none is configured.
        """
        if self.ytdlp_cookies_file:
            return "file", self.ytdlp_cookies_file
        if self.ytdlp_cookies_browser:
            return "browser", self.ytdlp_cookies_browser
        return None

    def ensure_directories(self) -> None:
        for directory in (
            self.inbox_dir,
            self.logs_dir,
            self.tmp_dir,
            self.vault_dir,
            self.skills_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


# Explicit environment override keys. Kept as a plain mapping (rather than generic
# prefix-scanning) so overrides are grep-able and typo-proof.
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "REEL_VAULT_DIR": ("paths", "vault_dir"),
    "REEL_SKILLS_DIR": ("paths", "skills_dir"),
    "REEL_TRANSCRIPTION_BACKEND": ("transcription", "backend"),
    "REEL_WEBHOOK_HOST": ("webhook", "host"),
    "REEL_WEBHOOK_PORT": ("webhook", "port"),
    "REEL_LLM_PROVIDER": ("llm", "provider"),
    "REEL_OLLAMA_HOST": ("llm", "ollama_host"),
    "REEL_LOG_LEVEL": ("log_level",),
}


def _apply_env_overrides(raw: dict, env: dict[str, str]) -> dict:
    for env_key, path in _ENV_OVERRIDES.items():
        value = env.get(env_key)
        if value is None or value == "":
            continue
        node = raw
        for key in path[:-1]:
            node = node.setdefault(key, {})
        leaf = path[-1]
        node[leaf] = int(value) if leaf == "port" else value
    return raw


def load_settings(
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    """Load Settings from a YAML file plus environment overrides.

    Args:
        config_path: path to settings.yaml. Defaults to config/settings.yaml at the project root.
        env: environment mapping to read overrides/secrets from. Defaults to os.environ,
            after loading PROJECT_ROOT/.env into it (real .env values never override
            already-set process env vars). Pass an explicit dict (e.g. in tests) to
            bypass .env loading entirely.
    """
    if env is None:
        load_dotenv(PROJECT_ROOT / ".env")
        env = dict(os.environ)
    else:
        env = dict(env)
    config_path = config_path or DEFAULT_SETTINGS_PATH

    raw: dict = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        raw = loaded or {}

    raw = _apply_env_overrides(raw, env)

    return Settings(
        log_level=raw.get("log_level", "INFO"),
        paths=PathsConfig(**raw.get("paths", {})),
        prompts=PromptsConfig(**raw.get("prompts", {})),
        transcription=TranscriptionConfig(**raw.get("transcription", {})),
        llm=LlmConfig(**raw.get("llm", {})),
        enrichment=EnrichmentConfig(**raw.get("enrichment", {})),
        skill_writer=SkillWriterConfig(**raw.get("skill_writer", {})),
        image_description=ImageDescriptionConfig(**raw.get("image_description", {})),
        download=DownloadConfig(**raw.get("download", {})),
        text_capture=TextCaptureConfig(**raw.get("text_capture", {})),
        webhook=WebhookConfig(**raw.get("webhook", {})),
        maintenance=MaintenanceConfig(**raw.get("maintenance", {})),
        retry=RetryConfig(**raw.get("retry", {})),
        webhook_secret=env.get("REEL_WEBHOOK_SECRET") or None,
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        openai_api_key=env.get("OPENAI_API_KEY") or None,
        groq_api_key=env.get("GROQ_API_KEY") or None,
        gemini_api_key=env.get("GEMINI_API_KEY") or None,
        instagram_cookies_file=env.get("REEL_INSTAGRAM_COOKIES_FILE") or None,
        instagram_cookies_browser=env.get("REEL_INSTAGRAM_COOKIES_BROWSER") or None,
        ytdlp_cookies_file=env.get("REEL_YTDLP_COOKIES_FILE") or None,
        ytdlp_cookies_browser=env.get("REEL_YTDLP_COOKIES_BROWSER") or None,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached process-wide Settings, loaded from the default location.

    Tests and callers needing custom config/env should use load_settings() directly
    instead of this cached accessor.
    """
    return load_settings()
