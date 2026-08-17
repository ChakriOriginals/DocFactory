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

    database_url: str = "postgresql+psycopg://docfactory:docfactory@localhost:5432/docfactory"

    aws_region: str = "us-east-1"

    # Object storage (S3 API; MinIO locally)
    s3_endpoint_url: str | None = None
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "docfactory"

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

    # Below this many extracted characters a PDF is treated as image-only
    # (needs_ocr). A real one-page invoice yields several hundred.
    min_parse_chars: int = 100

    max_upload_mb: int = 25

    phoenix_collector_endpoint: str = "http://localhost:6006"

    default_tenant_id: str = "dev-tenant"


@lru_cache
def get_settings() -> Settings:
    return Settings()
