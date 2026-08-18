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

## From Phase 3b

- **No pipeline management API yet.** Definitions are seeded by migration and
  loaded from `config/pipelines/*.json`. `parse_definition` is the validation
  boundary and is tested as such, but the POST/PUT endpoints that would let a
  tenant author one, and the version-bump-on-edit behaviour, are not built.
- **Rule vocabulary is small on purpose.** `sum_equals`, `terms_equal`,
  `product_equals`, `date_order`, `regex`, `required`. Adding a rule type is a
  code change by design — arbitrary tenant-supplied predicates would be
  remote code execution.

## From Phase 3c

- **The confidence model is fitted on invoices and served to every type.**
  `PipelineDefinition.confidence_model_path` exists and is parsed, but
  `get_confidence_model()` still loads the one path in settings. Purchase
  orders are therefore routed by weights fitted on invoice errors. The
  features are kind-driven so the vectors are meaningful, but the *calibration*
  is not this type's. Wiring per-pipeline model paths is a small change; the
  work is the study that produces a second model.
- **The corruption/calibration study is invoice-only.** `ERROR_CLASS_FIELDS`
  names invoice fields (`total`, `vendor`, `invoice_date`), and
  `make calibrate` reads the invoice labels file. A per-type study needs the
  error classes expressed against field *kinds* the way the signals now are.
- **The mock backend needs reading hints per document type.**
  `config/mock_extraction/<slug>_v<n>.json` tells the mock how a layout prints
  its fields. That is deliberate — the mock is a stand-in for a model, and a
  type without hints must run in `anthropic` mode — but it does mean a new
  type is two config files, not one.
- **An empty `line_items` list no longer triggers a schema retry.** Pydantic's
  `min_length=1` is gone and the JSON-Schema equivalent (`minItems`) is
  outside the structured-outputs subset the schema deliberately stays within.
  An empty table now fails `line_items_sum_to_subtotal` and routes to review
  instead of failing the document outright — arguably the better outcome, but
  it is a behaviour change, recorded here rather than discovered later.
- **The eval golden set is capped at a third of a corpus.** One generic rule
  rather than per-type configuration, so the invoice set stays at 100 and the
  smaller purchase-order corpus yields 40. If a corpus is ever generated much
  smaller than its golden size, that cap is what will surprise someone.
- **README still describes Phase 0.** It predates every phase since; the
  quickstart commands work but the state description does not.

## From Phase 3a

- **The app DB role is created by a migration.** Roles are cluster-level, not
  schema-level, so this is unusual; it lives there to keep local dev
  reproducible from `make migrate` alone. In AWS the role and its password
  belong in Terraform + Secrets Manager, and the migration should assert the
  role exists rather than create it.
- **`tenants.id` is a text slug, not a uuid.** It doubles as the object-store
  prefix and appears in every log line, and every existing tenant_id column
  already holds exactly this value — so a uuid would have meant rewriting five
  tables for no isolation benefit. The RLS policy compares text.
- **`tenants`/`api_keys` carry no RLS policy** because authentication must read
  them before a tenant context exists. They are readable by the app role and
  writable only by the owner. A tenant-management API needs its own
  authorization story (admin keys), which does not exist yet.
- **Cost is a flat per-call price.** Real token-based metering is Phase 4;
  `cost_usd` is now populated so budget caps are enforceable and testable.
- **Budget check is not transactional with the spend.** Two concurrent workers
  can both pass the cap check and both charge, overshooting by one call each.
  Acceptable at current volume; a reservation or a DB-side counter fixes it.
- **No admin API for tenants or keys.** Tenants are seeded by migration and
  keys issued via `core.auth` from a shell. Fine while there is one tenant.

## From Phase 2.3c

- **No reviewer UI.** The review queue is API + tests only. A deliberately
  ugly, time-boxed React page (queue list, document view, flagged cells,
  approve/correct) is a separate task — the API shape is settled, so it is
  presentation work, not design work.
- **eval_cases are written but not yet consumed.** Human corrections
  accumulate as labelled data; nothing feeds them into `make eval` or the
  calibration refit yet. That wiring is what makes review compound and should
  come before anyone leans on the queue at volume.
- **SLA is detectable, not alerted.** `GET /review/queue` exposes depth,
  oldest age and breach count, and breached tasks are queryable. Alerting,
  burn-rate and backpressure are Phase 5.
- **SLA is a single global value.** `review_sla_hours` is one setting; Phase 3
  makes it per-tenant/per-pipeline. Deadlines are frozen onto tasks at
  creation, so that change cannot retroactively breach queued work.
- **No reviewer identity or audit trail.** Resolutions record what changed but
  not who changed it; that needs the auth work in Phase 3.

## From Phase 2.3a

- **The operating-point rule picks a threshold that lets arithmetic errors
  through.** "Maximise coverage subject to precision >= 99%" selects 0.42,
  which sits *below* the score band of fields with one failed validation rule,
  so 4 `arithmetic_drift` totals are auto-approved. The same v2 model at 0.67
  yields 3 survivors and 99.57% precision for 1.4pp less coverage. Consider
  changing the selection rule to "highest precision within 2pp of max
  coverage", or simply operate at 0.67 — a one-line config change.
- **Coverage is less discrete but still bounded below.** Distinct selectable
  coverage levels went 7 -> 14 and distinct scores 6 -> 15, because the
  1/(1+days) date distance is continuous on *erroneous* documents. Clean
  documents still cluster at one score, so the gap between 0% and ~93%
  coverage remains: fine control exists only inside the 93-97% band.
- **Truncated/casing vendor errors are the new residual.** With dates handled,
  2 of the 3 survivors at threshold 0.67 are vendor errors that token-shape
  and groundedness both miss.

## From Phase 2.2c

- **99% auto-approve precision is unreachable, and the cause is one error
  class.** Best achievable on the held-out golden set is 98.29% precision at
  94.9% coverage. Of the 12 wrong fields that survive auto-approval, 10 are
  `shifted_date` — an injected error that moves the invoice date earlier while
  leaving every validation rule satisfied. Every *detectable* class scores
  97-100% precision. Closing the gap needs a date-corroboration signal (does
  the extracted date appear near a date label in the source text?), not more
  weight tuning.
- **The confidence score is discrete, not continuous.** 702 of 740 holdout
  fields receive an identical score because their signal features are all
  zero, so coverage jumps from 0% to ~95% with nothing selectable between.
  Only ~7 operating points exist. Phase 2.3 picks one of them; it cannot dial
  coverage finely. Finer control needs a feature that varies across *clean*
  documents — a per-field extraction margin, for example.
- **`needed_retry` fits to exactly zero weight.** No document in the corpus
  needed a schema retry, so the feature carries no information. Keep it (it
  will matter in `anthropic` mode) but know it is currently inert.
- **Calibration has never been run against a real model.** Everything is mock
  + injected corruption. The error *distribution* from a real model will
  differ, so the fitted weights should be re-fit from an `anthropic`-mode
  dataset before anyone trusts the threshold in production.
- **Holdout is small.** 74 documents / 740 fields, with 42 wrong fields total.
  Per-error-class cells are 30-100 fields, so those precision figures carry
  wide error bars.

## From Phase 2.2a

- **Groundedness detects hallucination, not truncation — and the corpus
  contains no hallucinations.** Measured over the same 74 digital golden
  docs, adding the groundedness signal changed precision/recall by exactly
  nothing (100% / 37.5%). All ten invisible errors were *truncations* of
  small-caps runs ("B B AG" for "Bloch Bloch AG"), and every one scored
  groundedness 1.0 because the kept characters genuinely are in the source
  text. The signal is correct but untested by this corpus; 2.2b's injected
  wrong-but-plausible vendors are what will exercise it.
- **Casing-only errors remain undetectable.** "RöhRicht" for "Röhricht" is
  the one remaining miss (recall 93.8%). No shape, arithmetic, or
  groundedness signal can see it. Catching it needs either case-normalized
  comparison against the source span or a second extraction pass.
- **Groundedness cannot flag over-truncation by construction.** A strict
  substring of the correct answer is, definitionally, grounded. Any future
  "coverage" signal (did we take *enough* of the relevant span?) is a
  different measurement from "is this invented?".

## From Phase 2.1

- **The deterministic scorer is blind to free-text errors — this is structural,
  not a tuning problem.** Measured over the 74 digital golden docs in mock
  mode: 58 fully correct (mean confidence 1.0000), 16 with a wrong field (mean
  0.9625). Only 6 documents scored below 1.0, and all 6 were genuinely flawed
  (precision 6/6), but **10 of the 16 wrong documents scored a perfect 1.00**
  — recall 37.5%. Every error was the euro `vendor` field. The cause: all five
  validation rules constrain money and dates, so nothing whatsoever constrains
  vendor. Refitting weights cannot fix a missing signal.
- **Groundedness is the missing signal class (top input to the 2.2 study).**
  Check whether an extracted value can be traced back to the parsed source
  text. Note the naive version fails here: the euro vendor is split across
  lines ("B" / "aum"), so exact substring matching reports "not present" for a
  *correct* extraction. It needs whitespace-insensitive or fuzzy matching
  against the text, which is real work, not a one-liner.
- **Calibration needs error variance that mock mode does not produce.** 68 of
  74 documents score exactly 1.00, so a curve fitted on this would be
  degenerate. Either run the study in `anthropic` mode or add a deliberate
  corruption mode to the mock client to manufacture labelled errors.
- **Confidence weights are unfitted priors.** `core/confidence.py` carries
  hand-picked penalties. Ordering is meaningful; the absolute number is not a
  probability and nothing should gate on it until 2.2 fits it.
