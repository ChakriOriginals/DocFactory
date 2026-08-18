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

- **Purchase orders still borrow the invoice confidence model.** Phase 4a
  wired per-pipeline model loading and made the borrow explicit on every
  extraction row (`confidence_calibration`), which was the correctness gap.
  What remains is the study that fits a purchase-order model — blocked on the
  corruption module below, and on a 120-document corpus being thin for a fit.
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

## From Phase 4c

- **The Terraform has never been applied.** It validates and is formatted, but
  no AWS account was reachable when it was written, so every cost figure in
  the runbook is a rate card number and the destroy round trip is a documented
  procedure rather than an observation. The first apply is the test; expect to
  find IAM permissions that are a shade too narrow.
- **No TLS on the demo URL.** The ALB listener is HTTP because the stack has no
  domain and therefore no ACM certificate. Fine for a mock-mode demo, not for
  anything carrying a real document.
- **Terraform state is local by default.** A commented S3 backend block is
  ready; CI deploys need it, because state in a runner is state you have lost.
- **The API blocks startup on infra verification.** `ensure_infra` retries for
  up to 30 seconds before the app serves `/healthz`. The ECS health-check grace
  period covers it, but a genuinely missing queue means a task that never
  becomes healthy and is replaced forever — loud, but a crash loop rather than
  a clear message in one place.
- **`ecs.tf` hardcodes container names in the CI roll step.** The deploy job
  patches `containerDefinitions[0]`, which is correct for single-container
  tasks and would silently patch the wrong one if a sidecar were added.
- **Autoscaling steps are guesses.** 1/3/max at 1/20/100 messages is a shape,
  not a measurement — nothing has been load-tested. Phase 5 replaces the
  numbers with observed throughput.

## From Phase 4b

- **The local storage-event bridge is not the AWS path.** MinIO cannot publish
  to ElasticMQ, so locally it posts the S3-shaped event to an API endpoint that
  republishes it onto the ingest queue. On AWS, S3 publishes to SQS directly
  and the bridge is not deployed. The event body, the queue and the worker
  handler are identical; the hop is not, and only the AWS deploy proves it.
- **Queue depth is exposed, not acted on.** `GET /usage` reports it and it is
  the metric worker autoscaling will target, but nothing throttles on it yet.
  Inventing a policy before the system has been load-tested would be guessing;
  Phase 5 measures first.
- **Rate limits and in-flight ceilings are global settings, not per tenant.**
  Every tenant gets the same bucket size and the same ceiling. Per-tenant
  overrides belong on the `tenants` row next to `budget_usd` and
  `review_sla_hours`, which already work that way.
- **A deferred message loses its receive count.** Deferral re-sends rather than
  letting the message redeliver, which is deliberate — being busy must never
  fill the DLQ — but it also means a document that is deferred forever would
  never reach the DLQ. A deferral counter on the payload would bound it.
- **Ingestion assumes one object is one document.** No zip/multi-page-batch
  unpacking, and a non-PDF drop is logged and dropped rather than reported
  anywhere the tenant can see. A per-tenant ingestion error surface is a real
  gap once customers use the drop path.

## From Phase 4a

- **The mock's token counts are a character-count approximation.** Four
  characters per token, applied to the real prompt and the real response. Good
  enough to make the pipeline's *shape* (calls, prompt size, output size)
  meterable, but the absolute unit cost will move under a real tokenizer. The
  first `anthropic`-mode run replaces the estimate with the provider's own
  numbers; nothing else changes, because the price and the arithmetic are
  already real.
- **The small tier's weakness is simulated.** `config/mock_models.json`
  degrades the cheap mock deliberately (truncated tables, no split-run repair)
  so routing has something to escalate. The measured escalation rate is
  therefore a property of that simulation, not a prediction about Haiku — the
  cost saving scales with it, and the eval reports both numbers so the
  sensitivity is visible.
- **A crashed worker leaves its reservation charged.** Reserve-then-settle
  over-counts if the process dies mid-call, which is the safe direction, and
  the row's `unsettled_calls` makes it visible. A sweeper that releases
  reservations older than a visibility timeout would close it.
- **Escalation is capped at one hop.** A pipeline names one `escalate_to`
  tier and the document is re-run once. Multi-step ladders (small → mid →
  frontier) need the policy to carry a list rather than a tier.
- **Unit costs are per tenant, not per document type per tenant per day.**
  The rollup groups by pipeline and tier; time-bucketing and a real dashboard
  are Phase 5 territory.

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
