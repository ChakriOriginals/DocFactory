COMPOSE := docker compose -f infra/compose/docker-compose.yml --env-file .env
LOCALSTACK := docker compose -f infra/localstack/docker-compose.yml
DATA_PLANE := infra/terraform/data-plane
LS_VARS := -var-file=../../localstack/data-plane.tfvars

# ECR is a LocalStack Pro service; Community answers ecr:CreateRepository with
# HTTP 501. The CI deploy role's inline policy references the repository ARNs,
# so it cannot be applied either. The 4f-B cost guard rails are excluded too:
# LocalStack Community has no Budgets API at all, and its SNS rejects the
# provider's dummy credentials with InvalidClientTokenId — an emulator quirk,
# not a configuration error, since the same credentials create S3, SQS, IAM and
# Secrets Manager resources in the same apply. Those two are covered by
# `terraform plan` instead; see docs/cost_model.md.
# Everything else in the layer is applied for real — see
# infra/localstack/README.md for exactly what that does and does not prove.
LS_TARGETS := \
	-target=aws_s3_bucket_notification.documents \
	-target=aws_s3_bucket_public_access_block.documents \
	-target=aws_s3_bucket_server_side_encryption_configuration.documents \
	-target=aws_s3_bucket_versioning.documents \
	-target=aws_iam_role_policy.api_task \
	-target=aws_iam_role_policy.worker_task \
	-target=aws_iam_role_policy.task_execution_secrets \
	-target=aws_iam_role_policy_attachment.task_execution \
	-target=aws_secretsmanager_secret_version.database_url_app \
	-target=aws_secretsmanager_secret_version.database_url_owner \
	-target=aws_secretsmanager_secret_version.anthropic_api_key \
	-target='aws_iam_openid_connect_provider.github[0]' \
	-target='aws_iam_role.github_deploy[0]'

.PHONY: setup up down seed test lint fmt eval eval-gate costs migrate calibrate calibrate-fit drift-experiment \
	aws-park aws-unpark aws-cost heal \
	localstack-up localstack-down localstack-apply localstack-verify localstack-destroy localstack-cycle

.env:
	cp .env.example .env

setup: .env
	uv sync --all-packages --all-groups

## Start the local stack (postgres, minio, elasticmq, phoenix) and ensure
## the bucket + queues exist — the same ensure logic the services run on startup.
up: .env
	$(COMPOSE) up -d --wait
	uv run python -m docfactory_core.bootstrap

down: .env
	$(COMPOSE) down

## Destroy the stack INCLUDING data volumes (db rows, stored objects, traces).
reset: .env
	$(COMPOSE) down -v

# DYLD_FALLBACK_LIBRARY_PATH lets WeasyPrint find Homebrew's pango on macOS; harmless elsewhere.
seed: .env
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python data/synth/generate.py --type invoice --count 500 --upload --previews
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python data/synth/generate.py --type purchase_order --count 120 --upload --previews

api: .env
	uv run uvicorn docfactory_api.main:app --port 8000

worker: .env
	uv run python -m docfactory_worker.main

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

## Calibration study (2.2c): build the labelled dataset, then fit offline.
## Corruption is enabled here and only here — the mock stays clean everywhere else.
calibrate: .env
	MOCK_CORRUPTION_RATE=0.35 uv run python -m docfactory_evals.calibrate

## Re-fit from already-stored signal vectors, no extraction pass.
calibrate-fit: .env
	uv run python -m docfactory_evals.calibrate --fit-only

## Field-accuracy eval on the golden set, per document type. Uses
## MODEL_PROVIDER from .env (mock by default). One type: --type purchase_order.
## For a real number: MODEL_PROVIDER=anthropic make eval
eval: .env
	uv run python -m docfactory_evals.run

## Unit-cost rollup from recorded usage events (what the worker actually spent).
costs: .env
	uv run python -m docfactory_evals.costs

## The template-swap drift experiment (4e.3): stage a vendor format change,
## measure detection lag against the quality of what was auto-approved, and
## run a no-swap control. Renders ~170 PDFs, so it takes a few minutes; mock
## mode, so it costs nothing. Writes docs/drift_experiment.md + the plot.
drift-experiment: .env
	DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python -m docfactory_evals.drift_experiment

## The CI gate: re-run the golden sets and fail if any type drops below its
## floor in config/eval_thresholds.json. Mock mode only — free and deterministic.
eval-gate: .env
	uv run python -m docfactory_evals.gate

migrate: .env
	uv run alembic upgrade head

## Force a healing sweep now: redrive what an outage dead-lettered, re-enqueue
## anything stranded. Idempotent, and the worker does it every 5 minutes anyway.
heal: .env
	uv run python -m docfactory_core.healing

# --- cost control on a deployed stack (4f-B) --------------------------------
#
# The on-demand version of the dead man's switch. Scales every service to zero
# without touching the infrastructure, so the stack comes back with `make
# aws-unpark` in seconds rather than a full apply.
#
# This does NOT stop the ALB, which is most of the idle cost. Only destroying
# the compute layer does that:  cd infra/terraform/compute-plane && terraform destroy
# See docs/cost_model.md for what each option actually saves.
CLUSTER ?= docfactory-dev

aws-park:
	@echo "Scaling $(CLUSTER) services to zero (the ALB keeps billing — destroy to stop it)."
	aws ecs update-service --cluster $(CLUSTER) --service $(CLUSTER)-api --desired-count 0 >/dev/null
	aws ecs update-service --cluster $(CLUSTER) --service $(CLUSTER)-worker --desired-count 0 >/dev/null
	aws application-autoscaling register-scalable-target \
		--service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
		--resource-id service/$(CLUSTER)/$(CLUSTER)-worker --min-capacity 0 --max-capacity 0
	@echo "Parked. Run 'make aws-unpark' to bring it back."

aws-unpark:
	aws application-autoscaling register-scalable-target \
		--service-namespace ecs --scalable-dimension ecs:service:DesiredCount \
		--resource-id service/$(CLUSTER)/$(CLUSTER)-worker --min-capacity 0 --max-capacity 6
	aws ecs update-service --cluster $(CLUSTER) --service $(CLUSTER)-api --desired-count 1 >/dev/null
	@echo "Unparked. Workers stay at zero until the backlog alarm needs them."

## What is billing right now, per service, month to date.
aws-cost:
	aws ce get-cost-and-usage --time-period Start=$$(date -u +%Y-%m-01),End=$$(date -u +%Y-%m-%d) \
		--granularity MONTHLY --metrics UnblendedCost \
		--group-by Type=DIMENSION,Key=SERVICE \
		--query 'ResultsByTime[0].Groups[?Metrics.UnblendedCost.Amount>`0.001`].[Keys[0],Metrics.UnblendedCost.Amount]' \
		--output table

# --- LocalStack validation of the data plane (4c.5b) ------------------------
#
# `terraform apply` for real, against an emulator, before it is ever applied to
# a real account. Proves the wiring stands up and tears down; proves nothing
# about IAM enforcement, which LocalStack Community does not do. Needs
# terraform on PATH and the LocalStack image (`docker pull localstack/localstack:4.0`).

localstack-up:
	$(LOCALSTACK) up -d --wait

localstack-down:
	$(LOCALSTACK) down -v

localstack-apply: localstack-up
	cd $(DATA_PLANE) && terraform init -input=false
	cd $(DATA_PLANE) && terraform apply -input=false -auto-approve $(LS_VARS) $(LS_TARGETS)
	cd $(DATA_PLANE) && terraform output -json > /tmp/docfactory-data-plane.json

## Query the emulator for what apply claims to have built — including two
## behavioural checks (an S3 drop must reach the queue; a message received
## three times must land in the DLQ) that a config-only check cannot make.
localstack-verify:
	uv run python infra/localstack/verify.py --outputs /tmp/docfactory-data-plane.json

localstack-destroy:
	cd $(DATA_PLANE) && terraform destroy -input=false -auto-approve $(LS_VARS)

## The whole cycle: stand it up, apply, verify, tear it down, confirm empty.
localstack-cycle: localstack-apply localstack-verify localstack-destroy
	$(LOCALSTACK) down -v
