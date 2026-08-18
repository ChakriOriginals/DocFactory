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
