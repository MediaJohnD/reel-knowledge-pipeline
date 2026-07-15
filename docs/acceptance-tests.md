# Acceptance Tests

Manual/end-to-end checklist mapped to the required capabilities. Automated
coverage for each is noted; items marked **(manual)** require a real network
call / API key and are not exercised by the automated test suite.

## 1. Queue ingestion from `data/inbox/queue.txt`

- [ ] Appending a supported-platform URL and running `run-once` produces a
      `done` record in `state.json` and a note in the vault.
      Automated: `tests/test_queue_manager.py`, `tests/test_worker_flow.py`.
- [ ] Re-running `run-once` with the same URL still in the file does not
      create a duplicate note or re-process the item.
      Automated: `test_sync_is_idempotent_across_restarts`,
      `test_second_run_once_is_a_no_op_after_success`.

## 2. Webhook ingestion with shared-secret validation

- [ ] `POST /webhook` without `X-Webhook-Secret` (or with the wrong value)
      returns `401`. **(manual - run `serve-webhook` and curl it)**
- [ ] `POST /webhook` with the correct secret and a valid URL returns `202`-style
      acceptance payload and the item appears in `state.json`.
      Automated (queue_manager layer): `test_webhook_ingested_item_is_processed_same_as_queue_file`.

## 3. URL validation and deduplication

- [ ] Malformed URLs are rejected with a clear reason, not a crash.
      Automated: `tests/test_queue_manager.py` (blocked-domain cases exercise the same path).
- [ ] Any domain in `download.blocked_domains` is routed to
      `needs-attention.txt`, not downloaded. (Instagram itself is currently
      allowed - a deliberate, documented exception - see `CLAUDE.md`'s Risk
      posture; this test exercises the general blocking mechanism.)
      Automated: `test_blocked_domain_routes_to_needs_attention_not_state_pending`.
- [ ] The same URL with different tracking query params (`?utm_source=...`)
      still maps to the same `content_id`.
      Automated: covered by `validators.normalize_url` behavior; add a direct
      unit test if extending this contract further.

## 4. Media download module

- [ ] A real YouTube URL downloads successfully via yt-dlp. **(manual)**
- [ ] An unreachable/removed URL fails cleanly and the item is marked
      `failed` with the yt-dlp error recorded. Automated (failure path with a
      fake downloader): `test_download_failure_marks_failed_and_records_needs_attention`.
- [ ] A video Instagram post is detected as `media_type=VIDEO`; a photo-only
      post/carousel is detected as `media_type=IMAGE` with all image paths in
      order; a post with neither raises a clear `DownloadError`.
      Automated: `test_gallery_dl_downloader_detects_video_over_images`,
      `test_gallery_dl_downloader_detects_image_carousel`,
      `test_gallery_dl_downloader_raises_when_no_media_found`.
- [ ] Facebook and LinkedIn URLs dispatch to yt-dlp (not gallery-dl), and
      optional cookies (`REEL_YTDLP_COOKIES_FILE`/`_BROWSER`) are passed
      through when configured, omitted when not.
      Automated: `test_dispatches_facebook_and_linkedin_urls_to_yt_dlp`,
      `test_yt_dlp_downloader_omits_cookies_when_not_configured`,
      `test_yt_dlp_downloader_passes_cookie_file_when_configured`,
      `test_yt_dlp_downloader_passes_cookies_from_browser_when_configured`.
- [ ] A real Facebook or LinkedIn URL downloads successfully with cookies
      configured. **(manual - requires real cookies)**

## 5. Transcription module

- [ ] Local backend (`faster-whisper`) produces text for a short audio file. **(manual)**
- [ ] OpenAI backend produces text given `OPENAI_API_KEY`. **(manual)**
- [ ] Selecting an unknown backend name raises a clear `ValueError` at
      startup, not deep in a pipeline run. Automated: exercised by
      `transcriber.get_transcriber` (see module for behavior; covered
      indirectly by config tests validating `transcription.backend`).

## 6. Image description module (photo posts / carousels)

- [ ] A real photo post or multi-image carousel produces a dense text
      description via the configured vision model, including verbatim
      transcription of any on-image text. **(manual - requires a vision-capable
      Ollama model pulled, or ANTHROPIC_API_KEY if `llm.provider` is
      `anthropic`)**
- [ ] The worker routes `media_type=IMAGE` items to `ImageDescriber`, never
      `Transcriber`, and vice versa for `media_type=VIDEO`. Automated:
      `test_image_post_routes_to_image_describer_not_transcriber`.
- [ ] `llm_client.describe_images` dispatches to the correct provider (Ollama
      `images` field / Claude image content blocks) and surfaces clear errors
      on failure. Automated: `tests/test_image_describer.py`.
- [ ] Multi-image carousels that exceed Ollama's default 4096-token context
      succeed once `llm.ollama_num_ctx` is raised (default 16384). **(manual -
      this was an actual failure mode hit during development with an 8-image
      carousel; see docs/runbook.md's troubleshooting table)**

## 7. Enrichment module

- [ ] A real transcript produces valid JSON matching the `EnrichmentResult`
      contract (title, summary, tags, tools_mentioned, key_takeaways,
      high_signal, skill_candidate_reason). **(manual - requires either Ollama
      running with the configured model pulled, or ANTHROPIC_API_KEY if
      `llm.provider` is `anthropic`)**
- [ ] Malformed/fenced JSON from the model is still parsed correctly (fence
      stripping / brace-extraction fallback). Covered by
      `enricher._extract_json` logic; exercised indirectly via worker tests
      with a fake enricher - add a direct unit test if the prompt changes.
- [ ] `llm_client.call_llm` dispatches to the correct provider and surfaces
      clear errors on failure. Automated: `tests/test_llm_client.py`.

## 8. Obsidian note writer

- [ ] Note frontmatter contains all required contract fields: title,
      source_url, content_id, created_at, tags, tools_mentioned, summary
      (body), key_takeaways (body), full transcript (body).
      Automated: `tests/test_obsidian_writer.py`.
- [ ] Filenames are deterministic and re-writing the same content_id
      overwrites rather than duplicates. Automated:
      `test_write_note_is_idempotent_and_overwrites_same_path`.

## 9. Optional skill writer for high-signal content

- [ ] `high_signal=false` never produces a skill artifact, and never calls
      the LLM. Automated: `test_generate_returns_none_and_makes_no_api_call_when_low_signal`.
- [ ] `high_signal=true` with a `skill_candidate_reason` produces a
      deterministically-pathed `SKILL.md` under `data/generated_skills/`.
      Automated: `test_generate_writes_deterministic_skill_path_for_high_signal_item`.

## 10. Structured logging

- [ ] `data/logs/pipeline.log` contains one JSON object per line with
      `timestamp`, `level`, `logger`, `message` fields after a `run-once`.
      **(manual - inspect the file after a real run)**

## 11. CLI

- [ ] `uv run python -m reel_pipeline.cli run-once` runs to completion and
      prints a processed/done/failed summary.
- [ ] `uv run python -m reel_pipeline.cli serve-webhook` starts without
      error when `REEL_WEBHOOK_SECRET` is set, and fails fast with a clear
      message when it isn't.

## Full verification log

See the final delivery summary for the actual `pytest` / `ruff` / `pyright`
output captured for this build.
