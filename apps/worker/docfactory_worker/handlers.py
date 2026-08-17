"""Pipeline stage handlers: parse and extract.

Both are idempotent under at-least-once redelivery by coordinating through the
document row's status inside a transaction (SELECT ... FOR UPDATE), never
through message ordering:

- a redelivery of an already-completed stage sees the advanced status and
  acknowledges without re-doing work;
- a redelivery after a crash mid-stage sees the in-progress status and
  reprocesses (all stage writes are re-runnable: same content to the same
  object key, extraction rows are append-only history).

Scanned PDFs (below the text threshold) transition to needs_ocr — a terminal,
*expected* outcome, not a failure and never a DLQ trip (R1). Corrupt PDFs
raise; the consumer leaves the message and the queue's redrive policy moves it
to the DLQ after max_receive_count receives, with the error captured on the
document row.
"""

import logging
import uuid
from datetime import UTC, datetime

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.extraction import ExtractionOutcome, run_extraction
from docfactory_core.llm import get_llm_client
from docfactory_core.models import Document, DocumentStatus, Extraction, ExtractionField
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.queues import QueueBroker
from docfactory_core.schemas import SCALAR_FIELD_NAMES, Invoice
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import extract_trace_context, inject_trace_context
from docfactory_core.validation import validate_invoice
from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer("docfactory")

_store: ObjectStore | None = None
_broker: QueueBroker | None = None


def _clients() -> tuple[ObjectStore, QueueBroker]:
    global _store, _broker
    if _store is None:
        _store = ObjectStore()
        _broker = QueueBroker()
    return _store, _broker


def handle_parse(payload: dict, *, receive_count: int = 1, final_attempt: bool = False) -> None:
    document_id = uuid.UUID(payload["document_id"])
    store, broker = _clients()
    settings = get_settings()

    with tracer.start_as_current_span(
        "document.parse", context=extract_trace_context(payload)
    ) as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("document_id", str(document_id))
        span.set_attribute("retry.receive_count", receive_count)

        with session_scope() as session:
            document = session.get(Document, document_id, with_for_update=True)
            if document is None:
                raise RuntimeError(f"document {document_id} not found")
            if document.status not in (DocumentStatus.RECEIVED, DocumentStatus.PARSING):
                span.set_attribute("outcome", f"skip:{document.status}")
                log.info("skipping duplicate delivery", extra={"status": document.status})
                return
            document.status = DocumentStatus.PARSING
            tenant_id, s3_key = document.tenant_id, document.s3_key

        try:
            text = extract_pdf_text(store.get_object(s3_key))
        except Exception as exc:
            _record_failure(document_id, f"parse: {exc}", final_attempt)
            span.set_attribute("outcome", "error")
            raise

        span.set_attribute("document.text_chars", len(text))
        if len(text) < settings.min_parse_chars:
            # Expected terminal state for image-only PDFs until the OCR tier
            # exists — not a failure, never the DLQ (R1).
            with session_scope() as session:
                document = session.get(Document, document_id, with_for_update=True)
                document.status = DocumentStatus.NEEDS_OCR
                document.text_chars = len(text)
            span.set_attribute("outcome", "needs_ocr")
            log.info("no extractable text; routed to needs_ocr", extra={"chars": len(text)})
            return

        text_key = f"{tenant_id}/parsed/{document_id}.txt"
        store.put_object(text_key, text.encode(), content_type="text/plain; charset=utf-8")
        with session_scope() as session:
            document = session.get(Document, document_id, with_for_update=True)
            document.status = DocumentStatus.PARSED
            document.text_s3_key = text_key
            document.text_chars = len(text)
            document.parsed_at = datetime.now(UTC)
        broker.send(settings.extract_queue, inject_trace_context({"document_id": str(document_id)}))
        span.set_attribute("outcome", "parsed")
        log.info("parsed", extra={"chars": len(text)})


def handle_extract(payload: dict, *, receive_count: int = 1, final_attempt: bool = False) -> None:
    document_id = uuid.UUID(payload["document_id"])
    store, _ = _clients()

    with tracer.start_as_current_span(
        "document.extract", context=extract_trace_context(payload)
    ) as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        span.set_attribute("document_id", str(document_id))
        span.set_attribute("retry.receive_count", receive_count)

        with session_scope() as session:
            document = session.get(Document, document_id, with_for_update=True)
            if document is None:
                raise RuntimeError(f"document {document_id} not found")
            if document.status not in (DocumentStatus.PARSED, DocumentStatus.EXTRACTING):
                span.set_attribute("outcome", f"skip:{document.status}")
                log.info("skipping duplicate delivery", extra={"status": document.status})
                return
            document.status = DocumentStatus.EXTRACTING
            tenant_id, text_key = document.tenant_id, document.text_s3_key

        try:
            text = store.get_object(text_key).decode("utf-8")
            outcome = run_extraction(text, get_llm_client())
        except Exception as exc:
            _record_failure(document_id, f"extract: {exc}", final_attempt)
            span.set_attribute("outcome", "error")
            raise

        if outcome.invoice is None:
            # Semantic failure after the schema retry: store the raw attempt
            # for debugging, mark failed, acknowledge (an identical re-run
            # would fail the same way — this is not a transport error).
            _store_extraction(document_id, tenant_id, outcome, invoice=None, validation=None)
            _record_failure(document_id, f"extraction invalid: {outcome.error}", final=True)
            span.set_attribute("outcome", "invalid_extraction")
            return

        with tracer.start_as_current_span("extraction.validate") as validate_span:
            validate_span.set_attribute("openinference.span.kind", "CHAIN")
            validation = validate_invoice(outcome.invoice)
            for rule, passed in validation.items():
                validate_span.set_attribute(f"validation.{rule}", passed)

        _store_extraction(document_id, tenant_id, outcome, outcome.invoice, validation)
        span.set_attribute("outcome", "extracted")
        span.set_attribute("validation.passed", all(validation.values()))
        log.info(
            "extracted",
            extra={
                "model": outcome.model,
                "attempts": outcome.attempts,
                "validation_passed": all(validation.values()),
            },
        )


def _store_extraction(
    document_id: uuid.UUID,
    tenant_id: str,
    outcome: ExtractionOutcome,
    invoice: Invoice | None,
    validation: dict[str, bool] | None,
) -> None:
    with session_scope() as session:
        extraction = Extraction(
            document_id=document_id,
            tenant_id=tenant_id,
            model=outcome.model,
            output=invoice.model_dump(mode="json")
            if invoice
            else {"raw": outcome.raw_output, "error": outcome.error},
            validation=validation,
            validation_passed=all(validation.values()) if validation else False,
            prompt_tokens=outcome.input_tokens,
            completion_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
        )
        session.add(extraction)
        session.flush()
        if invoice is not None:
            for name, value in _flatten_fields(invoice).items():
                session.add(
                    ExtractionField(
                        extraction_id=extraction.id, tenant_id=tenant_id, name=name, value=value
                    )
                )
            document = session.get(Document, document_id, with_for_update=True)
            document.status = DocumentStatus.EXTRACTED
            document.extracted_at = datetime.now(UTC)


def _flatten_fields(invoice: Invoice) -> dict[str, str]:
    dumped = invoice.model_dump(mode="json")
    fields = {key: str(dumped[key]) for key in SCALAR_FIELD_NAMES}
    fields["line_items.count"] = str(len(dumped["line_items"]))
    for index, item in enumerate(dumped["line_items"]):
        for key, value in item.items():
            fields[f"line_items.{index}.{key}"] = str(value)
    return fields


def _record_failure(document_id: uuid.UUID, error: str, final: bool) -> None:
    """Best-effort error capture; sets FAILED only on the final attempt."""
    try:
        with session_scope() as session:
            document = session.get(Document, document_id, with_for_update=True)
            if document is None:
                return
            document.last_error = error[:2000]
            if final:
                document.status = DocumentStatus.FAILED
    except Exception:
        log.exception("could not record failure on document row")
