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
    FAILED = "failed"


_STATUS_CHECK = "status IN ({})".format(", ".join(f"'{s}'" for s in DocumentStatus))


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
