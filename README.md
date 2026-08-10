# Reel Knowledge Pipeline

[![CI](https://github.com/MediaJohnD/reel-knowledge-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/MediaJohnD/reel-knowledge-pipeline/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

A safe-by-default pipeline that turns a video, reel, photo post, or article
URL into a structured [Obsidian](https://obsidian.md) note — and, for
high-signal content, a reusable Claude/Codex skill artifact.

Feed it a URL (via a queue file or webhook). It downloads the media,
transcribes or vision-describes it, enriches the transcript into structured
metadata with an LLM, writes an Obsidian note with a consistent frontmatter
contract, and — only when the content clears a "high signal" bar — generates
a `SKILL.md` you can hand to an agent.

## Why this exists

Watching something worth remembering shouldn't mean it evaporates the moment
you close the app. This pipeline is the boring, deterministic machinery
between "I saw a good video" and "I have a searchable, tagged note about it
in my second brain" — so you can spend your attention on the content, not on
transcribing and summarizing it by hand.

## Table of contents

- [Features](#features)
- [Quickstart](#quickstart)
- [Commands](#commands)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Safety and scope](#safety-and-scope)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Multiple ingestion paths** — a plain text file (`data/inbox/queue.txt`),
  an authenticated webhook (for an iOS Shortcut, a bookmarklet, or any HTTP
  client), or GitHub repos/Notion pages/shared Drive documents/any other
  public URL captured as text.
- **Video, photo posts, and carousels** — automatically detects photo-only
  posts vs. video and routes to the right extraction path; multi-clip
  carousels are transcribed per-clip and combined.
- **Pluggable transcription and enrichment** — local
  [faster-whisper](https://github.com/SYSTRAN/faster-whisper) or the OpenAI
  API for transcription; local [Ollama](https://ollama.com), Claude, Groq, or
  Gemini for enrichment — configured per-provider, no code changes needed.
- **Restart-safe and idempotent by construction** — every stage transition is
  durably recorded in `state.json`, keyed by a deterministic content hash. A
  crash mid-pipeline resumes from the last completed stage on the next run
  instead of redoing already-finished (and possibly costly) work.
- **Capped retries with exponential backoff** — a transient failure is
  retried on a schedule, not forever; a permanently-failing item stops
  burning API/download calls after a configurable number of attempts, and is
  logged (not silently dropped) with a one-command recovery path once fixed.
- **Deterministic Obsidian notes** — a fixed frontmatter contract (title,
  source URL, tags, tools mentioned, summary, key takeaways, full
  transcript) and stable, deterministic filenames — no duplicate notes from
  re-processing the same URL.
- **Safe by default** — see [Safety and scope](#safety-and-scope) below.

## Quickstart

```bash
git clone https://github.com/MediaJohnD/reel-knowledge-pipeline.git
cd reel-knowledge-pipeline
cp .env.example .env
# edit .env: set REEL_WEBHOOK_SECRET at minimum
# (enrichment defaults to a local Ollama model - see docs/runbook.md's "LLM provider"
# section to use hosted Claude/Groq/Gemini instead)

uv sync
# uv sync --extra local-whisper   # add this if using the local transcription backend

echo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >> data/inbox/queue.txt
uv run python -m reel_pipeline.cli run-once
```

Notes land in `data/notes/` (or wherever `REEL_VAULT_DIR` points, e.g. your
Obsidian vault). Any high-signal content also gets a `SKILL.md` under
`data/generated_skills/`.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). No database, no
external services beyond whichever LLM/transcription backend you configure.

## Commands

| Command | Purpose |
| --- | --- |
| `uv sync` | Install dependencies (add `--extra local-whisper` for the local transcription backend; dev tooling installs by default) |
| `uv run python -m reel_pipeline.cli run-once` | Drain the queue/webhook backlog once |
| `uv run python -m reel_pipeline.cli serve-webhook` | Start the webhook ingestion server |
| `uv run python -m reel_pipeline.cli retry <content_id>` | Reset a permanently-failed item back to pending so the next `run-once` retries it |
| `uv run python -m reel_pipeline.cli retry --all-failed-permanent` | Same, for every permanently-failed item at once |
| `uv run pytest` | Run tests |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run pyright` | Typecheck |
| `uv audit` | Scan dependencies for known vulnerabilities |
| `uv lock --check` | Verify `uv.lock` matches `pyproject.toml` |

A `Makefile` wraps the verification commands (`make install`, `make test`, `make check`, etc.) —
`make check` runs everything CI runs (lockfile check, lint, typecheck, test, audit).

## Configuration

Non-secret settings live in [`config/settings.yaml`](config/settings.yaml)
(paths, allowed/blocked domains, model names, LLM provider, retry/backoff
policy). Secrets live only in the environment — see
[`.env.example`](.env.example) for the full list (`REEL_WEBHOOK_SECRET`
always; provider API keys only if that provider is selected; Instagram/yt-dlp
cookie paths only if those platforms are enabled). Any `config/settings.yaml`
value can be overridden by the matching `REEL_*` environment variable — see
`src/reel_pipeline/config.py` for the exact list.

Enrichment and skill-generation prompts live in
[`config/prompts/`](config/prompts/) as versioned markdown files, not
hardcoded strings.

See [`docs/runbook.md`](docs/runbook.md) for day-to-day operation, including
platform-specific setup (Instagram, Facebook, LinkedIn, Google Drive) and a
troubleshooting table.

## Architecture

```
data/inbox/queue.txt ---\
                         >-- QueueManager --> data/inbox/state.json (source of truth)
webhook POST /webhook --/                 \-> data/inbox/needs-attention.txt (blocked/invalid/failed URLs)

WorkerPipeline.run_once(), for each actionable record:
  validate/dedup (already done at registration) -> classify text vs. media
    text  -> TextFetcher.fetch(url)                        -> TranscriptResult
    media -> Downloader.download(url) -> branch on media_type:
               VIDEO -> Transcriber.transcribe(...)         -> TranscriptResult
               IMAGE -> ImageDescriber.describe(...)        -> TranscriptResult (vision-model description)
  -> Enricher.enrich(transcript)     -> EnrichmentResult (LLM + config/prompts/)
  -> obsidian_writer.write_note()    -> data/notes/<slug>.md (or REEL_VAULT_DIR)
  -> SkillWriter.generate()          -> data/generated_skills/<slug>/SKILL.md, only if high_signal
  each stage transition persisted to state.json - a crash resumes from the last one, not from scratch
```

See [`docs/architecture.md`](docs/architecture.md) for the full module-by-module
design, and [`docs/runbook.md`](docs/runbook.md) for operational details
(retry/backoff behavior, resumability, log locations, troubleshooting).

### Repository layout

```
config/                 settings.yaml + prompt templates
data/inbox/              queue.txt (input), state.json (durable dedup/status), needs-attention.txt
data/logs/               structured JSON logs
data/notes/              generated Obsidian notes (default vault location)
data/generated_skills/   generated SKILL.md artifacts for high-signal content
src/reel_pipeline/       all business logic (see docs/architecture.md)
tests/                   pytest suite (fakes only - no real network calls)
docs/                    architecture, runbook, acceptance tests
.claude/skills/          this repo's own authoring/maintenance skills (distinct from generated content skills)
```

## Safety and scope

- **No browser automation, ever.** Both downloaders (`yt-dlp`, `gallery-dl`)
  are cookie-authenticated CLI tools, never a driven browser session.
- **Allow-list enforced up front.** Only platforms in
  `download.allowed_domains` are ever attempted; anything in
  `download.blocked_domains` is routed to `needs-attention.txt` instead of
  downloaded.
- **Login-gated platforms are explicit, scoped opt-ins**, not defaults.
  Instagram, Facebook, LinkedIn, and Google Drive are each enabled
  individually via cookie authentication, intended for low-volume personal
  use with your own real account — not bulk or scheduled scraping, and not
  automation of someone else's account. YouTube/TikTok/X/Vimeo work
  anonymously. See `docs/runbook.md`'s per-platform setup sections before
  enabling any of these, and reconsider the tradeoff before raising volume if
  you fork this for a different use case.
- **Text capture is separate from media download, and open by default.** Any
  URL that isn't a media platform is fetched as plain text via a public REST
  API (GitHub) or a plain HTTP GET + `trafilatura` extraction (everything
  else) — no OAuth, no API keys, no browser automation, no JS execution.
  `text_capture.blocked_domains` is the escape hatch for excluding a specific
  domain from that default.
- **Secrets never live in config.** `config/settings.yaml` is safe to commit
  and never contains a secret; everything sensitive (webhook secret, LLM API
  keys, cookie file paths) is read from the environment via `.env` (never
  committed — only `.env.example`'s placeholders are versioned).

The full policy this repository is built against — including the reasoning
behind each platform decision — is documented in [`CLAUDE.md`](CLAUDE.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for development setup, the
pre-PR checklist, and project conventions.

## License

[MIT](LICENSE)
