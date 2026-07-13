---
name: validating-release-readiness
description: Use before declaring any change to this repository complete - runs and confirms lint, typecheck, tests, and a realistic worker-flow verification, per CLAUDE.md's verification rule.
---

# Validating Release Readiness

This repo's `CLAUDE.md` states: "Do not claim success without lint,
typecheck, tests, and at least one realistic worker-flow verification." This
skill operationalizes that rule.

## When to use this

- Before telling the user a feature/fix is done.
- Before merging any PR touching `src/reel_pipeline/` or `tests/`.
- After resolving merge conflicts in generated files (`config/settings.yaml`, `pyproject.toml`).

## Steps

1. Install/sync deps if `pyproject.toml` changed: `uv sync` (dev tooling installs by default via `[dependency-groups]`).
2. Lockfile check: `uv lock --check` - if `pyproject.toml` changed and this fails, run `uv lock` to update `uv.lock` before continuing.
3. Lint: `uv run ruff check .` - fix findings, don't suppress with inline ignores unless justified in a comment.
4. Typecheck: `uv run pyright` - resolve errors; this repo uses `basic` mode, so untyped defs are allowed but type mismatches are not.
5. Test: `uv run pytest` - all tests must pass. If you added a module, confirm it has corresponding test coverage (see `tests/` for the existing pattern: fakes satisfying each Protocol, no real network calls).
6. Dependency audit: `uv audit` - investigate any newly-flagged vulnerability before merging.
7. Worker-flow verification: run `uv run python -m reel_pipeline.cli run-once` against at least one realistic scenario (a real URL if credentials are configured, or inspect `tests/test_worker_flow.py`'s happy-path assertions if not) and confirm the reported `processed`/`done`/`failed` counts match expectations.
6. Re-check the diff for scope creep: does every changed file trace back to the request?

## Reporting

When finishing a task, report:
- Files created/changed.
- Exact commands run (lint/typecheck/test) and their pass/fail outcome.
- Any step that couldn't be verified (e.g. no `ANTHROPIC_API_KEY` available) - state this explicitly rather than implying it was checked.
- Remaining risks or TODOs.

## Notes / gotchas

- A green `pytest` run with only fakes is necessary but not sufficient for
  claiming the *real* API integrations (Claude, yt-dlp, Whisper) work -
  call this out explicitly as unverified when no credentials/network are
  available, per `docs/acceptance-tests.md`'s "(manual)" markers.
