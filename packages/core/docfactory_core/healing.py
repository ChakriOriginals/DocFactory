"""Recovery that needs no human: the stuck-document reaper and DLQ redrive.

Two failure shapes the queue cannot fix on its own.

THE STRANDED DOCUMENT. Both ingest paths commit the `documents` row and then
enqueue it. If that enqueue fails — an SQS blip, an expired credential, a
process killed between the two — the row exists and no message drives it, and
nothing will ever notice. There is no message to redeliver, so the visibility
timeout cannot help and the DLQ never sees it. The document simply sits in
`received` forever. (A worker killed *mid-document* is a different and already
solved case: its message was never deleted, so SQS redelivers it after the
visibility timeout into a handler that is idempotent by status guard. That path
needs no reaper and `tests/test_healing.py` proves it still works.)

THE OUTAGE-SHAPED DLQ. A provider outage that lasts longer than three receives
sends every in-flight document to the dead-letter queue. Those documents are
not poison — they are ordinary documents that arrived at a bad moment — and
when the provider comes back nothing brings them home. A bounded redrive does,
while leaving genuinely un-processable documents where they are.

BOTH ARE BOUNDED, AND THAT IS THE DESIGN. An unbounded redrive is an infinite
loop with a queue in the middle: a poison document returns, fails three times,
goes back to the DLQ, and is redriven again forever, burning a model call each
time. Every message carries its own redrive count, capped, and a document that
exhausts it stays dead.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope, tenant_context
from docfactory_core.models import Document, DocumentStatus, Tenant
from docfactory_core.queues import dlq_name
from docfactory_core.tracing import inject_trace_context

log = logging.getLogger(__name__)

# Where a document goes back to, per stage it got stuck in. A document stuck
# before its text exists must re-parse; one that has text goes straight to
# extract, so recovery does not redo work that survived.
_REQUEUE_STAGE = {
    DocumentStatus.RECEIVED: "parse",
    DocumentStatus.PARSING: "parse",
    DocumentStatus.PARSED: "extract",
    DocumentStatus.EXTRACTING: "extract",
    DocumentStatus.BUDGET_EXCEEDED: "extract",
}

# Statuses a reaper must never touch.
#
# `needs_ocr` and `failed` are terminal by design; `approved` and
# `needs_review` are finished — re-enqueueing any of them would re-run work
# whose outcome is already recorded, and for `approved` would silently re-bill
# it.
#
# `extracted` is here for a subtler reason and was very nearly missed. It is
# the PRE-ROUTING state, which the worker never leaves a document in — routing
# always advances it to approved or needs_review in the same transaction. The
# rows that carry it come from the calibration harness, which scores without
# routing on purpose. The first run of the reaper against the development
# database scanned 345 of them and requeued nothing, which is the correct
# outcome arrived at by accident: the status simply was not in the requeue map.
# Re-extracting a calibration corpus would be a healing mechanism doing real
# damage, and in `anthropic` mode it would have a bill attached.
#
# THE COVERAGE IS ASSERTED, not assumed. `test_every_document_status_is_
# classified` fails if a new status appears and is put in neither set — because
# "a status nobody classified" is exactly the shape of the budget_exceeded bug
# that 4f-A found.
TERMINAL = frozenset(
    {
        DocumentStatus.APPROVED,
        DocumentStatus.NEEDS_REVIEW,
        DocumentStatus.NEEDS_OCR,
        DocumentStatus.FAILED,
        DocumentStatus.EXTRACTED,
    }
)

REDRIVE_KEY = "_redrive_attempt"


@dataclass
class ReapReport:
    scanned: int = 0
    requeued: int = 0
    skipped_budget: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    document_ids: list[str] = field(default_factory=list)


@dataclass
class RedriveReport:
    queue: str = ""
    moved: int = 0
    exhausted: int = 0
    inspected: int = 0


def _queue_for(stage: str) -> str:
    settings = get_settings()
    return {"parse": settings.parse_queue, "extract": settings.extract_queue}[stage]


def reap_stuck_documents(
    broker,
    *,
    stale_after: timedelta | None = None,
    now: datetime | None = None,
    limit: int = 200,
) -> ReapReport:
    """Re-enqueue documents stranded in a non-terminal state with no message.

    `stale_after` MUST exceed the longest visibility timeout, or the reaper
    races live messages: a document legitimately being worked on looks
    identical to a stranded one from the database side. The default is derived
    from the extract queue's 90-second timeout with a wide margin rather than
    picked as a round number.

    Re-enqueueing is safe even when the reaper is wrong about a document. Every
    handler opens with a status guard and returns early on a state it has
    already passed, so a duplicate message costs one no-op receive. That is why
    this can afford to be approximate.

    Runs per tenant under RLS rather than through the owner connection. The
    worker has no owner credentials by design (4d), and a sweeper that needed
    them would be a reason to hand them out.
    """
    settings = get_settings()
    stale_after = stale_after or timedelta(minutes=15)
    now = now or datetime.now(UTC)
    cutoff = now - stale_after
    report = ReapReport()

    # `tenants` is readable unscoped by the app role — the same property the
    # auth path depends on.
    with session_scope(require_tenant=False) as session:
        tenant_ids = list(session.scalars(select(Tenant.id)).all())

    for tenant_id in tenant_ids:
        with tenant_context(tenant_id), session_scope() as session:
            stale = list(
                session.scalars(
                    select(Document)
                    .where(
                        Document.status.notin_([status.value for status in TERMINAL]),
                        Document.updated_at < cutoff,
                    )
                    .order_by(Document.updated_at)
                    .limit(limit)
                ).all()
            )
            candidates = [(document.id, document.status, document.tenant_id) for document in stale]

        for document_id, status, owner in candidates:
            report.scanned += 1
            stage = _REQUEUE_STAGE.get(DocumentStatus(status))
            if stage is None:
                continue

            if status == DocumentStatus.BUDGET_EXCEEDED:
                # Resumable, but only once there is budget to resume into.
                # Re-enqueueing an over-budget document just pauses it again,
                # one model-call reservation at a time.
                from docfactory_core.budget import budget_state

                with tenant_context(owner):
                    if budget_state(owner).exceeded:
                        report.skipped_budget += 1
                        continue

            with tenant_context(owner):
                broker.send(
                    _queue_for(stage),
                    inject_trace_context(
                        {
                            "document_id": str(document_id),
                            "tenant_id": owner,
                            "_reaped": True,
                        }
                    ),
                )
            report.requeued += 1
            report.by_status[str(status)] = report.by_status.get(str(status), 0) + 1
            report.document_ids.append(str(document_id))
            log.warning(
                "reaped a stranded document",
                extra={
                    "document_id": str(document_id),
                    "tenant_id": owner,
                    "status": str(status),
                    "stage": stage,
                    "stale_after_s": stale_after.total_seconds(),
                },
            )

    if report.requeued or report.skipped_budget:
        log.info(
            "reaper finished",
            extra={
                "scanned": report.scanned,
                "requeued": report.requeued,
                "skipped_budget": report.skipped_budget,
                "settings_extract_queue": settings.extract_queue,
            },
        )
    return report


def redrive_dlq(
    broker,
    queue: str,
    *,
    max_messages: int = 100,
    max_attempts: int = 2,
    wait_seconds: int = 1,
) -> RedriveReport:
    """Move dead-lettered messages back, a bounded number of times.

    Called after a provider outage clears. The messages in a DLQ after an
    outage are ordinary documents that arrived at a bad moment, and they should
    come home; the messages in a DLQ after a bad document are poison, and they
    should not. Nothing in the message says which it is, so the bound is what
    keeps the second case from becoming an infinite loop with a queue in the
    middle.

    A message that has already used its redrive attempts is left in the DLQ,
    which is exactly where a human should find it.
    """
    dead_letter = dlq_name(queue)
    source_url = broker.queue_url(dead_letter)
    report = RedriveReport(queue=dead_letter)

    while report.inspected < max_messages:
        batch = broker._sqs.receive_message(
            QueueUrl=source_url,
            MaxNumberOfMessages=min(10, max_messages - report.inspected),
            WaitTimeSeconds=wait_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        ).get("Messages", [])
        if not batch:
            break

        for message in batch:
            report.inspected += 1
            try:
                payload = json.loads(message["Body"])
            except (TypeError, ValueError):
                # Not ours, or not JSON. Leave it: a redrive that mangles
                # messages it does not understand is worse than one that
                # ignores them.
                log.warning("unparseable DLQ message left in place", extra={"queue": dead_letter})
                continue

            attempt = int(payload.get(REDRIVE_KEY, 0))
            if attempt >= max_attempts:
                report.exhausted += 1
                log.warning(
                    "DLQ message has exhausted its redrives; leaving it dead",
                    extra={
                        "queue": dead_letter,
                        "attempts": attempt,
                        "document_id": payload.get("document_id"),
                    },
                )
                continue

            payload[REDRIVE_KEY] = attempt + 1
            tenant_id = payload.get("tenant_id")
            # The send is tenant-scoped for symmetry with every other enqueue;
            # the queue itself is not tenant-partitioned, the message is.
            if tenant_id:
                with tenant_context(tenant_id):
                    broker.send(queue, payload)
            else:
                broker.send(queue, payload)

            broker._sqs.delete_message(QueueUrl=source_url, ReceiptHandle=message["ReceiptHandle"])
            report.moved += 1

    if report.moved or report.exhausted:
        log.info(
            "dlq redrive finished",
            extra={
                "queue": dead_letter,
                "moved": report.moved,
                "exhausted": report.exhausted,
                "inspected": report.inspected,
            },
        )
    return report


def redrive_all(broker, *, max_messages: int = 100, max_attempts: int = 2) -> list[RedriveReport]:
    settings = get_settings()
    return [
        redrive_dlq(broker, queue, max_messages=max_messages, max_attempts=max_attempts)
        for queue in (settings.ingest_queue, settings.parse_queue, settings.extract_queue)
    ]


def heal(broker, *, stale_after: timedelta | None = None) -> dict:
    """One healing pass: redrive what an outage killed, reap what was stranded.

    Redrive first, deliberately. A redriven message puts its document back into
    a non-terminal state, and running the reaper first would mean scanning rows
    that are about to have a message again — harmless, but it makes the reaper's
    numbers lie about how much was actually stranded.
    """
    redrives = redrive_all(broker)
    reaped = reap_stuck_documents(broker, stale_after=stale_after)
    return {
        "redriven": sum(report.moved for report in redrives),
        "exhausted": sum(report.exhausted for report in redrives),
        "requeued": reaped.requeued,
        "scanned": reaped.scanned,
        "skipped_budget": reaped.skipped_budget,
    }


def _document_ids(report: ReapReport) -> list[uuid.UUID]:
    return [uuid.UUID(value) for value in report.document_ids]


def main() -> None:
    """Force a sweep from the command line. `make heal`.

    The worker does this on a timer; this exists for the moment after an outage
    clears when waiting five minutes is five minutes too long.
    """
    import json as _json

    from docfactory_core.logging import configure_logging
    from docfactory_core.queues import QueueBroker

    configure_logging("healing")
    settings = get_settings()
    summary = heal(QueueBroker(), stale_after=timedelta(seconds=settings.heal_stale_after_seconds))
    print(_json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
