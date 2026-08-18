# Eval results — field accuracy on the golden set

- date: 2026-08-18
- provider/model: `mock:mock-extractor-v1` · small tier: `mock:mock-extractor-small-v1`
- costs are the models' published per-token rates applied to the tokens each call actually used

## invoice (`invoice` v1)

### single model — the Phase 1-3 regression guard

- golden set: 100 of 500 docs, deterministic sha256(doc_id) split
- digital evaluated: 74 · needs_ocr (excluded, no text layer): 26 · extraction failures (counted as wrong): 0

| field | classic (n=26) | euro (n=18) | modern (n=30) | overall |
|---|---|---|---|---|
| vendor | 100.0% | 11.1% | 100.0% | 78.4% |
| invoice_number | 100.0% | 100.0% | 100.0% | 100.0% |
| invoice_date | 100.0% | 100.0% | 100.0% | 100.0% |
| due_date | 100.0% | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% | 100.0% |
| tax_rate | 100.0% | 100.0% | 100.0% | 100.0% |
| tax | 100.0% | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% | 100.0% |
| line_items (exact list) | 100.0% | 100.0% | 100.0% | 100.0% |
| **all fields** | **100.0%** | **91.1%** | **100.0%** | **97.8%** |


### routed — cheap tier first, escalating on a failed check

- golden set: 100 of 500 docs, deterministic sha256(doc_id) split
- digital evaluated: 74 · needs_ocr (excluded, no text layer): 26 · extraction failures (counted as wrong): 0

| field | classic (n=26) | euro (n=18) | modern (n=30) | overall |
|---|---|---|---|---|
| vendor | 100.0% | 5.6% | 100.0% | 77.0% |
| invoice_number | 100.0% | 100.0% | 100.0% | 100.0% |
| invoice_date | 100.0% | 100.0% | 100.0% | 100.0% |
| due_date | 100.0% | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% | 100.0% |
| tax_rate | 100.0% | 100.0% | 100.0% | 100.0% |
| tax | 100.0% | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% | 100.0% |
| line_items (exact list) | 100.0% | 100.0% | 100.0% | 100.0% |
| **all fields** | **100.0%** | **90.6%** | **100.0%** | **97.7%** |


- escalated: 47 of 74 digital docs (63.5%)
- cost per document: **$0.008942 routed** vs $0.010077 single-model — **11.3% cheaper** (routed spend is 0.89x the single-model spend)
- accuracy: 97.7% routed vs 97.8% single-model, all fields


## purchase order (`purchase_order` v1)

### single model — the Phase 1-3 regression guard

- golden set: 40 of 120 docs, deterministic sha256(doc_id) split, capped at a third of the corpus (asked 100)
- digital evaluated: 28 · needs_ocr (excluded, no text layer): 12 · extraction failures (counted as wrong): 0

| field | euro (n=13) | standard (n=15) | overall |
|---|---|---|---|
| buyer | 100.0% | 100.0% | 100.0% |
| vendor | 100.0% | 100.0% | 100.0% |
| po_number | 100.0% | 100.0% | 100.0% |
| order_date | 100.0% | 100.0% | 100.0% |
| delivery_date | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% |
| shipping | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% |
| line_items (exact list) | 100.0% | 100.0% | 100.0% |
| **all fields** | **100.0%** | **100.0%** | **100.0%** |


### routed — cheap tier first, escalating on a failed check

- golden set: 40 of 120 docs, deterministic sha256(doc_id) split, capped at a third of the corpus (asked 100)
- digital evaluated: 28 · needs_ocr (excluded, no text layer): 12 · extraction failures (counted as wrong): 0

| field | euro (n=13) | standard (n=15) | overall |
|---|---|---|---|
| buyer | 100.0% | 100.0% | 100.0% |
| vendor | 100.0% | 100.0% | 100.0% |
| po_number | 100.0% | 100.0% | 100.0% |
| order_date | 100.0% | 100.0% | 100.0% |
| delivery_date | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% |
| shipping | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% |
| line_items (exact list) | 100.0% | 100.0% | 100.0% |
| **all fields** | **100.0%** | **100.0%** | **100.0%** |


- escalated: 10 of 28 digital docs (35.7%)
- cost per document: **$0.005528 routed** vs $0.009050 single-model — **38.9% cheaper** (routed spend is 0.61x the single-model spend)
- accuracy: 100.0% routed vs 100.0% single-model, all fields
