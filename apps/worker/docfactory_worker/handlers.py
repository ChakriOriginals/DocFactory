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
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from docfactory_core.backpressure import admits, in_flight
from docfactory_core.budget import (
    Reservation,
    budget_state,
    release,
    reserve,
    settle,
    sync_tenant_status,
)
from docfactory_core.confidence import ConfidenceReport, failed_extraction_report, score_extraction
from docfactory_core.confidence_model import (
    RoutingDecision,
    confidence_model_for,
    route_extraction,
)
from docfactory_core.config import get_settings
from docfactory_core.db import current_tenant, session_scope, tenant_context
from docfactory_core.drift import observe as observe_drift
from docfactory_core.extraction import (
    ExtractionOutcome,
    build_system_prompt,
    build_user_message,
    run_extraction,
)
from docfactory_core.ingest import ingest_object, parse_s3_events, route_key
from docfactory_core.llm import get_llm_client
from docfactory_core.metering import PURPOSE_ESCALATION, PURPOSE_EXTRACT, record_usage
from docfactory_core.models import Document, DocumentStatus, Extraction, ExtractionField
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline import (
    PipelineConfigError,
    PipelineDefinition,
    evaluate_rules,
    json_record,
)
from docfactory_core.pipeline_registry import default_pipeline, load_from_file
from docfactory_core.pricing import call_cost_usd, estimate_input_tokens, worst_case_cost_usd
from docfactory_core.queues import QueueBroker
from docfactory_core.resilience import RetryPolicy, get_breaker, retry_transient
from docfactory_core.review import ensure_review_task
from docfactory_core.routing import default_policy, escalation_trigger, model_for_tier
from docfactory_core.storage import ObjectStore
from docfactory_core.tracing import extract_trace_context, inject_trace_context
from opentelemetry import trace

log = logging.getLogger(__name__)
tracer = trace.get_tracer("docfactory")

_store: ObjectStore | None = None
_broker: QueueBroker | None = None


def _clients() -> tuple[ObjectStore, QueueBroker]:
    """Lazily build the AWS clients, all-or-nothing.

    The guard used to be `if _store is None`, which tested one global while
    assigning two. If QueueBroker() raised after ObjectStore() had succeeded —
    an SQS blip, a slow credential refresh, anything transient at startup —
    `_store` stayed set and `_broker` stayed None. Every later call then saw a
    non-None `_store`, skipped initialisation entirely, and returned a broker
    of None. The transient failure became permanent, for the life of the
    process.

    The shape it produced was nastier than a crash. handle_parse extracts the
    text, writes it to S3, commits status=parsed, and only then calls
    broker.send — so the work was done and persisted before the AttributeError.
    On redelivery the status guard sees `parsed`, logs "skipping duplicate
    delivery" and returns cleanly, so the message is deleted and never reaches
    a DLQ. The document sits at `parsed` forever, with nothing anywhere
    reporting an error. Observed in local testing, not theorised:

      AttributeError: 'NoneType' object has no attribute 'send'
      ... then: "skipping duplicate delivery", status=parsed

    Assigning through locals means a half-built pair is never published, so the
    next message retries construction instead of inheriting the failure.
    """
    global _store, _broker
    if _store is None or _broker is None:
        store = ObjectStore()
        broker = QueueBroker()
        _store, _broker = store, broker
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


def handle_ingest(payload: dict, *, receive_count: int = 1, final_attempt: bool = False) -> None:
    """A document dropped into a tenant's storage prefix.

    The message is an S3-shaped object-created event, so this handler is the
    same code on AWS (S3 -> SQS) as it is locally (MinIO -> webhook bridge ->
    queue). It converges on the API upload path immediately: same row, same
    parse stage, same (tenant, sha256) dedupe.
    """
    store, broker = _clients()
    with tracer.start_as_current_span(
        "document.ingest", context=extract_trace_context(payload)
    ) as span:
        span.set_attribute("openinference.span.kind", "CHAIN")
        refs = parse_s3_events(payload)
        span.set_attribute("ingest.records", len(refs))
        for ref in refs:
            tenant_id, _ = route_key(ref.key)
            if tenant_id is None:
                span.set_attribute("ingest.skipped", "not-an-ingest-key")
                continue
            with tenant_context(tenant_id):
                result = ingest_object(ref, store, broker)
            span.set_attribute("tenant_id", tenant_id)
            span.set_attribute("ingest.duplicate", result.duplicate)
            if result.skipped:
                span.set_attribute("ingest.skipped", result.skipped)


@tenant_scoped
def handle_parse(payload: dict, *, receive_count: int = 1, final_attempt: bool = False) -> None:
    document_id = uuid.UUID(payload["document_id"])
    tenant_id = current_tenant.get()
    store, broker = _clients()
    settings = get_settings()

    if _defer_if_saturated(payload, settings.parse_queue, tenant_id, document_id):
        return

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

    if _defer_if_provider_down(payload, get_settings().extract_queue, document_id):
        return

    if _defer_if_saturated(
        payload, get_settings().extract_queue, current_tenant.get(), document_id
    ):
        return

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

        policy = definition.model_routing or default_policy()
        provider = get_settings().model_provider
        span.set_attribute("routing.primary_tier", policy.primary_tier)

        try:
            text = store.get_object(text_key).decode("utf-8")
        except Exception as exc:
            _record_failure(document_id, f"extract: {exc}", final_attempt)
            span.set_attribute("outcome", "error")
            raise

        # First attempt, on the tier the pipeline nominates. The budget is
        # reserved before the call and settled after — see budget.py for why
        # the check and the charge have to be one statement.
        try:
            call = _metered_extraction(
                text=text,
                definition=definition,
                tenant_id=tenant_id,
                document_id=document_id,
                provider=provider,
                tier=policy.primary_tier,
                purpose=PURPOSE_EXTRACT,
            )
        except Exception as exc:
            _record_failure(document_id, f"extract: {exc}", final_attempt)
            span.set_attribute("outcome", "error")
            raise

        if call is None:
            _pause_on_budget(document_id, tenant_id, span)
            return

        outcome, spend = call.outcome, call.cost_usd
        validation, confidence, routing = _assess(outcome, text, definition, span)

        # Escalation: re-run on a stronger model, but only when the
        # deterministic checks say the cheap answer is suspect.
        trigger = escalation_trigger(
            policy,
            extraction_failed=outcome.record is None,
            validation_passed=all(validation.values()) if validation else None,
            routing_decision=routing.decision if routing else None,
        )
        if trigger:
            span.set_attribute("routing.escalation_trigger", trigger)
            escalated = _metered_extraction(
                text=text,
                definition=definition,
                tenant_id=tenant_id,
                document_id=document_id,
                provider=provider,
                tier=policy.escalate_to,
                purpose=PURPOSE_ESCALATION,
            )
            if escalated is None:
                # Out of budget for the second call: keep the first answer
                # rather than losing the work already paid for.
                log.warning("escalation skipped: tenant over budget", extra={"trigger": trigger})
                span.set_attribute("routing.escalation", "skipped:budget")
            else:
                spend += escalated.cost_usd
                if escalated.outcome.record is not None:
                    outcome = escalated.outcome
                    validation, confidence, routing = _assess(outcome, text, definition, span)
                    span.set_attribute("routing.escalation", "accepted")
                else:
                    span.set_attribute("routing.escalation", "rejected:invalid")

        span.set_attribute("cost.usd", float(spend))
        span.set_attribute("model", outcome.model)

        if outcome.record is None:
            # Semantic failure after the schema retry (and after any
            # escalation): store the raw attempt for debugging, mark failed,
            # acknowledge — an identical re-run would fail the same way.
            _store_extraction(
                document_id,
                tenant_id,
                outcome,
                record=None,
                validation=None,
                confidence=failed_extraction_report(outcome.error),
                definition=definition,
                cost_usd=spend,
                source_text=text,
            )
            _record_failure(document_id, f"extraction invalid: {outcome.error}", final=True)
            span.set_attribute("outcome", "invalid_extraction")
            return

        _store_extraction(
            document_id,
            tenant_id,
            outcome,
            outcome.record,
            validation,
            confidence,
            routing,
            definition,
            cost_usd=spend,
            source_text=text,
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
                "cost_usd": str(spend),
                "escalated": bool(trigger),
                "confidence_model_version": routing.model_version,
                "confidence_calibration": routing.calibration,
            },
        )


@dataclass(frozen=True)
class MeteredCall:
    """One model call, with what it cost."""

    outcome: ExtractionOutcome
    cost_usd: Decimal
    model: str
    tier: str


def _metered_extraction(
    *,
    text: str,
    definition: PipelineDefinition,
    tenant_id: str,
    document_id: uuid.UUID,
    provider: str,
    tier: str,
    purpose: str,
) -> MeteredCall | None:
    """Run one extraction under the budget, and record what it cost.

    Returns None when the tenant cannot afford the call. The reservation is the
    call's worst case — its prompt plus the hard `max_tokens` ceiling — so a
    cap can never be crossed by a call whose size was not yet known; it is
    settled to the real cost from the provider's own token counts.
    """
    model_id = model_for_tier(provider, tier)
    client = get_llm_client(model=model_id)
    label = f"{client.provider}:{client.model}"

    settings = get_settings()
    prompt_tokens = estimate_input_tokens(
        build_system_prompt(definition), build_user_message(text, definition)
    )
    worst_case = worst_case_cost_usd(label, prompt_tokens, settings.llm_max_tokens)

    reservation: Reservation | None = reserve(tenant_id, worst_case)
    if reservation is None:
        return None

    breaker = _model_breaker()
    try:
        # Breaker outside, retry inside. The retry handles a single unlucky
        # call; the breaker handles a provider that is down, and it must see
        # the whole retried attempt as one failure rather than three — three
        # workers x three retries would otherwise open it on one bad minute.
        outcome = breaker.call(
            lambda: retry_transient(
                lambda: run_extraction(text, client, definition),
                policy=RetryPolicy(attempts=3, base_delay=0.5, max_delay=4.0),
            )
        )
    except Exception:
        # The call did not happen, or did not produce an answer. Give the money
        # back before re-raising: a reservation held for a call that never
        # completed is spend the tenant never made.
        release(reservation)
        raise

    cost = call_cost_usd(label, outcome.input_tokens, outcome.output_tokens)
    state = settle(reservation, cost)
    sync_tenant_status(tenant_id, state)

    record_usage(
        tenant_id=tenant_id,
        model=label,
        purpose=purpose,
        input_tokens=outcome.input_tokens,
        output_tokens=outcome.output_tokens,
        document_id=document_id,
        pipeline_slug=definition.slug,
        latency_ms=outcome.latency_ms,
    )
    return MeteredCall(outcome=outcome, cost_usd=cost, model=label, tier=tier)


def _assess(outcome: ExtractionOutcome, text: str, definition: PipelineDefinition, span):
    """Validate, score and route one extraction. Returns (validation, confidence, routing)."""
    if outcome.record is None:
        return None, failed_extraction_report(outcome.error), None

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
        for name, entry in sorted(confidence.fields.items(), key=lambda kv: kv[1].confidence)[:3]:
            score_span.set_attribute(f"confidence.field.{name}", entry.confidence)

    # Routing consumes the calibrated model; the threshold comes from the
    # config file, never from code (2.3b).
    with tracer.start_as_current_span("extraction.route") as route_span:
        route_span.set_attribute("openinference.span.kind", "CHAIN")
        routing = route_extraction(confidence.signals, confidence_model_for(definition), definition)
        route_span.set_attribute("routing.decision", routing.decision)
        route_span.set_attribute("routing.calibration", routing.calibration)
        route_span.set_attribute("routing.threshold", routing.threshold)
        route_span.set_attribute("routing.model_version", routing.model_version)
        route_span.set_attribute("routing.flagged_fields", list(routing.flagged_fields))

    return validation, confidence, routing


def _defer_if_saturated(payload: dict, queue: str, tenant_id: str, document_id: uuid.UUID) -> bool:
    """Put the message back if this tenant already fills its share of the pipeline.

    Fairness, not throttling: the message returns to the queue with a short
    delay and the worker moves straight on to whatever is next, which is how a
    second tenant's single document gets served while the first is mid-flood.
    Deferral is not failure — the message is re-sent rather than left to
    redeliver, so it never counts against the redrive policy and can never
    reach the DLQ for being busy.
    """
    if admits(tenant_id, exclude_document=str(document_id)):
        return False
    _, broker = _clients()
    broker.send(queue, payload, delay_seconds=get_settings().defer_seconds)
    log.info(
        "deferred: tenant at its in-flight ceiling",
        extra={
            "tenant_id": tenant_id,
            "queue": queue,
            "in_flight": in_flight(tenant_id),
            "limit": get_settings().max_in_flight_per_tenant,
        },
    )
    return True


def _model_breaker():
    """The one breaker for the model provider, configured from settings."""
    settings = get_settings()
    return get_breaker(
        "model",
        threshold=settings.breaker_threshold,
        cooldown_seconds=settings.breaker_cooldown_seconds,
    )


def _defer_if_provider_down(payload: dict, queue: str, document_id: uuid.UUID) -> bool:
    """Put the message back, untouched, while the model provider is down.

    Deferring rather than failing is the whole point. A failed handler leaves
    the message to redeliver, which burns one of its three receives; an outage
    lasting more than three receives would push every in-flight document into
    the DLQ for the provider's sake rather than their own. Re-sending with a
    delay costs the document nothing and its receive count stays where it was.

    The delay is the breaker's remaining cooldown rather than a fixed number,
    so the fleet comes back roughly when the provider does instead of drifting
    further behind on every deferral.
    """
    breaker = _model_breaker()
    if breaker.state != "open":
        return False

    _, broker = _clients()
    delay = min(int(breaker.cooldown.total_seconds()), 900)
    broker.send(queue, payload, delay_seconds=delay)
    log.warning(
        "deferred: model provider circuit is open",
        extra={"document_id": str(document_id), "queue": queue, "retry_in_s": delay},
    )
    return True


def _pause_on_budget(document_id: uuid.UUID, tenant_id: str, span) -> None:
    """Stop the document at a resumable state and say why."""
    state = budget_state(tenant_id)
    sync_tenant_status(tenant_id, state)
    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        document.status = DocumentStatus.BUDGET_EXCEEDED
        document.last_error = (
            f"tenant budget exhausted: spent {state.spent_usd} of {state.budget_usd} USD"
        )
    span.set_attribute("outcome", "budget_exceeded")
    log.warning(
        "paused: tenant over budget",
        extra={"spent_usd": str(state.spent_usd), "budget_usd": str(state.budget_usd)},
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


def _observe_drift_safely(**kwargs) -> None:
    """Fold a document into its tenant's drift state, and never fail because of it.

    This runs inside the extraction transaction, which is what makes the
    observation atomic with the row it describes — and also what makes a bug
    here dangerous: an exception would roll the extraction back, the message
    would redeliver, and after `max_receive_count` the document would land in
    the DLQ. Monitoring would have destroyed the thing it was monitoring.

    So the detector is allowed to fail quietly. Losing a drift observation
    costs a data point in a rolling baseline; losing a document costs a
    document. (A database-level error still aborts the transaction, and should:
    that is not a drift bug, it is the transaction failing.)

    Found the hard way — a profile-dimension change left stale centroids that
    raised inside this call and failed four worker tests, which were extraction
    tests, not drift tests.
    """
    try:
        observation = observe_drift(**kwargs)
    except Exception:
        log.exception(
            "drift observation failed; continuing",
            extra={"document_id": str(kwargs.get("document_id"))},
        )
        return
    if observation.newly_flagged:
        # Recorded and observable, not paged. Alerting is the ops surface that
        # waits for the deployed environment.
        log.warning(
            "drift detected",
            extra={
                "doc_type": observation.doc_type,
                "signals": list(observation.flagged),
                "z_scores": {k: round(v, 3) for k, v in observation.z_scores.items()},
                "n_observed": observation.n_observed,
            },
        )


def _store_extraction(
    document_id: uuid.UUID,
    tenant_id: str,
    outcome: ExtractionOutcome,
    record: dict | None,
    validation: dict[str, bool] | None,
    confidence: ConfidenceReport,
    routing: RoutingDecision | None = None,
    definition: PipelineDefinition | None = None,
    cost_usd: Decimal | None = None,
    source_text: str | None = None,
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
            confidence_calibration=routing.calibration if routing else None,
            pipeline_slug=definition.slug,
            pipeline_version=definition.version,
            confidence_signals=confidence.signals,
            prompt_tokens=outcome.input_tokens,
            completion_tokens=outcome.output_tokens,
            latency_ms=outcome.latency_ms,
            # Everything this document cost, escalations included; the
            # per-call breakdown lives in usage_events.
            cost_usd=cost_usd if cost_usd is not None else Decimal("0"),
        )
        session.add(extraction)
        session.flush()

        # Drift, in the same transaction as the row it describes: a document
        # cannot be counted into a baseline without existing, or exist without
        # being counted. Costs one row update and no model call — every input
        # was computed above.
        if source_text is not None:
            _observe_drift_safely(
                tenant_id=tenant_id,
                doc_type=definition.slug,
                doc_confidence=extraction.doc_confidence,
                validation_passed=extraction.validation_passed,
                text=source_text,
                document_id=document_id,
                session=session,
            )
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
