"""Backpressure: rate limits, in-flight ceilings, and fairness between tenants.

Under sustained load a system either degrades gracefully or falls over, and the
difference is whether anything says no. Three mechanisms, each answering a
different question:

*How fast may a tenant hand us work?* A token bucket per tenant, refilled by
elapsed time. Over the rate is a clean 429 with a Retry-After, never a silent
drop — the client can back off and retry, which is the whole point of telling
it. The bucket lives in Postgres because the API runs as more than one process,
and it is spent with the same single-statement conditional UPDATE the budget
cap uses: check and take cannot be separated by another request.

*How much of the pipeline may one tenant occupy?* An in-flight ceiling. Without
it a tenant that uploads ten thousand documents owns every worker until it is
done, and a second tenant's single document waits behind all of them. Over the
ceiling, the message is deferred back to the queue with a delay rather than
processed — the worker immediately picks up whoever is next, which is what
fairness means here.

*Is the system as a whole falling behind?* Queue depth, read from the broker.
It is exposed rather than acted on automatically: at this phase the honest use
is an operator signal and an autoscaling input, and inventing a throttle
policy on top of an un-load-tested system would be guessing.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import text

from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import DocumentStatus

log = logging.getLogger(__name__)

# Statuses that mean "the pipeline is currently working on this document".
IN_FLIGHT_STATUSES = (
    DocumentStatus.RECEIVED,
    DocumentStatus.PARSING,
    DocumentStatus.PARSED,
    DocumentStatus.EXTRACTING,
)


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    tokens_left: float
    retry_after_seconds: float


_ENSURE_BUCKET = text(
    "INSERT INTO tenant_rate_limits (tenant_id, tokens, refilled_at) "
    "VALUES (:tenant, :burst, now()) ON CONFLICT (tenant_id) DO NOTHING"
)

# Refill and spend in one statement. The bucket is refilled by however much
# wall-clock time has passed since it was last touched, capped at the burst
# size, and the token is only taken if one is there afterwards — so two
# concurrent requests cannot both spend the last token.
_SPEND = text(
    """
    UPDATE tenant_rate_limits SET
        tokens = LEAST(
            tokens + EXTRACT(EPOCH FROM (now() - refilled_at)) * :refill_per_second,
            :burst
        ) - 1,
        refilled_at = now()
    WHERE tenant_id = :tenant
      AND LEAST(
            tokens + EXTRACT(EPOCH FROM (now() - refilled_at)) * :refill_per_second,
            :burst
          ) >= 1
    RETURNING tokens
    """
)

_PEEK = text(
    "SELECT LEAST(tokens + EXTRACT(EPOCH FROM (now() - refilled_at)) * :refill_per_second, "
    ":burst) FROM tenant_rate_limits WHERE tenant_id = :tenant"
)


def check_rate_limit(tenant_id: str) -> RateDecision:
    """Take one token, or say how long to wait for the next one."""
    settings = get_settings()
    refill_per_second = settings.rate_limit_per_minute / 60.0
    params = {
        "tenant": tenant_id,
        "burst": float(settings.rate_limit_burst),
        "refill_per_second": refill_per_second,
    }
    with session_scope(tenant_id) as session:
        session.execute(_ENSURE_BUCKET, params)
        row = session.execute(_SPEND, params).first()
        if row is not None:
            return RateDecision(True, float(row[0]), 0.0)
        available = session.execute(_PEEK, params).scalar() or 0.0

    # How long until one whole token exists again.
    deficit = max(1.0 - float(available), 0.0)
    retry_after = deficit / refill_per_second if refill_per_second else 60.0
    log.info(
        "tenant rate limited",
        extra={"tenant_id": tenant_id, "retry_after_seconds": round(retry_after, 2)},
    )
    return RateDecision(False, float(available), round(retry_after, 2))


def in_flight(tenant_id: str) -> int:
    """Documents this tenant currently occupies pipeline capacity with."""
    with session_scope(tenant_id) as session:
        return int(
            session.execute(
                text("SELECT COUNT(*) FROM documents WHERE status = ANY(:statuses)"),
                {"statuses": list(IN_FLIGHT_STATUSES)},
            ).scalar()
            or 0
        )


def admits(tenant_id: str, *, exclude_document: str | None = None) -> bool:
    """Whether this tenant is under its in-flight ceiling.

    The document being considered is excluded from the count — it is already
    in flight by definition, and counting it would make a ceiling of one
    unreachable.
    """
    limit = get_settings().max_in_flight_per_tenant
    if limit <= 0:
        return True
    with session_scope(tenant_id) as session:
        count = int(
            session.execute(
                text(
                    "SELECT COUNT(*) FROM documents WHERE status = ANY(:statuses) "
                    "AND (CAST(:exclude AS uuid) IS NULL OR id <> CAST(:exclude AS uuid))"
                ),
                {"statuses": list(IN_FLIGHT_STATUSES), "exclude": exclude_document},
            ).scalar()
            or 0
        )
    return count < limit


def queue_depth(broker, queue: str) -> int:
    """Messages waiting on a queue — the autoscaling signal, exposed not acted on."""
    attributes = broker._sqs.get_queue_attributes(
        QueueUrl=broker.queue_url(queue),
        AttributeNames=["ApproximateNumberOfMessages"],
    )["Attributes"]
    return int(attributes.get("ApproximateNumberOfMessages", 0))
