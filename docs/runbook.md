# Operations runbook — what recovers itself, and what needs you

The deploy procedure is [`deploy_runbook.md`](deploy_runbook.md). This is the
other one: what the pipeline does when things go wrong, what it does *not* do,
and what to reach for when it needs help.

The organising idea is that every failure is either **transient** — the same
call would probably work in a moment — or **permanent** — it will fail
identically forever. The queue cannot tell them apart, because it counts
receives rather than reasons. So the classification happens in process
(`docfactory_core/resilience.py`) and the two get different machinery.

---

## What heals itself

| failure | mechanism | how long | operator action |
|---|---|---|---|
| Model API 5xx / timeout / rate limit | in-process retry, exponential backoff + full jitter, 3 attempts | ~4s | none |
| Model provider down | circuit breaker opens after 5 consecutive transient failures; documents deferred, not failed | reopens 60s after recovery | none |
| S3 / SQS throttle or blip | same retry path — classified from the AWS error code, not the HTTP status | seconds | none |
| Database connection drop | classified transient; retried | seconds | none |
| Worker killed mid-document | SQS visibility timeout redelivers; handlers are idempotent by status guard | ≤ 90s | none |
| Document committed but never enqueued | stuck-document reaper re-enqueues it | ≤ 15 min | none |
| Documents dead-lettered by an outage | bounded DLQ redrive, 2 attempts | ≤ 5 min after recovery | none |
| Tenant over budget, cap then raised | reaper re-enqueues `budget_exceeded` documents once there is budget | ≤ 15 min | raise the cap |
| Queue backlog | worker autoscaling, 0 → N → 0 | ~1 min to scale out | none |
| Fleet stuck at >0 with nothing to do | dead man's switch parks it after 3h | 3h | none |

## What does not heal itself

| failure | why | what to do |
|---|---|---|
| Corrupt or non-PDF document | permanent by classification; it fails fast to the DLQ rather than burning three retries | inspect the DLQ, fix the source |
| Extraction that will not satisfy the schema | permanent after the built-in schema retry | look at the document; it is usually genuinely odd |
| Image-only PDF | `needs_ocr` is a terminal *expected* state, never the DLQ | wait for the OCR tier |
| A message that has used both redrives | the bound exists so a poison document cannot loop forever | inspect it by hand |
| An IAM denial | every call fails identically; retrying cannot fix a policy | read the AccessDenied, widen one statement — see the deploy runbook |
| The ALB and the API task billing | nothing turns them off but you | `make aws-park`, or destroy the compute layer |

---

## The circuit breaker

Opens after **5 consecutive transient failures** against the model provider,
stays open for **60s**, then allows exactly **one** probe through. A good probe
closes it; a failed probe re-opens it and restarts the cooldown.

While it is open, extract messages are **deferred, not failed** — re-sent with
a delay equal to the remaining cooldown. This matters more than it looks: a
failed handler leaves the message to redeliver, which spends one of its three
receives, so an outage lasting longer than three receives would push every
in-flight document into the DLQ for the provider's sake rather than their own.
Deferral costs the document nothing.

Only transient failures count towards opening it. A provider rejecting a
malformed request is not a provider that is down, and counting those would open
the breaker on our own bug and hide it.

```
# is it open right now? the worker logs the transition at ERROR
aws logs filter-log-events --log-group-name /ecs/docfactory-dev/worker \
  --filter-pattern '"circuit opened"' --start-time $(($(date +%s) - 3600))000
```

**Known limitation:** the breaker is process-local. Six worker tasks have six
breakers, so an outage is noticed six times rather than once. A shared breaker
would put the database on the hot path of every model call, which costs more
than it saves at this fleet size. Each worker still stops within 5 failures
instead of retrying until the budget is gone.

## The reaper

Every running worker sweeps every **5 minutes** for documents in a non-terminal
state whose `updated_at` is older than **15 minutes**, and re-enqueues them at
the right stage — `received`/`parsing` back to parse, `parsed`/`extracting`
straight to extract, so recovery does not redo work that survived.

The 15-minute threshold **must stay above the longest visibility timeout**
(extract, 90s). A document being worked on right now is indistinguishable from
a stranded one on the database side; age is the only thing separating them.

Re-enqueueing a document that was fine is harmless — every handler opens with a
status guard and returns early on a state it has already passed — which is
exactly why the reaper can afford to be approximate.

It runs per tenant under RLS rather than through the owner connection. The
worker holds no owner credentials by design, and a sweeper that needed them
would be a reason to hand them out.

```bash
aws logs filter-log-events --log-group-name /ecs/docfactory-dev/worker \
  --filter-pattern '"reaped a stranded document"'
```

## The DLQ redrive

Runs in the same sweep. Moves dead-lettered messages back to their queue, at
most **twice** per message; the count rides on the message itself. A message
that has used both attempts is left in the DLQ, which is where a human should
find it.

The bound is the whole design. Without it a poison document returns, fails
three times, goes back to the DLQ, and is redriven again forever — an infinite
loop with a queue in the middle, burning a model call per lap.

```bash
# what is dead right now
for q in ingest parse extract; do
  url=$(aws sqs get-queue-url --queue-name docfactory-dev-$q-dlq --query QueueUrl --output text)
  echo -n "$q-dlq: "
  aws sqs get-queue-attributes --queue-url "$url" \
    --attribute-names ApproximateNumberOfMessages \
    --query 'Attributes.ApproximateNumberOfMessages' --output text
done
```

```bash
# force a sweep now rather than waiting for the interval
make heal
```

## Tuning

All of it is configuration, never constants:

| setting | default | what it controls |
|---|---|---|
| `HEAL_INTERVAL_SECONDS` | 300 | how often a worker sweeps; 0 disables |
| `HEAL_STALE_AFTER_SECONDS` | 900 | age before a document counts as stranded |
| `DLQ_MAX_REDRIVES` | 2 | laps a dead-lettered message may take |
| `BREAKER_THRESHOLD` | 5 | consecutive transient failures before opening |
| `BREAKER_COOLDOWN_SECONDS` | 60 | how long the provider is left alone |

## The rule every healing mechanism follows

**Nothing that heals may harm what it heals.** The drift observer taught this in
4e — it ran inside the extraction transaction, and a stale centroid raising
there rolled back documents and would eventually have dead-lettered them. So:

- the healing sweep runs in a daemon thread that catches everything and logs;
  a failed sweep never takes the worker down;
- the reaper only ever *adds* a message, never mutates document state;
- the redrive leaves anything it does not understand exactly where it is;
- deferral re-sends rather than failing, so no healing path ever spends a
  document's receive budget.

`tests/test_healing.py` and `tests/test_resilience.py` assert all of it with
counted, deterministic fault injection — 52 tests, no sampling, no sleeps.
