# LocalStack validation of the data plane

`terraform apply`, for real, before it is ever applied to a real AWS account.

The same local-first move as MinIO and ElasticMQ: run the thing against an
emulator on the machine you already have, and spend real-cloud time only on
what an emulator cannot answer.

```bash
make localstack-cycle      # up -> apply -> verify -> destroy -> down
```

or a step at a time:

```bash
make localstack-up         # start the emulator
make localstack-apply      # terraform apply the data-plane layer against it
make localstack-verify     # query it for what apply claims to have built
make localstack-destroy    # tear the stack down
make localstack-down       # stop the emulator
```

Needs `terraform` on `PATH` and the image (`docker pull localstack/localstack:4.0`).

---

## What this proves

Every one of these is checked by `verify.py` against the running emulator after
apply, not asserted from the configuration:

- the bucket exists, is versioned, is encrypted, and blocks all public access;
- the S3 → SQS notification is configured **and fires** — a `.pdf` dropped
  under `{tenant}/dropbox/` produces a message on the ingest queue, and that
  message parses with the worker's own `parse_s3_events`;
- a `.txt` dropped in the same place produces nothing, so the suffix filter
  really does keep the pipeline's own artifacts from re-triggering ingestion;
- all three queues and all three DLQs exist, with `maxReceiveCount = 3`
  pointing at the matching DLQ, the right visibility timeouts, and long polling;
- **the redrive works**: a message received three times is moved to the DLQ by
  the queue service, with no consumer code counting anything;
- the ingest queue's resource policy allows `s3.amazonaws.com` to send, scoped
  to this bucket and this account;
- the three secrets exist and are readable;
- the IAM roles and their inline policies exist, trust only
  `ecs-tasks.amazonaws.com`, and hold no infrastructure-creating grant;
- the application's own `INFRA_MODE=assert` startup check passes against the
  Terraform-built stack — which is how the two halves are held to the same
  names, `docfactory-dev-parse-dlq` on one side and `f"{queue}-dlq"` on the
  other;
- `terraform destroy` removes all 28 resources and leaves the emulator empty.

## What this does NOT prove

**IAM enforcement.** LocalStack Community stores IAM roles and policies as
objects and never evaluates them: every call in the run above would have
succeeded with an empty policy, or none at all. A green run here is evidence
about **wiring**, and none whatsoever about **permissions**.

A green LocalStack apply is not a verified deploy. Do not report it as one.

Permissions are covered instead by the static pre-flight in
[`docs/deploy_runbook.md`](../../docs/deploy_runbook.md) — every AWS call the
services make, mapped to the grant that authorizes it — and finally by the
first real `terraform apply`, which is the only thing that can settle it.

## What LocalStack could not represent

| Thing | Why | Where it gets covered |
|---|---|---|
| ECR repositories + lifecycle policy | Community returns HTTP 501 for `ecr:CreateRepository` (Pro feature) | first real apply |
| CI deploy role's inline policy | references the ECR repository ARNs above | first real apply |
| The whole compute layer | no Fargate, ALB or application autoscaling in Community | `terraform plan` + the runbook |
| IAM enforcement | policies stored, never evaluated | the pre-flight table, then the first real apply |

The apply is therefore run with `-target` (see `LS_TARGETS` in the `Makefile`),
which selects everything except the ECR resources and the deploy-role policy
that depends on them. 28 of 33 resources.

Because the compute layer's images come from `ecr_repositories`, a
`terraform plan` of `compute-plane/` against a LocalStack data plane cannot
resolve either. That cross-layer contract is checked a different way, without
Terraform at all: `tests/test_terraform_layers.py` asserts that every
`local.data_plane.*` reference in the compute layer names an output the data
layer actually declares — and it runs in CI with everything else.

## Things this run actually caught

Kept here because "we ran it and it was fine" is not why the exercise is worth
doing.

1. **The batch path would have been silently dead in the cloud.**
   `parse_s3_events` matched `s3:ObjectCreated…`. That is MinIO's spelling.
   Real S3 delivering to SQS sends `ObjectCreated:Put`, with no `s3:` prefix —
   the prefix belongs to the notification *configuration*, not to the event.
   The worker would have received every message, parsed zero references,
   acknowledged, and dropped the document on the floor without an error
   anywhere. No local run could find this, because locally MinIO is the only
   producer.

2. **The orphan check reported CLEAN for calls that failed.**
   `scripts/aws_orphan_check.sh` sent every call's stderr to `/dev/null` and
   treated an empty result as an empty account. Against LocalStack, where half
   these services are not implemented, it printed a clean bill of health for
   eight checks that never ran. On real AWS an expired SSO session would have
   done the same over a billing ALB. It now reports `UNKNOWN` and exits 3.

3. **`force_destroy_documents = true` does not take effect on a destroy run.**
   Terraform destroys with the flag as stored in state, so flipping the
   variable and running `destroy` still fails `BucketNotEmpty`. You have to
   `apply` first, then destroy. Verified both ways here; in the runbook.

The guard rail itself was also confirmed the right way round: with
`force_destroy_documents = false` and one object in the bucket, `destroy` fails
with `BucketNotEmpty` and the documents survive, which is the intent.
