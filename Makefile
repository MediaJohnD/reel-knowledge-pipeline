.PHONY: install test browser-test lint format format-check typecheck audit lockcheck run-once serve-webhook check

install:
	# settings.yaml defaults to the local transcription backend; a bare `uv sync`
	# uninstalls faster-whisper and every video item then fails.
	uv sync --extra local-whisper

test:
	uv run pytest

# The real-Chromium tests that need no network. Separate from `check` because they
# need `uv run playwright install chromium` first.
browser-test:
	uv run pytest -m playwright -k "dns_rebinding or route_handler"

lint:
	uv run ruff check .

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

audit:
	uv audit

lockcheck:
	uv lock --check

check: lockcheck lint format-check typecheck test audit

run-once:
	uv run python -m reel_pipeline.cli run-once

serve-webhook:
	uv run python -m reel_pipeline.cli serve-webhook
