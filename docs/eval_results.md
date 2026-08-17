# Eval results — field accuracy on the golden set

- date: 2026-08-17
- provider/model: `mock:mock-extractor-v1`
- golden set: 100 of 500 docs, deterministic sha256(doc_id) split
- digital evaluated: 74 · needs_ocr (excluded, no text layer): 26 · extraction failures (counted as wrong): 0

| field | classic (n=26) | modern (n=30) | euro (n=18) | overall |
|---|---|---|---|---|
| vendor | 100.0% | 100.0% | 11.1% | 78.4% |
| invoice_number | 100.0% | 100.0% | 100.0% | 100.0% |
| invoice_date | 100.0% | 100.0% | 100.0% | 100.0% |
| due_date | 100.0% | 100.0% | 100.0% | 100.0% |
| currency | 100.0% | 100.0% | 100.0% | 100.0% |
| subtotal | 100.0% | 100.0% | 100.0% | 100.0% |
| tax_rate | 100.0% | 100.0% | 100.0% | 100.0% |
| tax | 100.0% | 100.0% | 100.0% | 100.0% |
| total | 100.0% | 100.0% | 100.0% | 100.0% |
| line_items (exact list) | 100.0% | 100.0% | 100.0% | 100.0% |
| **all fields** | **100.0%** | **100.0%** | **91.1%** | **97.8%** |
