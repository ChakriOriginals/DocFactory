"""Database models.

Conventions:
- Every table: uuid id, created_at, updated_at (timezone-aware).
- tenant_id lives on every tenant-scoped table *now* so Phase 3 row-level
  security is a policy addition, not a schema rewrite.
- status is text + a named CHECK constraint rather than a native PG enum:
  adding a status later is a transactional drop/recreate of the constraint,
  while ALTER TYPE ... ADD VALUE cannot run in a transaction and enum values
  can never be dropped.
- Money/token/cost columns exist on extractions now (schema stability);
  metering logic that fills cost is a later phase.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class DocumentStatus(enum.StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    PARSED = "parsed"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    # Terminal, *expected* outcome for image-only PDFs until the OCR tier
    # exists. Deliberately distinct from FAILED: nothing went wrong.
    NEEDS_OCR = "needs_ocr"
    # Post-extraction routing outcomes (2.3b). Adding these is a one-line
    # change to the CHECK constraint precisely because status is a string
    # column rather than a native Postgres enum.
    APPROVED = "approved"
    NEEDS_REVIEW = "needs_review"
    # The tenant's budget cap was reached before this document could be
    # extracted. Distinct from FAILED: nothing is wrong with the document, and
    # it becomes processable again when the cap is raised.
    BUDGET_EXCEEDED = "budget_exceeded"
    FAILED = "failed"


_STATUS_CHECK = "status IN ({})".format(", ".join(f"'{s}'" for s in DocumentStatus))


class TenantStatus(enum.StrEnum):
    ACTIVE = "active"
    # Over budget: no further model calls until the cap is raised.
    PAUSED = "paused"


class Base(DeclarativeBase):
    pass


class ColumnsMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Tenant(Base):
    """A customer of the platform.

    The primary key is a slug, not a uuid, because it is also the object-store
    prefix ({tenant_id}/incoming/...) and appears in every log line — a
    readable key is worth more here than a synthetic one, and every
    tenant_id column in the schema already holds exactly this value.
    """

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=TenantStatus.ACTIVE)
    # Per-tenant settings that were global constants before Phase 3.
    review_sla_hours: Mapped[float] = mapped_column(
        Numeric(8, 2), nullable=False, server_default="24"
    )
    budget_usd: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False, server_default="100")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ({})".format(", ".join(f"'{s}'" for s in TenantStatus)),
            name="ck_tenants_status",
        ),
    )


class ApiKey(ColumnsMixin, Base):
    """An API credential for a tenant.

    Only a hash is stored. The plaintext key is shown once at issue time and
    is unrecoverable afterwards, so a database leak cannot be replayed as
    valid credentials.
    """

    __tablename__ = "api_keys"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Leading characters, for identifying a key in a UI without revealing it.
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("key_hash", name="uq_api_keys_hash"),
        Index("ix_api_keys_tenant", "tenant_id"),
    )


class Pipeline(ColumnsMixin, Base):
    """A tenant's document-processing definition, versioned.

    Editing a pipeline creates a new version rather than mutating the current
    one, so documents in flight keep the definition they started under and an
    extraction can always be explained by the config that produced it.
    """

    __tablename__ = "pipelines"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    document_type: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", "version", name="uq_pipelines_tenant_slug_version"),
        Index("ix_pipelines_tenant_active", "tenant_id", "slug", "is_active"),
    )


class Document(ColumnsMixin, Base):
    __tablename__ = "documents"

    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    s3_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    doc_type: Mapped[str] = mapped_column(Text, nullable=False, server_default="invoice")
    # Unknown at upload; a later phase may classify. Nullable by design.
    layout: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DocumentStatus.RECEIVED
    )
    # Parsed text is an artifact, not a row attribute: it lives in object
    # storage and the row stores the pointer (and size, for observability).
    text_s3_key: Mapped[str | None] = mapped_column(Text)
    text_chars: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    extractions: Mapped[list["Extraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="Extraction.created_at"
    )

    __table_args__ = (
        # DB-enforced upload idempotency: same bytes for the same tenant can
        # only ever be one document, even under concurrent uploads.
        UniqueConstraint("tenant_id", "sha256", name="uq_documents_tenant_sha256"),
        CheckConstraint(_STATUS_CHECK, name="ck_documents_status"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
    )


class Extraction(ColumnsMixin, Base):
    __tablename__ = "extractions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "anthropic:claude-…"
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Per-rule deterministic validation results, e.g. {"totals_add_up": true}.
    validation: Mapped[dict | None] = mapped_column(JSONB)
    validation_passed: Mapped[bool | None] = mapped_column(Boolean)
    # Calibrated probability of the weakest field, so it is directly
    # comparable to the model's threshold: the document clears it exactly when
    # every field does. (Before 2.3b this held the uncalibrated prior score.)
    doc_confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))
    # Which model made the call, so an approval can always be traced to the
    # weights that approved it.
    routing_decision: Mapped[str | None] = mapped_column(String(32))
    confidence_model_version: Mapped[int | None] = mapped_column(Integer)
    # Whether those weights were ever fitted on THIS document type: the
    # pipeline slug when they were, "borrowed:<slug>" when they were not.
    confidence_calibration: Mapped[str | None] = mapped_column(Text)
    # Which pipeline definition produced this, mirroring the model-version
    # discipline: an extraction can always be explained by its config.
    pipeline_slug: Mapped[str | None] = mapped_column(Text)
    pipeline_version: Mapped[int | None] = mapped_column(Integer)
    # Raw scorer inputs (rule outcomes, residual magnitudes, shape flags).
    # Persisted so confidence weights can be refit offline against the golden
    # set without reprocessing documents — see confidence.py.
    confidence_signals: Mapped[dict | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    document: Mapped[Document] = relationship(back_populates="extractions")
    fields: Mapped[list["ExtractionField"]] = relationship(
        back_populates="extraction", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_extractions_document_created", "document_id", "created_at"),)


class ExtractionField(ColumnsMixin, Base):
    __tablename__ = "extraction_fields"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Dotted paths for nested values, e.g. "line_items.0.unit_price".
    name: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4))

    extraction: Mapped[Extraction] = relationship(back_populates="fields")

    __table_args__ = (
        UniqueConstraint("extraction_id", "name", name="uq_extraction_fields_extraction_name"),
    )


class UsageEvent(ColumnsMixin, Base):
    """One metered model call.

    The audit trail behind every cost number: what was spent, on which model
    and tier, for which document and pipeline, and why the call happened (the
    first extraction, or an escalation to a stronger model). Token counts come
    from the provider's own usage report; `cost_usd` is those tokens priced by
    `config/model_pricing.json`.

    Kept separate from `extractions` because one extraction can involve more
    than one call — routing escalates — and because a call that produced no
    usable extraction still cost money.
    """

    __tablename__ = "usage_events"

    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    # SET NULL, never CASCADE: deleting a document must not delete the record
    # that money was spent on it. The link is expendable; the spend is not.
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    extraction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("extractions.id", ondelete="SET NULL")
    )
    pipeline_slug: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, nullable=False)  # "provider:model-id"
    model_tier: Mapped[str] = mapped_column(Text, nullable=False)  # "small" | "frontier"
    # Why this call happened: the first attempt, or an escalation after it.
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), nullable=False, server_default="0")
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index("ix_usage_events_tenant_created", "tenant_id", "created_at"),
        Index("ix_usage_events_document", "document_id"),
    )


class TenantSpend(Base):
    """A tenant's running spend, as a counter rather than a query.

    Summing `usage_events` gives the same number, but a cap enforced by
    read-then-write races: two workers can both read a spend below the cap and
    both charge. This row is the serialization point — the charge is a single
    conditional UPDATE, so the second worker re-evaluates the cap while holding
    the row lock and loses cleanly.

    The events remain the audit trail; this is the enforcement point, and a
    test asserts the two agree.
    """

    __tablename__ = "tenant_spend"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    spent_usd: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False, server_default="0")
    # Calls charged but never settled (the worker died mid-call). Diagnostic
    # only: the estimate stays charged, which is the safe direction.
    unsettled_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class TenantRateLimit(Base):
    """A tenant's token bucket, shared by every API process.

    In-process rate limiting is per-process rate limiting: two API replicas
    would each allow the full rate. The bucket lives here so the limit is the
    tenant's, not the replica's, and it is spent with a conditional UPDATE for
    the same reason the budget counter is.
    """

    __tablename__ = "tenant_rate_limits"

    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True
    )
    tokens: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, server_default="0")
    refilled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DriftStat(Base):
    """Rolling baseline and drift state for one tenant's document type.

    One row per (tenant, doc_type, window). It holds the baseline the detector
    compares against AND the running state of that comparison, because both are
    updated by the same event — a document arriving — and splitting them would
    mean two writes that could disagree.

    `stats` carries Welford accumulators per signal, so the baseline is built
    in one pass with no stored history: a running mean, an M2 for the variance,
    and the consecutive-breach counter that makes a single odd document
    something other than an incident. `centroid` is the mean lexical profile of
    the baseline's text, which is what a new document's text distance is
    measured against.

    Nothing here requires a model call. Every input is already computed by the
    time a document finishes extracting — that is the design constraint:
    drift detection that doubles inference cost defeats its own purpose.
    """

    __tablename__ = "drift_stats"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    tenant_id: Mapped[str] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)
    # Which baseline this row is. Re-baselining after an acknowledged drift
    # opens "baseline-2" rather than mutating history, so a past detection
    # stays explicable by the numbers that produced it.
    window_key: Mapped[str] = mapped_column(Text, nullable=False, server_default="baseline-1")

    # "baseline"  still collecting; makes no drift claims (the cold-start guard)
    # "stable"    baseline frozen, nothing breaching
    # "drifting"  a signal breached on consecutive_k consecutive documents
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="baseline")

    n_observed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # Per-signal {mean, m2, n, consecutive, last_z}. JSONB rather than columns
    # because the signal set is expected to grow and a new signal should not
    # be a migration.
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Mean lexical profile of the baseline's document text.
    centroid: Mapped[list | None] = mapped_column(JSONB)
    flagged_signals: Mapped[list | None] = mapped_column(JSONB)
    first_flagged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Which document tripped it, so a flag can be traced to a page.
    flagged_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "doc_type", "window_key", name="uq_drift_stats_window"),
        CheckConstraint(
            "status IN ('baseline', 'stable', 'drifting')", name="ck_drift_stats_status"
        ),
    )


class ReviewStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReviewResolution(enum.StrEnum):
    APPROVED_AS_IS = "approved_as_is"
    CORRECTED = "corrected"


_REVIEW_STATUS_CHECK = "status IN ({})".format(", ".join(f"'{s}'" for s in ReviewStatus))


class ReviewTask(ColumnsMixin, Base):
    """One human review of one extraction.

    `flagged_fields` carries the specific below-threshold field names rather
    than just the document id — the fault localization built in 2.1 exists so
    a reviewer can be pointed at the cells that look wrong instead of re-reading
    the whole invoice.
    """

    __tablename__ = "review_tasks"

    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=ReviewStatus.OPEN)
    # Absolute deadline, computed from a configurable SLA duration at creation
    # time. Storing the deadline rather than the duration means a later config
    # change cannot retroactively breach or un-breach existing tasks.
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    flagged_fields: Mapped[list] = mapped_column(JSONB, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(32))

    extraction: Mapped[Extraction] = relationship()

    __table_args__ = (
        # One task per extraction: redelivery must not queue the same work twice.
        UniqueConstraint("extraction_id", name="uq_review_tasks_extraction"),
        CheckConstraint(_REVIEW_STATUS_CHECK, name="ck_review_tasks_status"),
        Index("ix_review_tasks_tenant_status_due", "tenant_id", "status", "sla_due_at"),
    )

    def is_breached(self, now: datetime) -> bool:
        return self.status == ReviewStatus.OPEN and now > self.sla_due_at


class EvalCase(ColumnsMixin, Base):
    """A human correction, captured as future eval data.

    This is the loop that makes review compound: every field a reviewer fixes
    becomes a labelled case the eval harness and future calibration runs can
    use, so corrections improve the system rather than only the one document.
    """

    __tablename__ = "eval_cases"

    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    extraction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("extractions.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_value: Mapped[str | None] = mapped_column(Text)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default="human_review")

    __table_args__ = (Index("ix_eval_cases_tenant_document", "tenant_id", "document_id"),)
