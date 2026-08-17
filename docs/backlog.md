# Backlog — noted and deliberately deferred

Later-phase work spotted during earlier phases. Do not build ahead of phase.

## From Phase 0

- **OCR path (Tier B) for scanned PDFs.** ~30% of the synthetic corpus is
  rasterized with no text layer. Phase 1 routes these to the terminal
  `needs_ocr` state (not the DLQ — see ADR-001); an OCR tier drains that
  state later.
- **Containerize api/worker into compose.** Both run host-side via
  `make api` / `make worker` today (fast dev loop). Moving them into compose
  is closer to prod and is a prerequisite for the AWS phase.
- **Terraform for AWS infra.** App-side `ensure_infra()` is the local/dev
  story; in AWS, buckets/queues/redrive policies become IaC and ensure
  degrades to a startup assertion.
- **Metering logic.** `prompt_tokens`/`completion_tokens`/`latency_ms` are now
  populated per extraction, but `cost_usd` is not computed and nothing
  aggregates spend per tenant or per document.
- **Corpus edge cases for later robustness work:** discounts, shipping lines,
  multi-page invoices, multi-currency, credit notes, handwritten-ish fields.

## From Phase 1

- **Euro small-caps vendor names defeat the mock extractor.** WeasyPrint
  renders `font-variant: small-caps` as separate text runs, so pdfplumber
  emits e.g. `B W S & C . Kg a` / `auer einhold tiftung o a` for "Bauer
  Weinhold Stiftung & Co. KGaA". The mock repairs the simple two-line case
  only, which is why euro `vendor` sits at 11% in mock mode. Do not "fix" the
  generator to dodge this — it is a realistic text-extraction artifact and a
  fair test of the extractor. Re-measure in `anthropic` mode before treating
  it as a real gap.
- **Eval runs extraction in-process, not through the queues.** It measures
  extraction quality with no services required (so it runs in CI); pipeline
  transport is covered by the integration tests. A later end-to-end eval mode
  that drives the real API + workers would also exercise throughput and SLA.
- **At-least-once can produce duplicate extraction rows.** If a visibility
  timeout expires mid-extract, a redelivery may write a second row (both are
  valid; reads take the latest). Accepted for now — see ADR-001 consequences.
  A stage-scoped idempotency key would make this exactly-once-ish.
- **Structured-outputs cache warming.** Each new JSON schema pays a one-time
  compilation cost on first use in `anthropic` mode; irrelevant at current
  volume, worth knowing when batches get large.
- **No CI workflow yet.** `make test` and `make lint` are CI-ready and green;
  wiring GitHub Actions is a later phase.
