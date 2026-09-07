# DocFactory on AWS — the Terraform

Two independently applyable root modules. **This file explains the shape and
the reasoning; the procedure lives in
[`docs/deploy_runbook.md`](../../docs/deploy_runbook.md).**

```
infra/terraform/
  data-plane/      S3 + notifications, SQS + DLQs, SSM parameters, ECR, IAM
  compute-plane/   VPC, ALB, ECS Fargate (api/worker/migrate), autoscaling
```

**Status: applied for real against LocalStack, never against AWS.** The data
plane stands up and tears down cleanly in an emulator
([`infra/localstack/`](../localstack/README.md)); the compute plane is
`validate`/`plan` only, because LocalStack Community emulates none of Fargate,
ALB or application autoscaling. Every cost figure below is a rate-card number.

---

## Why two layers

**The dependency runs one way: compute → data.** The compute layer reads the
data layer's declared outputs through `terraform_remote_state`
(`compute-plane/data_plane.tf`); nothing in the data layer knows the compute
layer exists.

That direction buys three things:

1. **An overnight park.** `terraform destroy` in `compute-plane/` removes the
   ALB — the stack's entire idle cost — and every Fargate task, and leaves the
   bucket, the queues *and their contents*, the secrets and the pushed images
   untouched. Bring it back in the morning and the pipeline resumes against the
   same data. Destroying compute is a complete, safe operation precisely
   because nothing depends on it.
2. **A cheap first apply.** The layer that can go wrong expensively is applied
   second, after the cheap one is proven.
3. **A blast radius you can state.** The data layer holds everything with a
   lifetime longer than a deploy. The compute layer holds everything that is
   disposable by design — and, per `docs/cost_model.md`, **96% of the cost**.

Order is: apply data, apply compute; destroy compute, destroy data. Destroying
the data layer while compute is up is not a supported operation, and Terraform
will not warn you — the reference does not exist in that direction.

**The contract is enforced in CI.** `terraform validate` cannot check it: it
type-checks one directory at a time, and the compute layer's view of the data
layer is only concrete once the other layer has been applied. So a reference to
a non-existent output is a clean `validate` and a failed `plan` — found at
deploy time, against a real account. `tests/test_terraform_layers.py` closes
that by asserting every `local.data_plane.*` reference names a declared output,
that the compute layer never declares its own copy of a data-layer resource
type, and that the dependency does not run backwards.

Both layers also share a `check "layers_agree"` block: if `name_prefix` or
`environment` disagree between them, the plan fails rather than building an ALB
in front of another stack's services.

---

## The decisions worth defending

**No NAT gateway.** ~$32/month before a byte moves, and the single most common
way a small AWS project quietly costs real money. Fargate tasks run in *public*
subnets with public IPs and are kept private by security groups instead:
nothing may reach a task except the ALB, on one port, and the worker accepts no
inbound traffic at all. The conventional alternative — private subnets plus
interface VPC endpoints for ECR/SQS/SSM/Logs — is ~$7/month per
endpoint per AZ and costs *more* than the NAT it replaces. The S3 *gateway*
endpoint is free and is included, so bucket traffic stays inside the VPC.

**Neon, not RDS.** RDS is ~$15/month minimum for a `db.t4g.micro` idle 95% of
the time. Neon scales to zero and is ordinary Postgres, so RLS, `FORCE ROW
LEVEL SECURITY` and the non-superuser `docfactory_app` role behave exactly as
they do against the compose Postgres. The only thing RDS would buy is being
inside the VPC, and this stack has no VPC-private data plane to speak of.

**Step scaling, not target tracking.** Target tracking cannot scale a service to
zero on a raw queue metric — "messages per task" is undefined at zero tasks, so
the fleet parks at one task forever, a permanent ~$9/month on an idle stack.
Step scaling on `ApproximateNumberOfMessagesVisible` fires whether or not
anything is running, so 0 → 1 works. Scale-in requires an empty queue **and**
nothing in flight for five minutes, because a queue can read empty while a
worker is mid-document and killing that worker would redeliver the message and
waste a model call already paid for.

That condition is one *metric-math* alarm (`visible + inflight < 1`), not a
composite alarm over two. A composite alarm cannot invoke an Application Auto
Scaling policy — CloudWatch only accepts SNS, Lambda and OpsItem actions there
— and, separately, step bounds are offsets from the triggering alarm's
threshold, which a composite alarm does not have. Metric math gives the policy
a number to offset from, and costs $0.50/month less.

**The documents bucket is outside the destroy blast radius.** `destroy` fails
loudly on a non-empty bucket unless `force_destroy_documents = true`. Compute is
disposable; customer documents are not, and the difference is enforced by the
tooling rather than by remembering. (Verified both ways against LocalStack.
Note that `force_destroy` is read from *state*, so flipping the variable and
running `destroy` still refuses — you must `apply` first.)

**Secrets are never task-definition environment variables.** Those are visible
to anyone who can call `DescribeTaskDefinition`. The task pulls them at runtime
from SSM Parameter Store, and the *execution* role's permission to read them is
scoped to exactly three ARNs. `recovery_window_in_days = 0` so a destroyed
secret does not keep its name reserved and break the next apply.

**No long-lived AWS keys in GitHub.** The CI role is assumed with a short-lived
OIDC token, scoped by a `token.actions.githubusercontent.com:sub` condition to
one repository — without that condition any GitHub repository in the world
could assume it. `iam:PassRole` is scoped to this stack's three roles with a
`PassedToService` condition; without it, the deploy role could pass any role in
the account to a task it starts.

**Least privilege, checked against the code.** Every grant in
`data-plane/iam.tf` has a call site, with one flagged exception
(`s3:GetBucketLocation`). The mapping is the pre-flight table in the runbook; it
found two real gaps in 4c.5c that would each have broken the first deploy.

---

## Local validation

```bash
make localstack-cycle
```

Applies the data plane for real against LocalStack, runs 47 checks against the
emulator, destroys, confirms empty. See
[`infra/localstack/README.md`](../localstack/README.md) for exactly what that
proves — and, importantly, what it does not: **LocalStack Community does not
enforce IAM**, so a green run is evidence about wiring and none at all about
permissions.

---

## Quick reference

| | data-plane | compute-plane |
|---|---|---|
| Holds | bucket, queues, secrets, registries, IAM | VPC, ALB, ECS, autoscaling, log groups |
| Idle cost | **~$0.11/mo** (images and bucket) | **~$13.27/mo** — one 256/512 API task + its public IPv4; no ALB by default |
| Safe to destroy alone | no (compute depends on it) | **yes — this is the overnight park** |
| Applied against LocalStack | yes, 28/33 resources | no (not emulated) |
| Survives its own destroy | the documents bucket, by design | nothing |
