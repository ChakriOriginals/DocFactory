"""Review queue: tasks, SLA deadlines, and the correction feedback loop.

A document routed to `needs_review` becomes one task carrying the specific
below-threshold field names. Pointing a reviewer at the cells rather than the
document is the whole payoff of the rule-to-field attribution built in 2.1 —
without it, every review re-reads a whole invoice.

Resolving a task either accepts the extraction as-is or supplies corrected
values. A correction updates the stored extraction *and* writes an
`eval_case`, so human effort becomes labelled data that future evals and
calibration refits can use, instead of a one-off repair.

SLA scope: this module makes breach *detectable* and exposes queue depth and
age. Alerting, burn-rate and backpressure are Phase 5 operations concerns and
are deliberately absent.
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import (
    Document,
    DocumentStatus,
    EvalCase,
    Extraction,
    ExtractionField,
    ReviewResolution,
    ReviewStatus,
    ReviewTask,
)

log = logging.getLogger(__name__)


def ensure_review_task(extraction_id: uuid.UUID, *, flagged_fields: tuple[str, ...]) -> uuid.UUID:
    """Create the review task for an extraction, or return the existing one.

    Idempotent under at-least-once redelivery, matching every other consumer
    in the pipeline: the same extraction must never queue two units of human
    work.
    """
    settings = get_settings()
    with session_scope() as session:
        existing = session.scalars(
            select(ReviewTask).where(ReviewTask.extraction_id == extraction_id)
        ).first()
        if existing is not None:
            return existing.id

        extraction = session.get(Extraction, extraction_id)
        if extraction is None:
            raise ValueError(f"extraction {extraction_id} not found")

        task = ReviewTask(
            extraction_id=extraction_id,
            document_id=extraction.document_id,
            tenant_id=extraction.tenant_id,
            status=ReviewStatus.OPEN,
            # Absolute deadline frozen at creation: a later SLA config change
            # must not retroactively breach or rescue queued work.
            sla_due_at=datetime.now(UTC) + timedelta(hours=settings.review_sla_hours),
            flagged_fields=list(flagged_fields),
        )
        session.add(task)
        session.flush()
        log.info(
            "review task created",
            extra={
                "review_task_id": str(task.id),
                "document_id": str(extraction.document_id),
                "flagged_fields": list(flagged_fields),
                "sla_due_at": task.sla_due_at.isoformat(),
            },
        )
        return task.id


def open_tasks(limit: int = 50) -> list[ReviewTask]:
    """Open tasks, oldest first — the queue a reviewer works top-down."""
    with session_scope() as session:
        tasks = session.scalars(
            select(ReviewTask)
            .where(ReviewTask.status == ReviewStatus.OPEN)
            .order_by(ReviewTask.created_at)
            .limit(limit)
        ).all()
        for task in tasks:
            session.expunge(task)
        return list(tasks)


def breached_tasks(now: datetime | None = None) -> list[ReviewTask]:
    """Open tasks past their deadline. Detection only — alerting is Phase 5."""
    moment = now or datetime.now(UTC)
    with session_scope() as session:
        tasks = session.scalars(
            select(ReviewTask)
            .where(ReviewTask.status == ReviewStatus.OPEN)
            .where(ReviewTask.sla_due_at < moment)
            .order_by(ReviewTask.sla_due_at)
        ).all()
        for task in tasks:
            session.expunge(task)
        return list(tasks)


def queue_depth(now: datetime | None = None) -> dict:
    """Depth and age of the open queue, for operability."""
    moment = now or datetime.now(UTC)
    with session_scope() as session:
        open_count = session.scalar(
            select(func.count())
            .select_from(ReviewTask)
            .where(ReviewTask.status == ReviewStatus.OPEN)
        )
        oldest = session.scalar(
            select(func.min(ReviewTask.created_at)).where(ReviewTask.status == ReviewStatus.OPEN)
        )
        breached = session.scalar(
            select(func.count())
            .select_from(ReviewTask)
            .where(ReviewTask.status == ReviewStatus.OPEN)
            .where(ReviewTask.sla_due_at < moment)
        )
    return {
        "open": int(open_count or 0),
        "breached": int(breached or 0),
        "oldest_age_seconds": int((moment - oldest).total_seconds()) if oldest else 0,
    }


def correct_extraction(
    document_id: uuid.UUID,
    *,
    corrections: dict[str, str],
    source: str = "client_correction",
) -> int:
    """Correct a document's latest extraction without a review task.

    THE GAP THIS CLOSES. resolve_task is the only write path in the system, and
    it needs a ReviewTask. A document that auto-approved never had one — by
    definition, because the confidence model was sure. So the errors a client
    is most likely to notice, the ones that sailed through with a clean score,
    were the exact errors that could not be reported: the stored extraction
    stayed wrong, the API kept serving the wrong value, and no eval_case was
    written.

    That last part is the compounding half. The feedback loop learned
    exclusively from errors the confidence model had already flagged, so the
    errors it is blind to could never enter the training data. A model cannot
    be corrected on its blind spot by a process that only samples its
    known unknowns.

    Writes the same ExtractionField update and EvalCase row resolve_task does,
    so a correction arriving this way is indistinguishable downstream except by
    `source` — which is worth distinguishing, because human review and a client
    disputing an auto-approval are different populations.

    Returns the number of fields corrected. Runs under RLS: a document
    belonging to another tenant is simply not found.
    """
    if not corrections:
        raise ValueError("a correction must name at least one field")

    with session_scope() as session:
        document = session.get(Document, document_id, with_for_update=True)
        if document is None:
            raise ValueError(f"document {document_id} not found")

        extraction = session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document_id)
            .order_by(Extraction.created_at.desc())
            .limit(1)
        ).first()
        if extraction is None:
            # needs_ocr and failed documents land here. There is nothing to
            # correct, and inventing an extraction to hang the correction on
            # would put a fabricated row into unit costs and drift baselines.
            raise ValueError(f"document {document_id} has no extraction to correct")

        unknown = sorted(set(corrections) - set(extraction.output))
        if unknown:
            raise ValueError(f"not fields of this extraction: {unknown}")

        for name, corrected in corrections.items():
            field = session.scalars(
                select(ExtractionField)
                .where(ExtractionField.extraction_id == extraction.id)
                .where(ExtractionField.name == name)
            ).first()
            previous = field.value if field else extraction.output.get(name)
            if previous == corrected:
                continue
            if field is not None:
                field.value = corrected
                field.confidence = 1.0
            output = dict(extraction.output)
            output[name] = corrected
            extraction.output = output
            session.add(
                EvalCase(
                    tenant_id=document.tenant_id,
                    document_id=document_id,
                    extraction_id=extraction.id,
                    field_name=name,
                    extracted_value=previous,
                    corrected_value=corrected,
                    source=source,
                )
            )

        log.info(
            "extraction corrected by client",
            extra={
                "document_id": str(document_id),
                "corrected_fields": sorted(corrections),
                "was_auto_approved": document.status == DocumentStatus.APPROVED,
            },
        )
        return len(corrections)


def resolve_task(
    task_id: uuid.UUID,
    *,
    resolution: ReviewResolution,
    corrections: dict[str, str] | None = None,
) -> None:
    """Close a task, optionally writing corrected field values.

    A correction updates both the extraction's JSON output and its
    `extraction_fields` rows, then records one `eval_case` per corrected field
    so the fix is reusable as evaluation data.
    """
    corrections = corrections or {}
    if resolution == ReviewResolution.CORRECTED and not corrections:
        raise ValueError("a corrected resolution requires corrections")

    with session_scope() as session:
        task = session.get(ReviewTask, task_id, with_for_update=True)
        if task is None:
            raise ValueError(f"review task {task_id} not found")
        if task.status == ReviewStatus.RESOLVED:
            raise ValueError(f"review task {task_id} is already resolved")

        extraction = session.get(Extraction, task.extraction_id)
        for name, corrected in corrections.items():
            field = session.scalars(
                select(ExtractionField)
                .where(ExtractionField.extraction_id == extraction.id)
                .where(ExtractionField.name == name)
            ).first()
            previous = field.value if field else extraction.output.get(name)
            if field is not None:
                field.value = corrected
                # A human-supplied value is ground truth, not a prediction.
                field.confidence = 1.0
            output = dict(extraction.output)
            output[name] = corrected
            extraction.output = output
            session.add(
                EvalCase(
                    tenant_id=task.tenant_id,
                    document_id=task.document_id,
                    extraction_id=extraction.id,
                    field_name=name,
                    extracted_value=previous,
                    corrected_value=corrected,
                    source="human_review",
                )
            )

        task.status = ReviewStatus.RESOLVED
        task.resolution = resolution
        task.resolved_at = datetime.now(UTC)

        document = session.get(Document, task.document_id, with_for_update=True)
        document.status = DocumentStatus.APPROVED

        log.info(
            "review task resolved",
            extra={
                "review_task_id": str(task_id),
                "resolution": str(resolution),
                "corrected_fields": sorted(corrections),
            },
        )
