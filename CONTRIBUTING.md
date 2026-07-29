# Contributing

Thanks for considering a contribution. This is a small, opinionated personal
pipeline shared in the hope it's useful to others - contributions are welcome,
but please open an issue to discuss anything beyond a small fix before
sending a large pull request.

## Development setup

```bash
git clone https://github.com/MediaJohnD/reel-knowledge-pipeline.git
cd reel-knowledge-pipeline
uv sync                          # dev tooling installs by default
# uv sync --extra local-whisper  # add if you plan to touch the transcriber
cp .env.example .env             # fill in what you need for the change you're making
```

## Before opening a pull request

Run the full check suite locally - this is exactly what CI runs:

```bash
make check   # uv lock --check, ruff check, pyright, pytest, uv audit
```

Or step by step:

| Command | What it checks |
| --- | --- |
| `uv lock --check` | `uv.lock` matches `pyproject.toml` |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Formatting (not part of `make check` - run it if CI's lint step flags formatting) |
| `uv run pyright` | Type checking (`basic` mode) |
| `uv run pytest` | Test suite (fakes only, no network/credentials required) |
| `uv audit` | Dependency vulnerability scan |

If your change touches `src/reel_pipeline/`, add or update tests using the
existing "fakes satisfying each Protocol" pattern (see `tests/test_worker_flow.py`)
rather than making real network calls in the automated suite.

## Project conventions

This repository was built with a set of internal guardrails documented in
[`CLAUDE.md`](CLAUDE.md) and `.claude/skills/` - they describe the module
boundaries, the ingestion safety posture (no browser automation, ever;
explicit opt-in per platform), and the verification checklist expected before
any change is considered done. Reading `CLAUDE.md`'s "Risk posture" and
"Architecture rules" sections first will save you a review round-trip if
your change touches ingestion, downloading, or credential handling at all.

In short:

- **No browser automation, ever.** Only cookie-authenticated CLI tools
  (`yt-dlp`, `gallery-dl`).
- **No hardcoded secrets or personal paths.** Everything comes from
  `Settings` (environment-sourced) or `config/settings.yaml` (non-secret).
- **Prompts live in `config/prompts/`** as versioned markdown, never as
  inline strings.
- **`state.json` is the single source of truth** for dedup/idempotency -
  don't introduce a parallel tracking mechanism.
- Keep module boundaries intact (see [`docs/architecture.md`](docs/architecture.md)):
  validation, queueing, downloading, transcription, enrichment, note
  writing, and skill writing each stay in their own module.

## Adding a new ingestion source or platform

Enabling a new platform (a new social platform, a new automated account
action, raising an existing platform's volume) is a deliberate decision in
this project, not a routine PR - see `CLAUDE.md`'s "Risk posture" for the
process past additions went through. Please open an issue proposing it
before writing code.

## Reporting a bug

Open a GitHub issue with: the command you ran, the full error/log output
(check `data/logs/pipeline.log` for the structured JSON log line), and
whether the item's `state.json` record shows a `status`/`error` worth
including. Please redact URLs to private/personal content if relevant.

## Code of conduct

Be respectful and assume good faith. This is a small project maintained in
spare time - response times will vary.
