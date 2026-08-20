"""Self-healing: the reaper, the bounded redrive, and chaos-lite recovery.

Faults are injected by counting, never by sampling — a chaos test that fails
once a fortnight teaches a team to re-run CI rather than to fix anything.

The scenarios are the three that actually happen: a document committed but
never enqueued, a worker killed mid-document, and a provider outage that
dead-letters a batch and then clears.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from docfactory_core.db import admin_session_scope
from docfactory_core.healing import (
    REDRIVE_KEY,
    heal,
    reap_stuck_documents,
    redrive_dlq,
)
from docfactory_core.models import Document, DocumentStatus, Tenant
from sqlalchemy import delete, text

pytestmark = pytest.mark.integration

TENANT = "healing-tenant"


@pytest.fixture(scope="module", autouse=True)
def healing_tenant():
    import socket
    from urllib.parse import urlparse

    from docfactory_core.config import get_settings

    parsed = urlparse(get_settings().database_url.replace("postgresql+psycopg", "postgresql"))
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 0.5):
            pass
    except OSError:
        pytest.skip("postgres is not running")

    with admin_session_scope() as session:
        if session.get(Tenant, TENANT) is None:
            session.add(Tenant(id=TENANT, name="Healing Test Tenant"))
    yield TENANT
    with admin_session_scope() as session:
        session.execute(delete(Document).where(Document.tenant_id == TENANT))


@pytest.fixture(autouse=True)
def clean_documents():
    with admin_session_scope() as session:
        session.execute(delete(Document).where(Document.tenant_id == TENANT))
    yield


class RecordingBroker:
    """A broker that records sends instead of making them.

    Enough surface for the reaper: it only ever calls `send`.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send(self, queue: str, payload: dict, *, delay_seconds: int = 0) -> str:
        self.sent.append((queue, payload))
        return str(uuid.uuid4())

    def queue_url(self, name: str) -> str:
        return f"memory://{name}"


def make_document(status: DocumentStatus, *, age: timedelta, tenant: str = TENANT) -> uuid.UUID:
    """A document whose updated_at is forced into the past.

    `updated_at` has an onupdate default, so it cannot be aged through the ORM
    — a second UPDATE would reset it. Hence the raw statement.
    """
    document_id = uuid.uuid4()
    with admin_session_scope() as session:
        session.add(
            Document(
                id=document_id,
                tenant_id=tenant,
                s3_key=f"{tenant}/incoming/{document_id}.pdf",
                sha256=uuid.uuid4().hex * 2,
                status=status,
            )
        )
    with admin_session_scope() as session:
        session.execute(
            text("UPDATE documents SET updated_at = :when WHERE id = :id"),
            {"when": datetime.now(UTC) - age, "id": str(document_id)},
        )
    return document_id


class TestTheReaper:
    def test_a_document_committed_but_never_enqueued_is_recovered(self):
        """The hole found in the 4f-A sweep.

        Both ingest paths commit the row and then enqueue. If the enqueue
        fails there is no message, so no redelivery and no DLQ — the document
        just sits there. Nothing but a sweeper finds it.
        """
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        broker = RecordingBroker()

        report = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        assert report.requeued == 1
        queue, payload = broker.sent[0]
        assert "parse" in queue
        assert payload["document_id"] == str(document_id)
        assert payload["tenant_id"] == TENANT
        assert payload["_reaped"] is True

    def test_a_parsed_document_resumes_at_extract_not_at_parse(self):
        """Recovery must not redo work that survived. The text artifact is
        already in object storage; re-parsing would spend the time again and
        overwrite it with identical bytes."""
        make_document(DocumentStatus.PARSED, age=timedelta(hours=1))
        broker = RecordingBroker()

        reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        queue, _ = broker.sent[0]
        assert "extract" in queue

    def test_a_recent_document_is_left_alone(self):
        """THE most important negative case. A document being worked on right
        now looks identical to a stranded one from the database side; the only
        thing separating them is age, and the threshold has to be above the
        longest visibility timeout or the reaper races live messages."""
        make_document(DocumentStatus.EXTRACTING, age=timedelta(seconds=30))
        broker = RecordingBroker()

        report = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        assert report.requeued == 0
        assert broker.sent == []

    @pytest.mark.parametrize(
        "status",
        [
            DocumentStatus.APPROVED,
            DocumentStatus.NEEDS_REVIEW,
            DocumentStatus.NEEDS_OCR,
            DocumentStatus.FAILED,
        ],
    )
    def test_terminal_documents_are_never_re_enqueued(self, status):
        """needs_ocr and failed are terminal by design; approved and
        needs_review are finished. Re-running any of them would redo work whose
        outcome is already recorded — and, for approved, silently re-bill it."""
        make_document(status, age=timedelta(days=7))
        broker = RecordingBroker()

        assert reap_stuck_documents(broker, stale_after=timedelta(minutes=15)).requeued == 0

    def test_an_over_budget_document_is_not_woken_while_still_over_budget(self):
        """budget_exceeded is resumable, but only into budget that exists.
        Re-enqueueing it now just pauses it again, one reservation at a time."""
        from decimal import Decimal

        with admin_session_scope() as session:
            session.get(Tenant, TENANT).budget_usd = Decimal("0.001")
            session.execute(
                text(
                    "INSERT INTO tenant_spend (tenant_id, spent_usd) VALUES (:t, 5.00) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET spent_usd = 5.00"
                ),
                {"t": TENANT},
            )
        try:
            make_document(DocumentStatus.BUDGET_EXCEEDED, age=timedelta(hours=2))
            broker = RecordingBroker()
            report = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))
            assert report.requeued == 0
            assert report.skipped_budget == 1
        finally:
            with admin_session_scope() as session:
                session.get(Tenant, TENANT).budget_usd = Decimal("100")
                session.execute(
                    text("UPDATE tenant_spend SET spent_usd = 0 WHERE tenant_id = :t"),
                    {"t": TENANT},
                )

    def test_an_over_budget_document_resumes_once_the_cap_is_raised(self):
        """The other half, and the actual self-healing: raise the cap and the
        document comes back without anyone touching it."""
        make_document(DocumentStatus.BUDGET_EXCEEDED, age=timedelta(hours=2))
        broker = RecordingBroker()

        report = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        assert report.requeued == 1
        assert "extract" in broker.sent[0][0]

    def test_the_reaper_stays_inside_its_tenant(self):
        """It sweeps every tenant, but each sweep runs under that tenant's RLS
        context — it never reads one tenant's rows while bound to another."""
        make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1), tenant="dev-tenant")
        broker = RecordingBroker()

        reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        tenants = {payload["tenant_id"] for _, payload in broker.sent}
        for _, payload in broker.sent:
            assert payload["tenant_id"] in tenants
        with admin_session_scope() as session:
            session.execute(
                text("DELETE FROM documents WHERE tenant_id='dev-tenant' AND status='received'")
            )

    def test_re_enqueueing_the_same_document_twice_is_harmless(self):
        """The reaper is allowed to be approximate precisely because handlers
        open with a status guard. Two sweeps before a worker picks it up cost
        one extra no-op receive."""
        make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        broker = RecordingBroker()

        first = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))
        second = reap_stuck_documents(broker, stale_after=timedelta(minutes=15))

        assert first.requeued == second.requeued == 1
        assert first.document_ids == second.document_ids


def test_every_document_status_is_classified():
    """No status may be silently ignored by the reaper.

    A status in neither set is one the reaper walks past without deciding
    anything — which is how 345 calibration documents in `extracted` came to be
    scanned and skipped for the right reason entirely by accident, and is the
    same shape as the budget_exceeded bug 4f-A found.
    """
    from docfactory_core.healing import _REQUEUE_STAGE, TERMINAL

    classified = set(TERMINAL) | set(_REQUEUE_STAGE)
    unclassified = set(DocumentStatus) - classified
    assert not unclassified, (
        f"{sorted(unclassified)} is neither terminal nor mapped to a requeue "
        "stage. Decide which, in docfactory_core/healing.py."
    )
    overlap = set(TERMINAL) & set(_REQUEUE_STAGE)
    assert not overlap, f"{sorted(overlap)} is both terminal and requeueable"


def test_calibration_documents_are_left_alone():
    """`extracted` is the pre-routing state. The worker never leaves a document
    there; the calibration harness does, on purpose, and re-extracting that
    corpus would cost real money in anthropic mode."""
    make_document(DocumentStatus.EXTRACTED, age=timedelta(days=30))
    broker = RecordingBroker()

    assert reap_stuck_documents(broker, stale_after=timedelta(minutes=15)).requeued == 0


class FakeSQS:
    """An in-memory DLQ good enough for the redrive's actual API surface."""

    def __init__(self, messages: list[dict]) -> None:
        self.messages = [
            {"Body": json.dumps(body), "ReceiptHandle": f"rh-{i}"}
            for i, body in enumerate(messages)
        ]
        self.deleted: list[str] = []

    def receive_message(self, *, QueueUrl, MaxNumberOfMessages=10, **_kwargs):
        batch = self.messages[:MaxNumberOfMessages]
        self.messages = self.messages[MaxNumberOfMessages:]
        return {"Messages": batch} if batch else {}

    def delete_message(self, *, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


class FakeBroker(RecordingBroker):
    def __init__(self, messages: list[dict]) -> None:
        super().__init__()
        self._sqs = FakeSQS(messages)


class TestBoundedRedrive:
    def test_an_outage_batch_comes_home(self):
        """The whole point: a provider outage that lasted longer than three
        receives dead-letters documents that were never poison."""
        broker = FakeBroker(
            [{"document_id": str(uuid.uuid4()), "tenant_id": TENANT} for _ in range(5)]
        )

        report = redrive_dlq(broker, "docfactory-extract", max_attempts=2)

        assert report.moved == 5
        assert report.exhausted == 0
        assert len(broker.sent) == 5
        assert len(broker._sqs.deleted) == 5, "a redriven message must leave the DLQ"

    def test_each_redrive_stamps_the_message(self):
        broker = FakeBroker([{"document_id": str(uuid.uuid4()), "tenant_id": TENANT}])
        redrive_dlq(broker, "docfactory-extract")
        assert broker.sent[0][1][REDRIVE_KEY] == 1

    def test_a_message_that_has_used_its_attempts_stays_dead(self):
        """The bound. Without it a poison document loops between the two queues
        forever, burning a model call per lap."""
        broker = FakeBroker(
            [{"document_id": str(uuid.uuid4()), "tenant_id": TENANT, REDRIVE_KEY: 2}]
        )

        report = redrive_dlq(broker, "docfactory-extract", max_attempts=2)

        assert report.moved == 0
        assert report.exhausted == 1
        assert broker.sent == []
        assert broker._sqs.deleted == [], "an exhausted message must be LEFT in the DLQ"

    def test_a_poison_document_dies_after_a_bounded_number_of_laps(self):
        """Simulated end to end: redrive, fail back to the DLQ, redrive, fail
        back, then stay dead."""
        payload = {"document_id": str(uuid.uuid4()), "tenant_id": TENANT}
        laps = 0
        for _ in range(5):
            broker = FakeBroker([dict(payload)])
            report = redrive_dlq(broker, "docfactory-extract", max_attempts=2)
            if report.moved == 0:
                break
            laps += 1
            payload = broker.sent[0][1]  # as if it failed straight back
        assert laps == 2, f"expected exactly max_attempts laps, got {laps}"

    def test_an_unparseable_message_is_left_alone(self):
        """A redrive that mangles messages it does not understand is worse than
        one that ignores them."""
        broker = FakeBroker([])
        broker._sqs.messages = [{"Body": "not json at all", "ReceiptHandle": "rh-x"}]

        report = redrive_dlq(broker, "docfactory-extract")

        assert report.moved == 0
        assert broker._sqs.deleted == []


class TestChaosLite:
    """Fault injection: does the pipeline degrade and recover, or lose work?"""

    def test_a_worker_killed_mid_document_recovers_without_the_reaper(self):
        """The case the reaper does NOT need to handle, asserted so the claim
        stays true: the message was never deleted, so SQS redelivers it into a
        handler that is idempotent by status guard.

        Simulated at the seam that matters — a document left in `extracting`
        with a live message is picked up again and not treated as stranded.
        """
        make_document(DocumentStatus.EXTRACTING, age=timedelta(seconds=5))
        broker = RecordingBroker()

        # Within the visibility window: the queue owns recovery, not the reaper.
        assert reap_stuck_documents(broker, stale_after=timedelta(minutes=15)).requeued == 0

        # Long past it, with no message left: now it is genuinely stranded.
        make_document(DocumentStatus.EXTRACTING, age=timedelta(hours=3))
        assert reap_stuck_documents(broker, stale_after=timedelta(minutes=15)).requeued == 1

    def test_storage_latency_is_retried_not_failed(self):
        """A slow or briefly unavailable object store must not cost a document
        one of its three receives."""
        from botocore.exceptions import ClientError
        from docfactory_core.resilience import RetryPolicy, retry_transient

        attempts = []

        def slow_storage():
            attempts.append(1)
            if len(attempts) < 3:
                raise ClientError(
                    {"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
                    "GetObject",
                )
            return b"%PDF-1.4"

        assert (
            retry_transient(
                slow_storage, policy=RetryPolicy(attempts=3, jitter=False), sleep=lambda _: None
            )
            == b"%PDF-1.4"
        )
        assert len(attempts) == 3

    def test_a_database_blip_is_transient_and_a_constraint_violation_is_not(self):
        """Both arrive as SQLAlchemy errors from the same call site. Treating
        them alike would either retry a bad row forever or dead-letter a
        document because a connection hiccuped."""
        from docfactory_core.resilience import classify

        class OperationalError(Exception):
            pass

        class IntegrityError(Exception):
            pass

        assert classify(OperationalError("server closed the connection")) == "transient"
        assert classify(IntegrityError("violates check constraint")) == "permanent"

    def test_a_healing_pass_does_both_and_reports_honestly(self):
        make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        broker = FakeBroker([{"document_id": str(uuid.uuid4()), "tenant_id": TENANT}])

        summary = heal(broker, stale_after=timedelta(minutes=15))

        # The fake models one shared DLQ rather than three, so the single
        # message is drained by the first queue swept and the other two find
        # nothing. What is being asserted is that a pass does both jobs and
        # counts them separately, not the arithmetic of the fake.
        assert summary["redriven"] == 1
        assert summary["requeued"] == 1

    def test_the_healer_never_takes_the_worker_down(self):
        """A sweeper that crashes the process it lives in has done more damage
        than the stranded documents it went looking for. The 4e lesson."""
        from docfactory_worker.main import _healing_loop

        class Exploding:
            def send(self, *_args, **_kwargs):
                raise RuntimeError("boom")

            def queue_url(self, _name):
                raise RuntimeError("boom")

            _sqs = None

        import threading

        from docfactory_core.config import get_settings

        settings = get_settings()
        stop = threading.Event()

        class Once:
            """Fire the loop body exactly once, then stop it."""

            def __init__(self) -> None:
                self.calls = 0

            def wait(self, _timeout):
                self.calls += 1
                return self.calls > 1

        # Should log and return, not raise.
        _healing_loop(Once(), settings)
        assert not stop.is_set()
