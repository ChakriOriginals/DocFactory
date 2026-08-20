# Cost model

What this stack bills, per resource, idle and active. Rate card is us-east-1
on-demand; a month is 730 hours. Every figure is arithmetic from published
rates, not an observation — **nothing here has been billed yet**, because the
stack has never been applied to a real account.

## The correction this document exists to make

Earlier documents in this repo said the idle cost is "the ALB, ~$16/month".
That is the largest *single* line, and it is roughly half the real number.

The API service runs `api_desired_count = 1` around the clock at 512 CPU units
and 1024 MiB, which is **$18.02/month** — more than the load balancer in front
of it. Saying "idle ≈ the ALB" was not a lie, since the same sentences said
"plus any running Fargate tasks", but it anchored on the wrong number. The
honest headline is that a standing stack costs about **$36/month**, and the
thing that makes this project cheap is not its architecture but the discipline
of destroying it.

## The three states, and what each actually saves

| state | what is running | monthly |
|---|---|---|
| **standing** | ALB + 1 API task + workers at 0 | **~$36.35** |
| **parked** (`make aws-park`) | ALB only; all tasks at 0 | **~$18.33** |
| **compute destroyed** (`terraform destroy` in `compute-plane/`) | secrets, images, bucket | **~$1.31** |
| **fully destroyed** | the documents bucket, if kept | **~$0.01** |

Parking halves it. Destroying the compute layer removes 96% of it. That is why
the runbook's teardown step is a step and not an appendix — and why the
two-layer split from 4c.5a is a cost decision as much as an architectural one.

A three-hour demo, brought up and destroyed the same evening, costs **$0.14**.

## Line by line

### Billed while the compute layer exists

| resource | rate | idle | active |
|---|---|---|---|
| **Application Load Balancer** | $0.0225/hr | **$16.43/mo** | + LCU charges, negligible below ~25 req/s |
| **Fargate — API task** | $0.04048/vCPU-hr, $0.004445/GB-hr | **$18.02/mo** (0.5 vCPU + 1 GB, always on) | same; the API does not scale |
| **Fargate — workers** | as above, $0.024685/task-hr | **$0.00** — `worker_min_count = 0` | $0.148 for a full 6-task fan-out for an hour |
| **CloudWatch composite alarm** | $0.50/alarm-mo | **$0.50/mo** | same |
| **CloudWatch metric alarms** | first 10 free | **$0.00** (5 in use) | same |
| **CloudWatch Logs** | $0.50/GB ingest, $0.03/GB-mo | pennies at 7-day retention | scales with worker output |

### Billed while the data layer exists

| resource | rate | idle |
|---|---|---|
| **Secrets Manager** | $0.40/secret-mo | **$1.20/mo** for three secrets |
| **ECR storage** | $0.10/GB-mo | ~$0.10/mo, untagged layers expire after 1 day |
| **S3 storage** | $0.023/GB-mo | ~$0.01/mo for a demo corpus |
| **S3 / SQS requests** | $0.005/1k PUT, $0.40/M SQS | inside the free tier at demo volume |
| **AWS Budgets** | first 2 free | **$0.00** |
| **SNS email** | first 1,000 free | **$0.00** |

Secrets Manager at $1.20/month is the one line that is not "pennies" — it is
most of the cost of a fully parked stack, and it is the reason the
compute-destroyed state is $1.31 rather than $0.10.

### Deliberately zero

| resource | why |
|---|---|
| **NAT gateway** | ~$32/mo, and none is created. Tasks run in public subnets behind tight security groups. This single decision is worth more than every other optimization in this document combined. |
| **Interface VPC endpoints** | ~$7/mo per endpoint per AZ — more than the NAT they would replace. |
| **Container Insights** | per-metric charges. Disabled; the dead man's switch uses free `AWS/ECS` metrics instead. |
| **Data transfer out** | first 100 GB/month is free; a demo moves megabytes. |
| **RDS** | Neon instead, which scales to zero. |

## What can and cannot run away

**Bounded by construction:**

- Model spend — the per-tenant budget cap is enforced by a conditional UPDATE
  in the same statement that spends it, and the deployed default is
  `MODEL_PROVIDER=mock`, which costs nothing at all.
- Worker fan-out — `worker_max_count = 6`, so the worst case is $0.148/hour.
- Log growth — 7-day retention, set explicitly. The default is *never expire*,
  which is a silent creeping charge and the most common way a small stack's
  bill drifts upward.
- Untagged image layers — expire after one day by lifecycle policy.

**Not bounded, and this is the honest part:** the ALB and the API task bill for
every hour they exist, whether or not a single request arrives. No alarm, no
budget and no scheduled action changes that. Only destroying them does.

## The three guard rails, and what each is actually good for

| mechanism | where | what it does | what it does NOT do |
|---|---|---|---|
| **AWS Budget** ($10, 50%/100% actual + 100% forecast) | `data-plane/cost_guard.tf` | emails you, in code from the first apply | stop anything |
| **CloudWatch `EstimatedCharges` alarm** | same, pinned to us-east-1 | second, independent tripwire | stop anything |
| **Dead man's switch** | `compute-plane/dead_mans_switch.tf` | parks the worker fleet after 3h of continuous running | touch the ALB or the API task |

Two of the three only tell you. That is a real limit, stated plainly: **AWS has
no mechanism that stops a resource billing on your behalf.** Budgets can be
wired to a Lambda that deletes things, and this stack deliberately does not do
that — a robot with permission to destroy your infrastructure on a billing
signal is a larger risk than the bill it prevents.

At $36/month standing, a **$10 budget breaches after about 8 days**. That is
the design: the alert arrives while a forgotten stack is still a rounding
error, not after a full month.

### Two things that will bite

1. **The CloudWatch billing alarm does nothing until you tick a box.**
   `AWS/Billing` metrics are not published until *Receive Billing Alerts* is
   enabled under Billing → Billing preferences. There is no API for it and no
   Terraform resource. Until then the alarm sits in `INSUFFICIENT_DATA`. It is
   configured with `treat_missing_data = "breaching"` so the silence is loud
   rather than reassuring, and it is in the runbook's prereq checklist.
2. **An unconfirmed SNS subscription is a silent alarm.** AWS emails a
   confirmation link; until it is clicked, the budget fires into nothing.

## Verification status

| claim | how it was checked |
|---|---|
| the Terraform is valid | `terraform validate` + `fmt -check`, both layers |
| the guard rails plan cleanly | `terraform plan` — budget, SNS topic, subscription, billing alarm all create |
| log retention is explicit | `retention_in_days = var.log_retention_days` on all three log groups, default 7 |
| teardown leaves nothing billing | `scripts/aws_orphan_check.sh`, extended in 4f-B to cover alarms, composite alarms, us-east-1 billing alarms, SNS topics, scalable targets and target groups |
| **the rates themselves** | **published rate card, not observed billing** |
| **the guard rails firing** | **never — no account has been billed** |

The budget and the SNS topic could not be applied against LocalStack:
Community has no Budgets API at all, and its SNS rejects the provider's dummy
credentials with `InvalidClientTokenId` while accepting the same credentials
for S3, SQS, IAM and Secrets Manager in the same apply — an emulator quirk
rather than a configuration error. `plan` is the verification those two get
until the first real apply.
