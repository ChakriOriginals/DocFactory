# DocFactory on AWS — runbook

Terraform for the Phase 4c stack: the API behind an ALB, a worker fleet that
autoscales on queue depth, S3 → SQS batch ingestion, and an OIDC role for CI.

**Status: written and validated, never applied.** No AWS account was reachable
from the machine this was built on, so every number below about cost is a rate
card figure and every "should" is a design intent, not an observation. The
first `apply` is the test.

---

## Cost safety — read before the first apply

This stack is designed to be destroyed. Bring it up for a demo, tear it down
after, and it costs a few dollars a month rather than a few dozen.

**What costs money while it exists**

| Resource | Rough idle cost | Notes |
|---|---|---|
| ALB | ~$16/mo | The price of a stable URL. Unavoidable if you want one. |
| Fargate tasks | per second | API: 1 task always. Workers: **0 when idle**. |
| NAT gateway | ~$32/mo | **This stack creates none — see below.** |
| S3, SQS, Secrets, ECR, logs | cents | Retention is capped; untagged layers expire. |

**The NAT decision.** Fargate tasks run in *public* subnets with public IPs and
are kept private by security groups: nothing may reach a task except the ALB,
on one port, and the worker accepts no inbound traffic at all. The conventional
alternative — private subnets plus a NAT gateway — is ~$32/month before any
traffic; the other alternative, private subnets plus interface VPC endpoints
for ECR/SQS/Secrets Manager/Logs, is ~$7/month per endpoint per AZ and costs
*more* than the NAT it replaces. For a stack cycled up and down by one person,
public subnets with tight security groups is the honest trade. The S3 *gateway*
endpoint is free and is included, so bucket traffic stays inside the VPC.

**What survives a destroy, on purpose.** The documents bucket. `destroy` fails
loudly if it still holds objects unless `force_destroy_documents = true`.
Compute is disposable; customer documents are not, and that difference is
enforced by the tooling rather than by remembering. Neon is outside Terraform
entirely, so the database survives too.

---

## First apply

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit

export TF_VAR_neon_database_url_owner='postgresql+psycopg://owner:...@...neon.tech/docfactory'
export TF_VAR_neon_database_url_app='postgresql+psycopg://docfactory_app:...@...neon.tech/docfactory'

terraform init
terraform plan      # read it — this is the first time it has ever run
terraform apply
```

`terraform output api_url` is the live URL.

### Before that: Neon

Neon rather than RDS is deliberate. RDS is ~$15/month minimum for a `db.t4g.micro`
that is idle 95% of the time; Neon scales to zero and costs nothing between
demos, and the Phase 3a isolation model works there unchanged — it is ordinary
Postgres, so RLS, `FORCE ROW LEVEL SECURITY`, and the non-superuser
`docfactory_app` role all behave exactly as they do against the compose
Postgres. The only thing RDS would buy here is being inside the VPC, and this
stack has no VPC-private data plane to speak of.

Create the project, then create the app role the way the migration does
locally — as the owner:

```sql
CREATE ROLE docfactory_app LOGIN PASSWORD '...' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
```

`alembic upgrade head` (run by the migrate task, as the owner) creates the rest.

### First images

The services reference `:latest` until CI pushes a real tag, so the first apply
needs an image to exist:

```bash
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin "$(terraform output -raw ecr_repositories | jq -r .api | cut -d/ -f1)"

docker build -f ../docker/Dockerfile.api    -t "$(terraform output -json ecr_repositories | jq -r .api):latest" ../..
docker build -f ../docker/Dockerfile.worker -t "$(terraform output -json ecr_repositories | jq -r .worker):latest" ../..
docker push "$(terraform output -json ecr_repositories | jq -r .api):latest"
docker push "$(terraform output -json ecr_repositories | jq -r .worker):latest"
```

### Migrations

Never from a laptop against production, and never by the app tasks — they
connect as `docfactory_app`, which holds no DDL rights by design. Run the
one-off task, which uses the owner secret:

```bash
aws ecs run-task --cluster docfactory-dev --task-definition docfactory-dev-migrate \
  --launch-type FARGATE --network-configuration \
  "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}"
```

CI does this automatically before rolling the services, and fails the deploy if
the migration exits non-zero.

---

## Teardown — the part to get right

```bash
terraform destroy
../../scripts/aws_orphan_check.sh us-east-1
```

`terraform destroy` reporting success means it deleted what it knew about. That
is not the same as "nothing is billing". **Always run the orphan check**, which
looks for anything tagged `project=docfactory` plus the categories that bite:

- **NAT gateways** — this stack creates none; any that appear are from
  something else and cost the most of anything on this list.
- **ALBs** — deletion protection is off, but a half-failed destroy can leave one.
- **Unattached Elastic IPs** — ~$3.60/month each for doing nothing.
- **Running ECS tasks** — a service left at `desired_count > 0` keeps launching them.
- **Log groups** — no retention means storage forever.
- **Secrets pending deletion** — these keep the *name* reserved and break the
  next apply. This stack sets `recovery_window_in_days = 0` so they go at once.

The check prints `CLEAN` or names what survived. The documents bucket is
reported as *kept*, not as an orphan — that is the design.

### If destroy fails

- *"BucketNotEmpty"* — intended. Copy what you need out, then either empty the
  bucket or set `force_destroy_documents = true` and destroy again.
- *"secret scheduled for deletion"* on the next apply — a previous destroy used
  a recovery window; `aws secretsmanager delete-secret --force-delete-without-recovery`.
- *ECS service stuck draining* — a task failing its health check on a loop.
  Set `desired_count` to 0, wait, destroy again.

---

## Autoscaling

The worker fleet scales on the extract queue's `ApproximateNumberOfMessagesVisible`:

- **Backlog alarm** (≥1 message, 1 minute) → step scaling to 1, 3, or the
  maximum, by backlog size.
- **Idle composite alarm** (nothing visible **and** nothing in flight, 5 minutes)
  → back to `worker_min_count`, which defaults to **0**.

Step scaling rather than target tracking, deliberately: target tracking on a
per-task metric cannot scale from zero — "messages per task" is undefined with
no tasks — so the fleet would park at one task forever, which is a permanent
Fargate charge on an idle stack. The composite idle alarm is what makes
scale-to-zero safe: a queue can read empty while a worker is mid-document, and
killing that worker would redeliver the message and waste a model call already
paid for.

The 4b backpressure work is unchanged by scaling: the per-tenant in-flight
ceiling is enforced against the database, not per process, so ten workers share
one ceiling and one tenant's flood still cannot starve another.

## Verifying the invariants in the cloud

After the first apply, these are the checks that prove deployment did not
quietly relax anything (each one has a local test that must also pass here):

| Invariant | How to check on the deployed stack |
|---|---|
| RLS isolation | Upload as tenant A, `GET /documents/{id}` with tenant B's key → **404**, not 403 |
| Non-superuser app role | `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='docfactory_app'` → `f, f` |
| No DDL from app tasks | `CREATE TABLE x(...)` as `docfactory_app` → permission denied |
| Idempotency | Upload the same PDF twice → 202 then 200, `duplicate: true`, one row |
| Batch path | `aws s3 cp inv.pdf s3://$BUCKET/dev-tenant/dropbox/invoice/` → document appears |
| Both paths converge | Same bytes by upload and by drop → one document |
| DLQ after 3 | Drop a corrupt PDF → 3 receives → message in `*-parse-dlq`, document `failed` |
| needs_ocr | Drop a scanned PDF → terminal `needs_ocr`, **not** the DLQ |
| Budget cap | Set `budget_usd` below one call → documents stop at `budget_exceeded` |
| Metering | `GET /usage` → non-zero `cost_usd`, tier split, queue depths |

## A real-model run

Mock is the deployed default and the stack should sit there. For one honest
cloud latency/cost number:

```bash
aws secretsmanager put-secret-value --secret-id docfactory-dev/anthropic-api-key \
  --secret-string "$REAL_KEY"
terraform apply -var model_provider=anthropic     # rolls the task definitions
# ... run a handful of documents, read GET /usage ...
terraform apply -var model_provider=mock          # and back
```

Leaving a deployed stack on `anthropic` is how a demo becomes a bill.
