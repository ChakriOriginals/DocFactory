COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env

.PHONY: setup up down seed test lint fmt eval migrate

.env:
	cp .env.example .env

setup: .env
	uv sync --all-packages --all-groups

## Start the local stack (postgres, minio, elasticmq, phoenix) and ensure
## the bucket + queues exist — the same ensure logic the services run on startup.
up: .env
	$(COMPOSE) up -d --wait
	uv run python -m docfactory_core.bootstrap

down: .env
	$(COMPOSE) down

## Destroy the stack INCLUDING data volumes (db rows, stored objects, traces).
reset: .env
	$(COMPOSE) down -v

# DYLD_FALLBACK_LIBRARY_PATH lets WeasyPrint find Homebrew's pango on macOS; harmless elsewhere.
seed: .env
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python data/synth/generate.py --count 500 --upload --previews

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

eval:
	@echo "make eval is wired in Phase 1.5 (packages/evals)"; exit 1

migrate:
	@echo "make migrate is wired in Phase 1.1 (first Alembic migration)"; exit 1
