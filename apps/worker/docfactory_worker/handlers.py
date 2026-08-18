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

import functools
import logging
import uuid
from datetime import UTC, datetime

from docfactory_core.budget import call_cost_usd, check_budget
from docfactory_core.confidence import ConfidenceReport, failed_extraction_report, score_extraction
from docfactory_core.confidence_model import (
    RoutingDecision,
    get_confidence_model,
    route_extraction,
)
from docfactory_core.config import get_settings
from docfactory_core.db import current_tenant, session_scope, tenant_context
from docfactory_core.extraction import ExtractionOutcome, run_extraction
from docfactory_core.llm import get_llm_client
from docfactory_core.models import Document, DocumentStatus, Extraction, ExtractionField
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline import (
    PipelineConfigError,
    PipelineDefinition,
    evaluate_rules,
    json_record,
)
from docfactory_core.pipeline_registry import default_pipeline, load_from_file
from docfactory_core.queues import QueueBroker
from docfactory_core.review import ensure_review_task
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import extract_trace_context, inject_trace_context
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


def tenant_scoped(handler):
    """Bind the message's tenant for the whole handler.

    Every read the handler makes passes through RLS, so the tenant has to be
    known before the first query — which is why the message carries it rather
    than the handler discovering it by reading the document.
    """

    @functools.wraps(handler)
    def wrapper(payload: dict, **kwargs):
        tenant_id = payload.get("tenant_id") or get_settings().default_tenant_id
        with tenant_context(tenant_id):
            return handler(payload, **kwargs)

    return wrapper


@tenant_scoped
def handle_parse(payload: dict, *, receive_count: int = 1, final_attempt: bool = False) -> None:
    document_id = uuid.UUID(payload["document_id"])
    tenant_id = current_tenant.get()
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
        broker.send(
            settings.extract_queue,
            inject_trace_context({"document_id": str(document_id), "tenant_id": tenant_id}),
        )
        span.set_attribute("outcome", "parsed")
        log.info("parsed", extra={"chars": len(text)})


@tenant_scoped
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
            if document.status not in (
                DocumentStatus.PARSED,
                DocumentStatus.EXTRACTING,
                # A document paused on budget is resumable once the cap rises.
                DocumentStatus.BUDGET_EXCEEDED,
            ):
                span.set_attribute("outcome", f"skip:{document.status}")
                log.info("skipping duplicate delivery", extra={"status": document.status})
                return
            document.status = DocumentStatus.EXTRACTING
            tenant_id, text_key = document.tenant_id, document.text_s3_key
            doc_type = document.doc_type

        # The pipeline definition decides the schema, the prompt, the rules,
        # and which signals apply to which field. It is reached from the
        # document's own type — pipelines are tenant-owned, and the tenant is
        # already bound for this handler.
        definition = _definition_for(doc_type)
        span.set_attribute("pipeline.slug", definition.slug)

        # Budget gate before the model call: the point is not to spend the
        # money, so checking afterwards would be too late.
        budget = check_budget(tenant_id)
        if budget.exceeded:
            with session_scope() as session:
                document = session.get(Document, document_id, with_for_update=True)
                document.status = DocumentStatus.BUDGET_EXCEEDED
                document.last_error = (
                    f"tenant budget exhausted: spent {budget.spent_usd} of {budget.budget_usd} USD"
                )
            span.set_attribute("outcome", "budget_exceeded")
            log.warning(
                "paused: tenant over budget",
                extra={"spent_usd": str(budget.spent_usd), "budget_usd": str(budget.budget_usd)},
            )
            return

        try:
            text = store.get_object(text_key).decode("utf-8")
            outcome = run_extraction(text, get_llm_client(), definition)
        except Exception as exc:
            _record_failure(document_id, f"extract: {exc}", final_attempt)
            span.set_attribute("outcome", "error")
            raise

        if outcome.record is None:
            # Semantic failure after the schema retry: store the raw attempt
            # for debugging, mark failed, acknowledge (an identical re-run
            # would fail the same way — this is not a transport error).
            _store_extraction(
                document_id,
                tenant_id,
                outcome,
                record=None,
                validation=None,
                confidence=failed_extraction_report(outcome.error),
                definition=definition,
            )
            _record_failure(document_id, f"extraction invalid: {outcome.error}", final=True)
            span.set_attribute("outcome", "invalid_extraction")
            return

        with tracer.start_as_current_span("extraction.validate") as validate_span:
            validate_span.set_attribute("openinference.span.kind", "CHAIN")
            validation = evaluate_rules(outcome.record, definition)
            for rule, passed in validation.items():
                validate_span.set_attribute(f"validation.{rule}", passed)

        with tracer.start_as_current_span("extraction.score") as score_span:
            score_span.set_attribute("openinference.span.kind", "CHAIN")
            confidence = score_extraction(
                outcome.record,
                validation,
                attempts=outcome.attempts,
                source_text=text,
                definition=definition,
            )
            score_span.set_attribute("confidence.doc", confidence.doc_confidence)
            score_span.set_attribute("confidence.reasons", list(confidence.reasons))
            # The weakest fields are what a reviewer would open first.
            for name, entry in sorted(confidence.fields.items(), key=lambda kv: kv[1].confidence)[
                :3
            ]:
                score_span.set_attribute(f"confidence.field.{name}", entry.confidence)

        # Routing consumes the calibrated model; the threshold comes from the
        # config file, never from code (2.3b).
        with tracer.start_as_current_span("extraction.route") as route_span:
            route_span.set_attribute("openinference.span.kind", "CHAIN")
            routing = route_extraction(confidence.signals, get_confidence_model(), definition)
            route_span.set_attribute("routing.decision", routing.decision)
            route_span.set_attribute("routing.threshold", routing.threshold)
            route_span.set_attribute("routing.model_version", routing.model_version)
            route_span.set_attribute("routing.flagged_fields", list(routing.flagged_fields))

        _store_extraction(
            document_id,
            tenant_id,
            outcome,
            outcome.record,
            validation,
            confidence,
            routing,
            definition,
        )
        span.set_attribute("outcome", routing.decision)
        span.set_attribute("validation.passed", all(validation.values()))
        span.set_attribute("confidence.doc", routing.doc_confidence)
        log.info(
            "extracted",
            extra={
                "model": outcome.model,
                "attempts": outcome.attempts,
                "validation_passed": all(validation.values()),
                "doc_confidence": routing.doc_confidence,
                "routing_decision": routing.decision,
                "flagged_fields": list(routing.flagged_fields),
                "confidence_model_version": routing.model_version,
            },
        )


def _definition_for(doc_type: str | None) -> PipelineDefinition:
    """The pipeline for a document's declared type, or the tenant's default.

    The type was validated against the available definitions when the document
    was accepted; a row that predates a definition being retired still resolves
    to something runnable rather than wedging the queue.
    """
    if not doc_type:
        return default_pipeline()
    try:
        return load_from_file(doc_type)
    except PipelineConfigError:
        log.warning("no pipeline for doc_type; using default", extra={"doc_type": doc_type})
        return default_pipeline()


def _store_extraction(
    document_id: uuid.UUID,
    tenant_id: str,
    outcome: ExtractionOutcome,
    record: dict | None,
    validation: dict[str, bool] | None,
    confidence: ConfidenceReport,
    routing: RoutingDecision | None = None,
    definition: PipelineDefinition | None = None,
) -> None:
    definition = definition or default_pipeline()
    queued_for_review: uuid.UUID | None = None
    flagged: tuple[str, ...] = ()
    with session_scope() as session:
        extraction = Extraction(
            document_id=document_id,
            tenant_id=tenant_id,
            model=outcome.model,
            output=json_record(record)
            if record is not None
            else {"raw": outcome.raw_output, "error": outcome.error},
            validation=validation,
            validation_passed=all(validation.values()) if validation else False,
            doc_confidence=routing.doc_confidence if routing else confidence.doc_confidence,
            routing_decision=routing.decision if routing else None,
            confidence_model_version=routing.model_version if routing else None,
            pipeline_slug=definition.slug,
            pipeline_version=definition.version,
            confidence_signals=confidence.signals,
            prompt_tokens=outcome.input_tokens,
            completion_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
            cost_usd=call_cost_usd(),
        )
        session.add(extraction)
        session.flush()
        if record is not None:
            for name, value in _flatten_fields(record, definition).items():
                session.add(
                    ExtractionField(
                        extraction_id=extraction.id,
                        tenant_id=tenant_id,
                        name=name,
                        value=value,
                        confidence=_field_score(name, routing, confidence, definition),
                    )
                )
            document = session.get(Document, document_id, with_for_update=True)
            # Routing decides the terminal state; EXTRACTED remains the
            # pre-routing state for callers that score without routing.
            document.status = (
                DocumentStatus(routing.decision) if routing else DocumentStatus.EXTRACTED
            )
            document.extracted_at = datetime.now(UTC)
            queued_for_review = (
                extraction.id if routing and routing.decision == "needs_review" else None
            )
            flagged = routing.flagged_fields if routing else ()

    # Outside the write transaction: task creation is itself idempotent, so a
    # crash between the two leaves the document routed and simply re-queues on
    # the next delivery rather than double-queueing human work.
    if queued_for_review is not None:
        ensure_review_task(queued_for_review, flagged_fields=flagged)


def _flatten_fields(record: dict, definition: PipelineDefinition) -> dict[str, str]:
    """One stored row per cell: scalars by name, table cells by path.

    Which fields exist, and which of them are tables, comes from the
    definition — the report order is the order the definition declares.
    """
    dumped = json_record(record)
    fields = {name: str(dumped.get(name)) for name in definition.scalar_fields}
    for table in definition.table_fields:
        rows = dumped.get(table) or []
        fields[f"{table}.count"] = str(len(rows))
        for index, row in enumerate(rows):
            for key, value in row.items():
                fields[f"{table}.{index}.{key}"] = str(value)
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


def _field_score(
    name: str,
    routing: RoutingDecision | None,
    confidence: ConfidenceReport,
    definition: PipelineDefinition,
):
    """Calibrated probability when routing ran, else the uncalibrated prior.

    Table cells inherit their parent's score, as they do in 2.1.
    """
    if routing is None:
        return confidence.field_confidence(name)
    if name in routing.field_confidence:
        return routing.field_confidence[name]
    table = name.split(".", 1)[0]
    if table in definition.table_fields:
        return routing.field_confidence.get(table)
    return None
