"""Per-tenant budget caps, enforced transactionally.

The cap is checked *before* the model call, not after, because the point is to
not spend the money. A tenant over cap pauses: its documents stop at a
`budget_exceeded` state that says so, and become processable again when the
cap is raised. Nothing crashes and no message is lost.

**Why a counter row and not `SUM(cost_usd)`.** Phase 3 summed the extractions
table and compared the total to the cap. That check is not atomic with the
spend it authorizes: two workers can both read a spend below the cap and both
charge, and the cap overshoots by one call per worker. The failure needs
concurrency to appear, which is exactly the kind of bug that surfaces in
production and not in a test.

The fix is to make the check and the charge one statement. `tenant_spend` holds
the running total; charging is a single conditional UPDATE whose WHERE clause
carries the cap. Postgres takes a row lock for the update and re-evaluates that
predicate after acquiring it, so a second worker arriving concurrently sees the
first worker's charge and fails the predicate rather than racing past it.

**Reserve, then settle.** The cost of a call is not known until it returns, so
the reservation charges the *worst case* the request can produce (its input
tokens plus its hard `max_tokens` ceiling, priced by the model's rate) and
settles to the actual cost afterwards. The cap therefore cannot be exceeded
even by a single call. A worker that dies between reserve and settle leaves the
worst case charged, which over-counts — the safe direction — and is visible as
`unsettled_calls` rather than silently absorbed.

`usage_events` remains the audit trail; this counter is the enforcement point,
and a test asserts the two agree.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text

from docfactory_core.db import session_scope
from docfactory_core.models import Tenant, TenantStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetState:
    tenant_id: str
    spent_usd: Decimal
    budget_usd: Decimal
    exceeded: bool

    @property
    def remaining_usd(self) -> Decimal:
        return max(self.budget_usd - self.spent_usd, Decimal("0"))


@dataclass(frozen=True)
class Reservation:
    """A granted claim on a tenant's remaining budget, awaiting settlement."""

    tenant_id: str
    reserved_usd: Decimal
    state: BudgetState

    @property
    def granted(self) -> bool:
        return True


_ENSURE_ROW = text(
    "INSERT INTO tenant_spend (tenant_id, spent_usd) VALUES (:tenant, 0) "
    "ON CONFLICT (tenant_id) DO NOTHING"
)

# One statement: the cap lives in the WHERE clause, so the check and the charge
# cannot be separated by another worker. Under READ COMMITTED, a concurrent
# updater blocks on the row lock and then re-evaluates this predicate against
# the committed row — which is precisely the serialization the old
# read-then-write version lacked.
_CHARGE = text(
    "UPDATE tenant_spend s "
    "SET spent_usd = s.spent_usd + :amount, "
    "    unsettled_calls = s.unsettled_calls + 1, "
    "    updated_at = now() "
    "FROM tenants t "
    "WHERE t.id = s.tenant_id "
    "  AND s.tenant_id = :tenant "
    "  AND s.spent_usd + :amount <= t.budget_usd "
    "RETURNING s.spent_usd, t.budget_usd"
)

_SETTLE = text(
    "UPDATE tenant_spend s "
    "SET spent_usd = GREATEST(s.spent_usd + :delta, 0), "
    "    unsettled_calls = GREATEST(s.unsettled_calls - 1, 0), "
    "    updated_at = now() "
    "WHERE s.tenant_id = :tenant "
    "RETURNING s.spent_usd"
)

_READ = text(
    "SELECT COALESCE(s.spent_usd, 0), t.budget_usd "
    "FROM tenants t LEFT JOIN tenant_spend s ON s.tenant_id = t.id "
    "WHERE t.id = :tenant"
)


def budget_state(tenant_id: str) -> BudgetState:
    """Spend against cap for a tenant, from the counter."""
    with session_scope(tenant_id) as session:
        row = session.execute(_READ, {"tenant": tenant_id}).first()
    if row is None:
        return BudgetState(tenant_id, Decimal("0"), Decimal("0"), exceeded=True)
    spent, budget = Decimal(str(row[0])), Decimal(str(row[1]))
    return BudgetState(tenant_id, spent, budget, exceeded=spent >= budget)


def reserve(tenant_id: str, amount_usd: Decimal) -> Reservation | None:
    """Claim `amount_usd` against the cap, atomically. None when it would exceed.

    The caller must `settle()` the reservation once the true cost is known.
    """
    with session_scope(tenant_id) as session:
        session.execute(_ENSURE_ROW, {"tenant": tenant_id})
        row = session.execute(_CHARGE, {"tenant": tenant_id, "amount": amount_usd}).first()
        if row is None:
            return None
        spent, budget = Decimal(str(row[0])), Decimal(str(row[1]))
    return Reservation(
        tenant_id=tenant_id,
        reserved_usd=amount_usd,
        state=BudgetState(tenant_id, spent, budget, exceeded=spent >= budget),
    )


def settle(reservation: Reservation, actual_usd: Decimal) -> BudgetState:
    """Replace a reservation with the call's real cost.

    Unconditional by design: the money is already spent, so refusing to record
    it would understate the tenant's spend. Settling above the reservation can
    leave the counter marginally over cap, which the next reservation refuses.
    """
    delta = actual_usd - reservation.reserved_usd
    with session_scope(reservation.tenant_id) as session:
        row = session.execute(_SETTLE, {"tenant": reservation.tenant_id, "delta": delta}).first()
        spent = Decimal(str(row[0])) if row else reservation.state.spent_usd
    budget = reservation.state.budget_usd
    return BudgetState(reservation.tenant_id, spent, budget, exceeded=spent >= budget)


def release(reservation: Reservation) -> BudgetState:
    """Give a reservation back when the call never happened."""
    return settle(reservation, Decimal("0"))


def sync_tenant_status(tenant_id: str, state: BudgetState) -> None:
    """Mirror the cap state onto the tenant row so an operator can see it.

    Best-effort: `tenants` is owner-writable only, so the app role may be
    refused. The document-level state is the authoritative signal.
    """
    desired = TenantStatus.PAUSED if state.exceeded else TenantStatus.ACTIVE
    with session_scope(tenant_id, require_tenant=False) as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is not None and tenant.status != desired:
            try:
                tenant.status = desired
                session.flush()
            except Exception:
                session.rollback()
    if state.exceeded:
        log.warning(
            "tenant over budget",
            extra={
                "tenant_id": tenant_id,
                "spent_usd": str(state.spent_usd),
                "budget_usd": str(state.budget_usd),
            },
        )
