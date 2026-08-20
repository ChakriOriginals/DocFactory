# Performance — profiled, and mostly left alone

Phase 4f-D. The rule was: profile first, change only what the numbers justify,
and "no bottleneck warranted a change" is an acceptable answer. It mostly was.

Reproduce with `uv run python scripts/bench_pipeline.py` (needs `make seed`).
Every number below is from that script on real corpus invoices, mock mode.

---

## What was profiled

`cProfile` over the full extract path — parse → extract → validate → score →
route — on 120 real invoices, then a per-stage benchmark and a threaded flood.

### The one thing worth changing

The profile put **0.742s of 0.774s** of total JSON-schema validation time inside
`check_schema`. Ninety-six percent of the cost of validating extracted records
was spent re-proving that the schema was still a schema.

`jsonschema.validate(payload, schema)` is a convenience wrapper that validates
the *schema* against its meta-schema on every single call before it looks at the
data. Building the validator once per pipeline definition and reusing it:

| | before | after |
|---|---|---|
| `validate_record` | 1.294 ms/call | **0.032 ms/call** |
| | | **40x faster**, 1.26 ms/document saved |

Cached on `(slug, version)`, which is the schema's real identity — a definition
is immutable for a given slug and version by design, because editing a pipeline
creates a new version so documents in flight keep the definition they started
under. `check_schema` still runs once, when the validator is built, so an
invalid schema still raises at the first validation attempt exactly as before.

Behaviour-preserving, checked three ways: identical output on the success path,
identical exception type and identical message on the failure path, and the
golden set unchanged at invoice 97.84% / 97.70% and PO 100% / 100%.

### Everything else: measured, not changed

Per document, model latency excluded:

| stage | ms/doc | share |
|---|---:|---:|
| `extract_pdf_text` (pdfplumber) | **11.378** | **96.7%** |
| `text_profile` (drift) | 0.089 | 0.8% |
| `validate_record` + normalize | 0.084 | 0.7% |
| `score_extraction` (confidence) | 0.072 | 0.6% |
| `groundedness` (one field) | 0.065 | 0.6% |
| `heuristic_extract` (mock model) | 0.053 | 0.5% |
| `route_extraction` | 0.028 | 0.2% |
| `evaluate_rules` | 0.002 | 0.0% |
| **total non-model CPU** | **11.771** | |

The confidence scorer, the groundedness matcher and the drift text-distance
profile — the three things this phase was asked to look at hardest — are
**0.072, 0.065 and 0.089 ms**. Together they are 2% of the CPU this pipeline
spends on a document, inside a system where the model call is 20–80 ms in mock
mode and one to several seconds against a real API. Optimizing any of them
would be measurable only by a profiler and would put verified extraction and
calibration logic at risk to save microseconds. **Not warranted, and not
changed.**

### The one real hotspot, deliberately not touched

PDF parsing is 96.7% of non-model CPU and there is a faster way to do it —
`layout=False`, or dropping to pdfminer directly, or a different library
entirely. It is not being changed, for one reason that outweighs the speed:
**the extracted text is the input to extraction.** Every accuracy number this
project reports — 97.84% invoice, 100% purchase order, the calibrated operating
point at threshold 0.675, the whole 2.2c study — was measured against the text
pdfplumber produces. A parser that is 3x faster and puts the words in a slightly
different order is not an optimization, it is an unmeasured change to the
system's accuracy wearing an optimization's clothes.

And the headroom is not needed. See below.

## Throughput

Threaded flood, 60 documents, mock latency included:

| threads | wall | docs/hour | scaling |
|---:|---:|---:|---:|
| 1 | 4.67s | 46,271 | 1.0x |
| 3 | 1.62s | 133,594 | 2.9x |
| 6 | 0.90s | 239,662 | 5.2x |
| 12 | 0.78s | 275,530 | 6.0x |

Near-linear to 6 threads, then flat — the machine's cores, as expected for
CPU-bound work under the GIL. Nothing is serialising that should not be.

**Phase 5's target is 1,000 documents/hour.** One worker task runs three
consumer threads, which measures at ~134,000 docs/hour in mock mode — roughly
**130x the target**. Even allowing an order of magnitude for real S3, real SQS
and real database round-trips, this code is not what will limit throughput. In
`anthropic` mode the model call will be, at one to several seconds per
document; that is a concurrency question answered by the worker autoscaling,
not a profiling question answered here.

That is the honest conclusion of this workstream: **one 40x fix to an obviously
redundant recompute, and nothing else, because nothing else was worth the
regression risk on a 548-test system.**

## Caveats

- Mock mode. The mock's simulated 20–80 ms latency stands in for a real model
  call and is excluded from the per-stage table by construction.
- One machine, one architecture (Apple Silicon, local disk). Fargate on x86
  with network storage will differ; the *shares* should not.
- Throughput here is in-process. It excludes SQS round-trips, S3 reads and
  database writes, which the deployed load test in Phase 5 will include and
  which will dominate.
- **This is not the load test.** No number here is a claim about the deployed
  system.
