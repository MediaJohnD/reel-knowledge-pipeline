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
- **`openai_compat`** - any local server with an OpenAI-style API (LM Studio,
  Lemonade, llama.cpp, vLLM) at `llm.openai_compat_base_url` (default
  `http://localhost:1234/v1`, LM Studio's; override with
  `REEL_OPENAI_COMPAT_BASE_URL`). No API key. Works in both waterfalls; use
  the model id the server lists.

Switch by editing `llm.provider` in `config/settings.yaml` (or override with
`REEL_LLM_PROVIDER`/`REEL_OLLAMA_HOST` in `.env` without touching the file).

**Ollama context window:** Ollama defaults every request to a 4096-token
context regardless of the model's real capacity - long transcripts or
multi-image carousels can silently exceed that and fail with
`exceed_context_size_error`. `llm.ollama_num_ctx` (default `16384`) overrides
this; raise it further if you process very long videos - but check it still
fits in VRAM, since this sizes the KV cache per request and pushing the model
into system RAM costs far more than the larger window gains.

For **carousels specifically**, `num_ctx` is no longer the lever -
`image_description.max_images_per_call` (default `3`) is. Images cost roughly
1000 prompt tokens each, so a long carousel used to overflow no matter how the
context was sized; `image_describer.py` now splits it across several requests
and joins the descriptions, which bounds request size by the cap rather than by
the length of the post.

## Photo posts / carousels (no video)

Posts with no video (a single photo, or a multi-image carousel) are detected
automatically by `downloader.py` and routed to `image_describer.py` instead
of `transcriber.py`. This requires a **vision-capable** model:

- **Ollama**: `image_description.model` in `config/settings.yaml` (default
  `gemma4:12b`) must be a model tagged with vision support - check with
  `ollama list` (look for `vision` in a model's capabilities) or pull one
  (`ollama pull gemma4:12b`, `mistral-small3.1`, `llava`, etc.).
- **Claude**: any current model already supports image input, so no separate
  config is needed beyond `enrichment.model`/`skill_writer.model`.

The vision model describes what's in the image(s) - transcribing any visible
text verbatim, since many carousels are screenshots of text - and that
description flows through the exact same enrichment/note/skill pipeline as a
video transcript. Large carousels (many images) take proportionally longer
and use more context; this is expected, not a bug.

### Re-capturing carousels saved before the `img_index` fix

Before 2026-11-01 a carousel link with `img_index` could capture only part of
the post. `scripts/recapture_carousels.py` re-captures those notes in place. It
keeps the note's folder, its inbound links, and any sections added by hand. The
old note is backed up to `data/review/carousel-recapture-backup/`.

```bash
uv run python scripts/recapture_carousels.py            # dry run: what is left, what is next
uv run python scripts/recapture_carousels.py --apply    # re-capture up to the daily cap
```

Run it by hand only. It takes at most 2 posts per day and halts for 24 hours on
a failure, to keep Instagram volume low. Progress is in
`data/review/carousel-recapture.json`. A failed post also adds a line to
`needs-attention.txt`, as any failed item does.

## Monthly link and repo audits

`run-review.bat` runs both audits after each review. Each one does nothing until
its last run is 30 days old, so no separate scheduled task is needed.

- `scripts/recheck_links.py` requests the URLs in every reel note and writes
  `data/review/link_health.json` plus a dated `Reels Link Recheck` note in the
  Reels folder. It skips login-walled social hosts, image assets, GitHub, and URLs
  carrying a token. `live` means the URL answered, not that its content still
  matches the note. `answers` means an API endpoint answered 401 or 405 (it exists
  and wants a key or another method), or an API base URL such as `/api/v1` answered
  404 because it has no route of its own. `blocked` means the site refuses scripts, so
  its state is unknown, and the audit does not pose as a browser to find out.
  Notes citing 200 or more URLs count as link directories and are reported apart
  from the reels.
- `scripts/vet_repos.py` checks every GitHub repo named in a note through the API
  and writes `data/review/repo_vetting.json` plus a dated `Reel Repos Vetting` note
  in `20-Resources/comparisons/`. It never clones, installs, or runs anything. A
  `review` grade means a serious pattern matched and someone has to read it. Record
  the decision in `data/review/repo_vetting_overrides.json`. It stands until the
  flags change.

Both notes are read-only toward the reel notes. Run either by hand with no flags to
force a fresh run. If a run misses, the Loop Digest marks that audit STALE at 45
days, and the session brief shows it.

## Ingesting content

**Queue file (manual paste):** append one URL per line to
`data/inbox/queue.txt`, then run:

```bash
uv run python -m reel_pipeline.cli run-once
```

The queue file and the webhook accept the same paste forms: a bare
`domain.tld/path`, `<https://...>`, a markdown link, or text holding several
links (each one becomes its own item). Redirect wrappers (`l.instagram.com`,
`l.facebook.com`, `lm.facebook.com`, `google.com/url`,
`linkedin.com/safety/go`, `youtube.com/redirect`) are unwrapped first, and the
real target is what gets validated.

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
the server - and exits with the server's own exit code
(`WScript.Quit WshShell.Run(...)`). Both are required for the task's
restart-on-failure setting (`RestartCount: 999`, `RestartInterval: 1 minute`)
to detect a crash or a failed startup and bring it back up within a minute:
without the exit code the task always recorded Last Result 0 and never
restarted (found 2026-09-10, when a post-reboot bind to the not-yet-up
Tailscale IP failed and the server stayed dead). `ExecutionTimeLimit`
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
   see the retry behavior note below for details. Before an item ends as an
   error, the legitimate fallbacks run: see "Fallbacks before an error" below.

## Guardrails you should know about

- **No *interactive* browser automation.** Every media platform is fetched by
  a CLI tool (yt-dlp, gallery-dl) using cookies you explicitly configure -
  never a logged-in browser session. Text-capture has one narrow, read-only
  exception (2026-08-10): when a page's content is assembled entirely
  client-side and plain `trafilatura` extraction fails, `text_fetcher.py`
  falls back to loading it in headless Chromium and reading the rendered
  text - no clicks, no forms, no cookies, no login, no anti-detection
  tooling. Toggle it off with `text_capture.render_fallback.enabled: false`
  in `config/settings.yaml`. Requires a one-time
  `uv run playwright install chromium` after `uv sync`.
- **A site that blocks automation is respected.** HTTP 403/401/429 and
  challenge pages never escalate to the headless render or the Wayback archive,
  and there are no unlockers or identity changes beyond the existing browser
  user agent then bot user agent order.
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
- **`code_version` in `/healthz` is the git HEAD the server *started* with.**
  The webhook server is long-lived (logon-triggered task) and imports the
  pipeline modules once, so committing a fix does nothing until the task
  restarts. After any change to code the server imports, restart the task and
  confirm the SHA moved:

  ```powershell
  Stop-ScheduledTask -TaskName ReelPipelineWebhook
  Start-ScheduledTask -TaskName ReelPipelineWebhook
  curl http://$env:REEL_WEBHOOK_HOST:8787/healthz   # code_version == git rev-parse --short=12 HEAD
  ```

  This is not hypothetical: 4b28ef6 (strip share/auth tokens from `source_url`)
  was correct in the repo on 2026-09-17 but two notes written that evening still
  carried a live `?stkn=`, because the process had been up since 2026-09-16.

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

## Comment-gated reels ("comment KEYWORD for the link")

The reviewer (`scripts/review_new_reels.py`) never marks these `skip`. It gives
them the verdict `comment-for-link`. On Instagram, when research found no
independent copy of the resource, it also queues them in
`data/review/comment_queue.json`. You can see the queue in the vault as
`Reel Comment Queue.md`.

One-time setup:
1. Set `REEL_IG_BROWSER_PROFILE` (a folder outside the repo, used only for this)
   and `REEL_IG_OWNER_HANDLE` in `.env`.
2. `uv run python -m reel_pipeline.cli comment-queue login`. A Chrome window
   opens; log into Instagram by hand, then press Enter in the terminal.

Working the queue (the "Reel Comment Queue" task runs it daily; you can also run
it yourself, and the rate limits apply either way):
- `uv run python -m reel_pipeline.cli comment-queue run`: a dry-run. It opens each
  reel and reports what it would do, with no follows, comments or clicks.
- `... comment-queue run --apply --max 1`: this one really acts. It follows the
  creator, waits (20-90 min in week 1), comments the keyword, then on a later run
  reads the DM, presses an allowlisted "Send link" button if needed, and writes
  the links into the note.
- Run it again every so often. Every step waits for its rate-limit slot, so
  running it more often never goes faster than the limits in `comment_gate:`.
- Exit code 2 means a halt: Instagram showed a challenge or an "action blocked"
  dialog. Everything stops for 24 h. Open the profile by hand and resolve it.
- A reel whose DM never brought a link becomes `dm-timeout`. Each `--apply` run
  re-reads up to 10 of those threads, each one weekly and at most 6 times. These
  are reads only and use no write slots.
- A bot's quick-reply buttons don't render on instagram.com. The queue note lists
  those threads under `Tap in the Instagram app`. Tap the button in the app, and
  the next run picks up the link.
- The next `review_new_reels.py --apply` re-reviews reels whose links arrived.
  `review_new_reels.py --backfill-comment-gates` lists the older gated `skip`
  reels, and `--apply` re-reviews them.

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
  so this needs no separate setup. Text, markdown and the binary formats
  listed below all work, and a large public file's virus-scan page is followed
  through its confirm form. See
  `docs/superpowers/specs/2026-08-10-drive-text-capture-design.md`.
- **Everything else** (Google Docs and Sheets, Airtable, blog posts, gists,
  GitHub Pages, personal knowledge links, ...): fetches the URL directly and
  routes on what actually came back (magic bytes, then content type, then URL
  suffix):
  - HTML: main text extracted with `trafilatura`. A client-rendered app shell
    goes to the headless fallback. A private or login-gated page fails with a
    clear error rather than producing a boilerplate note.
  - Documents: PDF, DOCX, PPTX, XLSX, EPUB, ODT/ODS/ODP. The file is attached
    in the vault next to the note. A scanned PDF with no text layer goes to
    the vision path. Capped by `text_capture.max_document_mb` (default 100).
  - Text and data: CSV, TSV, plain text, markdown, YAML, pretty-printed JSON,
    and RSS or Atom feeds.
  - Direct audio, video and image files download to the transcribe or vision
    path, capped by `text_capture.max_media_mb` (default 2048).
  - A thin page (under about 400 chars) with embedded media: a direct file, an
    iframe or `og:video` on an allow-listed host, one hop through an HTML
    player, or an HLS playlist. HLS is downloaded through the guarded client at
    the lowest bandwidth, never handed to yt-dlp. Encrypted HLS is refused.

### Fallbacks before an error

Each fallback only runs when the one before it had nothing to offer:

- **Thin page, render failed or off:** a note from the title and description
  (at least 60 chars), labeled "Only the page's title and description were
  available". Pages with neither still fail, with a clear error. Not used after
  a challenge page or on login text.
- **HTTP 404/410:** the Wayback Machine at once, and the note says so ("Archived
  copy from YYYY-MM-DD; the live page returned HTTP 404"). **5xx or connection
  errors:** normal retries, then the archive on the last attempt. If there's no
  snapshot, or the archive itself refuses the lookup, the error names both
  statuses. A URL whose query has token-like params is never sent to the archive.
  Snapshots repeat the original page's `Content-Encoding` over bytes that may not
  be compressed, so archived bodies are read raw and gunzipped only if the bytes
  are really gzip.
- **GitHub or Notion capture failed:** generic capture, keeping both messages.
- **Instagram:** an empty result for one carousel slide retries the whole post.
  A gallery-dl failure tries yt-dlp once, unless it reads as a rate limit,
  challenge, checkpoint or login.
- **Media host with no media** (text post on `x.com`, `twitter.com`,
  `facebook.com`, `fb.watch`, `linkedin.com`, `reddit.com`; the list is
  `text_capture.media_text_fallback_domains`): text capture, needing at least
  200 chars. "Post unavailable" stays terminal.

The media allow-list (`download.allowed_domains`) gained `loom.com`, `reddit.com`, `v.redd.it`, `soundcloud.com`, `twitch.tv`,
`rumble.com`, `dailymotion.com`, `ted.com`, `streamable.com`, `wistia.com` and
`wistia.net` on 2026-11-01. They need no setup. `download.anonymous_domains` lists
the same hosts: `REEL_YTDLP_COOKIES_FILE` and `REEL_YTDLP_COOKIES_BROWSER` never
go to them, even when set.

Three of them fail anonymously today, checked live on 2026-11-01. These are limits, not
bugs, so requeueing won't help unless the site changes:

- **Reddit, `v.redd.it`:** yt-dlp asks for an account, and the page blocks
  automation. Cookies stay off, because no Reddit link was on record as of
  2026-11-01. Reconsider if they start arriving.
- **Dailymotion:** yt-dlp needs impersonation tooling (`curl_cffi`), which this
  pipeline refuses as anti-detection.
- **Rumble:** 403 to yt-dlp and to text capture.

Each tab of a Google Sheet is its own item (`gid=0` is the sheet itself). A thin
rendered page (under 400 chars) that is only the site's own error message is
rejected and falls back to page metadata, so it does not become a note.

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

## Search-index sync (new notes findable the same day)

After a run writes notes, `index_sync.py` asks the local Khoj index to pick them up (`search_index_sync` in `config/settings.yaml`), so a note is searchable by meaning within about a minute instead of after the 02:00 scheduled reindex. It is best-effort (a down index never fails a run), loopback URLs only, and throttled to one call per `min_interval_minutes` (30), with the last call recorded in `data/inbox/.index_sync.json`. Measured 2026-11-11: an incremental reindex took 56 s. Set `enabled: false` to turn it off. The daily 02:00 task stays as the backstop.


## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `RuntimeError: REEL_WEBHOOK_SECRET is not set` | `.env` not loaded / secret missing before `serve-webhook` |
| `RuntimeError: ANTHROPIC_API_KEY is not set` | Only applies when `llm.provider: anthropic` - set the key, or switch `llm.provider` to `ollama` in `config/settings.yaml` |
| `LlmCallError: Ollama request ... failed` | Ollama isn't running, or the model in `enrichment.model`/`skill_writer.model` isn't pulled yet (`ollama pull <model>`) |
| `exceed_context_size_error` from Ollama | For a long transcript, raise `llm.ollama_num_ctx` in `config/settings.yaml` (default 16384). For a photo carousel, lower `image_description.max_images_per_call` (default 3) instead - each image costs ~1000 prompt tokens, so the fix is fewer images per request, not a bigger window. The full error text now includes Ollama's own message, which names the token counts |
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
| `returned HTTP 404; no usable archived copy (...)` | The page is gone and the archive had no snapshot, or refused the lookup (the bracket says which, for example `archive lookup returned HTTP 429`). A later retry may get through; an archive refusal isn't worked around |
| `produced no usable text even after rendering` | A thin page with no description of at least 60 chars, so there was nothing to keep. Expected on API doc shells such as Redoc pages |
| `HLS stream ... is encrypted` | The stream uses `EXT-X-KEY`. Refused on purpose: no DRM or key handling. When the stream came from a page rather than a submitted `.m3u8` URL, the page carries on to the render and title-and-description fallbacks |
| `is larger than the N MB limit` | The file passed the size cap. Raise `text_capture.max_document_mb` or `max_media_mb` in `config/settings.yaml` if you really want the file |
| `PostUnavailableError`: Instagram says the post is gone | The post was deleted or hidden, and Instagram's API answered "Media not found or unavailable". The item gives up on the first attempt. Any other Instagram HTTP 400 (checkpoint, useragent mismatch) stays retryable, because it means account trouble rather than a missing post |
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
  (default 5000 items) turns this into a *monitored* deferral: once item count
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
