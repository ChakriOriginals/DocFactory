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
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import BinaryIO, Literal

from docfactory_core.auth import AuthError, resolve_tenant
from docfactory_core.backpressure import check_rate_limit, in_flight, status_counts
from docfactory_core.backpressure import queue_depth as broker_queue_depth
from docfactory_core.bootstrap import ensure_infra
from docfactory_core.budget import budget_state
from docfactory_core.config import get_settings
from docfactory_core.db import current_tenant, session_scope
from docfactory_core.drift import drift_status
from docfactory_core.logging import configure_logging, document_id_var
from docfactory_core.metering import unit_costs
from docfactory_core.models import (
    Document,
    DocumentStatus,
    Extraction,
    ReviewResolution,
    ReviewTask,
)
from docfactory_core.pipeline_registry import available_slugs
from docfactory_core.queues import QueueBroker
from docfactory_core.review import correct_extraction, open_tasks, queue_depth, resolve_task
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import inject_trace_context, setup_tracing
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)
tracer = trace.get_tracer("docfactory")

_CHUNK_SIZE = 1024 * 1024
_PDF_MAGIC = b"%PDF-"


# The stages this process is ALLOWED to requeue.
#
# The API task role may send to parse and ingest, and deliberately not to
# extract: it never consumes from a queue and never drives extraction. That
# boundary is worth keeping, so the sweep is filtered to match it rather than
# the role being widened to make a sweeper convenient.
#
# It is not a partial fix. The stage the API can reap is exactly the stage that
# strands when nothing else is running — a document is created here, in status
# `received`, and enqueued to parse immediately after; if that send fails the
# row exists with no message and only this process is awake to notice.
# Documents stranded at parsed/extracting reached those states inside a worker,
# and a worker that ran will sweep them.
_API_REAPABLE_STAGES = frozenset({"parse"})


def _reaper_loop(stop: threading.Event, settings) -> None:
    """Sweep for documents stranded before their first message existed.

    THE REASON THIS LIVES HERE AND NOT ONLY IN THE WORKER. The worker's sweep
    reasons that "a fleet that scales to zero has nobody to run a cron, so
    every running worker sweeps". True, and it leaves the gap this covers:
    worker_min_count is 0, and a stranded document has no message, so there is
    no queue depth, so the backlog alarm never fires, so no worker ever starts
    to run the sweep that would have found it. The document waits for an
    unrelated upload — until tomorrow on a quiet day, until Monday on a quiet
    week.

    The API is desired_count 1 and always awake, which is the whole point.

    NOTHING HERE MAY KILL THE API. A sweeper that takes down the process it
    lives in has done more damage than the documents it was looking for — the
    same rule the worker's loop states, and the API is the more costly place to
    break it.
    """
    from datetime import timedelta

    from docfactory_core.healing import reap_stuck_documents

    stale_after = timedelta(seconds=settings.heal_stale_after_seconds)

    # Wait first, so a restart does not sweep in the same instant it starts
    # serving.
    while not stop.wait(settings.heal_interval_seconds):
        try:
            report = reap_stuck_documents(
                app.state.broker,
                stale_after=stale_after,
                stages=_API_REAPABLE_STAGES,
            )
        except Exception:
            log.exception("reaper sweep failed; the API continues")
            continue
        if report.requeued or report.skipped_stage:
            log.info(
                "reaper sweep",
                extra={
                    "requeued": report.requeued,
                    "scanned": report.scanned,
                    "skipped_stage": report.skipped_stage,
                    "skipped_budget": report.skipped_budget,
                },
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("api")
    setup_tracing("docfactory-api")
    ensure_infra()
    app.state.store = ObjectStore()
    app.state.broker = QueueBroker()

    settings = get_settings()
    stop = threading.Event()
    reaper = threading.Thread(
        target=_reaper_loop, args=(stop, settings), name="reaper", daemon=True
    )
    reaper.start()
    try:
        yield
    finally:
        # Daemon, so shutdown does not depend on it — but signalling lets an
        # in-flight wait return promptly instead of holding a sweep interval.
        stop.set()


app = FastAPI(title="DocFactory API", lifespan=lifespan)

# Endpoints reachable without a tenant: liveness, the OpenAPI surface, and the
# storage-event bridge — whose caller is the object store, which holds no API
# key and no tenant. It carries its own shared secret instead, and the tenant
# comes from the object key, resolved in the worker.
_UNAUTHENTICATED_PATHS = frozenset(
    {
        "/healthz",
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/internal/storage-events",
    }
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


class DocumentListEntry(BaseModel):
    """One row of a document listing.

    Deliberately not DocumentView: that carries the latest extraction, which
    would mean a join per row for a field nobody reconciling a list needs.
    Fetch the detail endpoint for the ones that matter.
    """

    document_id: uuid.UUID
    doc_type: str
    status: str
    text_chars: int | None
    last_error: str | None
    received_at: datetime


class DocumentPage(BaseModel):
    documents: list[DocumentListEntry]
    # Total matching the filter, not the page — the number a client is
    # reconciling against.
    total: int
    limit: int
    offset: int


class CorrectionRequest(BaseModel):
    """Field values a client says are wrong, and what they should be."""

    corrections: dict[str, str]


class CorrectionResponse(BaseModel):
    document_id: uuid.UUID
    corrected_fields: int


class UsageRollup(BaseModel):
    """What a document type costs to process, from recorded usage."""

    pipeline_slug: str
    documents: int
    calls: int
    cost_usd: float
    cost_per_document_usd: float
    escalation_rate: float
    cost_by_tier_usd: dict[str, float]


class DriftView(BaseModel):
    """Drift state for one document type.

    Observable, not alerted: this phase makes drift something an operator can
    read, and stops short of paging them. Alerting belongs with the SLA
    burn-rate surface, which waits for the deployed environment.
    """

    doc_type: str
    # "baseline" (still learning — makes no claims), "stable", "drifting"
    status: str
    n_observed: int
    baseline_n: int
    flagged_signals: list[str]
    first_flagged_at: datetime | None
    signals: dict[str, dict]


class SpendSummary(BaseModel):
    tenant_id: str
    spent_usd: float
    budget_usd: float
    remaining_usd: float
    exceeded: bool
    unit_costs: list[UsageRollup]
    # Operational signals: what the pipeline is carrying right now. Queue depth
    # is the metric worker autoscaling will target on AWS.
    in_flight: int
    queue_depth: dict[str, int]
    # Per document type. Empty until a tenant has processed anything.
    drift: list[DriftView]
    # Every status this tenant holds, and how many.
    #
    # `needs_ocr` is why this exists. It is terminal and produces nothing — a
    # scanned PDF is accepted with a 202 and then stops, with no extraction, no
    # review task, no DLQ message and no alarm. That is correct behaviour, and
    # it was also completely invisible: a client could only discover it by
    # reconciling their own totals and finding documents that never came back.
    # Roughly a quarter of the synthetic corpus lands there.
    status_counts: dict[str, int]


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

    # Backpressure: over the tenant's rate is a clean 429 with a Retry-After,
    # never a silent drop. The bucket is shared across API processes, so the
    # limit belongs to the tenant rather than to whichever replica answered.
    decision = check_rate_limit(tenant_id)
    if not decision.allowed:
        raise HTTPException(
            status_code=429,
            detail=(
                f"rate limit exceeded for tenant {tenant_id}; "
                f"retry in {decision.retry_after_seconds:.1f}s"
            ),
            headers={"Retry-After": str(max(int(decision.retry_after_seconds), 1))},
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


@app.post("/internal/storage-events", status_code=202, include_in_schema=False)
async def storage_events(request: Request) -> dict:
    """Local bridge: MinIO bucket notifications onto the ingest queue.

    MinIO cannot publish to ElasticMQ, so locally it posts the S3-shaped event
    here and this republishes it unchanged. On AWS, S3 publishes to SQS
    directly and this endpoint is not deployed — the event body, the queue and
    the worker handler are identical either way, which is the point.

    Authenticated by a shared secret rather than an API key: the caller is the
    object store, which has no tenant of its own. The tenant is derived from
    the object key, inside the worker.
    """
    settings = get_settings()
    token = request.headers.get("authorization", "")
    expected = f"Bearer {settings.ingest_webhook_token}"
    if not settings.ingest_webhook_token or token != expected:
        raise HTTPException(status_code=401, detail="invalid storage-event token")

    payload = await request.json()
    records = payload.get("Records", [])
    if not records:
        return {"accepted": 0}
    app.state.broker.send(settings.ingest_queue, payload)
    return {"accepted": len(records)}


@app.get("/usage", response_model=SpendSummary)
def get_usage() -> SpendSummary:
    """This tenant's spend and its unit cost per document type.

    Both come from recorded usage: the counter the budget cap is enforced
    against, and the per-call `usage_events` rows behind it. RLS scopes every
    row to the calling tenant, so there is no tenant filter in the queries.
    """
    tenant_id = current_tenant.get()
    state = budget_state(tenant_id)
    return SpendSummary(
        tenant_id=tenant_id,
        spent_usd=float(state.spent_usd),
        budget_usd=float(state.budget_usd),
        remaining_usd=float(state.remaining_usd),
        exceeded=state.exceeded,
        in_flight=in_flight(tenant_id),
        status_counts=status_counts(tenant_id),
        queue_depth={
            queue: broker_queue_depth(app.state.broker, queue)
            for queue in (
                get_settings().ingest_queue,
                get_settings().parse_queue,
                get_settings().extract_queue,
            )
        },
        drift=[
            DriftView(
                doc_type=row.doc_type,
                status=row.status,
                n_observed=row.n_observed,
                baseline_n=row.baseline_n,
                flagged_signals=list(row.flagged_signals),
                first_flagged_at=row.first_flagged_at,
                signals=row.signals,
            )
            for row in drift_status(tenant_id)
        ],
        unit_costs=[
            UsageRollup(
                pipeline_slug=row.pipeline_slug,
                documents=row.documents,
                calls=row.calls,
                cost_usd=float(row.cost_usd),
                cost_per_document_usd=float(row.cost_per_document),
                escalation_rate=round(row.escalation_rate, 4),
                cost_by_tier_usd={tier: float(cost) for tier, cost in row.by_tier.items()},
            )
            for row in unit_costs(tenant_id)
        ],
    )


@app.get("/documents", response_model=DocumentPage)
def list_documents(
    status: str | None = None,
    doc_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DocumentPage:
    """List this tenant's documents, newest first.

    THE GAP THIS CLOSES. There was no way to list documents at all — only
    `GET /documents/{id}`, which needs an id the client has to have kept. So a
    document that finished in a state producing no output, `needs_ocr` above
    all, was undiscoverable except by reconciling totals: you know you sent
    500, you can count 497 that came back, and nothing tells you which three
    did not or why.

    `?status=needs_ocr` answers that directly, and the status_counts field on
    /usage says how big the number is before you go looking.

    RLS scopes the query, so this cannot return another tenant's rows even if
    the filters were wrong.
    """
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must not be negative")

    valid = {item.value for item in DocumentStatus}
    if status is not None and status not in valid:
        # Naming the valid set matters here: the states are the product's
        # vocabulary, and a client filtering for a state that cannot exist
        # would otherwise read an empty page as "nothing is stuck".
        raise HTTPException(
            status_code=422,
            detail=f"unknown status {status!r}; valid statuses are {sorted(valid)}",
        )

    with session_scope() as session:
        conditions = []
        if status is not None:
            conditions.append(Document.status == status)
        if doc_type is not None:
            conditions.append(Document.doc_type == doc_type)

        total = int(
            session.scalar(select(func.count()).select_from(Document).where(*conditions)) or 0
        )
        rows = list(
            session.scalars(
                select(Document)
                .where(*conditions)
                .order_by(Document.received_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
        return DocumentPage(
            documents=[
                DocumentListEntry(
                    document_id=row.id,
                    doc_type=row.doc_type,
                    status=row.status,
                    text_chars=row.text_chars,
                    last_error=row.last_error,
                    received_at=row.received_at,
                )
                for row in rows
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


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


@app.post("/documents/{document_id}/corrections", response_model=CorrectionResponse)
def correct_document(document_id: uuid.UUID, request: CorrectionRequest) -> CorrectionResponse:
    """Correct an extraction the client says is wrong.

    THE GAP. POST /review/tasks/{id}/resolve was the only write path in the
    API, and it needs a ReviewTask. A document that auto-approved never had
    one — by definition, because the confidence model was sure about it. So the
    errors a client actually notices, the ones that came back clean and wrong,
    were exactly the errors they had no way to report: the stored extraction
    stayed wrong and the API kept serving it.

    It compounds, too. Every eval_case in the corpus came from a resolved
    review task, so the feedback loop learned only from errors the confidence
    model had already caught. The errors it is blind to could not enter the
    training data by construction.

    RLS scopes the lookup, so another tenant's document is a 404 rather than a
    403 — the same reasoning as GET /documents/{id}.
    """
    try:
        corrected = correct_extraction(document_id, corrections=request.corrections)
    except ValueError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message) from exc
        # A field that is not part of this extraction, an empty correction, or
        # a document with nothing to correct (needs_ocr, failed) are all the
        # caller asking for something that cannot be done, not a server fault.
        raise HTTPException(status_code=422, detail=message) from exc
    return CorrectionResponse(document_id=document_id, corrected_fields=corrected)


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
