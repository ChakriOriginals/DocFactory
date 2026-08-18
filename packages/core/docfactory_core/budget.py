"""Per-tenant budget caps.

Spend is the sum of `cost_usd` across a tenant's extractions — the column has
existed since Phase 1 and is finally populated here. Cost is a flat price per
model call this phase; real token-based metering is Phase 4, and the shape of
this module does not change when it arrives, only where the number comes from.

The cap is checked *before* the model call, not after, because the point is to
not spend the money. A tenant over cap pauses: its documents stop at a
`budget_exceeded` state that says so and becomes processable again when the
cap is raised. Nothing crashes and no message is lost.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import Extraction, Tenant, TenantStatus

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


def call_cost_usd() -> Decimal:
    """Price of one model call. Flat this phase; metered in Phase 4."""
    return Decimal(str(get_settings().cost_per_extraction_usd))


def budget_state(tenant_id: str) -> BudgetState:
    """Spend against cap for a tenant.

    The spend query is deliberately unscoped by tenant in its WHERE clause —
    RLS supplies the filter. If the policy were ever dropped this would read
    another tenant's spend, so the isolation suite asserts it stays correct.
    """
    with session_scope(tenant_id) as session:
        spent = session.scalar(select(func.coalesce(func.sum(Extraction.cost_usd), 0)))
        tenant = session.get(Tenant, tenant_id)
        budget = Decimal(str(tenant.budget_usd)) if tenant else Decimal("0")
    spent = Decimal(str(spent or 0))
    return BudgetState(
        tenant_id=tenant_id,
        spent_usd=spent,
        budget_usd=budget,
        exceeded=spent >= budget,
    )


def check_budget(tenant_id: str) -> BudgetState:
    """Evaluate the cap and flip the tenant's status to match.

    Called before each model call. Pausing is recorded on the tenant so the state
    is visible to an operator, not only inferable from stalled documents.
    """
    state = budget_state(tenant_id)
    desired = TenantStatus.PAUSED if state.exceeded else TenantStatus.ACTIVE
    with session_scope(tenant_id, require_tenant=False) as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is not None and tenant.status != desired:
            # tenants is owner-writable only, so this may be refused for the
            # app role; the document-level state is the authoritative signal.
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
    return state
