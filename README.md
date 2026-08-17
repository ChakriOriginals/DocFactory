# DocFactory

Multi-tenant document-intelligence platform, built in strict phases to a production bar.
Current state: **Phase 0** — local stack + synthetic invoice corpus. One hardcoded
invoice pipeline (upload → store → queue → parse → extract → validate) lands in Phase 1.

## Layout

```
apps/api/          FastAPI: upload + status endpoints
apps/worker/       queue consumers: parse + extract
packages/core/     shared: config, logging, storage/queue clients, db models, llm client
packages/evals/    golden set + eval harness
data/synth/        synthetic invoice generator (Jinja2 + WeasyPrint)
infra/compose/     local stack: postgres, minio, elasticmq, phoenix
docs/              ADRs, samples, eval results
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (manages Python 3.12 and all deps)
- Docker with compose v2 (this machine uses colima: `colima start`)
- WeasyPrint needs native libs on macOS: `brew install pango`

## Quickstart

```bash
make setup   # create .env from example, install workspace
make up      # start stack, wait for healthchecks, ensure bucket + queues
make seed    # generate 500 synthetic invoices + ground truth, upload to MinIO
make test    # pytest (integration tests auto-skip if the stack is down)
```

Consoles: MinIO http://localhost:9001 · Phoenix traces http://localhost:6006 ·
ElasticMQ http://localhost:9325

Config is env-only via `.env` (see `.env.example`). `MODEL_PROVIDER=mock` is the
default — real Anthropic calls happen only when explicitly switched on.
