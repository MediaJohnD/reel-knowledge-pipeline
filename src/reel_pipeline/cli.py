"""Typer CLI entry point.

Usage:
    uv run python -m reel_pipeline.cli run-once
    uv run python -m reel_pipeline.cli serve-webhook
"""

from __future__ import annotations

import asyncio
import errno
import socket
import sys
import time
from collections.abc import Callable

import typer

from reel_pipeline.config import get_settings
from reel_pipeline.logging_setup import configure_logging, get_logger, resolve_log_level

app = typer.Typer(help="Reel Knowledge Pipeline CLI", no_args_is_help=True)
logger = get_logger(__name__)


def _wait_for_bindable(
    host: str,
    port: int,
    timeout: float,
    poll_interval: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Block until (host, port) is bindable, or re-raise the bind error at `timeout`.

    uvicorn treats an address that doesn't exist *yet* as fatal: it logs
    "could not bind on any address" and exits (code 3) about two seconds in.
    This machine binds `REEL_WEBHOOK_HOST` to a Tailscale IP, and at logon the
    Tailscale service may not have assigned it yet - so the server lost a race
    with its own network stack and died. Seen 2026-09-10, and again 2026-09-20.

    Recovery used to live entirely outside the process, in Task Scheduler's
    restart-on-failure. That is not a safe thing to depend on here: it stopped
    after two attempts on 2026-09-20 and left the server down for hours while
    the address became bindable in between, and it cannot be audited on this
    machine because the TaskScheduler/Operational log is disabled. Waiting
    in-process turns a not-yet-assigned address into a slow start instead of a
    failed one, and needs nothing external to be configured correctly.

    Only EADDRNOTAVAIL is waited on - that specifically means "this address does
    not exist on this host". Every other error, EADDRINUSE above all, returns
    immediately so uvicorn still reports a genuinely occupied port the way it
    always has. Set `timeout` to 0 to disable waiting entirely.

    A probe bind is inherently advisory: the port could be taken in the moment
    between this check and uvicorn's own bind. That is fine - uvicorn then fails
    exactly as it does today.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind((host, port))
            return
        except OSError as exc:
            if exc.errno != errno.EADDRNOTAVAIL:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(
                    "bind address never became available",
                    extra={"context": {"host": host, "port": port, "waited_seconds": timeout}},
                )
                raise
            logger.warning(
                "bind address not available yet, waiting",
                extra={
                    "context": {
                        "host": host,
                        "port": port,
                        "seconds_remaining": round(remaining, 1),
                    }
                },
            )
            sleep(min(poll_interval, remaining))


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
    if summary.provider_outage:
        typer.echo(f"  stopped early - LLM provider account failure: {summary.provider_outage}")
        typer.echo("  remaining items were left PENDING and will retry once it is fixed.")

    if summary.failed:
        raise typer.Exit(code=1)


@app.command("retry")
def retry(
    content_id: str | None = typer.Argument(
        default=None, help="content_id of a FAILED_PERMANENT record to reset."
    ),
    all_failed_permanent: bool = typer.Option(
        False,
        "--all-failed-permanent",
        help="Reset every FAILED_PERMANENT record instead of a single content_id.",
    ),
) -> None:
    """Reset FAILED_PERMANENT record(s) back to PENDING so the next run-once retries them.

    Only records currently in FAILED_PERMANENT status are touched - a
    content_id that's DONE, still FAILED (mid-backoff), etc. is left as-is.
    """
    from reel_pipeline.queue_manager import QueueManager

    if content_id is None and not all_failed_permanent:
        typer.echo("Provide a content_id or use --all-failed-permanent", err=True)
        raise typer.Exit(code=1)
    if content_id is not None and all_failed_permanent:
        typer.echo("Provide either a content_id or --all-failed-permanent, not both", err=True)
        raise typer.Exit(code=1)

    settings = get_settings()
    settings.ensure_directories()
    reset_ids = QueueManager(settings).reset_for_retry(
        content_id=content_id, all_failed_permanent=all_failed_permanent
    )
    if not reset_ids:
        typer.echo("No matching FAILED_PERMANENT records found.")
        raise typer.Exit(code=1)
    for reset_id in reset_ids:
        typer.echo(f"reset: {reset_id}")


@app.command("organize-vault")
def organize_vault_cmd() -> None:
    """Normalize each note's filename to its frontmatter title slug, in place -
    never moves a note between folders (vault foldering is manual/topic-based,
    not something this pipeline decides). Safe to run repeatedly - already-
    correct notes are skipped.
    """
    from reel_pipeline.vault_organizer import find_duplicate_notes, organize_vault

    settings = get_settings()
    settings.ensure_directories()
    configure_logging(settings.logs_dir, level=resolve_log_level(settings.log_level))

    changes = organize_vault(settings)
    if not changes:
        typer.echo("vault already organized, no changes")
    else:
        for change in changes:
            typer.echo(f"  {change}")
        typer.echo(f"{len(changes)} note(s) touched")

    duplicates = find_duplicate_notes(settings)
    if duplicates:
        typer.echo(f"{len(duplicates)} duplicate note(s) found:")
        for finding in duplicates:
            typer.echo(f"  DUPLICATE: {finding}")
    else:
        typer.echo("no duplicate notes found")


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

    # Must precede uvicorn.run: uvicorn exits rather than waiting for a bind
    # address that hasn't been assigned yet. See _wait_for_bindable.
    _wait_for_bindable(
        settings.webhook.host,
        settings.webhook.port,
        timeout=settings.webhook.bind_retry_seconds,
    )

    fastapi_app = create_app(settings)
    uvicorn.run(fastapi_app, host=settings.webhook.host, port=settings.webhook.port)


if __name__ == "__main__":
    app()
