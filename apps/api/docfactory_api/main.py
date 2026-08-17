"""Upload API.

POST /documents accepts a PDF, streams it to object storage, and enqueues the
parse stage; the pipeline runs asynchronously and GET /documents/{id} reports
progress. Endpoints are plain `def` — FastAPI runs them in its threadpool, so
sync boto3/SQLAlchemy calls don't block the event loop.

Idempotency: the object key is content-addressed ({tenant}/incoming/{sha}.pdf)
and the DB enforces one document per (tenant_id, sha256) — a concurrent
duplicate upload loses the INSERT race and returns the existing document.
"""

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import BinaryIO

from docfactory_core.bootstrap import ensure_infra
from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.logging import configure_logging, document_id_var
from docfactory_core.models import Document, DocumentStatus, Extraction
from docfactory_core.queues import QueueBroker
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import inject_trace_context, setup_tracing
from fastapi import FastAPI, HTTPException, Response, UploadFile
from opentelemetry import trace
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)
tracer = trace.get_tracer("docfactory")

_CHUNK_SIZE = 1024 * 1024
_PDF_MAGIC = b"%PDF-"


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("api")
    setup_tracing("docfactory-api")
    ensure_infra()
    app.state.store = ObjectStore()
    app.state.broker = QueueBroker()
    yield


app = FastAPI(title="DocFactory API", lifespan=lifespan)


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    duplicate: bool = False


class ExtractionView(BaseModel):
    model: str
    output: dict
    validation: dict | None
    validation_passed: bool | None
    # Uncalibrated (see core.confidence): usable for ranking, not as a
    # probability, and deliberately not compared against any threshold yet.
    doc_confidence: float | None
    field_confidence: dict[str, float]
    created_at: datetime


class DocumentView(BaseModel):
    document_id: uuid.UUID
    tenant_id: str
    doc_type: str
    status: str
    sha256: str
    s3_key: str
    text_chars: int | None
    last_error: str | None
    received_at: datetime
    parsed_at: datetime | None
    extracted_at: datetime | None
    extraction: ExtractionView | None


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/documents", status_code=202, response_model=UploadResponse)
def upload_document(file: UploadFile, response: Response) -> UploadResponse:
    settings = get_settings()
    tenant_id = settings.default_tenant_id

    sha256, size = _hash_stream(file.file)
    if size == 0:
        raise HTTPException(status_code=400, detail="empty upload")

    file.file.seek(0)
    if file.file.read(len(_PDF_MAGIC)) != _PDF_MAGIC:
        raise HTTPException(status_code=400, detail="not a PDF (missing %PDF- magic bytes)")

    with tracer.start_as_current_span("document.upload") as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("tenant_id", tenant_id)
        span.set_attribute("document.sha256", sha256)

        with session_scope() as session:
            existing = session.scalar(
                select(Document).where(Document.tenant_id == tenant_id, Document.sha256 == sha256)
            )
            if existing is not None:
                _mark_duplicate(span, existing)
                response.status_code = 200
                return UploadResponse(
                    document_id=existing.id, status=existing.status, duplicate=True
                )

        s3_key = f"{tenant_id}/incoming/{sha256}.pdf"
        file.file.seek(0)
        app.state.store.put_object(s3_key, file.file, content_type="application/pdf")

        document = Document(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            s3_key=s3_key,
            sha256=sha256,
            status=DocumentStatus.RECEIVED,
        )
        try:
            with session_scope() as session:
                session.add(document)
        except IntegrityError:
            # lost a race against a concurrent identical upload; the object
            # write above was a harmless re-PUT of the same content
            with session_scope() as session:
                existing = session.scalar(
                    select(Document).where(
                        Document.tenant_id == tenant_id, Document.sha256 == sha256
                    )
                )
            _mark_duplicate(span, existing)
            response.status_code = 200
            return UploadResponse(document_id=existing.id, status=existing.status, duplicate=True)

        document_id_var.set(str(document.id))
        span.set_attribute("document_id", str(document.id))
        app.state.broker.send(
            settings.parse_queue,
            inject_trace_context({"document_id": str(document.id)}),
        )
        log.info("document received", extra={"sha256": sha256, "bytes": size})
        return UploadResponse(document_id=document.id, status=DocumentStatus.RECEIVED)


@app.get("/documents/{document_id}", response_model=DocumentView)
def get_document(document_id: uuid.UUID) -> DocumentView:
    with session_scope() as session:
        document = session.get(Document, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        latest = session.scalar(
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.created_at.desc())
            .limit(1)
        )
        return DocumentView(
            document_id=document.id,
            tenant_id=document.tenant_id,
            doc_type=document.doc_type,
            status=document.status,
            sha256=document.sha256,
            s3_key=document.s3_key,
            text_chars=document.text_chars,
            last_error=document.last_error,
            received_at=document.received_at,
            parsed_at=document.parsed_at,
            extracted_at=document.extracted_at,
            extraction=ExtractionView(
                model=latest.model,
                output=latest.output,
                validation=latest.validation,
                validation_passed=latest.validation_passed,
                doc_confidence=float(latest.doc_confidence)
                if latest.doc_confidence is not None
                else None,
                # Scalar fields only: the per-row line-item cells all inherit
                # one score, so listing them would be noise.
                field_confidence={
                    f.name: float(f.confidence)
                    for f in latest.fields
                    if f.confidence is not None and not f.name.startswith("line_items.")
                },
                created_at=latest.created_at,
            )
            if latest
            else None,
        )


def _hash_stream(stream: BinaryIO) -> tuple[str, int]:
    """SHA-256 + size from starlette's spooled upload, chunk by chunk."""
    settings = get_settings()
    max_bytes = settings.max_upload_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    stream.seek(0)
    while chunk := stream.read(_CHUNK_SIZE):
        digest.update(chunk)
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail=f"upload exceeds {settings.max_upload_mb} MB"
            )
    return digest.hexdigest(), size


def _mark_duplicate(span: trace.Span, document: Document) -> None:
    span.set_attribute("document_id", str(document.id))
    span.set_attribute("document.duplicate", True)
    log.info(
        "duplicate upload, returning existing document",
        extra={"document_id": str(document.id), "status": document.status},
    )
