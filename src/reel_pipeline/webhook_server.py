"""Webhook ingestion server.

Accepts POST /webhook {"url": "..."} requests authenticated with a shared
secret (REEL_WEBHOOK_SECRET), validates + dedups the URL via QueueManager,
and kicks off a background worker run so accepted items get processed
without blocking the HTTP response.

No browser automation, cookies, or account credentials are ever involved -
this is the "webhook ingestion" path called out in CLAUDE.md as the safe
alternative to scraping platforms like Instagram directly.
"""

from __future__ import annotations

import hmac
import threading

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from reel_pipeline.config import Settings, get_settings
from reel_pipeline.logging_setup import (
    configure_logging,
    get_logger,
    log_context,
    resolve_log_level,
)
from reel_pipeline.models import ItemStatus, QueueSource
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.worker import build_worker

logger = get_logger(__name__)

# Minimal, dependency-free submission page - lets any browser on the tailnet send a
# link without needing the iOS Shortcut. The secret is never embedded in the page's
# HTML (a prior version did, meaning anyone who could merely load GET / - any device
# reaching this host/port, not just ones handed the secret - could read it straight
# out of the page source). Instead the page prompts for the secret once and keeps it
# in the browser's localStorage, so the value only ever exists client-side after the
# user (who already knows it) types it in - the server never re-serves it.
_FORM_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send to Reel Pipeline</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; background: #111; color: #eee;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         margin: 0; padding: 1.5rem; box-sizing: border-box; }
  .card { width: 100%; max-width: 420px; }
  h1 { font-size: 1.1rem; font-weight: 600; margin-bottom: 1rem; }
  input[type=text] { width: 100%; padding: 0.9rem; font-size: 1rem; border-radius: 8px;
                      border: 1px solid #444; background: #222; color: #eee;
                      box-sizing: border-box; margin-bottom: 0.75rem; }
  button { width: 100%; padding: 0.9rem; font-size: 1rem; font-weight: 600; border-radius: 8px;
           border: none; background: #3b82f6; color: white; }
  button:active { background: #2563eb; }
  #status { margin-top: 1rem; font-size: 0.9rem; white-space: pre-wrap; word-break: break-word; }
  #secretRow { margin-top: 1.5rem; font-size: 0.8rem; opacity: 0.7; }
  #secretRow a { color: #93c5fd; cursor: pointer; }
</style>
</head>
<body>
<div class="card">
  <h1>Send a link to the pipeline</h1>
  <input type="text" id="url" placeholder="Paste a link..." autofocus autocomplete="off"
         autocapitalize="off" autocorrect="off">
  <button onclick="send()">Send</button>
  <div id="status"></div>
  <div id="secretRow"><a onclick="setSecret()">Set/change webhook secret</a></div>
</div>
<script>
const SECRET_KEY = 'reel_pipeline_webhook_secret';
function setSecret() {
  const value = prompt('Webhook secret (from your .env):', localStorage.getItem(SECRET_KEY) || '');
  if (value) localStorage.setItem(SECRET_KEY, value);
}
async function send() {
  const urlBox = document.getElementById('url');
  const status = document.getElementById('status');
  const url = urlBox.value.trim();
  if (!url) { status.textContent = 'Paste a link first.'; return; }
  let secret = localStorage.getItem(SECRET_KEY);
  if (!secret) { setSecret(); secret = localStorage.getItem(SECRET_KEY); }
  if (!secret) { status.textContent = 'A webhook secret is required.'; return; }
  status.textContent = 'Sending...';
  try {
    const resp = await fetch('/webhook', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Webhook-Secret': secret},
      body: JSON.stringify({url: url})
    });
    const data = await resp.json();
    if (resp.ok && data.accepted) {
      status.textContent = 'Accepted (' + data.status + ')';
      urlBox.value = '';
    } else if (resp.status === 401) {
      status.textContent = 'Rejected: invalid secret. Use "Set/change webhook secret" below.';
    } else {
      status.textContent = 'Rejected: ' + (data.reason || data.detail || 'unknown error');
    }
  } catch (e) {
    status.textContent = 'Request failed: ' + e;
  }
}
document.getElementById('url').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') send();
});
</script>
</body>
</html>"""


class WebhookPayload(BaseModel):
    url: str


class WebhookResponse(BaseModel):
    accepted: bool
    content_id: str
    status: str
    reason: str | None = None


# Single-flight guard: get_actionable_items() has no "currently being processed" concept,
# so two webhook POSTs arriving close together (a double-tap, or the same link submitted
# twice before the first pass finishes) used to each schedule their own background
# run_once(), and both would pick up and fully reprocess the same not-yet-DONE record -
# duplicate downloads, duplicate LLM calls, and (since enrichment titles aren't
# deterministic) two differently-named notes/skills for the same content_id. Confirmed
# happening in production logs before this fix. Only one run_once() loop runs at a time
# per process now; a request that arrives mid-run just marks the loop to run one more
# pass after finishing (draining), rather than spawning a concurrent duplicate.
_worker_lock = threading.Lock()
_rerun_requested = threading.Event()


def _run_worker_in_background(settings: Settings) -> None:
    if not _worker_lock.acquire(blocking=False):
        _rerun_requested.set()
        return
    try:
        while True:
            _rerun_requested.clear()
            try:
                summary = build_worker(settings).run_once()
                log_context(
                    logger,
                    20,
                    "background run_once complete",
                    processed=summary.processed,
                    done=summary.done,
                    failed=summary.failed,
                )
            except Exception as exc:  # noqa: BLE001 - background task must never crash the server
                log_context(logger, 40, "background run_once failed", error=str(exc))
                break
            if not _rerun_requested.is_set():
                break
    finally:
        _worker_lock.release()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_directories()
    configure_logging(settings.logs_dir, level=resolve_log_level(settings.log_level))

    app = FastAPI(title="Reel Knowledge Pipeline Webhook")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz() -> dict[str, object]:
        # Proves the pipeline is actually processing, not just that the FastAPI
        # process is alive - a stuck worker or growing failure backlog would
        # otherwise look identical to "healthy" from this endpoint alone.
        state = QueueManager(settings).load_state()
        records = state.values()
        failed_count = sum(1 for r in records if r.status is ItemStatus.FAILED)
        queue_depth = sum(
            1 for r in records if r.status not in (ItemStatus.DONE, ItemStatus.BLOCKED)
        )
        done_times = [r.updated_at for r in records if r.status is ItemStatus.DONE]
        last_success_at = max(done_times).isoformat() if done_times else None
        return {
            "status": "ok",
            "queue_depth": queue_depth,
            "failed_count": failed_count,
            "last_success_at": last_success_at,
        }

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        settings.require_webhook_secret()  # fail fast with a clear message if unset
        return _FORM_PAGE

    @app.post("/webhook", response_model=WebhookResponse)
    def webhook(
        payload: WebhookPayload,
        background_tasks: BackgroundTasks,
        x_webhook_secret: str | None = Header(default=None),
    ) -> WebhookResponse:
        expected_secret = settings.require_webhook_secret()
        provided_secret = x_webhook_secret or ""
        if not hmac.compare_digest(provided_secret, expected_secret):
            raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Secret")

        queue_manager = QueueManager(settings)
        record = queue_manager.add_url(payload.url, source=QueueSource.WEBHOOK)
        log_context(
            logger,
            20,
            "webhook accepted url",
            content_id=record.content_id,
            status=record.status.value,
        )

        if record.status.value not in ("done", "blocked"):
            background_tasks.add_task(_run_worker_in_background, settings)

        return WebhookResponse(
            accepted=record.status.value != "blocked",
            content_id=record.content_id,
            status=record.status.value,
            reason=record.error,
        )

    return app
