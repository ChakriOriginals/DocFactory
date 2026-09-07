# Container CVE review

Scope: the two images that run in production, `docfactory-dev-api` and
`docfactory-dev-worker`, as scanned by ECR basic scanning on 2026-09-06.

Both images report an identical finding set, which is expected: they share a
base image and differ only in which workspace package they install. Every
finding is in a Debian bookworm OS package. Zero findings are in the Python
dependency tree.

```
before:  CRITICAL 4    HIGH 15    MEDIUM 6     (25 total, per image)
after:   CRITICAL 4    HIGH 14    MEDIUM 6     (24 total, per image)
```

One HIGH removed, by the one fix that exists. Both images are identical in
this respect, before and after.

## The short version

Eighteen of the nineteen critical/high findings have **no fix available** —
not "not yet applied", but no patched version published to
`bookworm-security` as of the scan date. The base tag is current: pulling
`python:3.12-slim-bookworm` fresh produces byte-identical package versions.

One finding has a fix, and it is now applied (see [Applied](#applied)).

The finding that matters most is not one of the four criticals. It is a HIGH
in zlib, and it has no fix.

## Reachability

"Reachable" here means: can input that an untrusted tenant controls reach the
vulnerable code with this image's actual runtime configuration? A finding is
only marked reachable if a specific call path was traced, not because the
package is installed.

| Package | Findings | Reachable? | Why |
|---|---|---|---|
| zlib | 1 HIGH | **Yes** | Tenant-uploaded PDFs. `pdfplumber` → `pdfminer.six` decodes `FlateDecode` streams through Python's `zlib` module, which links `/lib/x86_64-linux-gnu/libz.so.1` (verified with `ldd` inside the image). Attacker-controlled bytes reach the vulnerable library on the normal ingest path. |
| openssl | 1 CRITICAL, 3 HIGH, 1 MEDIUM | Marginal | Python's `ssl` links system OpenSSL 3.0.20. But the only TLS this image speaks is outbound, to endpoints we choose: Neon, AWS, the Anthropic API. Exploiting it requires controlling one of those endpoints or a MITM position inside the VPC. The image serves plain HTTP behind the ALB; TLS termination is the ALB's job, not the container's. |
| perl | 3 CRITICAL, 5 HIGH, 4 MEDIUM | No | `/usr/bin/perl` exists in the base image, but nothing shipped invokes it. Verified: `grep -rn 'subprocess\|os.system\|os.popen'` across `packages/core`, `apps/api`, `apps/worker` returns nothing. The only `subprocess` call site in the repo is `packages/evals/docfactory_evals/calibrate.py`, and that package is no longer copied into the image at all (it never was importable — `import docfactory_evals` raised `ModuleNotFoundError` in the old image — but shipping the source was pointless, so the `COPY` was narrowed). Reaching perl needs command execution first, at which point the perl CVE is not the problem you have. |
| util-linux | 5 HIGH, 1 MEDIUM | No | `mount`, `login`, `su` and friends. The container runs as uid 10001 with no setuid path exercised and no shell in the entrypoint. |
| pcre2 | 1 HIGH | No | `libpcre2-8.so.0` is linked into the image, but Python's `re` module uses its own engine, not PCRE2. No shipped code path calls into it. |

The uncomfortable conclusion: the one genuinely reachable finding is the one
with no fix available, and the one with a fix available is unreachable. That
inversion is worth stating plainly rather than reporting "1 of 19 fixed" and
implying the risk moved.

### zlib, in more detail

`CVE-2026-85091`, zlib `1.2.13.dfsg-1`, no fixed version published.

The exposure is real but bounded:

- Parsing happens in the worker, not the API. A crash takes out one consumer
  thread, and the supervisor stops writing the heartbeat, so the ECS health
  check kills and replaces the task. The message goes back to SQS and, after
  `maxReceiveCount = 3`, to the DLQ.
- Every upload is already size-capped and content-type checked before it
  reaches the parser.
- A worker task holds no long-lived credentials of its own: it has the task
  role, which is scoped to this stack's own buckets and queues, and RLS is
  enforced in the database against a non-superuser role. Code execution in a
  worker does not hand over other tenants' data for free.

Mitigation if it becomes exploited in the wild rather than theoretical: move
PDF parsing to a distroless or Alpine base (different libz build), or accept
the tradeoff and pin a self-built zlib. Neither is worth doing today for a CVE
with no published exploit and no patch.

## Applied

`libpcre2-8-0` `10.42-1` → `10.42-1+deb12u1` (`CVE-2026-86145`).

Pinned explicitly in both Dockerfiles rather than upgraded with a bare
`apt-get upgrade`. The reason is reproducibility: an unpinned upgrade makes
the contents of the image a function of the date it was built, which means two
builds of the same commit can differ and a rollback can silently ship
different libraries than the tag it rolled back from. The cost of pinning is
that the pin will eventually stop resolving when Debian supersedes the
version — that failure is the intended signal to re-run this review.

Also applied in the same change: `COPY packages/ packages/` narrowed to
`COPY packages/core/ packages/core/`, removing the calibration harness source
from both runtime images. It was dead weight — not importable, not a
dependency — and it is the only code in the repo that calls `subprocess`.

Verified in the rebuilt images, not assumed:

```
arch:         x86_64                      (Fargate declares X86_64)
user:         uid=10001(docfactory)
pcre2:        10.42-1+deb12u1             (was 10.42-1)
evals dir:    /app/packages/evals/pyproject.toml   (manifest only, no source)
subprocess call sites in image:  (none)
```

and confirmed against the post-push rescan: HIGH went 15 -> 14 in both
repositories, and nothing else moved.

Neither change alters runtime behaviour. `apps/` is still copied wholesale, so
the API image carries the worker's source and vice versa; that is first-party
code either way and not a finding, just untidiness.

## What this review does not claim

- Not "the images are secure". Nineteen critical/high findings are still
  present after the fix, because eighteen of them cannot be fixed today.
- Not "unreachable means harmless". Reachability was traced against the
  current code. New code that shells out, or a new dependency that uses PCRE2,
  invalidates a row in that table without anyone noticing.
- ECR basic scanning only covers OS packages against the Debian security
  feed. It is not a full SCA of the Python dependency tree — the "zero Python
  findings" line above means "nothing reported", not "nothing there".

## Re-running this

```bash
aws ecr describe-image-scan-findings \
  --repository-name docfactory-dev-worker \
  --image-id imageTag=latest --region us-east-2
```

Scan-on-push is enabled, so every push produces a fresh finding set. Compare
against the counts at the top of this file; a change in either direction is
worth a look, including a drop (a package disappearing from the image is a
change too).
