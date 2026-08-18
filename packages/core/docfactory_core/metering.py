"""Token metering: what each call cost, and what a document costs.

Every model call writes a `usage_events` row carrying the provider's own token
counts priced by `config/model_pricing.json`. The unit-cost rollup is then a
query over those rows rather than an estimate — "$0.0X per invoice" is
arithmetic on recorded usage, and can be broken down by document type and by
model tier because both are on the event.

What the number means in mock mode: the token counts are real (derived from the
actual prompt and the actual response) and the prices are real, but the model
is a stand-in. It is an honest unit cost for the pipeline's *shape* — how many
calls, how much prompt, how much output — not a measurement of a real model's
token efficiency. An `anthropic`-mode run replaces the shape with the truth.
"""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text

from docfactory_core.db import session_scope
from docfactory_core.models import UsageEvent
from docfactory_core.pricing import call_cost_usd, tier_of

# Why a call happened. Escalations are separated so the routing cost delta can
# be read straight off the rollup.
PURPOSE_EXTRACT = "extract"
PURPOSE_ESCALATION = "escalation"


def record_usage(
    *,
    tenant_id: str,
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    document_id=None,
    extraction_id=None,
    pipeline_slug: str | None = None,
    latency_ms: int | None = None,
    session=None,
) -> Decimal:
    """Price one call and persist it. Returns the cost."""
    cost = call_cost_usd(model, input_tokens, output_tokens)
    event = UsageEvent(
        tenant_id=tenant_id,
        document_id=document_id,
        extraction_id=extraction_id,
        pipeline_slug=pipeline_slug,
        model=model,
        model_tier=tier_of(model),
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
    )
    if session is not None:
        session.add(event)
        return cost
    with session_scope(tenant_id) as own:
        own.add(event)
    return cost


@dataclass(frozen=True)
class CostRow:
    pipeline_slug: str
    model_tier: str
    model: str
    calls: int
    documents: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class UnitCost:
    pipeline_slug: str
    documents: int
    calls: int
    cost_usd: Decimal
    by_tier: dict[str, Decimal]
    escalation_rate: float

    @property
    def cost_per_document(self) -> Decimal:
        if not self.documents:
            return Decimal("0")
        return (self.cost_usd / self.documents).quantize(Decimal("0.000001"))


_ROLLUP = text(
    "SELECT pipeline_slug, model_tier, model, COUNT(*) AS calls, "
    "       COUNT(DISTINCT document_id) AS documents, "
    "       COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0), "
    "       COALESCE(SUM(cost_usd), 0) "
    "FROM usage_events "
    "GROUP BY pipeline_slug, model_tier, model "
    "ORDER BY pipeline_slug, model_tier, model"
)

# Per pipeline: documents touched, calls made, spend, and how many of those
# calls were escalations (the routing story's denominator).
_PER_PIPELINE = text(
    "SELECT pipeline_slug, COUNT(DISTINCT document_id) AS documents, COUNT(*) AS calls, "
    "       COALESCE(SUM(cost_usd), 0) AS cost, "
    "       COUNT(*) FILTER (WHERE purpose = :escalation) AS escalations "
    "FROM usage_events GROUP BY pipeline_slug ORDER BY pipeline_slug"
)


def cost_rows(tenant_id: str) -> list[CostRow]:
    """Spend broken down by pipeline, tier and model.

    The query carries no tenant filter — RLS supplies it, the same discipline
    the rest of the read paths follow.
    """
    with session_scope(tenant_id) as session:
        rows = session.execute(_ROLLUP).all()
    return [
        CostRow(
            pipeline_slug=row[0] or "unknown",
            model_tier=row[1],
            model=row[2],
            calls=int(row[3]),
            documents=int(row[4]),
            input_tokens=int(row[5]),
            output_tokens=int(row[6]),
            cost_usd=Decimal(str(row[7])),
        )
        for row in rows
    ]


def unit_costs(tenant_id: str) -> list[UnitCost]:
    """Cost per document, per document type."""
    rows = cost_rows(tenant_id)
    by_pipeline: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        by_pipeline.setdefault(row.pipeline_slug, {}).setdefault(row.model_tier, Decimal("0"))
        by_pipeline[row.pipeline_slug][row.model_tier] += row.cost_usd

    with session_scope(tenant_id) as session:
        totals = session.execute(_PER_PIPELINE, {"escalation": PURPOSE_ESCALATION}).all()

    out = []
    for slug, documents, calls, cost, escalations in totals:
        slug = slug or "unknown"
        out.append(
            UnitCost(
                pipeline_slug=slug,
                documents=int(documents),
                calls=int(calls),
                cost_usd=Decimal(str(cost)),
                by_tier=by_pipeline.get(slug, {}),
                escalation_rate=(int(escalations) / int(documents)) if documents else 0.0,
            )
        )
    return out
