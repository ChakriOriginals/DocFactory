"""Backpressure: rate limits, in-flight ceilings, and fairness under a flood.

Degrading gracefully means something says no, clearly, to the right party. The
tests here are about *who* gets told no: a tenant over its rate gets a 429 with
a Retry-After, a tenant hogging the pipeline gets its messages deferred, and a
quiet tenant next door keeps being served throughout.
"""

import json
import socket
import uuid
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from docfactory_core.backpressure import IN_FLIGHT_STATUSES, admits, check_rate_limit, in_flight
from docfactory_core.config import get_settings
from docfactory_core.db import admin_session_scope, session_scope, tenant_context
from docfactory_core.models import Document, DocumentStatus, Tenant
from sqlalchemy import delete, text

pytestmark = pytest.mark.integration

NOISY = "flood-tenant"
QUIET = "quiet-tenant"


def _stack_or_skip():
    settings = get_settings()
    parsed = urlparse(settings.s3_endpoint_url or "")
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            pass
    except OSError:
        pytest.skip("compose stack is not running")
    try:
        with admin_session_scope() as session:
            session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
    except Exception:
        pytest.skip("postgres is not migrated/reachable")
    return settings


def _postgres_or_skip():
    """Postgres alone, for tests that stub the broker and need no object store.

    `_stack_or_skip` also demands MinIO and ElasticMQ, which CI deliberately
    does not run — see the Tests step in .github/workflows/deploy.yml. Gating a
    database-only test behind that check makes it skip in CI and guard nothing,
    which is worse than not writing it: the run stays green and nobody learns
    the test is absent.
    """
    try:
        with admin_session_scope() as session:
            session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
    except Exception:
        pytest.skip("postgres is not migrated/reachable")
    return get_settings()


def _provision_two_tenants():
    with admin_session_scope() as session:
        for tenant_id in (NOISY, QUIET):
            session.merge(Tenant(id=tenant_id, name=tenant_id, budget_usd=Decimal("100")))
        session.flush()
        session.execute(
            text("DELETE FROM tenant_rate_limits WHERE tenant_id = ANY(:ids)"),
            {"ids": [NOISY, QUIET]},
        )
    yield NOISY, QUIET
    for tenant_id in (NOISY, QUIET):
        with tenant_context(tenant_id), session_scope() as session:
            session.execute(delete(Document).where(Document.tenant_id == tenant_id))
    with admin_session_scope() as session:
        session.execute(
            text("DELETE FROM tenant_rate_limits WHERE tenant_id = ANY(:ids)"),
            {"ids": [NOISY, QUIET]},
        )
        session.execute(text("DELETE FROM tenants WHERE id = ANY(:ids)"), {"ids": [NOISY, QUIET]})


@pytest.fixture
def two_tenants():
    """Two tenants, with the full compose stack required."""
    _stack_or_skip()
    yield from _provision_two_tenants()


@pytest.fixture
def db_tenants():
    """Two tenants, requiring Postgres only — so the test runs in CI."""
    _postgres_or_skip()
    yield from _provision_two_tenants()


def _flood(tenant_id: str, count: int, status=DocumentStatus.EXTRACTING) -> None:
    """Put `count` documents of a tenant in flight."""
    with tenant_context(tenant_id), session_scope() as session:
        for _ in range(count):
            session.add(
                Document(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    s3_key=f"{tenant_id}/incoming/{uuid.uuid4().hex}.pdf",
                    sha256=uuid.uuid4().hex * 2,
                    status=status,
                )
            )


class TestPerTenantRateLimiting:
    def test_a_burst_is_allowed_then_refused_with_a_retry_after(self, two_tenants):
        burst = get_settings().rate_limit_burst
        allowed = sum(check_rate_limit(NOISY).allowed for _ in range(burst + 10))
        assert allowed <= burst, "the bucket handed out more than its burst"
        assert allowed >= burst - 1  # refill during the loop can add at most a token

        refusal = check_rate_limit(NOISY)
        assert refusal.allowed is False
        assert refusal.retry_after_seconds > 0, "a 429 must say when to come back"

    def test_one_tenant_s_flood_does_not_spend_another_s_tokens(self, two_tenants):
        for _ in range(get_settings().rate_limit_burst + 20):
            check_rate_limit(NOISY)
        assert check_rate_limit(QUIET).allowed is True, "buckets must be per tenant"

    def test_the_bucket_refills_over_time(self, two_tenants):
        settings = get_settings()
        for _ in range(settings.rate_limit_burst + 5):
            check_rate_limit(NOISY)
        assert check_rate_limit(NOISY).allowed is False

        # Rewind the refill clock rather than sleeping: the arithmetic under
        # test is "tokens accrue with elapsed time", and waiting real seconds
        # would only make the suite slower.
        with admin_session_scope() as session:
            session.execute(
                text(
                    "UPDATE tenant_rate_limits SET refilled_at = now() - interval '1 minute' "
                    "WHERE tenant_id = :t"
                ),
                {"t": NOISY},
            )
        assert check_rate_limit(NOISY).allowed is True

    def test_the_api_returns_429_with_a_retry_after_header(self, two_tenants):
        """The contract a client codes against."""
        from conftest import authenticated_client

        settings = get_settings()
        with admin_session_scope() as session:
            session.execute(
                text(
                    "INSERT INTO tenant_rate_limits (tenant_id, tokens, refilled_at) "
                    "VALUES (:t, 0, now()) ON CONFLICT (tenant_id) DO UPDATE "
                    "SET tokens = 0, refilled_at = now()"
                ),
                {"t": settings.default_tenant_id},
            )
        try:
            with authenticated_client() as client:
                response = client.post(
                    "/documents",
                    files={"file": ("x.pdf", b"%PDF-1.7\nrate limited", "application/pdf")},
                )
            assert response.status_code == 429
            assert int(response.headers["retry-after"]) >= 1
            assert "rate limit exceeded" in response.json()["detail"]
        finally:
            with admin_session_scope() as session:
                session.execute(
                    text("DELETE FROM tenant_rate_limits WHERE tenant_id = :t"),
                    {"t": settings.default_tenant_id},
                )


class TestFairnessUnderAFlood:
    def test_a_tenant_over_its_ceiling_stops_being_admitted(self, two_tenants):
        limit = get_settings().max_in_flight_per_tenant
        _flood(NOISY, limit + 5)
        assert in_flight(NOISY) >= limit
        assert admits(NOISY) is False

    def test_a_quiet_tenant_is_still_served_while_another_floods(self, two_tenants):
        """The whole point: one tenant's flood must not starve another."""
        _flood(NOISY, get_settings().max_in_flight_per_tenant + 50)
        _flood(QUIET, 1)

        assert admits(NOISY) is False
        assert admits(QUIET) is True, "a flooding neighbour must not block this tenant"

    def test_the_ceiling_counts_only_this_tenant_s_work(self, two_tenants):
        _flood(NOISY, get_settings().max_in_flight_per_tenant + 5)
        assert in_flight(QUIET) == 0, "in-flight must be per tenant, not global"

    def test_the_document_being_considered_is_not_counted_against_itself(self, two_tenants):
        """Otherwise a ceiling of one could never admit anything."""
        with tenant_context(NOISY), session_scope() as session:
            doc = Document(
                id=uuid.uuid4(),
                tenant_id=NOISY,
                s3_key=f"{NOISY}/incoming/self.pdf",
                sha256=uuid.uuid4().hex * 2,
                status=DocumentStatus.PARSED,
            )
            session.add(doc)
            document_id = doc.id
        _flood(NOISY, get_settings().max_in_flight_per_tenant - 1)
        assert admits(NOISY, exclude_document=str(document_id)) is True

    def test_terminal_documents_do_not_hold_capacity(self, two_tenants):
        _flood(NOISY, 5, status=DocumentStatus.APPROVED)
        _flood(NOISY, 5, status=DocumentStatus.NEEDS_OCR)
        assert in_flight(NOISY) == 0
        assert DocumentStatus.APPROVED not in IN_FLIGHT_STATUSES
        # needs_ocr is terminal and expected — it must not look like backlog.
        assert DocumentStatus.NEEDS_OCR not in IN_FLIGHT_STATUSES


class TestDeferralIsNotFailure:
    def test_a_deferred_message_is_requeued_rather_than_left_to_redeliver(self, two_tenants):
        """A busy tenant must never fill the DLQ with documents that were fine.

        Deferral re-sends the message and acknowledges the original, so the
        redrive counter never advances — the DLQ stays for poison messages.
        """
        from docfactory_worker import handlers

        _flood(NOISY, get_settings().max_in_flight_per_tenant + 2)
        sent: list[tuple[str, dict, int]] = []

        class RecordingBroker:
            def send(self, queue, payload, *, delay_seconds=0):
                sent.append((queue, payload, delay_seconds))
                return "message-id"

        original = handlers._clients
        handlers._clients = lambda: (None, RecordingBroker())
        try:
            document_id = uuid.uuid4()
            deferred = handlers._defer_if_saturated(
                {"document_id": str(document_id), "tenant_id": NOISY},
                "docfactory-parse",
                NOISY,
                document_id,
            )
        finally:
            handlers._clients = original

        assert deferred is True
        assert len(sent) == 1
        queue, payload, delay = sent[0]
        assert queue == "docfactory-parse"
        assert payload["document_id"] == str(document_id)
        assert delay == get_settings().defer_seconds, "a deferral must wait before retrying"

    def test_a_tenant_under_the_ceiling_is_processed_immediately(self, two_tenants):
        from docfactory_worker import handlers

        _flood(QUIET, 1)
        assert (
            handlers._defer_if_saturated(
                {"document_id": str(uuid.uuid4())}, "docfactory-parse", QUIET, uuid.uuid4()
            )
            is False
        )


class TestTheDeferralIsBounded:
    """A busy tenant must not be able to stall itself forever.

    The gate runs before the status transition and the ceiling counts queued
    statuses, so past the ceiling every document is deferred on account of the
    others and none reaches the write that would release the rest. Deferral
    re-sends and acknowledges, so the receive count resets and the DLQ never
    catches it. Nothing escalates; nothing finishes. These tests are the exit.
    """

    @staticmethod
    def _recording_broker():
        """Record the serialised body, never the caller's dict.

        Storing the dict lets the next call mutate a message already "sent" —
        the handler updates the counter in place — so the recording rewrites
        its own history and every assertion about it is meaningless. Keeping
        the JSON is also what SQS actually does, which is the property under
        test: a counter that does not survive serialisation is not a bound.
        """
        sent: list[tuple[str, str, int]] = []

        class RecordingBroker:
            def send(self, queue, payload, *, delay_seconds=0):
                sent.append((queue, json.dumps(payload), delay_seconds))
                return "message-id"

        return sent, RecordingBroker()

    @staticmethod
    def _next_delivery(sent):
        """The message the worker would receive next, deserialised afresh."""
        return json.loads(sent[-1][1])

    def test_a_document_deferred_repeatedly_is_finally_admitted(self, db_tenants):
        """Replay the re-send loop and require it to terminate.

        Saturate with RECEIVED rather than the helper's default EXTRACTING:
        `received` is a *queued* status, and queued work is what makes the
        deadlock circular — those documents are waiting for the very gate whose
        decision they are counted in. Flooding with an active status would still
        exercise the bound, but it would not be the situation that stalls.
        """
        from docfactory_worker import handlers

        _flood(
            NOISY,
            get_settings().max_in_flight_per_tenant + 5,
            status=DocumentStatus.RECEIVED,
        )
        bound = get_settings().max_defer_attempts
        sent, broker = self._recording_broker()

        original = handlers._clients
        handlers._clients = lambda: (None, broker)
        try:
            document_id = uuid.uuid4()
            payload = {"document_id": str(document_id), "tenant_id": NOISY}
            deferrals = 0
            for _ in range(bound + 1):
                if not handlers._defer_if_saturated(
                    payload, "docfactory-parse", NOISY, document_id
                ):
                    break
                deferrals += 1
                # The worker never sees the caller's dict again: the next
                # delivery is the message that was actually put on the queue.
                payload = self._next_delivery(sent)
            else:
                pytest.fail(f"still deferring after {bound + 1} rounds — the ceiling livelocks")
        finally:
            handlers._clients = original

        assert deferrals == bound, "the bound must be spent, not skipped"
        # Tie the admission to the counter. Asserting the tenant is still over
        # the ceiling would pass no matter what the code did — the fixture put
        # it there and nothing in this test takes it back down.
        assert payload[handlers.DEFER_KEY] == bound, (
            "the admitted message must be the one carrying the exhausted budget"
        )
        assert admits(NOISY, exclude_document=str(document_id)) is False, (
            "admission must be the bound giving way, not capacity reappearing"
        )

    def test_the_attempt_counter_rides_the_message(self, db_tenants):
        """A counter that resets on re-send is not a bound.

        This is precisely how the DLQ escape works: re-sending makes a new
        message, so ApproximateReceiveCount starts again at zero. A deferral
        counter held anywhere but the payload would reset the same way, and the
        loop would run forever while every individual call looked bounded.
        """
        from docfactory_worker import handlers

        _flood(NOISY, get_settings().max_in_flight_per_tenant + 5)
        sent, broker = self._recording_broker()

        original = handlers._clients
        handlers._clients = lambda: (None, broker)
        try:
            document_id = uuid.uuid4()
            payload = {"document_id": str(document_id), "tenant_id": NOISY}
            for _ in range(3):
                handlers._defer_if_saturated(payload, "docfactory-parse", NOISY, document_id)
                payload = self._next_delivery(sent)
        finally:
            handlers._clients = original

        assert [json.loads(body)[handlers.DEFER_KEY] for _, body, _ in sent] == [1, 2, 3]

    def test_the_extract_gate_is_bounded_too(self, db_tenants):
        """Both gates defer, so both need the bound.

        handle_extract calls the same helper with the extract queue. A bound
        that held only on the parse path would move the stall one stage down
        rather than remove it.
        """
        from docfactory_worker import handlers

        _flood(
            NOISY,
            get_settings().max_in_flight_per_tenant + 5,
            status=DocumentStatus.PARSED,
        )
        bound = get_settings().max_defer_attempts
        sent, broker = self._recording_broker()

        original = handlers._clients
        handlers._clients = lambda: (None, broker)
        try:
            document_id = uuid.uuid4()
            payload = {"document_id": str(document_id), "tenant_id": NOISY}
            deferrals = 0
            for _ in range(bound + 1):
                if not handlers._defer_if_saturated(
                    payload, "docfactory-extract", NOISY, document_id
                ):
                    break
                deferrals += 1
                payload = self._next_delivery(sent)
            else:
                pytest.fail(f"extract still deferring after {bound + 1} rounds")
        finally:
            handlers._clients = original

        assert deferrals == bound
        assert {queue for queue, _, _ in sent} == {"docfactory-extract"}

    def test_a_fresh_message_is_still_deferred_while_over_the_ceiling(self, db_tenants):
        """The bound must not quietly turn the ceiling off."""
        from docfactory_worker import handlers

        _flood(NOISY, get_settings().max_in_flight_per_tenant + 5)
        _, broker = self._recording_broker()

        original = handlers._clients
        handlers._clients = lambda: (None, broker)
        try:
            document_id = uuid.uuid4()
            assert (
                handlers._defer_if_saturated(
                    {"document_id": str(document_id), "tenant_id": NOISY},
                    "docfactory-parse",
                    NOISY,
                    document_id,
                )
                is True
            )
        finally:
            handlers._clients = original
