# Unit costs — from recorded usage events

A snapshot: this is whatever the tenant has processed so far, so it moves
with traffic. The reproducible per-document number lives in the eval report,
which measures a fixed golden set.

- tenant: `dev-tenant`
- calls metered: 40

## Per document type

| document type | documents | calls | escalation rate | total | **per document** |
|---|---|---|---|---|---|
| invoice | 13 | 21 | 61.5% | $0.114062 | **$0.008774** |
| purchase_order | 13 | 19 | 46.2% | $0.089262 | **$0.006866** |

## Per model tier

| document type | tier | model | calls | input tokens | output tokens | cost |
|---|---|---|---|---|---|---|
| invoice | frontier | `mock:mock-extractor-v1` | 8 | 7,260 | 2,169 | $0.090525 |
| invoice | small | `mock:mock-extractor-small-v1` | 13 | 11,572 | 2,393 | $0.023537 |
| purchase_order | frontier | `mock:mock-extractor-v1` | 6 | 5,429 | 1,567 | $0.066320 |
| purchase_order | small | `mock:mock-extractor-small-v1` | 13 | 11,457 | 2,297 | $0.022942 |
