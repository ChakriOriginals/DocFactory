# Eval results — field accuracy on the golden set

- date: 2026-08-18
- provider/model: `mock:mock-extractor-v1`

## invoice (`invoice` v1)

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


## purchase order (`purchase_order` v1)

- golden set: 40 of 120 docs, deterministic sha256(doc_id) split (limited from 100)
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
