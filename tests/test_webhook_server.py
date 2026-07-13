from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

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
