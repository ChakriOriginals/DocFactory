"""Application configuration.

All config comes from environment variables (12-factor). A local ``.env`` file
is read as a convenience for host-run dev; explicitly-set env vars always win.

Endpoint URLs default to ``None``, which means "use the boto3 default
credential chain against real AWS". Local dev opts into MinIO/ElasticMQ by
setting the endpoints in ``.env`` — production never has to unset anything.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        # allow the natural field name model_provider despite pydantic's
        # protected "model_" namespace
        protected_namespaces=(),
    )

    # The application connects as a NON-SUPERUSER role: superusers bypass row
    # level security entirely, even with FORCE ROW LEVEL SECURITY, so using the
    # owner here would silently disable every isolation policy.
    database_url: str = (
        "postgresql+psycopg://docfactory_app:docfactory_app@localhost:5432/docfactory"
    )
    # Owner connection, used only by Alembic: migrations create tables, roles
    # and policies, which the app role must not be able to do.
    database_admin_url: str = "postgresql+psycopg://docfactory:docfactory@localhost:5432/docfactory"
    app_db_password: str = "docfactory_app"

    aws_region: str = "us-east-1"

    # Object storage (S3 API; MinIO locally)
    s3_endpoint_url: str | None = None
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "docfactory"

    # Who owns the bucket and the queues.
    #
    # "ensure"  create them if missing — right for local dev, where compose
    #           brings up empty MinIO/ElasticMQ and nothing else will.
    # "assert"  verify they exist and fail loudly if not — right for AWS,
    #           where Terraform owns them and the task role deliberately has
    #           no CreateQueue/CreateBucket rights. Creating infrastructure is
    #           not something an application task should be able to do.
    # "auto"    assert when no endpoint overrides are set (i.e. real AWS),
    #           ensure otherwise (i.e. local compose).
    infra_mode: Literal["auto", "ensure", "assert"] = "auto"

    # Queues (SQS API; ElasticMQ locally)
    sqs_endpoint_url: str | None = None
    parse_queue: str = "docfactory-parse"
    extract_queue: str = "docfactory-extract"
    # After this many failed receives, the queue's redrive policy moves the
    # message to the DLQ. 3 = one flaky failure forgiven twice, then quarantine.
    max_receive_count: int = 3

    # LLM: mock is the default everywhere; anthropic only by explicit choice.
    model_provider: Literal["mock", "anthropic"] = "mock"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    # "low" effort: bulk structured extraction doesn't need deep reasoning,
    # and low effort on current models is strong at a fraction of the tokens.
    llm_effort: str = "low"
    # Hard cap on thinking + response tokens per call.
    llm_max_tokens: int = 8192

    # Deterministic labelled-error injection for the calibration study.
    # 0.0 keeps mock extraction clean, so ordinary tests, evals and pipeline
    # runs are unaffected; the study raises it explicitly.
    mock_corruption_rate: float = 0.0
    mock_corruption_seed: int = 1337

    # Below this many extracted characters a PDF is treated as image-only
    # (needs_ocr). A real one-page invoice yields several hundred.
    min_parse_chars: int = 100

    max_upload_mb: int = 25

    # --- ingestion -------------------------------------------------------
    # Documents dropped under {tenant}/{ingest_prefix}/ are picked up from a
    # storage event; the API upload path stays available and both converge on
    # the same pipeline.
    ingest_queue: str = "docfactory-ingest"
    ingest_prefix: str = "dropbox"
    # Shared secret for the local MinIO -> API notification bridge. On AWS,
    # S3 delivers to SQS directly and this is unused.
    #
    # DEFAULTS EMPTY, AND THAT IS THE POINT. The bridge endpoint is exempt from
    # API-key auth -- its caller is the object store, which has no tenant -- so
    # this string is the only thing standing in front of a route that puts its
    # body straight onto the ingest queue. A committed default is a shared
    # secret that is not secret, and this repository is public. Empty makes the
    # endpoint fail closed everywhere it is not deliberately configured, which
    # on AWS is everywhere. Local dev sets it in .env (see .env.example).
    ingest_webhook_token: str = ""
    # Notification target for object-created events. Locally MinIO's webhook
    # target (arn:minio:sqs::PRIMARY:webhook), which posts to the API bridge;
    # on AWS the ingest queue's own ARN, and no bridge is deployed. Empty
    # disables the batch path, leaving the API upload path alone.
    ingest_notify_target: str = ""

    # --- self-healing (4f-C) ----------------------------------------------
    # How often the worker sweeps for work the queue cannot recover on its
    # own: documents committed but never enqueued, and DLQ messages left by an
    # outage that has since cleared. 0 disables the sweeper entirely.
    heal_interval_seconds: int = 300
    # A document is only "stranded" if nothing could still be working on it.
    # This MUST stay above the longest visibility timeout (extract, 90s) or the
    # reaper races live messages and re-enqueues documents that were fine.
    heal_stale_after_seconds: int = 900
    # Times a dead-lettered message may be brought back before it is left dead.
    # The bound is what stops a poison document looping between the two queues
    # forever, burning a model call per lap.
    dlq_max_redrives: int = 2

    # Liveness. The worker touches this file only while every consumer thread
    # is alive; the container health check reads its age. A path under /tmp
    # because the task filesystem is ephemeral and this is not state worth
    # keeping — it is a claim about the last few seconds.
    worker_heartbeat_path: str = "/tmp/worker-heartbeat"
    worker_heartbeat_interval_seconds: int = 15

    # Model-provider circuit breaker. Consecutive TRANSIENT failures before the
    # fleet stops calling a provider that is evidently down, and how long it
    # waits before letting one probe through.
    breaker_threshold: int = 5
    breaker_cooldown_seconds: int = 60

    # --- backpressure ----------------------------------------------------
    # Uploads per tenant per minute, and how much of that a burst may spend at
    # once. Over the rate is a 429 with Retry-After, never a silent drop.
    rate_limit_per_minute: float = 120.0
    rate_limit_burst: int = 30
    # How many documents one tenant may occupy the pipeline with at once. A
    # message over the ceiling is deferred back to the queue so another
    # tenant's work is picked up instead — fairness, not throughput.
    max_in_flight_per_tenant: int = 25
    # How long a deferred message waits before it is eligible again.
    defer_seconds: int = 5

    # Fitted confidence model consumed by routing. The threshold lives inside
    # this file, never in code, so a refit changes behaviour by swapping the
    # config. Point at a different version to roll forward or back.
    confidence_model_path: str = "config/confidence_model_v2.json"

    # How long a review task has before it breaches. Configurable rather than
    # constant: Phase 3 makes this per-tenant/per-pipeline, and the deadline is
    # frozen onto each task at creation so changing it cannot retroactively
    # breach work already queued.
    # Default for newly created tenants; the live value is per-tenant on the
    # tenants row (Phase 3 made it configurable per tenant).
    review_sla_hours: float = 24.0

    # Flat price per model call. Real token-based metering is Phase 4; this is
    # enough to make budget caps enforceable and testable now.
    cost_per_extraction_usd: float = 0.01

    phoenix_collector_endpoint: str = "http://localhost:6006"

    default_tenant_id: str = "dev-tenant"


@lru_cache
def get_settings() -> Settings:
    return Settings()
