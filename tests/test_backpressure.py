"""Backpressure: rate limits, in-flight ceilings, and fairness under a flood.

Degrading gracefully means something says no, clearly, to the right party. The
tests here are about *who* gets told no: a tenant over its rate gets a 429 with
a Retry-After, a tenant hogging the pipeline gets its messages deferred, and a
quiet tenant next door keeps being served throughout.
"""

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


@pytest.fixture
def two_tenants():
    _stack_or_skip()
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
