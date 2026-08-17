# Backlog — noted and deliberately deferred

Later-phase work spotted during earlier phases. Do not build ahead of phase.

- **OCR path (Tier B) for scanned PDFs.** ~30% of the synthetic corpus is
  rasterized with no text layer; in Phase 1 those documents will (correctly)
  fail Tier-A parse and land in the DLQ. An OCR tier drains them later.
- **Containerize api/worker into compose.** Phase 0 has no app processes;
  decide at Phase 1 whether apps run host-side via uv (faster dev loop) or as
  compose services (closer to prod).
- **Terraform for AWS infra.** App-side ensure_infra() is the local/dev story;
  in AWS, buckets/queues/redrive policies become IaC and ensure degrades to a
  startup assertion.
- **Metering logic.** Cost/token columns are in the Phase 1 schema, but
  computing and aggregating them is deferred.
- **Eval golden-set split must hash doc_id, not file bytes.** PDF bytes are
  not identical across regenerations (embedded timestamps); doc_id is the
  stable identity. Corpus *data* is seed-deterministic and test-pinned.
- **Corpus edge cases for later robustness work:** discounts, shipping lines,
  multi-page invoices, multi-currency, credit notes, handwritten-ish fields.
