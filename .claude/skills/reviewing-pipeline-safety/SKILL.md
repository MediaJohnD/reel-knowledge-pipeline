---
name: reviewing-pipeline-safety
description: Use before merging any change to this repository's ingestion, download, or credential-handling code - checks the guardrails from CLAUDE.md/AGENTS.md are still intact.
---

# Reviewing Pipeline Safety

A pre-merge checklist for anything touching ingestion (`validators.py`,
`queue_manager.py`, `downloader.py`, `webhook_server.py`) or configuration
(`config.py`, `config/settings.yaml`, `.env.example`).

## When to use this

- Before merging a PR that touches URL validation, downloading, or the webhook server.
- Before adding a new ingestion source (a new platform, a new webhook route, a share-sheet integration).
- When reviewing a change that adds a new environment variable or config field.

## Checklist

1. **No new browser automation.** Grep the diff for Selenium/Playwright/Puppeteer/driven-browser-session usage. This repo's guardrail (`CLAUDE.md`) forbids it always - Instagram support uses gallery-dl (a CLI tool with cookie auth), never a driven browser.
2. **Any newly-blocked platform still routes to `needs-attention.txt`.** Confirm `config/settings.yaml`'s `download.blocked_domains` wasn't quietly narrowed, and that `validators.validate_url` still checks it before any download attempt. (Instagram itself is an intentional, documented exception as of 2026-07-11 - see `CLAUDE.md`'s Risk posture section - so its presence in `allowed_domains` is expected, not a regression.)
3. **Instagram's authentication stays explicit, not silent.** `GalleryDlDownloader` must keep requiring `REEL_INSTAGRAM_COOKIES_FILE`/`_BROWSER` via `Settings.require_instagram_cookies()` and fail loudly if neither is set - never silently fall back to anonymous access (which the research found unreliable) or embed a credential directly.
4. **No hardcoded secrets or personal paths.** Grep the diff for `sk-`, `api_key =`, absolute `C:\Users\...` or `/home/...` paths, or a literal cookies file path. All secrets (including Instagram cookies config) must come from `Settings` (environment-sourced); all paths must come from `Settings.resolve()`.
5. **`.env.example` stays placeholder-only.** If a new secret was added, it must appear in `.env.example` with an obviously-fake value, and be documented in `docs/runbook.md`.
6. **New ingestion paths still converge on `state.json`.** Any new way to submit a URL must go through `QueueManager.add_url()` or `sync_queue_file_into_state()` - not a parallel dedup mechanism.
7. **Webhook auth still uses constant-time comparison.** `webhook_server.py` compares secrets with `hmac.compare_digest`, never `==`.
8. **Failure paths don't leak stack traces to external callers.** The webhook should return generic error responses; details belong in `data/logs/pipeline.log`, not the HTTP response body.

## Notes / gotchas

- "Manual paste, webhook, share-sheet, or /watch-style ingestion" is the
  approved ingestion surface per `AGENTS.md`/`CLAUDE.md`. Instagram via
  gallery-dl is now also approved, scoped to the account owner's own real
  account at low volume (see Risk posture in `CLAUDE.md`) - treat any request
  to raise that volume, add a burner account, or scrape a *different*
  login-gated platform as a new decision requiring the same explicit
  brainstorming-and-research process this one went through, not an automatic
  extension of the existing exception.
