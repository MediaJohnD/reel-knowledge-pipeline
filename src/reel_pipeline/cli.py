"""Typer CLI entry point.

Usage:
    uv run python -m reel_pipeline.cli run-once
    uv run python -m reel_pipeline.cli serve-webhook
"""

from __future__ import annotations

import asyncio
import sys

import typer

from reel_pipeline.config import get_settings
from reel_pipeline.logging_setup import configure_logging, get_logger, resolve_log_level

app = typer.Typer(help="Reel Knowledge Pipeline CLI", no_args_is_help=True)
logger = get_logger(__name__)


@app.command("run-once")
def run_once() -> None:
    """Drain the current queue/webhook backlog once: download, transcribe, enrich, write notes."""
    from reel_pipeline.worker import build_worker

    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.logs_dir, level=resolve_log_level(settings.log_level))

    summary = build_worker(settings).run_once()
    typer.echo(f"processed={summary.processed} done={summary.done} failed={summary.failed}")
    for path in summary.note_paths:
        typer.echo(f"  note: {path}")
    for path in summary.skill_paths:
        typer.echo(f"  skill: {path}")
    for error in summary.errors:
        typer.echo(f"  error: {error}")

    if summary.failed:
        raise typer.Exit(code=1)


@app.command("serve-webhook")
def serve_webhook() -> None:
    """Start the webhook ingestion server (blocking)."""
    import uvicorn

    from reel_pipeline.webhook_server import create_app

    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.logs_dir, level=resolve_log_level(settings.log_level))
    settings.require_webhook_secret()  # fail fast with a clear message if unset

    if sys.platform == "win32":
        # Avoid noisy ERROR-level ConnectionResetError spam from ProactorEventLoop's
        # known ProactorBasePipeTransport._call_connection_lost bug on abrupt client disconnects.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    fastapi_app = create_app(settings)
    uvicorn.run(fastapi_app, host=settings.webhook.host, port=settings.webhook.port)


if __name__ == "__main__":
    app()
