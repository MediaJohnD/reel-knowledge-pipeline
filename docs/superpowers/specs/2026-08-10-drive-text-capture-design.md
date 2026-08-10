# Google Drive Document Text Capture (`DriveFetcher`) — Design

Status: implemented 2026-08-10.

## Purpose

`drive.google.com` was scoped into `download.allowed_domains` on 2026-07-16
for one purpose: video files shared via Drive, via yt-dlp's GoogleDrive
extractor. But Drive is a general-purpose file-sharing platform - people
share text docs, markdown, PDFs, PPTs through it just as often as video, and
every `drive.google.com` URL was routed to the media/yt-dlp path
unconditionally, regardless of what the file actually was.

This surfaced as a real failure, not a hypothetical: a shared `.md` skill
file (`agency-agentic-os-skill.md`, a Claude Code skill someone shared)
landed in `failed_permanent` after 5 attempts, each failing with `yt-dlp
failed to download ...: ERROR: [GoogleDrive] ... Unable to download JSON
metadata: HTTP Error 400: Bad Request`. Checked the file's actual Drive
sharing settings directly (via the connected Drive MCP tool) before assuming
anything: `permissions: [{"role": "reader", "type": "anyone"}]` - fully
public. Not a sharing problem. The file's real `mimeType` was
`text/markdown` - it was never a video, so yt-dlp's video-metadata API call
was doomed regardless of permissions.

## Why classification is harder here than for Notion/GitHub

Every other domain in this pipeline's dispatch tables is unambiguous by URL
shape alone - `github.com` is always text, `youtube.com` is always media.
`drive.google.com` genuinely isn't: the same URL shape
(`/file/d/<id>/view`) covers both an actual video file and an arbitrary
document, and there's no way to tell which from the URL.

Two ways to find out were tried and one was rejected:

- **`HEAD` request + `Content-Type` header**: tested live against the real
  failing file - returns `application/octet-stream` regardless of the
  file's actual type. Drive's generic direct-download endpoint doesn't
  expose real MIME type this way. Getting the true `mimeType` needs the
  Drive API v3 (`files.get`), which requires OAuth or an API key - out of
  scope per this project's established no-OAuth/no-API-key text-capture
  posture (the same reasoning that kept GitHub/Notion API-key-free).
- **A metadata-only yt-dlp probe** (`extract_info(url, download=False)`,
  `skip_download: True`): tested live against the real failing file - fails
  the exact same way (`Unable to download JSON metadata: HTTP Error 400`)
  as an actual download attempt would, but without downloading anything.
  This is a reliable "is this actually a video" signal using a dependency
  already in the project, no new credentials. **This is what got built.**

## Design

`validators.classify_url_kind()` special-cases `drive.google.com`: instead
of returning `"media"` immediately (as every other `download.allowed_domains`
host does), it runs `_classify_drive_url_kind()` - the yt-dlp probe above.
Any failure (not a video, network hiccup, anything) falls through to
`"text"` rather than `"media"`; if it's also not text-fetchable,
`DriveFetcher` raises its own clear error, which is strictly more useful
than yt-dlp's opaque "Bad Request" was.

This required one structural fix: `queue_manager._register()` previously
hardcoded `content_kind="media"` for every URL `validate_url()` accepted,
never calling `classify_url_kind()` at all in that branch (it was only
invoked for URLs `validate_url()` had already rejected, as the text-capture
catch-all fallback). Changed to call `classify_url_kind()` unconditionally
and use its answer - a no-op for every other host (they still return
`"media"` immediately, no network call), and the enabling change for Drive's
probe to ever run.

`DriveFetcher` (new, in `text_fetcher.py`) then fetches the file via Drive's
unauthenticated direct-download endpoint
(`https://drive.google.com/uc?export=download&id=<id>`) - a plain GET,
verified live to return the real file bytes directly for a publicly-shared
file, no auth, no JS. The bytes are treated as:
- plain text/markdown, if they decode as UTF-8 and don't look like HTML
- HTML export (Google-native Docs/Sheets/Slides sometimes export this way),
  extracted via `trafilatura`, if they decode as UTF-8 and start with an
  HTML doctype/tag
- otherwise (a binary format), a clear `TextFetchError` naming the gap,
  rather than garbage text or a silently empty note

## Known v1 limitations (accepted, not solved now)

- **Binary document formats** (PDF, PPTX, DOCX, XLSX, ...) aren't parsed -
  no new parsing dependency (`pdfplumber`/`python-docx`/`python-pptx`) was
  added without discussing it first; these fail cleanly with a "binary
  format not supported" error.
- **Files too large for Drive's virus-scan** (roughly >25MB) get an HTML
  interstitial page instead of raw bytes from the direct-download endpoint -
  not handled; out of scope for the "handful of shared items" volume this
  project targets.
- **The yt-dlp probe adds a network round-trip during registration**,
  specifically and only for `drive.google.com` URLs - bounded by a 15s
  socket timeout so a hung probe can't block registration indefinitely.

## Testing

- `tests/test_validators.py`: `classify_url_kind()` returns `"media"` for a
  Drive URL when the yt-dlp probe succeeds, `"text"` when it fails (fake
  `yt_dlp` module injection, no real network call).
- `tests/test_queue_manager.py`: end-to-end registration test proving a
  Drive video URL gets `content_kind="media"` and a Drive document URL gets
  `content_kind="text"` (not stuck as an unfetchable media item heading for
  `failed_permanent`).
- `tests/test_text_fetcher.py`: `DriveFetcher` tests for plain-text
  extraction, HTML-export extraction via `trafilatura`, the binary-format
  error path, the missing-file-id error path, and an HTTP-failure path.
  `DispatchingTextFetcher` routing test proving `drive.google.com` reaches
  `DriveFetcher`, not the generic HTML fetcher.
- All of the above verified live against the real failing file
  (`agency-agentic-os-skill.md`) before any test was written, the same
  methodology used for the Notion API design.
