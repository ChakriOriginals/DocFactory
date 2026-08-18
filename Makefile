COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env

.PHONY: setup up down seed test lint fmt eval migrate calibrate calibrate-fit

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
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python data/synth/generate.py --type invoice --count 500 --upload --previews
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python data/synth/generate.py --type purchase_order --count 120 --upload --previews

api: .env
	uv run uvicorn docfactory_api.main:app --port 8000

worker: .env
	uv run python -m docfactory_worker.main

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

## Calibration study (2.2c): build the labelled dataset, then fit offline.
## Corruption is enabled here and only here — the mock stays clean everywhere else.
calibrate: .env
	MOCK_CORRUPTION_RATE=0.35 uv run python -m docfactory_evals.calibrate

## Re-fit from already-stored signal vectors, no extraction pass.
calibrate-fit: .env
	uv run python -m docfactory_evals.calibrate --fit-only

## Field-accuracy eval on the golden set, per document type. Uses
## MODEL_PROVIDER from .env (mock by default). One type: --type purchase_order.
## For a real number: MODEL_PROVIDER=anthropic make eval
eval: .env
	uv run python -m docfactory_evals.run

## Unit-cost rollup from recorded usage events (what the worker actually spent).
costs: .env
	uv run python -m docfactory_evals.costs

migrate: .env
	uv run alembic upgrade head
