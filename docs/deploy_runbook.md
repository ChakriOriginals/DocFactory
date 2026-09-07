# Deploy runbook — DocFactory on AWS

The ordered procedure for putting this stack on a real AWS account. Every step
has a **verify** and a **rollback**; run them.

**Status.** Nothing here has been run against a real account. What *has* been
run: the data-plane layer applies and destroys cleanly against LocalStack
(`make localstack-cycle`, 47 checks, see
[`infra/localstack/README.md`](../infra/localstack/README.md)), both layers pass
`terraform validate`, and every AWS call the services make has been
cross-checked against the task-role grants by hand (§IAM pre-flight below).
That closes everything an emulator can close. The one thing it cannot close is
whether the IAM policies are *sufficient*, because LocalStack Community does
not enforce IAM at all. Expect the first apply to surface one or two
`AccessDenied` errors; §"When the first apply fails on IAM" makes that routine
rather than alarming.

**Two layers.** `infra/terraform/data-plane/` (bucket, queues, secrets,
registries, IAM) and `infra/terraform/compute-plane/` (VPC, ALB, ECS,
autoscaling). Compute depends on data plane's outputs; nothing runs the other
way. That is what lets you `terraform destroy` the compute layer alone to stop
the ~$16/month ALB overnight and bring it back the next morning against the
same bucket, the same queues and the same images.

Set this once and leave it open in every shell:

```bash
export AWS_REGION=us-east-2
export TF_DATA=infra/terraform/data-plane
export TF_COMPUTE=infra/terraform/compute-plane
```

---

## 1. Prereqs

Work down the list. Each row has one command that either proves it or tells you
what is missing.

| # | Prereq | Verify |
|---|---|---|
| 1.1 | AWS account with a budget alarm | Terraform creates it — see 1.1 below |
| 1.1b | **Billing alerts enabled** (console-only) | see 1.1 below |
| 1.2 | CLI authenticated | `aws sts get-caller-identity` |
| 1.3 | Terraform ≥ 1.9 | `terraform version` |
| 1.4 | Docker running | `docker info --format '{{.ServerVersion}}'` |
| 1.5 | GitHub repo + remote | `git remote -v` |
| 1.6 | Neon project reachable | `psql "$NEON_OWNER_URL" -c 'select version()'` |
| 1.7 | Neon app role exists, unprivileged | see 1.7 below |

### 1.1 The cost guard rails

The budget itself is **Terraform-managed** (`data-plane/cost_guard.tf`), so it
exists from the first apply rather than depending on you remembering a console
click. Set where the alerts go:

```bash
# in infra/terraform/data-plane/terraform.tfvars
cost_alert_email   = "you@example.com"
monthly_budget_usd = 10
```

Two things Terraform cannot do for you, both of which turn a guard rail into
decoration if skipped:

**1. Enable billing alerts.** `AWS/Billing` metrics are not published at all
until *Billing → Billing preferences → Receive Billing Alerts* is ticked. There
is no API and no Terraform resource for it. Until it is on, the
`docfactory-dev-estimated-charges` alarm sits in `INSUFFICIENT_DATA`.

**2. Confirm the SNS subscription.** AWS emails a confirmation link when the
topic is created. An unconfirmed subscription is a silent alarm.

Verify both after the data-plane apply:

```bash
aws budgets describe-budgets \
  --account-id "$(aws sts get-caller-identity --query Account --output text)" \
  --query 'Budgets[].{Name:BudgetName,Limit:BudgetLimit.Amount}' --output table

# PendingConfirmation must be "false"
aws sns list-subscriptions-by-topic \
  --topic-arn "$(cd "$TF_DATA" && terraform output -raw cost_alerts_topic_arn)" \
  --query 'Subscriptions[].{Endpoint:Endpoint,Pending:PendingConfirmation}' --output table

# INSUFFICIENT_DATA here means billing alerts are still off
aws cloudwatch describe-alarms --region us-east-1 \
  --alarm-names docfactory-dev-estimated-charges \
  --query 'MetricAlarms[].{Name:AlarmName,State:StateValue}' --output table
```

At ~$13.27/month standing, a $10 budget breaches after about **23 days**. If
you are on a promotional-credit account, the budget that matters more is
`docfactory-dev-out-of-pocket`: it counts spend with credits EXCLUDED, so it
reads $0.00 while the balance holds and alerts on the first cent of real money.
`terraform output cost_runway` prints how long the balance lasts in each
resting state.

### 1.2 CLI authentication

IAM Identity Center is the right answer — short-lived credentials, nothing
long-lived on disk:

```bash
aws configure sso
```

An IAM user with an access key also works and is simpler for one person; if you
go that way, give it a permissions boundary or at least delete the key when the
project is over.

```bash
aws sts get-caller-identity
```

Should print an account id and an ARN. If it prints
`Unable to locate credentials`, nothing below will work.

### 1.6–1.7 Neon

Neon rather than RDS: RDS is ~$15/month minimum for a `db.t4g.micro` that is
idle 95% of the time, Neon scales to zero, and the Phase 3a isolation model
works there unchanged because it is ordinary Postgres.

Create the project, then create the app role **as the owner** — this is the
role the API and worker connect as, and everything in the isolation model
depends on it being unprivileged:

```sql
CREATE ROLE docfactory_app LOGIN PASSWORD '<generate one>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
```

**Verify** with the pre-flight script, which checks all of it at once and
prints no secrets, so its output is safe to paste anywhere:

```bash
./scripts/verify_neon.sh
```

It asserts what the isolation model actually rests on: both URLs connect and
point at the same database, the server is Postgres 16 (matching compose and
CI), and the app role is not a superuser, has no BYPASSRLS, cannot create
databases or roles, is genuinely refused `CREATE TABLE`, and connects over TLS.

The superuser check is the one that matters most. A superuser ignores row level
security completely — even with `FORCE ROW LEVEL SECURITY` — so a typo in
`CREATE ROLE` would leave every policy in the schema as decoration while the
isolation suite went on passing. That failure is invisible from inside the
application, which is why it is checked against the real database before
anything is deployed on top of it.

Keep both URLs to hand:

```bash
export TF_VAR_neon_database_url_owner='postgresql+psycopg://owner:...@...neon.tech/docfactory'
export TF_VAR_neon_database_url_app='postgresql+psycopg://docfactory_app:...@...neon.tech/docfactory'
```

They go into SSM Parameter Store at apply time and are never written to a file in
the repo.

### 1.8 Model API key

Optional. The deployed default is `MODEL_PROVIDER=mock`; the key is only needed
for the single deliberate real-model run in §10.

```bash
export TF_VAR_anthropic_api_key='sk-ant-...'   # or leave unset
```

---

## 2. Apply the data plane

The cheap, safe layer. Everything here costs cents at rest.

```bash
cd "$TF_DATA"
cp terraform.tfvars.example terraform.tfvars   # then edit: owner, github_repository
terraform init
terraform plan       # READ IT. This is the first time it has ever run for real.
terraform apply
```

**Verify**

```bash
terraform output
aws s3api head-bucket --bucket "$(terraform output -raw documents_bucket)"
aws sqs get-queue-attributes \
  --queue-url "$(terraform output -json queue_urls | jq -r .extract)" \
  --attribute-names RedrivePolicy VisibilityTimeout
aws s3api get-bucket-notification-configuration \
  --bucket "$(terraform output -raw documents_bucket)"
```

The redrive policy must show `maxReceiveCount: 3` and a `deadLetterTargetArn`
ending `-extract-dlq`. The notification must show one `QueueConfiguration`
pointing at the ingest queue with a `.pdf` suffix filter.

**Rollback**: `terraform destroy`. At this point the bucket is empty, so it goes
without argument.

---

## 3. Build and push the first images

The compute layer references `:latest` until CI pushes a real tag, so an image
has to exist before the services can start. CI does this automatically on every
push to `main` (§9) — this is the bootstrap.

```bash
cd "$TF_DATA"
API_REPO=$(terraform output -json ecr_repositories | jq -r .api)
WORKER_REPO=$(terraform output -json ecr_repositories | jq -r .worker)
REGISTRY=${API_REPO%%/*}

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

cd "$(git rev-parse --show-toplevel)"

# Pull the bases for the TARGET arch first. This is the step that is easy to
# skip and expensive to skip -- see the note below.
docker rmi -f python:3.12-slim-bookworm ghcr.io/astral-sh/uv:0.5.11 2>/dev/null || true
docker pull --platform linux/amd64 python:3.12-slim-bookworm
docker pull --platform linux/amd64 ghcr.io/astral-sh/uv:0.5.11

docker build --platform linux/amd64 -f infra/docker/Dockerfile.api    -t "$API_REPO:latest" .
docker build --platform linux/amd64 -f infra/docker/Dockerfile.worker -t "$WORKER_REPO:latest" .

# Assert the arch BEFORE pushing. Do not skip this: it is the only cheap
# moment to catch a wrong-arch image.
for repo in "$API_REPO" "$WORKER_REPO"; do
  arch=$(docker run --rm --entrypoint uname "$repo:latest" -m)
  [ "$arch" = "x86_64" ] || { echo "WRONG ARCH: $repo is $arch"; exit 1; }
  echo "OK $repo -> $arch"
done

docker push "$API_REPO:latest"
docker push "$WORKER_REPO:latest"
```

`--platform linux/amd64` is not optional on an Apple Silicon machine: the task
definitions declare `X86_64`, and an arm64 image lands as `exec format error`
in a task that starts and dies with no useful log line.

**And `--platform` on `docker build` is not sufficient on its own.** With the
classic builder, `FROM` is satisfied from whatever is already in the local
image store, and the store is keyed by tag, not by tag+platform. If an arm64
`python:3.12-slim-bookworm` is cached -- and one gets cached by any unrelated
`docker run` or `docker build` without a platform flag -- the build silently
uses it. Worse, `docker pull --platform linux/amd64` will not fix it either:
it prints `Image is up to date` and returns 0 without checking that the cached
image is the wrong platform. Hence the explicit `docker rmi` above.

This was caught the hard way: a rebuild that had worked days earlier failed at
`COPY --from`, and the apt output in the build log was fetching `arm64` `.deb`
files under a command that said `--platform linux/amd64`. The failure was
lucky. The unlucky version of this bug is a build that succeeds and pushes an
arm64 image, which then fails at task start with `exec format error`.

**Verify** — including that the images really are non-root, which is a claim
this project makes and should be able to show:

```bash
aws ecr describe-images --repository-name "${API_REPO##*/}" \
  --query 'imageDetails[].{Tags:imageTags,Pushed:imagePushedAt}' --output table
docker run --rm --entrypoint id "$API_REPO:latest"
docker run --rm --entrypoint id "$WORKER_REPO:latest"
```

Both `id` calls must print a non-zero uid.

Scan-on-push is enabled, so the push also produces a fresh CVE finding set.
What the current findings are, which of them are reachable, and which have no
fix available: `docs/container_cves.md`.

**Rollback**: nothing to undo; pushing a new tag replaces it.

---

## 4. Apply the compute plane

The expensive layer. From here the stack costs about $16/month for the ALB plus
Fargate time.

```bash
cd "$TF_COMPUTE"
cp terraform.tfvars.example terraform.tfvars   # owner/environment must MATCH the data layer
terraform init
terraform plan
terraform apply
export API_URL=$(terraform output -raw api_url)
echo "$API_URL"
```

If `plan` fails with *"compute-plane name … != data-plane name …"*, the two
layers' `name_prefix`/`environment` disagree — that check exists so you find out
here instead of after building an ALB in front of nothing.

**Verify**

```bash
curl -fsS "$API_URL/healthz" && echo
aws ecs describe-services --cluster "$(terraform output -raw cluster_name)" \
  --services docfactory-dev-api \
  --query 'services[0].{running:runningCount,desired:desiredCount,status:status}'
```

`/healthz` answering means the task pulled its image, read its secrets, passed
`INFRA_MODE=assert`, and registered with the target group. If it does not
answer within ~3 minutes, go straight to the logs — this is where a missing IAM
grant shows up:

```bash
aws logs tail /ecs/docfactory-dev/api --since 10m --follow
```

**Rollback**: `terraform destroy` in this directory only. The data layer is
untouched.

---

## 5. Migrate the schema

Never from a laptop against a deployed database, and never by the app tasks —
they connect as `docfactory_app`, which holds no DDL rights by design. Run the
one-off task, which is the only thing wired to the owner secret:

```bash
cd "$TF_COMPUTE"
CLUSTER=$(terraform output -raw cluster_name)
SUBNETS=$(terraform output -json subnet_ids | jq -r 'join(",")')
SG=$(terraform output -raw worker_security_group_id)

TASK=$(aws ecs run-task --cluster "$CLUSTER" \
  --task-definition "$(terraform output -raw migrate_task_definition)" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --query 'tasks[0].taskArn' --output text)

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
  --query 'tasks[0].containers[0].exitCode'
```

Exit code must be `0`.

**Verify** the schema exists *and* that the app role still cannot change it:

```bash
psql "$NEON_OWNER_URL" -c '\dt'
psql "$NEON_APP_URL"   -c 'CREATE TABLE should_fail (x int)'   # expect: permission denied
psql "$NEON_OWNER_URL" -c \
  "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='docfactory_app'"
```

Then mint an API key. Do this **as the owner**:

```bash
DATABASE_ADMIN_URL="$NEON_OWNER_URL" uv run python -c "
from docfactory_core.auth import issue_api_key
print(issue_api_key('dev-tenant', 'runbook').plaintext)"
```

Save it: `export DK=dk_...`. The plaintext is shown once and stored only as a
SHA-256 hash.

> **Why `DATABASE_ADMIN_URL` and not `DATABASE_URL`.** Key issuance is an owner
> operation: `issue_api_key` connects on the owner URL, and the app role holds
> `SELECT` on `api_keys` and nothing else, so it can authenticate but cannot
> mint. That was the intent from Phase 3a and was only made true in 4d — see
> [tenant_isolation_audit.md](tenant_isolation_audit.md). A deployed task has
> no `DATABASE_ADMIN_URL` (only the one-off migrate task holds the owner
> secret), so this command runs from your machine, not from the stack.

**Rollback**: `alembic downgrade` via the same one-off task, or drop and
recreate the Neon branch — it is a demo database.

---

## 6. Smoke test, in mock mode

Two ingest paths, both reaching `approved` without a model call costing
anything.

**6a — the API path**

```bash
DOC=$(curl -fsS -X POST "$API_URL/documents?doc_type=invoice" \
  -H "x-api-key: $DK" -F "file=@data/synth/out/invoices/invoice_0001.pdf" \
  | jq -r .document_id)

for i in $(seq 1 30); do
  STATUS=$(curl -fsS "$API_URL/documents/$DOC" -H "x-api-key: $DK" | jq -r .status)
  echo "$STATUS"; [ "$STATUS" = "approved" ] && break; sleep 5
done
```

**6b — the S3 drop path**

```bash
BUCKET=$(cd "$TF_DATA" && terraform output -raw documents_bucket)
aws s3 cp data/synth/out/invoices/invoice_0002.pdf \
  "s3://$BUCKET/dev-tenant/dropbox/invoice/invoice_0002.pdf"

sleep 30
curl -fsS "$API_URL/usage" -H "x-api-key: $DK" | jq '.queue_depth, .in_flight'
```

The dropped document has no id you were handed, so find it by hash:

```bash
SHA=$(shasum -a 256 data/synth/out/invoices/invoice_0002.pdf | cut -d' ' -f1)
psql "$NEON_OWNER_URL" -c \
  "SELECT id, status, s3_key FROM documents WHERE sha256 = '$SHA'"
```

If the drop produces nothing, the notification did not fire or the worker did
not parse it. Check, in order: the ingest queue's depth, the worker logs, and
the bucket notification configuration.

**Rollback**: delete the rows and the objects; nothing here is load-bearing.

---

## 7. The invariants, against the live stack

These are the guarantees the project claims. Each has a local test that must
also hold in the cloud — a deploy that quietly relaxes one of them is a deploy
that lied.

| # | Invariant | Command | Expected |
|---|---|---|---|
| 1 | **Cross-tenant read is a 404, not a 403** | `curl -s -o /dev/null -w '%{http_code}' "$API_URL/documents/$DOC" -H "x-api-key: $OTHER_DK"` | `404` — a 403 would confirm the id exists |
| 2 | **App role is not a superuser** | `psql "$NEON_OWNER_URL" -c "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname='docfactory_app'"` | `f \| f` |
| 3 | **App role holds no DDL** | `psql "$NEON_APP_URL" -c 'CREATE TABLE x (i int)'` | `ERROR: permission denied for schema public` |
| 3b | **App role cannot mint credentials** | `psql "$NEON_APP_URL" -c "INSERT INTO api_keys (tenant_id,name,key_hash,key_prefix) VALUES ('dev-tenant','forged',repeat('f',64),'dk_x')"` | `ERROR: permission denied for table api_keys` |
| 3c | **Every tenant table is policy-protected** | `psql "$NEON_OWNER_URL" -f scripts/isolation_check.sql` | no rows — every table with a `tenant_id` has forced RLS and a policy |
| 4 | **Dedupe, API path** | re-POST the same PDF from 6a | HTTP `200`, `duplicate: true`, same `document_id` |
| 5 | **Dedupe across both paths** | `aws s3 cp <the 6a file> s3://$BUCKET/dev-tenant/dropbox/invoice/` then count rows for that sha | exactly **one** row |
| 6 | **DLQ after 3 receives** | `printf 'not a pdf' > /tmp/corrupt.pdf; aws s3 cp /tmp/corrupt.pdf s3://$BUCKET/dev-tenant/dropbox/invoice/corrupt.pdf` — then poll the parse DLQ | message in `docfactory-dev-parse-dlq`, document `failed` |
| 7 | **`needs_ocr` is terminal, never the DLQ** | drop a scanned/image-only PDF | status `needs_ocr`, DLQ depth unchanged |
| 8 | **Budget cap holds** | `psql "$NEON_OWNER_URL" -c "UPDATE tenants SET budget_usd = 0.001 WHERE id='dev-tenant'"` then upload | documents stop at `budget_exceeded` |
| 9 | **Metering is populated** | `curl -fsS "$API_URL/usage" -H "x-api-key: $DK" \| jq .unit_costs` | non-zero `cost_usd`, a tier split, per-pipeline rows |
| 10 | **Queue depth is live** | `curl -fsS "$API_URL/usage" -H "x-api-key: $DK" \| jq .queue_depth` | three named queues with integer depths |

For #1 and #5 you need a second tenant:

```bash
psql "$NEON_OWNER_URL" -c \
  "INSERT INTO tenants (id, name, status, review_sla_hours, budget_usd)
   VALUES ('acme-tenant','Acme','active',24,100) ON CONFLICT DO NOTHING"
DATABASE_ADMIN_URL="$NEON_OWNER_URL" uv run python -c "
from docfactory_core.auth import issue_api_key
print(issue_api_key('acme-tenant','runbook').plaintext)"
```

Reset the budget after #8:

```bash
psql "$NEON_OWNER_URL" -c "UPDATE tenants SET budget_usd = 100 WHERE id='dev-tenant'"
```

Watching the DLQ for #6 and #7:

```bash
DLQ=$(cd "$TF_DATA" && terraform output -json dlq_urls | jq -r .parse)
aws sqs get-queue-attributes --queue-url "$DLQ" \
  --attribute-names ApproximateNumberOfMessages
```

#6 takes about a minute: three receives at a 10-second visibility timeout, plus
the redrive.

---

## 8. Drive the autoscaler

The headline artifact: workers at **0**, a backlog arrives, the fleet fans out,
the queue drains, the fleet goes back to **0**.

```bash
# Confirm the starting point: zero workers.
CLUSTER=$(cd "$TF_COMPUTE" && terraform output -raw cluster_name)
aws ecs describe-services --cluster "$CLUSTER" --services docfactory-dev-worker \
  --query 'services[0].{running:runningCount,desired:desiredCount}'

# Backlog: 120 documents into the drop prefix, mock mode, costs nothing.
BUCKET=$(cd "$TF_DATA" && terraform output -raw documents_bucket)
aws s3 cp data/synth/out/invoices/ "s3://$BUCKET/dev-tenant/dropbox/invoice/" \
  --recursive --exclude '*' --include 'invoice_01*.pdf'

# Watch it move.
watch -n 20 "aws ecs describe-services --cluster $CLUSTER \
  --services docfactory-dev-worker \
  --query 'services[0].{running:runningCount,desired:desiredCount}'"
```

Expect roughly: backlog visible within a minute → the
`docfactory-dev-extract-backlog` alarm fires → desired count steps to 1, then 3,
then the maximum by backlog size → the queue drains → five minutes of an empty
queue **and** nothing in flight → the composite `docfactory-dev-worker-idle`
alarm fires → back to 0.

**The screenshot.** CloudWatch → Metrics → All metrics → SQS → Queue Metrics →
`docfactory-dev-extract` → `ApproximateNumberOfMessagesVisible`. Add a second
metric from ECS → ClusterName, ServiceName → `docfactory-dev-worker` →
`RunningTaskCount`. Set the range to 1 hour, statistic Maximum, period 1 minute.
Both lines on one graph — backlog rising and task count following it up and back
to zero — is the picture. Take it after the fleet has returned to 0, so the
whole shape is in frame.

Also screenshot the alarm history: CloudWatch → Alarms → `docfactory-dev-worker-idle`
→ History. It shows the composite alarm transitioning, which is the part of the
design that makes scale-to-zero safe.

**Rollback**: nothing. It scales itself back down; that is the point.

---

## 9. Wire GitHub Actions

```bash
cd "$TF_DATA"
# Set github_repository in terraform.tfvars to "<you>/DocFactory", then:
terraform apply
terraform output -raw github_deploy_role_arn
```

In the repository: Settings → Secrets and variables → Actions → **Variables** →
New repository variable, `AWS_DEPLOY_ROLE_ARN` = that ARN. A *variable*, not a
secret — it is an ARN, not a credential, and there is no AWS key to store: the
workflow assumes the role with a short-lived OIDC token.

```bash
git push origin main
gh run watch
```

`test → eval-gate → build → deploy`, in that order. Tests prove the code works;
the eval gate proves the *pipeline* still extracts as well as it did; only then
is an image built and rolled out.

**Verify the gate actually blocks a merge** — a green CI that has never gone red
proves nothing:

```bash
git checkout -b regression-demo
# Lower a floor's opposite: make the pipeline worse on purpose. E.g. in
# config/eval_thresholds.json raise the invoice floor above the measured score,
# or degrade a normalizer in packages/core/docfactory_core/normalize.py.
git commit -am "deliberate regression, do not merge"
git push -u origin regression-demo
gh pr create --fill
gh run watch          # screenshot the RED eval-gate job here
git push origin --delete regression-demo && git checkout main
```

Screenshot the failed `eval gate (golden sets)` job with the threshold line in
its output. The pull request stops there and never reaches `build-and-deploy` —
that `if: github.ref == 'refs/heads/main'` guard is why a PR can run the gate
safely.

**Rollback**: delete the branch. Nothing deployed.

---

## 10. One real-model run

Mock is the deployed default and the stack should sit there. Do this once, for
an honest cloud latency and cost number, and put it back.

```bash
aws ssm put-parameter --overwrite \
  --name /docfactory-dev/anthropic-api-key --type SecureString --value "$REAL_KEY"

cd "$TF_COMPUTE"
terraform apply -var model_provider=anthropic   # rolls both task definitions

# Ten documents. Not a hundred.
for f in data/synth/out/invoices/invoice_02*.pdf; do
  curl -fsS -X POST "$API_URL/documents?doc_type=invoice" \
    -H "x-api-key: $DK" -F "file=@$f" > /dev/null
done
sleep 120
curl -fsS "$API_URL/usage" -H "x-api-key: $DK" | jq '.spent_usd, .unit_costs'

terraform apply -var model_provider=mock        # and BACK. Do not skip this.
```

**Verify you actually went back:**

```bash
aws ecs describe-task-definition --task-definition docfactory-dev-worker \
  --query "taskDefinition.containerDefinitions[0].environment[?name=='MODEL_PROVIDER']"
```

Leaving a deployed stack on `anthropic` is how a demo becomes a bill.

---

## 11. Teardown

Two shapes. **Park** is what you do most nights.

**Park — stop the ~$16/month, keep everything:**

```bash
cd "$TF_COMPUTE" && terraform destroy
```

The bucket, the queues and their contents, the secrets and the pushed images
all survive. `terraform apply` here brings the URL back in a few minutes.

**Full teardown:**

```bash
cd "$TF_COMPUTE" && terraform destroy
cd "$TF_DATA"    && terraform destroy
./scripts/aws_orphan_check.sh "$AWS_REGION"
```

The data-plane destroy will **refuse** while the documents bucket holds
objects. That is the guard rail working — compute is disposable, documents are
not. To go through with it:

```bash
# Copy out anything you want first.
aws s3 sync "s3://$BUCKET" ./backup/

# force_destroy is read from STATE, not from the variable, on a destroy run.
# You must apply the flag first, then destroy. Verified in 4c.5b.
cd "$TF_DATA"
terraform apply  -var force_destroy_documents=true
terraform destroy -var force_destroy_documents=true
```

**Then run the orphan check, every time.** `terraform destroy` reporting success
means it deleted what it knew about — not that nothing is billing.

```bash
./scripts/aws_orphan_check.sh "$AWS_REGION"
```

- exit **0** / `CLEAN` — nothing billable survived.
- exit **1** / `ORPHANS FOUND` — the output names them.
- exit **3** / `UNKNOWN` — one or more checks could not run. **This is not
  clean.** Usually an expired session or the wrong region. Fix and re-run. (The
  earlier version of this script printed CLEAN in exactly this case; it was
  caught by running it against LocalStack in 4c.5b.)

Console verification, in the order things hurt:

| Check | Console | Expected |
|---|---|---|
| NAT gateways | VPC → NAT gateways | **none, ever** — this stack creates none by design |
| Load balancers | EC2 → Load balancers | none named `docfactory` |
| Elastic IPs | EC2 → Elastic IPs | none unattached (~$3.60/mo each) |
| Running tasks | ECS → Clusters → Tasks | none |
| Log groups | CloudWatch → Log groups | none under `/ecs/docfactory` |
| SSM parameters | Systems Manager → Parameter Store | none under `/docfactory-dev` — they delete immediately, with no recovery window to block the next apply |
| ECR repositories | ECR | none named `docfactory` |
| S3 bucket | S3 | **kept on purpose** unless you forced it |

---

## IAM pre-flight — every call, and what authorizes it

Built by reading the code rather than by waiting for an `AccessDenied`.
LocalStack could not check any of this — Community stores IAM policies and never
evaluates them — so this table is the substitute, and it is the part of 4c.5
that most directly shortens the first apply.

### Application task roles

| # | Call site | AWS API | Required action | Resource | api | worker | Status |
|---|---|---|---|---|:--:|:--:|---|
| 1 | `storage.py` `assert_bucket` | HeadBucket | `s3:ListBucket` | bucket ARN | ✅ | ✅ | ok — note the call is spelled nothing like the permission |
| 2 | `storage.py` `put_object` | PutObject | `s3:PutObject` | `bucket/*` | ✅ | ✅ | ok |
| 3 | `storage.py` `get_object` | GetObject | `s3:GetObject` | `bucket/*` | ✅ | ✅ | ok |
| 4 | `storage.py` `list_keys` | ListObjectsV2 | `s3:ListBucket` | bucket ARN | ✅ | ✅ | ok (tests only at runtime) |
| 5 | `queues.py` `queue_url` | GetQueueUrl | `sqs:GetQueueUrl` | 3 main queue ARNs | ✅ | ✅ | ok |
| 6 | `queues.py` `assert_queues` → DLQs | GetQueueUrl | `sqs:GetQueueUrl` | **3 DLQ ARNs** | ✅ | ✅ | **GAP, FIXED in 4c.5c** |
| 7 | API upload / bridge `send` | SendMessage | `sqs:SendMessage` | parse, ingest | ✅ | — | **narrowed** — `extract` removed from the API |
| 8 | worker stage hand-off + 4b defer | SendMessage | `sqs:SendMessage` | all 3 main | — | ✅ | ok |
| 9 | `consumer.py` poll | ReceiveMessage | `sqs:ReceiveMessage` | 3 main | — | ✅ | ok |
| 10 | `consumer.py` ack | DeleteMessage | `sqs:DeleteMessage` | 3 main | — | ✅ | ok |
| 10b | `healing.py` `redrive_dlq` | ReceiveMessage, DeleteMessage | `sqs:ReceiveMessage`, `sqs:DeleteMessage` | **3 DLQ ARNs** | — | ✅ | **added in 4f-C** — bounded redrive after a provider outage; receive+delete only, worker only |
| 11 | `backpressure.py` `queue_depth` | GetQueueAttributes | `sqs:GetQueueAttributes` | 3 main | ✅ | ✅ | ok |
| 12 | — (no call site) | — | `s3:GetBucketLocation` | bucket ARN | ✅ | ✅ | **over-grant, kept deliberately** — boto3 issues it on a region redirect |
| 13 | — (no call site) | — | `sqs:ChangeMessageVisibility` | — | — | — | **over-grant, REMOVED in 4c.5c** |
| 14 | `storage.py` `ensure_bucket` | CreateBucket | `s3:CreateBucket` | — | ❌ | ❌ | **correct** — `INFRA_MODE=assert`, never reached |
| 15 | `queues.py` `ensure_queues` | CreateQueue / SetQueueAttributes | `sqs:CreateQueue` | — | ❌ | ❌ | **correct** — the bug fixed in 4c |
| 16 | `storage.py` `ensure_bucket_notifications` | PutBucketNotificationConfiguration | — | — | ❌ | ❌ | **correct** — guarded by `s3_endpoint_url`, unset on AWS |

Not on the task roles, and correctly so:

| Concern | Who holds it | Note |
|---|---|---|
| `ssm:GetParameters` on 3 parameter ARNs | **execution** role | ECS reads them and injects them; the app never calls SSM. Note the **plural** — ECS calls `GetParameters` even for one parameter, and granting only `GetParameter` is a task that dies on AccessDenied |
| `kms:Decrypt`, condition `kms:ViaService = ssm.<region>.amazonaws.com` | **execution** role | SecureString parameters are KMS-encrypted; without this the task fails with a KMS error rather than an SSM one, which sends you looking in the wrong service. Resource is `*` because the AWS-managed key's ARN does not exist until the account's first SecureString creates it; the condition is what scopes it |
| ECR pull, CloudWatch Logs write | **execution** role | via `AmazonECSTaskExecutionRolePolicy` |
| `kms:Decrypt` | nobody | secrets use the AWS-managed key, which grants via `kms:ViaService`; S3 uses SSE-S3 (AES256), not KMS |
| `sts:GetCallerIdentity` | Terraform's own principal | no application code calls STS |

### CI deploy role (GitHub OIDC)

| # | Where | AWS API | Action | Resource | Status |
|---|---|---|---|---|---|
| 17 | `amazon-ecr-login` | GetAuthorizationToken | `ecr:GetAuthorizationToken` | `*` (action takes no resource) | ok |
| 18 | `docker push` | layer upload / PutImage | 7 `ecr:*` push actions | 2 repository ARNs | ok |
| 19 | migrate step | **DescribeSubnets, DescribeSecurityGroups** | `ec2:Describe*` | `*` | **GAP, FIXED in 4c.5c** |
| 20 | migrate step | RunTask, DescribeTasks | `ecs:RunTask`, `ecs:DescribeTasks` | `*` | ok |
| 21 | roll step | Describe/Register TaskDefinition, UpdateService | 4 `ecs:*` | `*` | ok |
| 22 | RunTask / RegisterTaskDefinition | — | `iam:PassRole` | 3 role ARNs, `PassedToService = ecs-tasks` | ok — without the condition this role could pass **any** role in the account |

**#19 is the one that would have hurt most.** The migrate step finds its subnets
and security group with `aws ec2 describe-subnets` before it can call
`ecs run-task`, and the deploy role had no EC2 grant. The failure lands *after*
the images are pushed and *before* the services roll — a half-done deploy, which
is the worst place to fail. `ec2:Describe*` does not support resource-level
permissions, so `"*"` is the only expressible scope; the actions are read-only.

**#6 is the one that would have hurt first.** Both services call `ensure_infra()`
at startup, which under `INFRA_MODE=assert` resolves a URL for every queue *and
its DLQ*. With no grant on the DLQ ARNs, the first `sqs:GetQueueUrl` against
`docfactory-dev-parse-dlq` returns `AccessDenied` and **both the API and the
worker crash-loop on their first task start**. Nothing local catches it —
ElasticMQ authorizes nothing and LocalStack does not enforce IAM either. There
is now a check in `infra/localstack/verify.py` asserting the grant exists as a
property of the policy document, so it cannot regress.

### Still unverified after all of this

- That every grant above is **sufficient in practice**. The table is a reading
  of the code; only a real apply evaluates a real policy.
- Anything ECR-related end to end (LocalStack Community has no ECR).
- The whole compute layer: Fargate, the ALB, application autoscaling.

---

## When the first apply fails on IAM

Expect this. It is the last mile the emulator could not close, and it is
routine.

**What it looks like.** A task that starts and dies within seconds, an ECS
service stuck at `runningCount: 0`, or a CI step that fails with
`AccessDeniedException`. The message is in the task logs, not in `terraform apply`
output:

```bash
aws logs tail /ecs/docfactory-dev/api --since 15m
aws logs tail /ecs/docfactory-dev/worker --since 15m
```

**How to read it.** An `AccessDenied` names all three things you need:

```
User: arn:aws:sts::123456789012:assumed-role/docfactory-dev-worker-task/abc
is not authorized to perform: sqs:GetQueueUrl
on resource: arn:aws:sqs:us-east-1:123456789012:docfactory-dev-parse-dlq
```

- **which role** — `docfactory-dev-worker-task` → `data.aws_iam_policy_document.worker_task`
- **which action** — `sqs:GetQueueUrl`
- **which resource** — the DLQ ARN

**The fix.** Widen exactly that one statement in
`infra/terraform/data-plane/iam.tf`. Add the single action to the single
resource. Do not reach for `sqs:*`, and do not reach for `"*"` — the whole point
of these roles is that a compromised task's blast radius is this stack's data
plane and nothing else. Then:

```bash
cd "$TF_DATA" && terraform apply
```

IAM changes propagate in seconds and the task role is re-evaluated on the next
task start; you do not need to re-apply the compute layer. Force a fresh start:

```bash
aws ecs update-service --cluster "$CLUSTER" --service docfactory-dev-api \
  --force-new-deployment
```

**Then write it down.** Add a row to the pre-flight table above with what the
call was and why it was missed. The table is only worth something if it stays
true, and the calls that were missed are more interesting than the ones that
were not.

**If you get stuck in a loop**, attach `ReadOnlyAccess` to the failing task role
*temporarily*, confirm that is genuinely the problem, enumerate every denial in
one pass, then remove it and add the specific grants. Never leave it attached.

---

## Cost summary

Full breakdown in [cost_model.md](cost_model.md). The defaults changed in
`7b7c4f1` for a credit-funded account: no load balancer, a 256/512 API task,
workers on Spot, and SSM Parameter Store instead of Secrets Manager. A standing
stack is **~$13.27/month**, down from ~$36; parked is $0.61.

| Resource | Idle cost | Note |
|---|---|---|
| Fargate — API | **$9.01/mo** at 1 task (256 CPU units, 512 MiB) | The largest line, and the only task that runs when idle. |
| Public IPv4 | **$3.65/mo** while a task runs | $0.005/hr per in-use address since Feb 2024. Unavoidable with no NAT gateway; charged per task-hour, so parking removes it. |
| ALB | **$0** by default | `enable_alb = false`; the task's public IP is the endpoint (`make api-url`). Turn it on for ~$16.43/mo when you need a stable hostname. |
| SSM Parameter Store | **$0.00** | Three SecureString parameters, Standard tier. Replaced Secrets Manager's $1.20/mo. |
| CloudWatch composite alarm | $0.50/mo | Not in the free tier; the metric alarms are. |
| Fargate — workers | **$0** idle | Zero when idle, and on Spot (~70% off) when not. |

Parked with `make aws-park` (all tasks at 0): **~$0.61/mo**. Compute destroyed,
data plane kept: **~$0.11/mo**. A three-hour demo brought up and destroyed the
same evening: **$0.05**.
