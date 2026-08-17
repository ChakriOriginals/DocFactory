# ADR-001: Pipeline architecture for the invoice vertical slice

Status: accepted · Date: 2026-08-17

## Context

One hardcoded invoice pipeline, built to a production bar, on which the
platform (multi-tenancy, pipeline config, scaling) will be layered. The local
stack mirrors AWS: MinIO behind the S3 API, ElasticMQ behind the SQS API, so
promotion changes configuration, not code.

## Decisions

**Queues + a document state machine, not synchronous processing.** Upload
returns in milliseconds with a 202; parse and extract run as queue consumers.
The document row's `status` is the single source of truth
(`received → parsing → parsed → extracting → extracted`, terminal
`needs_ocr`/`failed`). Consumers coordinate through that state under row
locks — never through message ordering or delivery counts, because SQS
guarantees neither. This is what lets throughput scale by adding consumers
later without touching semantics.

**Idempotency via SHA-256, enforced by the database.** The object key is
content-addressed (`{tenant}/incoming/{sha256}.pdf`) and `(tenant_id,
sha256)` is a unique constraint, so "same bytes, same tenant" is one document
even under concurrent uploads — the INSERT loser returns the winner's id.
Re-uploads cost one no-op object PUT and zero duplicate work. Every handler
tolerates redelivery: completed stages ack without rework; interrupted stages
reprocess (stage writes are re-runnable by construction).

**Poison messages: DLQ after 3 receives, enforced by the queue layer.** Each
queue is created with a redrive policy (`maxReceiveCount: 3`). A handler that
fails simply doesn't delete the message; retry pacing (visibility timeout)
and quarantine are the queue service's job, identical in real SQS. Three
receives forgives two transient failures (network blips, deploys) before
declaring a message poisoned. The error is captured on the document row
(`last_error`, status `failed` on the final attempt); the DLQ holds the
original message for re-drive. A bad document can never crash a worker or
block a queue.

**`needs_ocr` is a terminal state, not a failure.** ~30% of the corpus is
image-only PDFs that Tier-A parsing (pdfplumber) correctly yields no text
for. That's an *expected outcome* of a deliberately unbuilt feature (the OCR
tier), so those documents transition to `needs_ocr` — they never retry, never
DLQ, never alarm. Failure states must mean "something went wrong", or they
become noise nobody reads.

**Mock-mode LLM is mandatory and default.** `MODEL_PROVIDER=mock` serves
schema-valid heuristic extractions with simulated latency and no network.
Tests, CI, and bulk runs can exercise the entire pipeline — including the
retry loop, persistence, and eval plumbing — at zero API cost, forever. Real
calls happen only when explicitly configured, on small batches. This is a
budget guardrail *and* a determinism guarantee for tests.

**Normalization is centralized in one module.** Locale canonicalization
(euro `25.832,09 €`, US `$1,152.24`, three date formats) lives in
`docfactory_core.normalize` and is consumed by both the extraction schema's
validators and the eval comparator. If those two disagreed, a normalization
bug would be indistinguishable from an extraction failure — the eval would
lie. One implementation, tested first (R2), everywhere.

## Consequences

- At-least-once delivery means rare duplicate side effects are possible
  (e.g. a second extraction row if a visibility timeout expires mid-run);
  extraction rows are append-only history and reads take the latest, so this
  is tolerated rather than prevented.
- The eval harness runs extraction in-process rather than through the queues;
  it measures extraction quality, while pipeline transport is covered by
  integration tests.
- App-side `ensure_infra()` creates buckets/queues locally; in AWS this
  becomes Terraform, and ensure degrades to a startup assertion.
