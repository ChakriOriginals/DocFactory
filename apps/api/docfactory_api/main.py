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
from datetime import UTC, datetime
from typing import BinaryIO, Literal

from docfactory_core.auth import AuthError, resolve_tenant
from docfactory_core.bootstrap import ensure_infra
from docfactory_core.config import get_settings
from docfactory_core.db import current_tenant, session_scope
from docfactory_core.logging import configure_logging, document_id_var
from docfactory_core.models import (
    Document,
    DocumentStatus,
    Extraction,
    ReviewResolution,
    ReviewTask,
)
from docfactory_core.pipeline_registry import available_slugs
from docfactory_core.queues import QueueBroker
from docfactory_core.review import open_tasks, queue_depth, resolve_task
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import inject_trace_context, setup_tracing
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field
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

# Endpoints reachable without a tenant: liveness and the OpenAPI surface.
_UNAUTHENTICATED_PATHS = frozenset(
    {"/healthz", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


@app.middleware("http")
async def bind_tenant(request: Request, call_next):
    """Resolve the API key and bind its tenant for the whole request.

    Binding here rather than per-endpoint means an endpoint physically cannot
    run without a tenant: session_scope raises without one, and the RLS
    policies see no tenant and return nothing. Forgetting the plumbing costs
    access, never privacy.
    """
    if request.url.path in _UNAUTHENTICATED_PATHS:
        return await call_next(request)
    try:
        tenant_id = resolve_tenant(request.headers.get("x-api-key"))
    except AuthError as exc:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    token = current_tenant.set(tenant_id)
    try:
        response = await call_next(request)
    finally:
        current_tenant.reset(token)
    return response


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    status: str
    duplicate: bool = False


class ExtractionView(BaseModel):
    model: str
    output: dict
    validation: dict | None
    validation_passed: bool | None
    # Since 2.3b this is the calibrated probability of the weakest field, so
    # it is directly comparable to the routing threshold.
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


class ReviewTaskSummary(BaseModel):
    task_id: uuid.UUID
    document_id: uuid.UUID
    status: str
    flagged_fields: list[str]
    created_at: datetime
    sla_due_at: datetime
    breached: bool


class FlaggedField(BaseModel):
    name: str
    value: str | None
    confidence: float | None


class ReviewTaskDetail(ReviewTaskSummary):
    doc_confidence: float | None
    confidence_model_version: int | None
    extraction_output: dict
    flagged: list[FlaggedField]


class ResolveRequest(BaseModel):
    resolution: Literal["approved_as_is", "corrected"]
    corrections: dict[str, str] = Field(default_factory=dict)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/documents", status_code=202, response_model=UploadResponse)
def upload_document(
    file: UploadFile, response: Response, doc_type: str = "invoice"
) -> UploadResponse:
    """Accept a PDF for a document type this deployment has a pipeline for.

    `doc_type` selects the pipeline definition — its schema, its rules, its
    SLA. It is caller-supplied, so an unknown type is rejected here, at the
    boundary, rather than discovered by a worker holding a document it has no
    definition for.
    """
    settings = get_settings()
    tenant_id = current_tenant.get()

    if doc_type not in available_slugs():
        raise HTTPException(
            status_code=400,
            detail=f"unknown doc_type {doc_type!r}; available: {list(available_slugs())}",
        )

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
            doc_type=doc_type,
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
            # The message carries its tenant: the worker must bind a tenant
            # context before it can read anything, so it cannot discover the
            # tenant by reading the document first.
            inject_trace_context({"document_id": str(document.id), "tenant_id": tenant_id}),
        )
        log.info("document received", extra={"sha256": sha256, "bytes": size})
        return UploadResponse(document_id=document.id, status=DocumentStatus.RECEIVED)


@app.get("/documents/{document_id}", response_model=DocumentView)
def get_document(document_id: uuid.UUID) -> DocumentView:
    """Fetch a document.

    Another tenant's document is a 404, not a 403: RLS filters the row out
    before this code sees it, so "absent" and "not yours" are indistinguishable
    from here — which is precisely the behaviour we want, since a 403 would
    confirm the id exists somewhere.
    """
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


def _summarize(task: ReviewTask, now: datetime) -> ReviewTaskSummary:
    return ReviewTaskSummary(
        task_id=task.id,
        document_id=task.document_id,
        status=task.status,
        flagged_fields=list(task.flagged_fields),
        created_at=task.created_at,
        sla_due_at=task.sla_due_at,
        breached=task.is_breached(now),
    )


@app.get("/review/tasks", response_model=list[ReviewTaskSummary])
def list_review_tasks(limit: int = 50) -> list[ReviewTaskSummary]:
    """Open review tasks, oldest first — the queue a reviewer works top-down."""
    now = datetime.now(UTC)
    return [_summarize(task, now) for task in open_tasks(limit=limit)]


@app.get("/review/queue", response_model=dict)
def review_queue_stats() -> dict:
    """Depth, age and breach count.

    Detection only: alerting and burn-rate are Phase 5 operations concerns.
    """
    return queue_depth()


@app.get("/review/tasks/{task_id}", response_model=ReviewTaskDetail)
def get_review_task(task_id: uuid.UUID) -> ReviewTaskDetail:
    """Another tenant's task is a 404 for the same reason as documents."""
    with session_scope() as session:
        task = session.get(ReviewTask, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="review task not found")
        extraction = session.get(Extraction, task.extraction_id)
        by_name = {field.name: field for field in extraction.fields}
        # Only the cells that tripped the threshold: pointing the reviewer at
        # those is the payoff of the field-level attribution from 2.1.
        flagged = [
            FlaggedField(
                name=name,
                value=by_name[name].value if name in by_name else None,
                confidence=float(by_name[name].confidence)
                if name in by_name and by_name[name].confidence is not None
                else None,
            )
            for name in task.flagged_fields
        ]
        return ReviewTaskDetail(
            **_summarize(task, datetime.now(UTC)).model_dump(),
            doc_confidence=float(extraction.doc_confidence)
            if extraction.doc_confidence is not None
            else None,
            confidence_model_version=extraction.confidence_model_version,
            extraction_output=extraction.output,
            flagged=flagged,
        )


@app.post("/review/tasks/{task_id}/resolve", status_code=200)
def resolve_review_task(task_id: uuid.UUID, request: ResolveRequest) -> dict:
    """Accept the extraction as-is, or write corrected values.

    A correction updates the stored extraction and appends one eval_case per
    field, so the reviewer's effort becomes future evaluation data.
    """
    try:
        resolve_task(
            task_id,
            resolution=ReviewResolution(request.resolution),
            corrections=request.corrections,
        )
    except ValueError as exc:
        status = 404 if "not found" in str(exc) else 409
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"task_id": str(task_id), "status": "resolved"}
