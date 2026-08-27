# Cost model

What this stack bills, per resource, idle and active. Rate card is us-east-1
on-demand; a month is 730 hours. Every figure is arithmetic from published
rates, not an observation — **nothing here has been billed yet**, because the
stack has never been applied to a real account.

## If you are on a $100 credit account, read this first

The stack's defaults were changed in `7b7c4f1` specifically for this case. At
those defaults — **no load balancer, a 256/512 API task, workers on Spot** —
here is what $100 buys:

| state | $/month | months on $100 |
|---|---:|---:|
| running 24/7 | **$14.47** | **7** |
| running 24/7 with `enable_alb = true` | $30.90 | 3 |
| parked (`make aws-park`) | $1.81 | 55 |
| compute destroyed, data plane kept | $1.31 | 76 |
| both layers destroyed | $0.01 | — |

A three-hour demo brought up and destroyed the same evening costs **$0.05** in
task hours. At that rate the credits outlast the degree.

> **Two corrections to the first version of this table**, both found while
> checking the stack against a credit-eligible service list.
>
> **Public IPv4 addresses bill at $0.005/hour** — $3.65/month per running task
> — and were missing entirely. AWS started charging for in-use public IPv4 in
> February 2024. This stack gives every task a public IP by design, because
> there is no NAT gateway, so it is unavoidable while a task runs. It is
> charged per task-hour, so parking or destroying removes it completely; it is
> a running cost, not a resting one.
>
> **"Compute destroyed" was quoted as $0.11 and is $1.31.** Secrets Manager
> lives in the *data* plane and survives a compute-plane destroy, so its $1.20
> stays. The $0.11 figure is what you get after destroying *both* layers — or
> after the SSM Parameter Store swap described at the end of this document,
> which is now 92% of the resting cost rather than two thirds.

**The one thing that could actually burn them.** Six workers pinned at maximum
around the clock is ~$119/month on-demand — the whole balance in under a month.
Three things stop it, and it is worth knowing all three rather than trusting
one: `worker_max_count = 6` caps the fan-out, the queue-depth autoscaler
returns the fleet to zero five minutes after the queue drains, and the dead
man's switch parks anything still running after three hours regardless.
Workers also default to Fargate Spot, which cuts that worst case to roughly
$36/month. It is a scenario to recognise, not one to expect.

**What credits do not cover.** The Anthropic API is billed by Anthropic, not
AWS — a real-model run spends money no AWS credit touches. `MODEL_PROVIDER` is
`mock` by default for exactly this reason, and the per-tenant budget cap bounds
it when you do flip it. Support plans and some Marketplace charges are also
outside most credit programmes.

**Check which account plan you are on, before anything else.** AWS's newer free
plan pauses an account when its credits run out; a paid plan bills the card. If
you are on the free plan, do not upgrade until you mean to. This is the single
most important cost control on the whole project and it is not something
Terraform can set — it is one screen in the Billing console. I am not certain
of the current terms and you should read them rather than take my word.

There is now a budget for precisely this question. `docfactory-dev-out-of-pocket`
counts spend with **credits excluded**, so it reads $0.00 for as long as the
balance holds and alerts on the first cent of real money. The ordinary monthly
budget counts spend *with* credits applied, which makes it a burn-rate gauge —
useful, but reassuring right up to the day the balance hits zero.

---

## The correction this document exists to make

An earlier version of this document said the idle cost was "the ALB, ~$16/month",
and then that a standing stack was ~$36.35. Both are now wrong, in the good
direction, because the defaults changed:

| | before `7b7c4f1` | now |
|---|---:|---:|
| ALB | $16.43 | **$0** — off by default |
| API task | $18.02 (512/1024) | **$9.01** (256/512) |
| workers | on-demand | Spot, ~70% cheaper |
| public IPv4 | (not counted — an error) | $3.65 |
| **standing total** | **$36.35** | **$14.47** |

The API task was always the largest single line — more than the load balancer
in front of it — which the "idle ≈ the ALB" framing hid. Both are now optional
or minimal, and what keeps this project cheap is still the same discipline:
destroy the compute layer when the demo is over.

## Line by line

### Billed while the compute layer exists

| resource | rate | idle | active |
|---|---|---|---|
| **Application Load Balancer** | $0.0225/hr | **$16.43/mo** | + LCU charges, negligible below ~25 req/s |
| **Fargate — API task** | $0.04048/vCPU-hr, $0.004445/GB-hr | **$18.02/mo** (0.5 vCPU + 1 GB, always on) | same; the API does not scale |
| **Fargate — workers** | as above, $0.024685/task-hr | **$0.00** — `worker_min_count = 0` | $0.148/hr for a full 6-task fan-out, less on Spot |
| **Public IPv4** | $0.005/hr per address **in use** | **$3.65/mo** for the one API task | +$0.005/hr per running worker |
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


---

## Still on the list

**Secrets Manager → SSM Parameter Store.** $1.20/month, which is two thirds of
the parked cost and the largest recurring line left after the ALB and the API
task were dealt with. Standard SSM parameters are free, hold SecureString
values, and ECS task definitions read them through the same `valueFrom` field —
the change is a swap in `secrets.tf`, the execution-role policy moving from
`secretsmanager:GetSecretValue` to `ssm:GetParameters` plus a `kms:Decrypt`
scoped by `kms:ViaService`, and updates to the IAM pre-flight table.

Deliberately not done in the same pass as the ALB and sizing changes: it
touches the credential path that Phases 4c.5 and 4d spent real effort getting
right, and there is currently no `terraform` on this machine to validate it
with. A $14/year saving is not worth breaking secret delivery on the first
apply. It should be done, with `validate` and a LocalStack apply behind it —
SSM is one of the services LocalStack Community does support, so it can be
tested properly.
