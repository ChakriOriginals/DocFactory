"""The transactional budget cap, proven against workers that actually race.

Phase 3 flagged this: the cap check was a SELECT and the spend was a later
UPDATE, so two workers could both read a spend below the cap and both charge.
A sequential test cannot see that — the bug only exists when two transactions
overlap — so these tests run real threads against real connections, released
together from a barrier.

The last test is the control: it reimplements the *old* read-then-write logic
and shows it overspending under exactly the same conditions. Without it, a
green suite would prove only that nothing raced, not that the fix is what
prevents the overspend.
"""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from urllib.parse import urlparse

import pytest
from docfactory_core.budget import budget_state, release, reserve, settle
from docfactory_core.config import get_settings
from docfactory_core.db import admin_session_scope, session_scope
from docfactory_core.models import Tenant
from sqlalchemy import text

pytestmark = pytest.mark.integration

TENANT = "budget-race-tenant"
CHARGE = Decimal("0.10")
BUDGET = Decimal("0.80")  # exactly 8 charges fit
WORKERS = 16


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def racing_tenant():
    settings = get_settings()
    if not settings.s3_endpoint_url or not _reachable(settings.s3_endpoint_url):
        pytest.skip("compose stack is not running")
    try:
        with admin_session_scope() as session:
            session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
    except Exception:
        pytest.skip("postgres is not migrated/reachable")

    with admin_session_scope() as session:
        session.merge(Tenant(id=TENANT, name="Budget race", budget_usd=BUDGET))
        session.flush()  # the spend row's FK needs the tenant to exist first
        session.execute(
            text(
                "INSERT INTO tenant_spend (tenant_id, spent_usd, unsettled_calls) "
                "VALUES (:t, 0, 0) ON CONFLICT (tenant_id) DO UPDATE "
                "SET spent_usd = 0, unsettled_calls = 0"
            ),
            {"t": TENANT},
        )
    yield TENANT
    with admin_session_scope() as session:
        session.execute(text("DELETE FROM tenant_spend WHERE tenant_id = :t"), {"t": TENANT})
        session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": TENANT})


def _race(work, workers: int = WORKERS):
    """Release `workers` threads at the same instant and collect their results."""
    barrier = threading.Barrier(workers)

    def run(index: int):
        barrier.wait(timeout=30)
        return work(index)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, range(workers)))


def _spent() -> Decimal:
    with admin_session_scope() as session:
        row = session.execute(
            text("SELECT spent_usd FROM tenant_spend WHERE tenant_id = :t"), {"t": TENANT}
        ).first()
    return Decimal(str(row[0]))


class TestTheCapHoldsUnderConcurrency:
    def test_exactly_the_affordable_number_of_calls_are_granted(self, racing_tenant):
        results = _race(lambda _: reserve(TENANT, CHARGE) is not None)
        granted = sum(results)

        assert granted == int(BUDGET / CHARGE), (
            f"{granted} of {WORKERS} concurrent workers were granted a charge; "
            f"the cap affords exactly {int(BUDGET / CHARGE)}"
        )
        assert _spent() == BUDGET
        assert _spent() <= BUDGET, "spend exceeded the cap under concurrency"

    def test_no_worker_sees_a_spend_above_the_cap(self, racing_tenant):
        states = _race(lambda _: reserve(TENANT, CHARGE))
        for reservation in filter(None, states):
            assert reservation.state.spent_usd <= BUDGET

    def test_settling_below_the_reservation_frees_the_difference(self, racing_tenant):
        """The reservation is the worst case; the real cost is usually smaller."""
        reservation = reserve(TENANT, CHARGE)
        assert reservation is not None
        assert _spent() == CHARGE

        settle(reservation, Decimal("0.02"))
        assert _spent() == Decimal("0.02")
        assert budget_state(TENANT).remaining_usd == BUDGET - Decimal("0.02")

    def test_a_released_reservation_costs_nothing(self, racing_tenant):
        reservation = reserve(TENANT, CHARGE)
        release(reservation)
        assert _spent() == Decimal("0")

    def test_concurrent_reserve_and_settle_leave_the_counter_exact(self, racing_tenant):
        """Reserve worst-case, settle to a tenth: the arithmetic must survive the race."""
        actual = Decimal("0.01")

        def work(_):
            reservation = reserve(TENANT, CHARGE)
            if reservation is None:
                return False
            settle(reservation, actual)
            return True

        granted = sum(_race(work))
        # Every worker that got in settled down to `actual`, so the final
        # counter is exactly what was really spent — no drift from the race.
        assert _spent() == actual * granted

    def test_the_unfixed_check_then_charge_overspends(self, racing_tenant):
        """The control: the Phase 3 logic, under the same race.

        SELECT the spend, decide, then UPDATE — the two statements the fix
        collapsed into one. If this does not overspend, the test harness is not
        actually racing and the tests above prove nothing.

        The gap between the two statements is widened with a short sleep so the
        control is deterministic rather than dependent on scheduling. That is
        honest: the sleep does not create the bug, it makes an existing window
        wide enough to observe every time. The fixed path needs no such help —
        its assertions hold whatever the interleaving, which is the difference.
        """

        def legacy(_):
            with session_scope(TENANT) as session:
                row = session.execute(
                    text(
                        "SELECT COALESCE(s.spent_usd, 0), t.budget_usd FROM tenants t "
                        "LEFT JOIN tenant_spend s ON s.tenant_id = t.id WHERE t.id = :t"
                    ),
                    {"t": TENANT},
                ).first()
                spent, budget = Decimal(str(row[0])), Decimal(str(row[1]))
                if spent + CHARGE > budget:
                    return False
            # ... another worker's charge lands in this gap ...
            time.sleep(0.05)
            with session_scope(TENANT) as session:
                session.execute(
                    text("UPDATE tenant_spend SET spent_usd = spent_usd + :c WHERE tenant_id = :t"),
                    {"c": CHARGE, "t": TENANT},
                )
            return True

        granted = sum(_race(legacy))
        assert granted > int(BUDGET / CHARGE), (
            "the unfixed logic did not overspend, so this suite is not racing — "
            "the fix's tests above would pass vacuously"
        )
        assert _spent() > BUDGET
