from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from reel_pipeline import webhook_server
from reel_pipeline.config import Settings
from reel_pipeline.models import ItemStatus, QueueSource, StateRecord
from reel_pipeline.queue_manager import QueueManager
from reel_pipeline.webhook_server import create_app


def make_settings(tmp_path) -> Settings:
    return Settings(project_root=tmp_path, webhook_secret="test-secret-123")


def test_healthz_returns_ok_with_empty_queue_stats(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "queue_depth": 0,
        "failed_count": 0,
        "last_success_at": None,
    }


def test_healthz_reports_queue_depth_failed_count_and_last_success(tmp_path):
    settings = make_settings(tmp_path)
    qm = QueueManager(settings)
    now = datetime.now(UTC)
    qm.save_state(
        {
            "done1": StateRecord(
                content_id="done1", url="https://youtube.com/1", normalized_url="https://youtube.com/1",
                source=QueueSource.QUEUE_FILE, status=ItemStatus.DONE, added_at=now, updated_at=now,
            ),
            "failed1": StateRecord(
                content_id="failed1", url="https://youtube.com/2",
                normalized_url="https://youtube.com/2",
                source=QueueSource.QUEUE_FILE, status=ItemStatus.FAILED,
                added_at=now, updated_at=now,
            ),
            "pending1": StateRecord(
                content_id="pending1", url="https://youtube.com/3",
                normalized_url="https://youtube.com/3",
                source=QueueSource.QUEUE_FILE, status=ItemStatus.PENDING,
                added_at=now, updated_at=now,
            ),
        }
    )
    client = TestClient(create_app(settings))

    response = client.get("/healthz")

    body = response.json()
    assert body["queue_depth"] == 2  # failed1 + pending1, not done1
    assert body["failed_count"] == 1
    assert body["last_success_at"] == now.isoformat()


def test_index_serves_form_page_without_embedding_secret(tmp_path):
    client = TestClient(create_app(make_settings(tmp_path)))

    response = client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Send a link to the pipeline" in response.text
    assert "test-secret-123" not in response.text


def test_index_raises_clear_error_when_secret_unset(tmp_path):
    settings = Settings(project_root=tmp_path)
    client = TestClient(create_app(settings), raise_server_exceptions=False)

    response = client.get("/")

    assert response.status_code == 500


def test_webhook_rejects_missing_secret(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        webhook_secret="test-secret-123",
    )
    client = TestClient(create_app(settings))

    response = client.post("/webhook", json={"url": "https://www.youtube.com/watch?v=abc"})

    assert response.status_code == 401


def test_concurrent_background_triggers_never_run_worker_concurrently(tmp_path, monkeypatch):
    # Reproduces (as a regression test) a real bug: two webhook POSTs for the same
    # item arriving close together used to each spawn their own run_once(), and both
    # would pick up and fully reprocess the same not-yet-DONE record - duplicate
    # downloads/LLM calls, and (since enrichment titles aren't deterministic) two
    # differently-named notes/skills for the same content_id.
    settings = make_settings(tmp_path)
    lock = threading.Lock()
    active = 0
    max_concurrent = 0
    run_count = 0

    class FakeSummary:
        processed = 0
        done = 0
        failed = 0

    class FakeWorker:
        def run_once(self):
            nonlocal active, max_concurrent, run_count
            with lock:
                active += 1
                max_concurrent = max(max_concurrent, active)
            time.sleep(0.05)
            with lock:
                active -= 1
                run_count += 1
            return FakeSummary()

    monkeypatch.setattr(webhook_server, "build_worker", lambda settings: FakeWorker())

    t1 = threading.Thread(target=webhook_server._run_worker_in_background, args=(settings,))
    t1.start()
    time.sleep(0.01)  # ensure t1 has acquired the lock before t2 starts
    t2 = threading.Thread(target=webhook_server._run_worker_in_background, args=(settings,))
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert max_concurrent == 1  # never ran two run_once() calls at the same time
    assert run_count == 2  # but t2's trigger still caused a drain pass, not silently lost
