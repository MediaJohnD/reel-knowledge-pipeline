# Runbook

## First-time setup

```bash
cp .env.example .env
# edit .env: set REEL_WEBHOOK_SECRET at minimum
uv sync
```

If you'll use the local transcription backend (the default), also run:

```bash
uv sync --extra local-whisper
```

`yt-dlp` additionally shells out to `ffmpeg` for audio extraction - install it
separately (`winget install ffmpeg` / `brew install ffmpeg` / `apt install ffmpeg`)
if it isn't already on your `PATH`.

## LLM provider (enrichment + skill generation)

`config/settings.yaml`'s `llm.provider` picks which LLM does enrichment and
skill generation:

- **`ollama` (default)** - runs fully locally against an already-running
  [Ollama](https://ollama.com) instance (`llm.ollama_host`, default
  `http://localhost:11434`). No API key, no cost. Pull the model named in
  `enrichment.model`/`skill_writer.model` first: `ollama pull qwen2.5:14b`.
  Any model with good instruction-following is fine; a 14B-class model is a
  reasonable default for following the JSON-output instructions in
  `config/prompts/enrich_transcript.md`.
- **`anthropic`** - hosted Claude API. Set `ANTHROPIC_API_KEY` in `.env` and
  set `enrichment.model`/`skill_writer.model` to a Claude model id (e.g.
  `claude-sonnet-5`).

Switch by editing `llm.provider` in `config/settings.yaml` (or override with
`REEL_LLM_PROVIDER`/`REEL_OLLAMA_HOST` in `.env` without touching the file).

**Ollama context window:** Ollama defaults every request to a 4096-token
context regardless of the model's real capacity - long transcripts or
multi-image carousels can silently exceed that and fail with
`exceed_context_size_error`. `llm.ollama_num_ctx` (default `16384`) overrides
this; raise it further if you process very long videos or large carousels.

## Photo posts / carousels (no video)

Posts with no video (a single photo, or a multi-image carousel) are detected
automatically by `downloader.py` and routed to `image_describer.py` instead
of `transcriber.py`. This requires a **vision-capable** model:

- **Ollama**: `image_description.model` in `config/settings.yaml` (default
  `mistral-small3.1`) must be a model tagged with vision support - check with
  `ollama list` (look for `vision` in a model's capabilities) or pull one
  (`ollama pull mistral-small3.1`, `llava`, `qwen2-vl`, etc.).
- **Claude**: any current model already supports image input, so no separate
  config is needed beyond `enrichment.model`/`skill_writer.model`.

The vision model describes what's in the image(s) - transcribing any visible
text verbatim, since many carousels are screenshots of text - and that
description flows through the exact same enrichment/note/skill pipeline as a
video transcript. Large carousels (many images) take proportionally longer
and use more context; this is expected, not a bug.

## Ingesting content

**Queue file (manual paste):** append one URL per line to
`data/inbox/queue.txt`, then run:

```bash
uv run python -m reel_pipeline.cli run-once
```

**Webhook:** start the server, then POST to it (e.g. from a Shortcuts
share-sheet action, a phone automation, or curl):

```bash
uv run python -m reel_pipeline.cli serve-webhook
curl -X POST http://127.0.0.1:8787/webhook \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: $REEL_WEBHOOK_SECRET" \
  -d '{"url": "https://www.youtube.com/watch?v=..."}'
```

The webhook validates the shared secret, dedups/validates the URL, and kicks
off a background `run_once()` so the note (and, if applicable, skill) show up
without a separate manual step.

**Auto-start at login (Windows):** a Scheduled Task named `ReelPipelineWebhook`
runs `scripts/start_webhook_hidden.vbs` at logon, which launches
`uv run python -m reel_pipeline.cli serve-webhook` with zero visible window
(stdout/stderr redirected to `data/logs/webhook_stdout.log`, capped at 5MB with
one rotated backup - the VBS script checks size and rotates before each start,
since this file is raw process-output redirection outside `pipeline.log`'s own
JSON-based rotation). This means the server survives a PC restart without you
having to manually relaunch it.

The launcher runs *synchronously* (`WshShell.Run(..., 0, True)`) rather than
fire-and-forget - the scheduled task's own tracked process lives and dies with
the server, so the task's restart-on-failure setting (`RestartCount: 999`,
`RestartInterval: 1 minute`) can actually detect a mid-session crash and bring
it back up within a minute, not just survive a full reboot. `ExecutionTimeLimit`
is explicitly `PT0S` (unlimited) - if you ever recreate this task from the GUI,
re-set that, since the GUI default is a 3-day kill switch that would silently
terminate a long-running personal server.

To inspect/remove: `Get-ScheduledTask -TaskName ReelPipelineWebhook` /
`Unregister-ScheduledTask -TaskName ReelPipelineWebhook`. If you ever change
`serve_webhook()`'s startup behavior, no task changes are needed - it just
re-runs the same CLI command.

## What happens to a URL

1. `queue_manager` normalizes the URL, computes a deterministic `content_id`,
   and checks it against `data/inbox/state.json`.
2. If the domain is on `download.blocked_domains` or isn't a valid http(s)
   URL, it's written to `data/inbox/needs-attention.txt` with a reason and
   never downloaded.
3. Otherwise: download -> transcribe (video) or describe (photo post/carousel,
   see "Photo posts" below) -> enrich -> write note -> maybe write skill. Each
   stage updates `state.json`'s `status` field.
4. On success: `data/notes/<title-slug>.md` (or your configured
   `REEL_VAULT_DIR`) is created/overwritten, and `status` becomes `done`.
5. On failure at any stage: `status` becomes `failed`, the error is recorded
   in `state.json`, and a line is appended to `needs-attention.txt`. The item
   stays actionable and is retried on a capped exponential backoff schedule -
   see the retry behavior note below for details.

## Guardrails you should know about

- **No browser automation, ever.** Every platform is fetched by a CLI tool
  (yt-dlp, gallery-dl) using cookies you explicitly configure - never a
  Selenium/Playwright-style logged-in browser session.
- **Instagram is a deliberate, scoped exception, not the default-open case.**
  `CLAUDE.md`'s baseline guardrail is "no Instagram by default"; this project
  explicitly enables it via `gallery-dl` for low-volume, personal use with the
  account owner's own real account (no burner, no bulk automation) - see
  "Instagram setup" below and the research note in the Obsidian vault under
  `20-Resources/Tools/Instagram Reel Ingestion Research (2026-07-11).md`. If
  you fork this for a different use case, reconsider whether that tradeoff
  still applies before raising the volume.
- **Never commit `.env`** (or any cookies file). Only `.env.example`
  (placeholders) is versioned.
- **Retries are capped with exponential backoff.** A `failed` item is retried
  on subsequent `run-once` passes only after its backoff delay has elapsed,
  following `config/settings.yaml`'s `retry.backoff_schedule_minutes` (default
  `[1, 5, 30, 120, 480]` minutes, indexed by attempt number). After
  `retry.max_attempts` failed attempts (default `5`), the item's status
  becomes `failed_permanent` and `run-once` stops picking it up automatically.
  To retry a `failed_permanent` item, fix the underlying cause, then run
  `uv run python -m reel_pipeline.cli retry <content_id>` (or
  `--all-failed-permanent` to reset every one) - this resets the record to
  `pending` with a clean attempt/backoff slate (preserving any already-
  completed download stage) so the next `run-once` picks it up. It only
  touches records currently in `failed_permanent` status. Tune either
  backoff setting in `config/settings.yaml`'s `retry:` block.
- **`needs-attention.txt` only gets a new line when a failure is new** (first
  occurrence, or the error message changed) - repeated retries of the same
  failure don't add duplicate lines. The one exception is the transition to
  `failed_permanent` itself: that always gets a line (even on an unchanged
  error), so giving up on an item is never silent.
- **`data/tmp/<content_id>/` is deleted automatically once an item succeeds** -
  its content is already captured in the note, so the raw download serves no
  further purpose. Failed items keep their tmp files (useful for debugging)
  until they succeed or you clean them up manually.
- **Both logs are also swept automatically on a retention window, no cron job
  needed.** Every `run_once()` (queue-file or webhook-triggered) starts by:
  - deleting any `data/tmp/<content_id>/` older than
    `maintenance.tmp_retention_days` (default 30), *regardless of status* -
    this is what eventually reclaims tmp files for permanently-failing items,
    which the success-path cleanup above doesn't touch;
  - dropping `needs-attention.txt` lines older than
    `maintenance.needs_attention_retention_days` (default 30) - the item
    itself is still tracked in `state.json` either way, this only trims the
    human-readable log.
  Set either to `0` in `config/settings.yaml` to disable that sweep and keep
  everything forever.
- **`data/logs/pipeline.log` is rotated automatically** at 5MB, keeping 5
  backups (`pipeline.log.1`-`.5`) - stdlib `RotatingFileHandler`, no extra
  setup. `data/logs/webhook_stdout.log` (raw process output, outside the JSON
  pipeline) gets a simpler single-backup rotation from
  `scripts/start_webhook_hidden.vbs` at each server start.
- **Log verbosity is a config knob, not a code change.** Set
  `REEL_LOG_LEVEL=DEBUG` in `.env` (or `log_level: DEBUG` in
  `config/settings.yaml`) to get finer-grained logs while diagnosing a stuck
  run, then set it back to `INFO` - no restart-with-edited-source needed
  beyond restarting the process to pick up the new value.
- **`GET /healthz` reports real pipeline health, not just process liveness** -
  `queue_depth` (items not yet done/blocked), `failed_count`, and
  `last_success_at` (timestamp of the most recent successful item), read
  straight from `state.json`. A stuck worker or growing failure backlog is now
  visible from this endpoint alone.

## Rotating secrets

If a secret is ever suspected leaked (shared a screen, backed up the iOS
Shortcut config, accidentally pasted somewhere):

- **Webhook secret:** regenerate with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`, update
  `REEL_WEBHOOK_SECRET` in `.env`, update the iOS Shortcut's header value (and
  the web form's saved secret - open the page, use "Set/change webhook
  secret"), then restart the webhook server.
- **Anthropic/OpenAI API keys:** regenerate from the provider's console,
  update `.env`, revoke the old key at the source.
- **Instagram cookies:** re-export (Option A) or just re-log-in (Option B) -
  see "Instagram setup" below.

## Instagram setup (cookies)

Instagram requires an authenticated session for gallery-dl to download
reliably, even for public content. Set exactly one of these in `.env`:

**Option A - cookies file (works from any browser, most portable):**
1. Log into instagram.com in a desktop browser on this machine with your real account.
2. Export cookies with a browser extension like "Get cookies.txt LOCALLY" (Chrome/Firefox), scoped to instagram.com.
3. Save the exported file somewhere outside the repo (e.g. `C:/Users/you/instagram-cookies.txt`) - never inside the project directory, so it can't accidentally get committed.
4. Set `REEL_INSTAGRAM_COOKIES_FILE=C:/Users/you/instagram-cookies.txt` in `.env`.

**Option B - read cookies directly from an installed browser:**
1. Log into instagram.com in Chrome, Edge, or Firefox on this machine.
2. Set `REEL_INSTAGRAM_COOKIES_BROWSER=chrome` (or `edge`, `firefox`) in `.env`.
3. Close the browser before running the pipeline if you hit a "database is locked" error - some browsers lock their cookie store while running.

Cookies expire and need periodic refresh (re-export or just stay logged in for
option B). This is documented as a real, accepted tradeoff for low-volume
personal use - see the guardrail note above.

## Facebook and LinkedIn setup (cookies, optional)

Facebook (like Instagram) generally requires an authenticated session for
yt-dlp to download most content, even public-looking posts. Set exactly one
of these in `.env` - same two options as Instagram, just a different pair
of variables since these go through yt-dlp, not gallery-dl:

**Option A - cookies file:**
1. Log into facebook.com in a desktop browser with your real account.
2. Export cookies with a browser extension like "Get cookies.txt LOCALLY", scoped to the site.
3. Save the exported file outside the repo (e.g. `C:/Users/you/ytdlp-cookies.txt`).
4. Set `REEL_YTDLP_COOKIES_FILE=C:/Users/you/ytdlp-cookies.txt` in `.env`.

**Option B - read cookies directly from an installed browser:**
1. Log into facebook.com in Chrome, Edge, or Firefox.
2. Set `REEL_YTDLP_COOKIES_BROWSER=chrome` (or `edge`, `firefox`) in `.env`.
3. Close the browser first if you hit a "database is locked" error.

Unlike Instagram's cookies, these are **optional** - YouTube, TikTok, X, and
Vimeo already work anonymously and will keep working if you never set this.
For Facebook, yt-dlp will fail with its own clear "log in required" message
if the content needs auth and none is configured.

**Known LinkedIn limitations (verified, not a cookie problem):**
1. **Cookies don't help.** yt-dlp 2026.07.04 (the version this project
   pins) removed LinkedIn login support entirely (upstream commit `a5e0f87`,
   issue [#17039](https://github.com/yt-dlp/yt-dlp/issues/17039)). Setting
   `REEL_YTDLP_COOKIES_FILE`/`_BROWSER` has no effect on LinkedIn URLs.
2. **Group posts aren't recognized at all.** yt-dlp's LinkedIn extractor only
   matches `linkedin.com/posts/...-<id>-<hash>` and
   `linkedin.com/feed/update/urn:li:activity:<id>` URLs. A
   `urn:li:groupPost:...` URL (shared from a LinkedIn Group) isn't matched by
   any LinkedIn-specific pattern, so it falls through to yt-dlp's generic
   webpage extractor and fails with an HTTP 404 - check the URL for
   `groupPost` before assuming misconfiguration.

Both are upstream yt-dlp limitations, confirmed by reading its LinkedIn
extractor source and changelog directly - not something a local config change
can fix. If LinkedIn support matters enough to unblock, the options are
tracking/contributing to yt-dlp's own group-post support, or capturing that
one post manually.

## Text capture (GitHub, and any other non-media URL)

Any URL that isn't a recognized media platform (`download.allowed_domains`)
is captured as text instead of downloaded as media - no setup needed, since
this only ever does an unauthenticated, no-JS HTTP GET:

- **GitHub**: uses GitHub's public REST API, unauthenticated (60 requests/hour,
  2 calls per URL - comfortably enough for personal sharing volume). Works for
  any public repo, either the repo root or a link to one specific file
  (`.../blob/<branch>/<path>`).
- **Notion** (`notion.so`/`notion.site`/`notion.com`): calls Notion's own
  internal JSON API directly (the same API its web client uses) instead of
  fetching HTML - Notion's frontend ships zero content server-side for any
  page, so a plain HTTP GET can never work here regardless of the page being
  public. Works for any public page whose share link includes a page id
  (the standard `.../Some-Title-<id>` shape); a bare custom-subdomain root
  link with no id in the path isn't supported yet. Unofficial/undocumented
  API - could break if Notion changes it. See
  `docs/superpowers/specs/2026-08-10-notion-api-text-capture-design.md`.
- **Google Drive documents** (`drive.google.com/file/d/<id>/...`): a shared
  Drive link could be a video (handled by the media/yt-dlp path, see
  "Google Drive setup" below) or a document (text, markdown, PDF, PPT, ...) -
  the pipeline runs a quick check to tell which before fetching either way,
  so this needs no separate setup. Text/markdown documents work directly;
  binary formats (PDF/PPTX/DOCX) aren't parsed yet and fail with a clear
  error rather than garbage output. See
  `docs/superpowers/specs/2026-08-10-drive-text-capture-design.md`.
- **Everything else** (Google Docs, Airtable, blog posts, GitHub Pages,
  personal knowledge links, ...): fetches the page directly and extracts the
  main text content with `trafilatura`. Only works for pages that actually
  render their content server-side - a client-rendered app shell (common on
  Airtable and similar SPA-heavy sites) or a private/login-gated page fails
  with a clear "produced no usable text" error rather than silently producing
  an empty or boilerplate note.

None of this requires an environment variable or credential. As of 2026-08-10
this is a catch-all by default (see `CLAUDE.md`'s 2026-08-10 entry) -
`text_capture.blocked_domains` in `config/settings.yaml` is the escape hatch
for excluding a specific domain.

## Remote ingestion via Tailscale + iOS Shortcut

For sending links from a phone without relying on the same WiFi network:

1. Install Tailscale on the machine running the pipeline and sign in.
2. Install the Tailscale app on your phone, sign into the **same** account.
3. Set `REEL_WEBHOOK_HOST` to this machine's Tailscale IP (not `0.0.0.0` or a
   LAN IP) so the webhook is reachable only from your own tailnet devices,
   never the public internet or local network.
4. Build an iOS Shortcut: **Get Clipboard** -> **Get Contents of URL** (POST
   to `http://<tailscale-magicdns-name>:<port>/webhook`, header
   `X-Webhook-Secret: <your REEL_WEBHOOK_SECRET>`, JSON body `{"url": <Clipboard>}`
   - the JSON key must be lowercase `url`). Add it to your Home Screen or bind
   it to the Action Button (iPhone 16 Pro and later) for one-tap sending after
   copying a link.
5. Flow: in Instagram (or any app), Share -> Copy Link -> run the Shortcut.

The webhook's shared-secret header remains the auth layer even over Tailscale;
Tailscale itself only controls *reachability*, not authentication.

### Alternative: web form, no Shortcut needed

The webhook server also serves a minimal page at `GET /` - a text box and a
Send button. The first time you use it (per device/browser), tap "Set/change
webhook secret" and paste in your `REEL_WEBHOOK_SECRET` - it's saved in that
browser's `localStorage`, never embedded in the page itself. Open
`http://<tailscale-magicdns-name>:<port>/` in any browser on a tailnet device
(phone, laptop, anything), paste a link, tap Send. Add it to your phone's
home screen (Safari: Share -> Add to Home Screen) for a one-tap-open icon.
Less slick than the Shortcut (no clipboard auto-read, no Action Button
binding), but needs zero setup beyond Tailscale itself and works from any
device/browser, not just iOS.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `RuntimeError: REEL_WEBHOOK_SECRET is not set` | `.env` not loaded / secret missing before `serve-webhook` |
| `RuntimeError: ANTHROPIC_API_KEY is not set` | Only applies when `llm.provider: anthropic` - set the key, or switch `llm.provider` to `ollama` in `config/settings.yaml` |
| `LlmCallError: Ollama request ... failed` | Ollama isn't running, or the model in `enrichment.model`/`skill_writer.model` isn't pulled yet (`ollama pull <model>`) |
| `exceed_context_size_error` from Ollama | Raise `llm.ollama_num_ctx` in `config/settings.yaml` (default 16384) - long transcripts or large image carousels need more than Ollama's 4096-token default |
| `ImageDescriptionError` / vision request fails | `image_description.model` isn't vision-capable, or (Ollama) isn't pulled - check with `ollama list` for a `vision` capability tag |
| `RuntimeError: Instagram downloads require a logged-in session` | Set `REEL_INSTAGRAM_COOKIES_FILE` or `REEL_INSTAGRAM_COOKIES_BROWSER` - see "Instagram setup" above |
| yt-dlp download errors | Unsupported/blocked domain, network issue, or `ffmpeg` missing |
| gallery-dl download errors / empty results | Cookies expired - re-export the cookies file, or make sure you're still logged in in the browser `REEL_INSTAGRAM_COOKIES_BROWSER` points at |
| yt-dlp "log in" / "requested content is not available" on Facebook URLs | Set `REEL_YTDLP_COOKIES_FILE` or `REEL_YTDLP_COOKIES_BROWSER` - see "Facebook and LinkedIn setup" above |
| yt-dlp fails on any LinkedIn URL, cookies configured or not | Cookies are inert for LinkedIn as of yt-dlp 2026.07.04 (login support removed upstream) - see "Known LinkedIn limitations" above, not a config problem |
| yt-dlp 404s specifically on a `linkedin.com/feed/update/urn:li:groupPost:...` URL | Confirmed unsupported URN type - yt-dlp's LinkedIn extractor only matches `urn:li:activity:` and `/posts/...` URLs, not group posts |
| `CERTIFICATE_VERIFY_FAILED` on any download/API call | A local security tool (antivirus HTTPS scanning, corporate proxy) is intercepting TLS with a malformed root certificate - this is a machine-level issue, not this project's code; check `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` env vars and your antivirus's HTTPS-scanning settings |
| `TranscriptionError: faster-whisper is not installed` | Run `uv sync --extra local-whisper`, or switch `REEL_TRANSCRIPTION_BACKEND=openai` |
| Note not appearing where expected | Check `REEL_VAULT_DIR` / `paths.vault_dir` in `config/settings.yaml` |
| Item stuck retrying every run | Check its `error` field in `data/inbox/state.json` and `needs-attention.txt` |
| Item's `status` is `failed_permanent` and it never gets retried | Expected once `retry.max_attempts` is exhausted - fix the underlying cause, then run `uv run python -m reel_pipeline.cli retry <content_id>` (or `--all-failed-permanent`) |
| `state.json has grown large enough that its per-mutation full-rewrite cost may start to matter` in the logs | Informational, not an error - see "Known limitations" below |
| Webhook unreachable from phone | Confirm both devices show "Connected"/online in Tailscale, and `REEL_WEBHOOK_HOST` is set to this machine's Tailscale IP |
| `Exception in callback _ProactorBasePipeTransport._call_connection_lost()` / `ConnectionResetError: [WinError 10054]` in webhook logs | **Known-fixed**: this was cosmetic ERROR-level noise from asyncio's default Windows `ProactorEventLoop` when a client (iOS Shortcut, web form) disconnects right after its request completes. `serve_webhook()` in `src/reel_pipeline/cli.py` now switches to `WindowsSelectorEventLoopPolicy` on Windows before starting uvicorn, which doesn't have this bug. If you still see it, confirm you're on the current `cli.py`. |

## Known limitations

- **`state.json` doesn't scale past a few hundred items without a monitored
  cost.** It's fully reparsed and rewritten on every single stage
  transition - an O(n) cost per mutation, O(n^2) over a full backlog pass.
  This is a deliberate tradeoff for this project's original "handful of
  reels" scale (keeps `state.json` a plain, greppable, human-readable file -
  see `CLAUDE.md`), not an oversight. `maintenance.state_size_warning_threshold`
  (default 500 items) turns this into a *monitored* deferral: once item count
  reaches it, `run-once` logs a warning each pass. If you hit this, the real
  fix is a SQLite/WAL migration (evaluated and deferred - see
  `docs/superpowers/specs/2026-07-29-state-reliability-design.md`'s
  "Deferred" section for what that would look like and why it wasn't done
  by default).
- **Stage-level caching lives in `data/tmp/`, not `state.json` itself.** A
  crash mid-pipeline resumes from the most-advanced cached artifact
  (downloaded media, `transcript.json`, `enrichment.json`) as long as
  `data/tmp/<content_id>/` hasn't been cleared since. If it has (e.g. the
  `maintenance.tmp_retention_days` sweep reclaimed it, or you deleted it
  manually), the pipeline falls back to redoing that missing stage - it
  never crashes or silently skips work, it just redoes more than a fresher
  cache would have needed to.
- **No cross-machine/cross-process coordination beyond file locks.** The
  locking model assumes every `run-once`/`serve-webhook` process shares the
  same local filesystem (`state.json`'s directory) - it isn't designed for a
  distributed deployment across multiple machines.

## Where things live

- Logs: `data/logs/pipeline.log` (structured JSON, one line per event) + console.
- Generated notes: `data/notes/` (or `REEL_VAULT_DIR`).
- Generated skill artifacts (high-signal content only): `data/generated_skills/`
  (or `REEL_SKILLS_DIR`) - distinct from this repo's own `.claude/skills/`.
