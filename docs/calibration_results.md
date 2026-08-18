# Confidence calibration study

Fitted 2026-08-18 · git `fb07f68` · dataset built 2026-08-18T01:51:46+00:00 from 345 documents · provider `mock` · corruption rate 0.35 (seed 1337)

Unit of analysis is the **field**: the decision being calibrated is whether one
cell can be accepted without a human looking at it. Weights are fitted on the
non-golden documents; every number below is measured on the held-out golden set,
so the threshold is not reported on its own training data.

- train rows (fields): **2710** across 271 documents
- holdout rows (fields): **740** across 74 documents
- error rate in holdout: **5.7%** of fields wrong

## Operating point

Target: auto-approved fields must be **>= 99%** correct.

Target met.

| | value |
|---|---|
| threshold | **0.6750** |
| auto-approve rate | **93.1%** of fields |
| precision among auto-approved | **99.71%** |
| fields still sent to review | 6.9% |
| target met | **yes** |

### What each precision target buys

| target | reachable | threshold | auto-approve rate | actual precision |
|---|---|---|---|---|
| 99.5% | yes | 0.675 | 93.1% | 99.71% |
| 99.0% | yes | 0.675 | 93.1% | 99.71% |
| 98.5% | yes | 0.670 | 93.5% | 99.57% |
| 98.0% | yes | 0.540 | 94.7% | 99.14% |
| 97.0% | yes | 0.420 | 94.9% | 99.00% |
| 95.0% | yes | 0.420 | 94.9% | 99.00% |

![precision vs auto-approve rate](calibration_precision_vs_rate.png)

**The score is discrete, not continuous.** Nearly every clean field gets an identical score, because their signal features are all zero, so coverage jumps from 0% straight to ~95% with nothing selectable in between. Only a handful of operating points exist. Phase 2.3 therefore cannot dial coverage finely: it picks one of these points. Finer control needs a feature that varies across *clean* documents (per-field extraction margin, say), not more weight tuning.

## Fitted weights

| feature | weight |
|---|---|
| `implicating_rules_failed` | -0.705 |
| `log_residual_magnitude` | -0.003 |
| `groundedness` | +1.726 |
| `shape_suspect` | -1.777 |
| `row_arithmetic_broken` | -0.707 |
| `needed_retry` | +0.000 |
| `date_corroboration` | +0.541 |
| _bias_ | +4.425 |

Positive weight = pushes towards *correct*. Standardized inputs, so magnitudes
are comparable across features.

## By layout (holdout)

| layout | fields | auto-approve rate | precision |
|---|---|---|---|
| classic | 260 | 95.0% | 100.00% |
| euro | 180 | 85.6% | 99.35% |
| modern | 300 | 96.0% | 99.65% |

## By injected error class (holdout)

Documents are grouped by the error deliberately injected into them. `none` means the document was left clean.

| error class | fields | auto-approve rate | precision |
|---|---|---|---|
| `arithmetic_drift` | 40 | 67.5% | 100.00% |
| `none` | 470 | 98.1% | 99.78% |
| `shifted_date` | 100 | 84.0% | 100.00% |
| `transposed_line_amounts` | 60 | 88.3% | 100.00% |
| `truncated_vendor` | 40 | 92.5% | 97.30% |
| `wrong_vendor` | 30 | 90.0% | 100.00% |

## Where the ceiling comes from

Of 42 wrong fields in the holdout, 2 would still be auto-approved at threshold 0.675. These are the residual error — the reason a higher precision target is or is not reachable.

| injected class | field | count |
|---|---|---|
| `truncated_vendor` | vendor | 1 |
| `none` | vendor | 1 |

`shifted_date` is undetectable by construction: it moves the invoice date earlier while leaving every validation rule satisfied, so no current signal can see it. It is injected deliberately so the ceiling appears in the results rather than being hidden by only testing catchable errors.

## Reliability

Does the score behave like a probability, or only like a ranking?

![reliability](calibration_reliability.png)

| predicted | empirical | n |
|---|---|---|
| 0.003 | 0.000 | 24 |
| 0.246 | 0.000 | 2 |
| 0.349 | 0.200 | 10 |
| 0.420 | 0.500 | 2 |
| 0.535 | 0.000 | 1 |
| 0.667 | 0.667 | 12 |
| 0.994 | 0.997 | 689 |
