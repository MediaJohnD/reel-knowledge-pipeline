# More formats and links, and fallbacks before giving up

Owner's direction, 2026-11-01: "we need to work to accept more formats and links, and fix the
issues before discarding or erroring them!" Accept more link types and file formats, and try
every legitimate fallback before a URL ends as an error.

## Limits that do not move

- A site that blocks automation is respected. 403/401/429 and challenge pages do not escalate
  to the headless render or to the archive. No anti-detection, no browser-UA tricks beyond the
  existing `_BROWSER_UA` then `_BOT_UA` order, no unlockers, no DRM circumvention.
- Cookies only in `downloader.py`. Nothing on the text path sends cookies.
- Every new fetch goes through `_guarded_client` / the private-target guard: embeds, archive,
  Drive confirm, HLS playlists and segments.
- yt-dlp and gallery-dl only ever get hosts that pass `validate_url`. Never hand yt-dlp an
  `.m3u8` or an arbitrary host, because its generic extractor would fetch it unguarded.
- `RenderedHtmlFetcher` stays read-only and confined to `text_fetcher.py`. `comment_gate.py` is
  out of scope.
- Prompts stay in `config/prompts/`; config and models stay typed; modules stay separate; the
  worker stays idempotent with `state.json` as source of truth. No hardcoded secrets or personal
  absolute paths.
- `pypdf` is the only document library. Use the standard library. Do not add a dependency.
- Never delete or rewrite `data/inbox/needs-attention.txt` history.

## New module and exception

- `src/reel_pipeline/document_text.py` holds binary and feed extraction, moved out of or called
  from `text_fetcher.py`.
- `MediaFound(Exception)` in `text_fetcher.py`, keyword-only: `.result: DownloadResult | None`,
  `.url: str | None`, `.context: str`. The worker text branch catches it:
  `download_result = found.result or self.downloader.download(found.url, cid)`, merges `context`
  into the caption, and continues into transcribing.
- SSRF: the `fetch_video(record.url, ...)` retry on an empty transcript runs only for validated
  media URLs. Use `found.url` or `record.url` for real media and `None` for direct files, so
  yt-dlp never receives an unvalidated host.

## Generic fetch streaming and classification

`GenericHtmlFetcher` streams: `client.send(client.build_request("GET", url, headers=...), stream=True)`,
closed in `finally`; 403 closes and retries with `_BOT_UA`. Read the head with
`next(response.iter_bytes(65536), b"")`. Classify by magic bytes (`%PDF-`, `PK\x03\x04` plus
suffix), then content-type, then URL suffix. Kinds: html, doc, text, json, feed, audio, video,
image, HLS. For octet-stream or empty type: URL suffix, else html if it looks like html, text if
it decodes, else attach. SVG attaches.

Media streams to `tmp_dir/<cid>/file.ext` with a cap. New `TextCaptureConfig` fields
`max_document_mb=100` and `max_media_mb=2048`; exceeding either raises a clear `TextFetchError`.

## Formats

- Module-level attach helper shared by Drive and generic (from `DriveFetcher._attach_binary`).
- Binary text adds `.xlsx` (regex over sharedStrings and sheets), `.epub`, `.odt/.ods/.odp`
  (`content.xml`). Zip-bomb guard: `_MAX_UNZIPPED_BYTES` about 200 MB via `getinfo().file_size`.
- CSV, TSV, plain, markdown, YAML go to text. JSON is pretty-printed.
- RSS and Atom via regex over `<item>|<entry>`: no XML parser, no entity expansion. XML with no
  items gets its tags stripped.
- Scanned PDF with no text: `page.images` handed on as `MediaFound` with media type IMAGE, the
  attachment embed as caption.
- Google: `_GOOGLE_DOC_RE` adds `spreadsheets` (`export?format=csv`, keep `gid`, check `text/csv`)
  and `(?:u/\d+/)?`.
- Suffix lists add `.heic,.gif,.avif,.bmp` images (extend `_NEEDS_REENCODE_SUFFIXES` in
  `image_describer.py`), `.m4v,.avi,.ts` video, `.ogg,.opus,.flac,.aac` audio, including
  `worker.py` `_VIDEO_SUFFIXES` and the hardcoded suffixes in `_cached_download_result`.

## Redirects and embeds

- After GET, if the final `response.url` is allow-listed media (`classify_url_kind(final) ==
  "media"`, skipping root and login paths) and the original was not: `MediaFound(url=final)`.
- Thin text (under about 400 chars) plus a direct file in `og:video:secure_url`, `og:video:url`,
  `og:video`, `twitter:player:stream`, `<video src>`, `<source src>` or `<audio src>`: guarded
  download.
- iframe or og:video to an allow-listed host (`validate_url(...).ok`): `MediaFound(url=...)`.
- `text/html` og:video on another host: one guarded hop looking for a file or `.m3u8`.
- Blog posts with real text stay text.
- HLS: find `.m3u8` in thin pages. Fetch through the guarded client, pick the lowest-bandwidth
  variant, download segments through the guarded client, concatenate (prepend `EXT-X-MAP` init
  for fmp4), honor `max_media_mb`, refuse any `EXT-X-KEY` other than NONE. If refused live,
  respect it and fall back to metadata. Live case: `livid.com/watch/f4UFumZGvNP-`, whose
  `og:video` is `https://livid.com/embed/f4UFumZGvNP-` (`text/html`).

## Metadata fallback

When thin and the render fails or is disabled: title plus og/meta description of at least 60
chars from the plain GET, labeled `[Only the page's title and description were available]`. Not
after a challenge. Reject metadata containing challenge or login markers. Live case:
`https://api.openverse.org/v1/` (200 html, 2161 bytes, title "Openverse API").

## Non-200 ladder

- 404/410 go to Wayback at once: `https://archive.org/wayback/available?url=...`, require snapshot
  status "200", fetch `https://web.archive.org/web/<ts>id_/<url>` through the guarded client, same
  routing with archive disabled, challenge and login checks apply, prepend
  `[Archived copy from YYYY-MM-DD; the live page returned HTTP 404]`, backend `wayback`.
  Read archived bodies raw: `id_` snapshots replay the original `Content-Encoding` over bytes
  that may not be compressed (seen live), so gunzip only on the gzip magic bytes.
- If no snapshot, the error still contains the status.
- Skip the lookup when the link carries a login, or its query or fragment has a param named like a
  credential (`_PRIVATE_PARAM_RE`: token, secret, signature, password, credential, auth, key, sig,
  session and their variants such as `sig_v1`, `accessKey`, `X-Amz-Credential`), so private links
  are not sent to the archive.
- 5xx and connection errors use normal backoff; on the final attempt the worker uses
  `getattr(self.text_fetcher, "fetch_archived", None)`.
- 403/401/429 and challenges do not escalate. No render on non-200.

## Dispatch

- GitHub exact `github.com` only (`gist.github.com` and `docs.github.com` go generic).
- On `TextFetchError` from GitHub or Notion try generic before the final error, keeping both
  messages. Drive excluded.
- Drive: follow the virus-scan interstitial for public files by parsing the form action and
  hidden inputs (`drive.usercontent.google.com/download?...confirm=t&uuid=`) through the guarded
  client with the cap. An unparseable form keeps the old error. Update the `DriveFetcher`
  docstring.
- Notion: bounded pagination (about 10 chunks) if the response shape confirms live. Linked database
  rows stay a documented gap.

## Downloader ladder

- Instagram empty `--range` result retries the whole post. Live case: `Dd3xE-qDS2N` with
  `img_index=9`.
- gallery-dl `DownloadError` (not `PostUnavailableError`) falls to yt-dlp once, unless the detail
  reads as a refusal (`is_refusal`: an HTTP 401, 403, 429 or 451, forbidden, unauthorized, rate
  limit, challenge, checkpoint, login, sign in). The submitted URL is removed first, so a post id
  of 403 isn't one, and a bare number or "rate" isn't one either: 401 is also a yt-dlp format id.
- New `NoMediaError(DownloadError)` for "unsupported url|no video|no media|nothing to
  download|requested format".

## Worker fallback

`NoMediaError` at once, plain `DownloadError` on the last attempt, and an empty transcript before
`EmptyTranscriptError` go to text capture, only for hosts in typed
`text_capture.media_text_fallback_domains` (x.com, twitter.com, facebook.com, fb.watch,
linkedin.com, reddit.com), requiring at least about 200 chars. `PostUnavailableError` stays
terminal. A refusal (`is_refusal`) keeps its normal retries but never goes to text capture. If the
fallback fails, raise the original error with the fallback reason appended.

## Allow-list additions

`download.allowed_domains` adds loom.com, reddit.com, v.redd.it, soundcloud.com, twitch.tv,
rumble.com, dailymotion.com, ted.com, streamable.com, wistia.com, wistia.net, anonymous (no new
cookies).
Owner's direction; document it in `CLAUDE.md`'s risk posture.

## Queue and validators

- `_embedded_url` handles a bare whole-string `domain.tld/path` (adds `https://`), `<https://...>`,
  and markdown links.
- New `unwrap_redirect` for `l.instagram.com/?u=`, `l.facebook.com/l.php?u=`, `lm.facebook.com`,
  `google.com/url?q=|url=`, `linkedin.com/safety/go?url=`, `youtube.com/redirect?q=`. The target is
  still validated.
- Multi-link pastes register the extras in `sync_queue_file_into_state` and `add_url`'s mutate;
  the first record is returned.

## Tests

Failing test first. Guard regressions to test: the private-target guard on every new fetch
(embed, archive, Drive confirm, HLS segments), size caps, no cookies on the text path, no
escalation on 403/401/429, yt-dlp never receives an unvalidated host. The Drive "too large" test
in `tests/test_text_fetcher.py` must change to following the confirm form; the 404 test must still
contain "404".
