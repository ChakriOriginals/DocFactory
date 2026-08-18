"""Batch ingestion from object-storage events.

A document arrives one of two ways: pushed to the API, or dropped into the
tenant's storage prefix. The second path is what a customer with a nightly
export actually wants, and it is the shape AWS gives you for free — S3 emits an
event, SQS carries it, a worker picks it up.

The two paths converge immediately: this module turns an event into the same
`documents` row the upload endpoint creates, enqueues the same parse stage, and
dedupes on the same (tenant, sha256) constraint. The same file arriving by both
routes is one document, not two.

Locally the event comes from MinIO's bucket notifications, which emit the S3
event schema — so the parsing here, and the worker handler that calls it, are
the code that runs on AWS unchanged. Only the transport differs: MinIO posts to
a webhook that republishes onto the ingest queue, where S3 publishes to SQS
directly.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import unquote_plus

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import Document, DocumentStatus
from docfactory_core.pipeline_registry import available_slugs
from docfactory_core.tracing import inject_trace_context

log = logging.getLogger(__name__)

_PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    size: int | None = None


@dataclass(frozen=True)
class IngestResult:
    document_id: uuid.UUID | None
    tenant_id: str | None
    duplicate: bool = False
    skipped: str | None = None


def parse_s3_events(payload: dict) -> list[ObjectRef]:
    """Object references from an S3-shaped notification.

    Only object-created events are acted on; deletes and lifecycle events are
    ignored rather than mistaken for new work. Keys are URL-decoded because
    both S3 and MinIO percent-encode them.

    THE `s3:` PREFIX IS NOT PART OF THE EVENT. It is part of the notification
    *configuration* ("s3:ObjectCreated:*"), and the two are easy to conflate.
    MinIO puts it on the record as well and emits "s3:ObjectCreated:Put"; real
    S3 delivering to SQS emits "ObjectCreated:Put". Matching only MinIO's form
    is silent, total failure of the batch path in the cloud: the worker
    receives the message, parses zero references, acknowledges it, and the
    dropped document is never seen again. Found by applying the data plane to
    LocalStack and dropping a real object through it (Phase 4c.5b) — never by a
    local run, because locally MinIO is the only producer.
    """
    refs: list[ObjectRef] = []
    for record in payload.get("Records", []):
        event_name = str(record.get("eventName", "")).removeprefix("s3:")
        if not event_name.startswith("ObjectCreated"):
            continue
        s3 = record.get("s3", {})
        bucket = s3.get("bucket", {}).get("name")
        obj = s3.get("object", {})
        key = obj.get("key")
        if not bucket or not key:
            continue
        refs.append(ObjectRef(bucket=bucket, key=unquote_plus(key), size=obj.get("size")))
    return refs


def route_key(key: str) -> tuple[str | None, str | None]:
    """(tenant, doc_type) implied by an object key, or (None, None) if it is not ours.

    Layout: `{tenant}/{ingest_prefix}/{doc_type}/{filename}`, with the document
    type optional — omitted means the tenant's default. The prefix is what
    keeps ingestion from re-ingesting the pipeline's own artifacts: parsed text
    and uploaded originals live under different prefixes in the same bucket.
    """
    settings = get_settings()
    parts = key.split("/")
    if len(parts) < 3 or parts[1] != settings.ingest_prefix:
        return None, None
    tenant = parts[0]
    doc_type = parts[2] if len(parts) > 3 and parts[2] in available_slugs() else None
    return tenant, doc_type


def ingest_object(ref: ObjectRef, store, broker) -> IngestResult:
    """Turn a dropped object into a document and start the pipeline.

    Idempotent by construction: the same bytes for the same tenant hit the
    (tenant_id, sha256) unique constraint and return the existing document,
    exactly as a duplicate upload does. A storage event delivered twice — which
    S3 explicitly allows — therefore costs one row, not two.
    """
    tenant_id, doc_type = route_key(ref.key)
    if tenant_id is None:
        return IngestResult(None, None, skipped="not-an-ingest-key")

    body = store.get_object(ref.key)
    if not body.startswith(_PDF_MAGIC):
        log.warning("ignoring non-PDF drop", extra={"key": ref.key})
        return IngestResult(None, tenant_id, skipped="not-a-pdf")

    sha256 = hashlib.sha256(body).hexdigest()
    settings = get_settings()

    with session_scope(tenant_id) as session:
        existing = session.scalar(select(Document).where(Document.sha256 == sha256))
        if existing is not None:
            return IngestResult(existing.id, tenant_id, duplicate=True)

    document = Document(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        s3_key=ref.key,
        sha256=sha256,
        doc_type=doc_type or "invoice",
        status=DocumentStatus.RECEIVED,
    )
    try:
        with session_scope(tenant_id) as session:
            session.add(document)
    except IntegrityError:
        # Lost a race with a concurrent delivery of the same object.
        with session_scope(tenant_id) as session:
            existing = session.scalar(select(Document).where(Document.sha256 == sha256))
        return IngestResult(existing.id if existing else None, tenant_id, duplicate=True)

    broker.send(
        settings.parse_queue,
        inject_trace_context({"document_id": str(document.id), "tenant_id": tenant_id}),
    )
    log.info(
        "ingested from storage event",
        extra={"key": ref.key, "tenant_id": tenant_id, "doc_type": document.doc_type},
    )
    return IngestResult(document.id, tenant_id)
