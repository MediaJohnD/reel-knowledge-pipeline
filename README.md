# Reel Knowledge Pipeline

A safe-by-default video/reel knowledge ingestion pipeline. Feed it a URL (via
a queue file or webhook), and it downloads the media, transcribes it,
enriches the transcript into structured metadata, writes an Obsidian note,
and - only for high-signal content - generates a reusable Claude/Codex skill
artifact.

No browser automation. No hardcoded secrets. No social-platform scraping by
default - YouTube/TikTok/X/Vimeo work anonymously; Instagram, Facebook,
LinkedIn, and Google Drive (video files only) are each a deliberate,
cookie-authenticated opt-in (see `docs/runbook.md`). GitHub and public Notion
pages are captured as text via a separate, public-links-only path (no
credentials needed). See [`docs/architecture.md`](docs/architecture.md) for
the full design and [`docs/runbook.md`](docs/runbook.md) for day-to-day
operation.

## Quickstart

```bash
cp .env.example .env
# edit .env: set REEL_WEBHOOK_SECRET at minimum
# (enrichment defaults to a local Ollama model - see docs/runbook.md's "LLM provider"
# section to use hosted Claude instead)

uv sync

echo "https://www.youtube.com/watch?v=dQw4w9WgXcQ" >> data/inbox/queue.txt
uv run python -m reel_pipeline.cli run-once
```

Notes land in `data/notes/` (or wherever `REEL_VAULT_DIR` points). Any
high-signal content also gets a `SKILL.md` under `data/generated_skills/`.

## Commands

| Command | Purpose |
| --- | --- |
| `uv sync` | Install dependencies (add `--extra local-whisper` for the local transcription backend; dev tooling installs by default) |
| `uv run pytest` | Run tests |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run pyright` | Typecheck |
| `uv audit` | Scan dependencies for known vulnerabilities |
| `uv lock --check` | Verify uv.lock matches pyproject.toml |
| `uv run python -m reel_pipeline.cli run-once` | Drain the queue/webhook backlog once |
| `uv run python -m reel_pipeline.cli serve-webhook` | Start the webhook ingestion server |

A `Makefile` wraps all of the above (`make install`, `make test`, `make check`, etc).

## Configuration

Non-secret settings live in [`config/settings.yaml`](config/settings.yaml)
(paths, allowed/blocked domains, model names, LLM provider). Secrets live only
in the environment - see [`.env.example`](.env.example) for the full list
(`REEL_WEBHOOK_SECRET` always; `ANTHROPIC_API_KEY` only if `llm.provider` is
set to `anthropic`; `OPENAI_API_KEY` only if the OpenAI transcription backend
is used). Any `config/settings.yaml` value can be overridden by the matching
`REEL_*` environment variable - see `src/reel_pipeline/config.py` for the exact list.

Enrichment and skill-generation prompts live in
[`config/prompts/`](config/prompts/) as versioned markdown files, not
hardcoded strings.

## Repository layout

```
config/            settings.yaml + prompt templates
data/inbox/         queue.txt (input), state.json (durable dedup/status), needs-attention.txt
data/logs/          structured JSON logs
data/notes/         generated Obsidian notes (default vault location)
data/generated_skills/  generated SKILL.md artifacts for high-signal content
src/reel_pipeline/  all business logic (see docs/architecture.md)
tests/              pytest suite
docs/               architecture, runbook, acceptance tests
.claude/skills/     this repo's own authoring/maintenance skills (distinct from generated content skills)
```

## Safety posture

- Only the platforms listed in `download.allowed_domains` are ever attempted, via yt-dlp or gallery-dl.
- No browser automation, ever - both downloaders are cookie-authenticated CLI tools, never a driven browser session.
- `download.blocked_domains` is enforced before any download attempt - those URLs are logged to `needs-attention.txt` instead.
- Instagram, Facebook, LinkedIn, and Google Drive are each a deliberate, scoped exception to a general "no social platform by default" baseline: enabled via gallery-dl (Instagram) or optional yt-dlp cookies (Facebook/LinkedIn/Drive) for low-volume personal use with the account owner's own real account. Drive is scoped to that exact subdomain, not all of `google.com`. YouTube/TikTok/X/Vimeo stay anonymous. See `docs/runbook.md`'s "Instagram setup" and "Facebook and LinkedIn setup" sections.
- GitHub repos/files and public Notion pages are captured as text (not downloaded as media) via a separate `text_capture.allowed_domains` list and `text_fetcher.py` - public links only, no credentials of any kind. Airtable was evaluated and excluded (its share views need JS execution to render, conflicting with the no-browser-automation rule).
- Secrets (including Instagram/yt-dlp cookies config) are read from the environment only; `config/settings.yaml` never contains secrets.

See [`CLAUDE.md`](CLAUDE.md) for the full set of project rules this repository was built against.
