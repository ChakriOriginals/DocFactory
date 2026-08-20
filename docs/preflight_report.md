# Pre-deploy verification sweep (Phase 4f-A)

Run before any AWS spend, against commit `913c16e` (post-4e).

**What this document is not.** It is not a claim that the system has no bugs.
It is a record of what was run, what it found, what was fixed, and — the part
that matters most — what is known to be unverified and why. A sweep that
reports "all clear" is reporting on the thoroughness of the sweep, not on the
correctness of the system.

---

## What was run

| check | result |
|---|---|
| `make test` (full suite) | **494 passed** (476 before this phase added 18) |
| `ruff format --check .` | clean, 100 files |
| `ruff check .` | clean |
| `terraform fmt -check -recursive` | clean, both layers |
| `terraform validate` — data-plane | Success |
| `terraform validate` — compute-plane | Success |
| migrations: `upgrade head` on a **fresh** database | clean to `a3f81d47c2e9` |
| migrations: `downgrade base` on a fresh database | clean — leaves only `alembic_version`, **0 policies** |
| LocalStack data-plane cycle (`make localstack-*`) | **51 passed, 0 failed**, 3 not representable |
| isolation suite as `docfactory_app` | 79 passed |
| eval gate | 12/12 — invoice 97.84% / 97.70%, PO 100% / 100% |

---

## What it found

### 1. The budget cap would have destroyed the documents it was protecting

**Severity: production-breaking. Fixed (`a3f81d47c2e9`).**

`DocumentStatus.BUDGET_EXCEEDED` was added to the Python enum in Phase 4a, and
`_pause_on_budget()` writes it whenever a tenant hits their cap. The CHECK
constraint on `documents.status` was last rewritten in Phase 2.3, for routing's
`approved` / `needs_review`, and **never learned the new value**.

Reproduced before the fix:

```
FAILED: IntegrityError
  (psycopg.errors.CheckViolation) new row for relation "documents"
  violates check constraint "ck_documents_status"
```

The consequence is worse than a failed update. That write happens inside the
extraction transaction, so the whole transaction rolls back, the SQS message is
never acknowledged, it redelivers, it fails identically, and after
`max_receive_count` the document lands in the DLQ. The feature whose entire
purpose is to pause a document *safely* — resumable the moment the cap is
raised — would instead have quarantined it, silently, three retries at a time.

**Why no test caught it.** Every budget test asserted on the enum
(`DocumentStatus.BUDGET_EXCEEDED == "budget_exceeded"`) or on the spend
counter. Not one of them wrote the status to a database. A string compared to a
string cannot discover that Postgres disagrees.

**The general fix, not just the specific one.** A Python enum and a Postgres
CHECK are two declarations of the same fact, kept in sync by hand, in separate
files, usually in separate commits. `tests/test_schema_contract.py` now asserts
them against each other in both directions, and writes **every** enum value to
a real database through the ordinary application session. A swept comparison
across all four status-like enums found exactly one mismatch — this one.
`test_every_status_like_enum_is_covered_by_this_file` fails if a new status
enum appears and nobody adds it, so the list of things being checked is itself
checked. Verified the guard works by downgrading one revision: three tests go
red.

### 2. A fresh HTTP client per document

**Severity: resource lifecycle. Fixed.**

`get_llm_client()` is called once per extraction attempt and again on every
escalation, and in `anthropic` mode each call constructed a new
`anthropic.Anthropic()` — a new httpx connection pool per document, TLS
handshakes that need not happen, and file descriptors left for the garbage
collector. Clients are now cached on `(provider, model)`; reuse is what the SDK
documents, and the client is thread-safe by design.

Behaviour is provably unchanged in mock mode: the golden set is byte-identical
(97.84% / 97.70% / 100% / 100%). The benefit is **unverified**, because it only
manifests against a real API and there is no key on this machine. Listed as a
limitation below rather than as a measured win.

### 3. A document can be committed and never enqueued

**Severity: stranding. Found here, fixed in workstream C.**

Both ingest paths commit the `documents` row and *then* enqueue:

```python
with session_scope() as session:
    session.add(document)          # committed here
...
app.state.broker.send(settings.parse_queue, ...)   # if this raises…
```

If the send fails — SQS blip, credential expiry, network — the request returns
500 and the row sits in `received` forever with no message to drive it. Same
shape in `ingest_object()` for the batch path. The two-phase-commit fix is
disproportionate; the right answer is a sweeper that re-enqueues anything
stranded in a non-terminal state, which is the stuck-document reaper in 4f-C.

---

## Classes swept and found clean

- **File handles.** `extract_pdf_text` opens the PDF inside a `with`; no other
  code opens files at runtime.
- **Database sessions.** Every session goes through `session_scope()` or
  `admin_session_scope()`, both of which commit/rollback and close in `finally`.
  The pooled engine is `lru_cache`d (one pool); `admin_session_scope` builds and
  **disposes** its own engine per call, which is deliberate — the owner
  connection is meant to be awkward to reach.
- **Consumer acknowledgement.** A message is deleted only after its handler
  returns. A failed `delete_message` redelivers into a handler that is
  idempotent by status guard.
- **Enum/CHECK agreement** across all four status-like enums — one mismatch,
  fixed above.
- **Migration downgrades that could strand a row in an illegal state.** Every
  CHECK-narrowing downgrade folds affected rows back first: Phase 2.3 folds
  `approved`/`needs_review` to `extracted`, and the new 4f migration folds
  `budget_exceeded` to `parsed`. Full `downgrade base` on a fresh database
  leaves zero policies and zero tables.

---

## Known limitations — accepted, not fixed

These are choices, with reasons. They are the honest half of this document.

| # | Limitation | Why it is being accepted |
|---|---|---|
| L1 | **A crash between `reserve` and `settle` double-charges on redelivery.** The extract handler treats `extracting` as "reprocess", so a redelivered document reserves budget again while the first reservation is still charged. | The error is in the safe direction — it over-counts spend and refuses work sooner, never under-counts and overspends. It is tracked rather than hidden: `tenant_spend.unsettled_calls` increments on reserve and decrements on settle, so the gap is visible. Fixing it properly needs a reservation keyed by document with idempotent settlement, which is a Phase 5 change to the money path and not something to attempt the week before a first deploy. |
| L2 | **`review_tasks.resolution` has no CHECK constraint.** | It is nullable until a task is resolved, and its values are validated at the API boundary. Recorded in the schema-contract test's allowlist with this reason so it is a decision rather than an oversight. |
| L3 | **`api_keys` is readable unscoped by the app role.** | Deliberate and documented in `tenant_isolation_audit.md`: authentication resolves a key hash *before* a tenant exists, so a tenant-scoped policy would fail every login. The rows hold SHA-256 hashes and 12-character prefixes, never plaintext. |
| L4 | **IAM policy sufficiency is unverified.** | LocalStack Community stores IAM policies and never evaluates them. The static pre-flight table in `deploy_runbook.md` maps every AWS call to its grant, and found two real gaps in 4c.5 — but only a real `apply` settles it. The runbook's "when the first apply fails on IAM" section exists for exactly this. |
| L5 | **`anthropic` mode is untested end to end.** | No API key on this machine. Every `anthropic` code path — the SDK call, structured-output parsing, refusal handling, real token accounting — has never executed. Mock mode is the deployed default precisely so this is not on the critical path, and the runbook's one deliberate real-model run is where it first executes. |
| L6 | **ECR and the entire compute plane are unapplied.** | LocalStack Community returns 501 for ECR and emulates neither Fargate, ALB, nor application autoscaling. `plan` and `validate` are clean; the first apply is the test. |
| L7 | **The load test has not been run.** | It needs the deployed autoscaling stack. Throughput, p50/p95/p99 and the 0→N→0 graph are Phase 5 and gated on the deploy — no number in this repo claims otherwise. |
| L8 | **Client-reuse benefit unmeasured.** | See finding 2. Correct by construction and unverifiable here. |

---

## The honest summary

Two real defects found and fixed, one of which would have broken a headline
feature in production the first time a tenant hit their budget cap. One
stranding hole found and routed to the self-healing workstream. Every
automated check green.

What that means: the checks that *can* be run here have been run, and the
latent-issue classes this codebase is prone to have been swept deliberately
rather than hoped over. What it does not mean: that the system is free of bugs.
The largest untested surfaces — real IAM enforcement, real Fargate, real model
calls, real load — are precisely the ones that need the deploy the runbook
describes, and they are listed above rather than quietly omitted.
