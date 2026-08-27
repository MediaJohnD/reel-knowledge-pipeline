.PHONY: install test lint format format-check typecheck audit lockcheck run-once serve-webhook check

install:
	uv sync

test:
	uv run pytest

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
