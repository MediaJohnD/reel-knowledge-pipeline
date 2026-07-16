# Project memory for Claude Code

## Mission
Build and maintain a safe-by-default short-form and video knowledge ingestion system that turns URLs into structured Obsidian notes and optional Claude/Codex skill artifacts.

## Primary workflow
The system supports:
1. URL ingestion from a queue file (`data/inbox/queue.txt`) and webhook (`POST /webhook`)
2. media download (`downloader.py`, via yt-dlp or gallery-dl for Instagram) - detects photo-only posts/carousels vs. video
3. content extraction: audio transcription for video (`transcriber.py`, local faster-whisper or OpenAI API) or vision-model description for photo posts (`image_describer.py`) - both produce the same `TranscriptResult` shape
4. transcript enrichment (`enricher.py`, LLM via `llm_client.py` - local Ollama by default, or Claude - + `config/prompts/enrich_transcript.md`)
5. Obsidian markdown note writing (`obsidian_writer.py`)
6. optional skill generation for high-signal content (`skill_writer.py`)
7. logging, idempotency, and recovery (`logging_setup.py`, `queue_manager.py`, `worker.py`)

## Risk posture
- No browser automation, ever - only cookie-authenticated CLI tools (yt-dlp, gallery-dl).
- Instagram is explicitly enabled as of 2026-07-11 - a deliberate, scoped exception to the
  general "no Instagram by default" baseline, made for the project owner's own real account
  (no burner) at low volume ("a handful of reels that matter", not bulk/scheduled scraping).
  See `docs/runbook.md`'s "Instagram setup" section and the research note in the Obsidian
  vault (`20-Resources/Tools/Instagram Reel Ingestion Research (2026-07-11).md`).
- Facebook and LinkedIn are enabled as of 2026-07-14 under the same terms as Instagram -
  cookie-authenticated via yt-dlp (`REEL_YTDLP_COOKIES_FILE`/`REEL_YTDLP_COOKIES_BROWSER`,
  optional since public YouTube works without them), owner's own real account, low volume.
  LinkedIn cookies are effectively inert as of yt-dlp 2026.07.04 (the pinned version), which
  removed LinkedIn login support entirely (upstream commit a5e0f87, issue #17039). Separately,
  yt-dlp's LinkedIn extractor only recognizes `linkedin.com/posts/...` and
  `linkedin.com/feed/update/urn:li:activity:<id>` URLs - `urn:li:groupPost:...` URLs (Group
  posts) aren't matched at all and 404 via the generic extractor. Both are verified, specific
  gaps (see `src/reel_pipeline/downloader.py`'s module docstring), not a config problem or
  vague "historically unreliable" hand-wave. YouTube/TikTok/X/Vimeo remain anonymous
  (no cookies configured/required) since they're public by default.
- `drive.google.com` is enabled as of 2026-07-16, scoped to that exact subdomain (not all of
  `google.com` - Docs/Sheets/Photos/Search stay unreachable) for video files shared via Google
  Drive, using yt-dlp's existing GoogleDrive extractor. Same optional cookie treatment as
  Facebook/LinkedIn. `validate_url` in `validators.py` matches `allowed_domains` against the
  full host, not a reduced two-label domain, specifically so a subdomain entry like this one
  doesn't accidentally open the whole parent domain.
- Text-capture ingestion (GitHub repos/files, public Notion pages) is enabled as of
  2026-07-16, scoped to `github.com`, `notion.so`, and `notion.site` in
  `text_capture.allowed_domains` - a separate allow-list from `download.allowed_domains`,
  since these aren't media platforms and go through `text_fetcher.py` (GitHub's public
  REST API, plain HTTP GET + `trafilatura` extraction for Notion) instead of
  yt-dlp/gallery-dl. Public links only - no OAuth or API keys, matching the project's
  no-account-automation posture. Airtable was explicitly evaluated and excluded: its
  public share views require JS execution to render real content, which conflicts with
  the no-browser-automation rule above. See
  `docs/superpowers/specs/2026-07-16-text-capture-ingestion-design.md`.
- Any `download.blocked_domains` entries are still enforced up front - blocked URLs route
  to `data/inbox/needs-attention.txt`, never downloaded.
- Treat any other third-party account automation as out of scope unless explicitly requested.
- Never hardcode secrets, cookies, or personal absolute paths. Secrets (including the
  Instagram/yt-dlp cookies file/browser choice) live only in environment variables (`.env`,
  never committed) - see `.env.example`.

## Architecture rules
- Capture, queue, downloader, transcriber, enricher, note writer, and skill writer stay in separate modules under `src/reel_pipeline/` (see `docs/architecture.md`).
- Prompts live in versioned markdown files in `config/prompts/`, never as inline strings.
- Use typed config (`config.py`) and typed models (`models.py`).
- The worker (`worker.py`) is restart-safe and idempotent: `state.json` is the single source of truth for dedup and in-flight status, keyed by a deterministic `content_id`.
- Note and skill filenames are deterministic (`<content_id>-<title-slug>`), so re-processing overwrites rather than duplicates.

## Output contracts
Each Obsidian note includes:
- title
- source_url
- content_id
- created_at
- tags
- tools_mentioned
- summary
- key_takeaways
- full transcript

Skill artifacts (`SKILL.md`, under `data/generated_skills/`) are created only when enrichment marks the content `high_signal` **and** supplies a `skill_candidate_reason`.

## Verification
Do not claim success without:
- lockfile in sync (`uv lock --check`)
- lint (`uv run ruff check .`)
- typecheck (`uv run pyright`)
- tests (`uv run pytest`)
- dependency vulnerability scan (`uv audit`)
- at least one realistic worker-flow verification (see `tests/test_worker_flow.py`, or a manual `run-once` with real credentials)

`make check` runs all of the above except the worker-flow verification.

## Commands
- Install: `uv sync` (add `--extra local-whisper` for the local transcription backend; dev tooling installs by default via `[dependency-groups]`, no `--extra` needed)
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Typecheck: `uv run pyright`
- Audit dependencies: `uv audit`
- Run worker once: `uv run python -m reel_pipeline.cli run-once`
- Run webhook server: `uv run python -m reel_pipeline.cli serve-webhook`

## Completion checklist
- request addressed directly
- code follows repo patterns
- tests and checks run
- diff reviewed for accidental scope creep
- risks summarized
