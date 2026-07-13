---
name: building-reel-pipeline
description: Use when adding, modifying, or extending modules in this reel-knowledge-pipeline repository - explains the module boundaries, data flow, and architectural rules to follow.
---

# Building the Reel Knowledge Pipeline

This repo turns video/reel URLs into structured Obsidian notes and optional
skill artifacts. Use this skill before adding features or refactoring modules
under `src/reel_pipeline/`.

## When to use this

- Adding a new pipeline stage (e.g. a new downloader source or transcription backend).
- Modifying `worker.py`'s orchestration.
- Unsure which module a piece of logic belongs in.
- Reviewing a PR that touches `src/reel_pipeline/`.

## Module boundaries (do not blur these)

| Module | Owns | Does NOT own |
| --- | --- | --- |
| `validators.py` | URL normalization, content_id hashing, allow/block-list checks | downloading, state persistence |
| `queue_manager.py` | `state.json` persistence, `queue.txt` draining, `needs-attention.txt` | actually processing an item |
| `downloader.py` | yt-dlp invocation | URL validation (already done upstream) |
| `transcriber.py` | audio -> text, pluggable backends | enrichment |
| `enricher.py` | transcript -> `EnrichmentResult` via Claude + `config/prompts/enrich_transcript.md` | writing files |
| `obsidian_writer.py` | note file format + deterministic naming | deciding whether to write a skill |
| `skill_writer.py` | `SKILL.md` generation via `config/prompts/create_skill.md` | note writing |
| `worker.py` | wiring the above together per item, updating `state.json` status at each stage | HTTP, CLI |
| `webhook_server.py` / `cli.py` | entry points | pipeline logic (delegate to `worker.build_worker`) |

## Steps for adding a new stage or backend

1. Define/extend the relevant `Protocol` (e.g. `Transcriber`, `Downloader`) rather than adding conditionals to `worker.py`.
2. Add a factory function (e.g. `get_transcriber(settings)`) that selects the implementation from `Settings` - never hardcode a backend choice inline.
3. Keep secrets out of `config/settings.yaml`; add new secrets to `.env.example` and read them via `Settings`.
4. Update `docs/architecture.md`'s module table.
5. Add or update tests in `tests/` using fakes that satisfy the Protocol (see `tests/test_worker_flow.py` for the pattern) - do not require real network access in the automated suite.
6. Run `make check` (lint + typecheck + tests) before considering the change done.

## Notes / gotchas

- `state.json` is the only durable source of truth for dedup/idempotency - never make a stage decide "have I seen this before," that's `queue_manager`'s job.
- Filenames for notes and skills must stay deterministic (content_id + slug). Don't introduce timestamps or random components into them.
