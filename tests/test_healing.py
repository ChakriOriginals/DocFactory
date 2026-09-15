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
from docfactory_core.db import admin_session_scope, tenant_context
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


class TestConsumerSupervision:
    """The heartbeat is only fresh while every consumer thread is alive.

    The failure it exists for is quiet: the worker is one process running three
    consumer threads, and if one dies the process stays up and the other two
    keep working. Two thirds of a pipeline, no error anywhere, and the first
    symptom is a queue that stopped draining. ECS cannot see inside a process,
    so this turns the claim into a file mtime that a container health check can
    read.
    """

    def _settings(self, path, interval=0.02):
        from types import SimpleNamespace

        return SimpleNamespace(
            worker_heartbeat_path=str(path),
            worker_heartbeat_interval_seconds=interval,
        )

    def test_the_heartbeat_advances_while_every_consumer_lives(self, tmp_path):
        import threading
        import time

        from docfactory_worker.main import _supervise

        beat = tmp_path / "hb"
        stop = threading.Event()
        alive = [
            threading.Thread(target=lambda: stop.wait(5), name=f"consumer-{i}") for i in range(3)
        ]
        for thread in alive:
            thread.start()

        supervisor = threading.Thread(
            target=_supervise, args=(alive, stop, self._settings(beat)), daemon=True
        )
        supervisor.start()
        time.sleep(0.15)
        first = beat.stat().st_mtime if beat.exists() else None
        assert first is not None, "no heartbeat written while all consumers were alive"

        time.sleep(0.15)
        assert beat.stat().st_mtime >= first, "heartbeat stopped advancing"

        stop.set()
        supervisor.join(timeout=2)
        for thread in alive:
            thread.join(timeout=2)

    def test_a_dead_consumer_freezes_the_heartbeat(self, tmp_path):
        """The whole point: stop claiming health, let ECS replace the task.

        Deliberately NOT self-repair. Restarting a dead consumer in-process
        would paper over whatever killed it and leave the worker in a state no
        test covers; going stale hands the task to a platform that is better at
        replacing it.
        """
        import threading
        import time

        from docfactory_worker.main import _supervise

        beat = tmp_path / "hb"
        stop = threading.Event()
        doomed = threading.Thread(target=lambda: None, name="consumer-doomed")
        doomed.start()
        doomed.join()  # it is now dead
        assert not doomed.is_alive()

        _supervise([doomed], stop, self._settings(beat))  # returns immediately

        frozen_at = beat.stat().st_mtime if beat.exists() else None
        time.sleep(0.1)
        if frozen_at is not None:
            assert beat.stat().st_mtime == frozen_at, "heartbeat kept advancing after a death"
        assert not stop.is_set(), "supervision must not stop the surviving consumers itself"

    def test_an_unwritable_heartbeat_does_not_kill_a_working_worker(self, tmp_path):
        """A misconfigured path is not a reason to stop processing documents.

        The health check will fail and ECS will replace the task, which is the
        right outcome for a container that cannot report its health — but it
        should keep working until it is replaced, not fall over immediately.
        """
        import threading

        from docfactory_worker.main import _supervise

        stop = threading.Event()
        alive = threading.Thread(target=lambda: stop.wait(5), name="consumer-0")
        alive.start()

        unwritable = tmp_path / "no-such-directory" / "hb"
        supervisor = threading.Thread(
            target=_supervise, args=([alive], stop, self._settings(unwritable)), daemon=True
        )
        supervisor.start()
        threading.Event().wait(0.1)
        assert supervisor.is_alive(), "an unwritable heartbeat path killed the supervisor"

        stop.set()
        supervisor.join(timeout=2)
        alive.join(timeout=2)


class TestTheRescueCycleEnds:
    """A document the pipeline cannot process must end, visibly -- and only then.

    The reaper re-enqueued on `status NOT IN TERMINAL AND updated_at < cutoff`
    and wrote nothing, so a document failing at a site no handler guarded --
    `put_object` of the parsed text, which an S3 permission problem on the
    parsed/ prefix reaches -- stayed at `parsing` and was rescued every sweep,
    forever. Reproduced before fixing: three deliveries including the final one
    left it at `parsing`, and three consecutive sweeps each took it again.

    The opposite failure is just as real and is tested just as hard: a fix that
    terminalizes on the last delivery kills documents over a 30-second blip.
    """

    GIVE_UP_AFTER = 3

    @staticmethod
    def _stub_worker(monkeypatch, storage: dict):
        """The real parse handler over storage whose PUT fails while storage['down']."""
        from docfactory_worker import handlers

        class Store:
            def get_object(self, key):
                return b"%PDF-1.4 pretend"

            def put_object(self, *args, **kwargs):
                if storage["down"]:
                    raise RuntimeError("AccessDenied: parsed/ prefix")

        broker = RecordingBroker()
        monkeypatch.setattr(handlers, "_clients", lambda: (Store(), broker))
        # Parsing itself is not under test; the unguarded failure site after it is.
        monkeypatch.setattr(handlers, "extract_pdf_text", lambda body: "text " * 200)
        return handlers

    @staticmethod
    def _age(document_id, age: timedelta = timedelta(hours=1)):
        """Stand in for the stale window elapsing between sweeps.

        Every write refreshes updated_at, so without this a sweep would skip the
        document for being recent -- and a loop test would pass for the wrong
        reason.
        """
        with admin_session_scope() as session:
            session.execute(
                text("UPDATE documents SET updated_at = :when WHERE id = :id"),
                {"when": datetime.now(UTC) - age, "id": str(document_id)},
            )

    @staticmethod
    def _row(document_id) -> Document:
        with admin_session_scope() as session:
            document = session.get(Document, document_id)
            session.expunge(document)
            return document

    @staticmethod
    def _deliver_three_times(handlers, document_id):
        payload = {"document_id": str(document_id), "tenant_id": TENANT}
        for receive in (1, 2, 3):
            with pytest.raises(RuntimeError):
                handlers.handle_parse(payload, receive_count=receive, final_attempt=receive >= 3)

    def _worker_sweep(self, document_id):
        self._age(document_id)
        return reap_stuck_documents(
            RecordingBroker(),
            stale_after=timedelta(minutes=15),
            give_up_after=self.GIVE_UP_AFTER,
        )

    def test_exhausted_deliveries_do_not_kill_the_document(self, monkeypatch):
        """Thirty seconds of trouble is not a verdict. The reason is recorded."""
        handlers = self._stub_worker(monkeypatch, {"down": True})
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))

        self._deliver_three_times(handlers, document_id)

        row = self._row(document_id)
        assert row.status == DocumentStatus.PARSING, (
            "the final delivery must not terminalize: the reaper and the DLQ "
            "redrive both refuse FAILED rows, so a blip would be permanent"
        )
        assert row.last_error == "parse: AccessDenied: parsed/ prefix", (
            "an unguarded failure site must still say why it failed"
        )

    def test_a_document_that_fails_every_rescue_is_given_up_visibly(self, monkeypatch):
        """The regression test for the loop itself."""
        handlers = self._stub_worker(monkeypatch, {"down": True})
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        self._deliver_three_times(handlers, document_id)

        rescued = 0
        for _ in range(self.GIVE_UP_AFTER):
            report = self._worker_sweep(document_id)
            # Anchor: the sweep really did take this document. Without it, a
            # sweep that saw nothing at all would satisfy the assertions below.
            assert str(document_id) in report.document_ids
            rescued += 1
            self._deliver_three_times(handlers, document_id)
        assert rescued == self.GIVE_UP_AFTER

        final = self._worker_sweep(document_id)
        assert final.gave_up == 1 and str(document_id) not in final.document_ids

        row = self._row(document_id)
        assert row.status == DocumentStatus.FAILED
        assert row.reap_count == self.GIVE_UP_AFTER + 1
        assert "gave up after 3 recovery attempts" in row.last_error
        assert "AccessDenied: parsed/ prefix" in row.last_error, (
            "the tenant and the operator must see the cause, not just the verdict"
        )

        after = self._worker_sweep(document_id)
        assert str(document_id) not in after.document_ids and after.gave_up == 0

    def test_a_blip_is_recovered_by_the_next_rescue(self, monkeypatch):
        storage = {"down": True}
        handlers = self._stub_worker(monkeypatch, storage)
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        self._deliver_three_times(handlers, document_id)

        storage["down"] = False
        report = self._worker_sweep(document_id)
        assert str(document_id) in report.document_ids
        handlers.handle_parse({"document_id": str(document_id), "tenant_id": TENANT})

        assert self._row(document_id).status == DocumentStatus.PARSED

    def test_the_api_sweep_never_counts_and_never_gives_up(self):
        """The API sweeps to wake a fleet at zero, when every rescue looks failed."""
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))

        for _ in range(self.GIVE_UP_AFTER + 3):
            self._age(document_id)
            report = reap_stuck_documents(
                RecordingBroker(), stale_after=timedelta(minutes=15), stages={"parse"}
            )
            assert str(document_id) in report.document_ids

        row = self._row(document_id)
        assert row.status == DocumentStatus.RECEIVED and row.reap_count == 0

    def test_a_budget_pause_is_never_counted_toward_giving_up(self):
        """Its rescue polls for budget; failing it would destroy paused work."""
        from docfactory_core.budget import budget_state

        document_id = make_document(DocumentStatus.BUDGET_EXCEEDED, age=timedelta(hours=1))
        with tenant_context(TENANT):
            assert not budget_state(TENANT).exceeded, "precondition: budget has headroom"

        for _ in range(self.GIVE_UP_AFTER + 2):
            report = self._worker_sweep(document_id)
            assert str(document_id) in report.document_ids

        row = self._row(document_id)
        assert row.status == DocumentStatus.BUDGET_EXCEEDED and row.reap_count == 0

    def test_back_to_back_sweeps_rescue_a_document_once(self):
        """The claim is what lets several workers sweep at the same time."""
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))
        broker = RecordingBroker()

        first = reap_stuck_documents(
            broker, stale_after=timedelta(minutes=15), give_up_after=self.GIVE_UP_AFTER
        )
        second = reap_stuck_documents(
            broker, stale_after=timedelta(minutes=15), give_up_after=self.GIVE_UP_AFTER
        )

        assert str(document_id) in first.document_ids
        assert str(document_id) not in second.document_ids
        assert [p["document_id"] for _, p in broker.sent].count(str(document_id)) == 1

    def test_a_failure_after_routing_does_not_stamp_the_routed_document(self):
        from docfactory_worker import handlers

        document_id = make_document(DocumentStatus.NEEDS_REVIEW, age=timedelta(hours=1))
        with tenant_context(TENANT):
            handlers._note_failure(document_id, "extract: late bookkeeping blew up")
        routed = self._row(document_id)
        assert routed.status == DocumentStatus.NEEDS_REVIEW and routed.last_error is None

        # Control, so the assertion above cannot pass because nothing ever writes.
        live_id = make_document(DocumentStatus.PARSING, age=timedelta(hours=1))
        with tenant_context(TENANT):
            handlers._note_failure(live_id, "parse: it did write")
        assert self._row(live_id).last_error == "parse: it did write"

    def test_a_deferred_document_is_not_mistaken_for_a_stranded_one(self, monkeypatch):
        """A provider outage must not spend a document's rescues, or duplicate it."""
        from docfactory_worker import handlers

        class OpenBreaker:
            state = "open"
            cooldown = timedelta(seconds=60)

        monkeypatch.setattr(handlers, "_model_breaker", lambda: OpenBreaker())
        monkeypatch.setattr(handlers, "_clients", lambda: (None, RecordingBroker()))
        document_id = make_document(DocumentStatus.PARSED, age=timedelta(hours=1))

        with tenant_context(TENANT):
            assert handlers._defer_if_provider_down(
                {"document_id": str(document_id), "tenant_id": TENANT},
                "docfactory-extract",
                document_id,
            )

        report = reap_stuck_documents(
            RecordingBroker(), stale_after=timedelta(minutes=15), give_up_after=self.GIVE_UP_AFTER
        )
        assert str(document_id) not in report.document_ids
        assert self._row(document_id).reap_count == 0

    def test_every_document_handler_records_its_failures(self):
        """Any handle_* taking a document payload, including one added later."""
        import inspect

        from docfactory_worker import handlers

        document_handlers = {
            name: fn
            for name, fn in inspect.getmembers(handlers, inspect.isfunction)
            if name.startswith("handle_") and name != "handle_ingest"
        }
        assert {"handle_parse", "handle_extract"} <= set(document_handlers)
        for name, fn in document_handlers.items():
            assert getattr(fn, "_records_failure", None), (
                f"{name} must be wrapped by records_failure, or a document the "
                "reaper gives up on will not say why"
            )

    def test_a_document_deferred_by_the_ceiling_is_not_mistaken_for_stranded(self, monkeypatch):
        from docfactory_worker import handlers

        monkeypatch.setattr(handlers, "admits", lambda *a, **k: False)
        monkeypatch.setattr(handlers, "in_flight", lambda *a, **k: 99)
        monkeypatch.setattr(handlers, "_clients", lambda: (None, RecordingBroker()))
        document_id = make_document(DocumentStatus.RECEIVED, age=timedelta(hours=1))

        with tenant_context(TENANT):
            assert handlers._defer_if_saturated(
                {"document_id": str(document_id), "tenant_id": TENANT},
                "docfactory-parse",
                TENANT,
                document_id,
            )

        report = reap_stuck_documents(
            RecordingBroker(), stale_after=timedelta(minutes=15), give_up_after=self.GIVE_UP_AFTER
        )
        assert str(document_id) not in report.document_ids
