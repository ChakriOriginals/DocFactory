"""Model routing: which model runs a document, and when to escalate.

The bulk of documents are easy, and paying frontier prices for all of them is
the whole cost problem. So a pipeline names a *tier* — "small" for the first
attempt, "frontier" for the escalation — and the deployment maps tiers to
concrete models in `config/model_routing.json`. Switching the cheap tier to a
different model is a config edit; nothing in the pipeline changes.

Escalation triggers come from a small allowlist and are *defensible*: the
document is re-run on the stronger model only when the deterministic checks say
something is wrong with the first answer — a validation rule failed, the
calibrated router flagged a field, or extraction failed outright. "Escalate
when unsure" is not a vibe here; it is the same confidence machinery that
decides human review.

A tenant authors the policy as part of its pipeline definition, so it is
untrusted input: unknown tiers and unknown triggers are rejected on write.
"""

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "model_routing.json"

# Reasons a document may be re-run on a stronger model. Anything else is a
# config error, not a silently ignored key.
ESCALATION_TRIGGERS = frozenset(
    {
        "validation_failed",  # a deterministic consistency rule did not hold
        "needs_review",  # the calibrated router flagged at least one field
        "extraction_failed",  # no usable record came back at all
    }
)


class RoutingConfigError(ValueError):
    """A routing policy is malformed. Raised on write, never at runtime."""


@dataclass(frozen=True)
class RoutingPolicy:
    primary_tier: str
    escalate_to: str | None = None
    escalate_on: tuple[str, ...] = ()

    @property
    def escalates(self) -> bool:
        return bool(self.escalate_to and self.escalate_on)


@lru_cache
def _config() -> dict:
    return json.loads(CONFIG_PATH.read_text())


@lru_cache
def tiers_for(provider: str) -> dict[str, str]:
    tiers = _config()["tiers"].get(provider)
    if not tiers:
        raise RoutingConfigError(
            f"no model tiers configured for provider {provider!r}; "
            f"known: {sorted(_config()['tiers'])}"
        )
    return tiers


def model_for_tier(provider: str, tier: str) -> str:
    """The concrete model id serving a tier for this provider."""
    try:
        return tiers_for(provider)[tier]
    except KeyError as exc:
        raise RoutingConfigError(
            f"provider {provider!r} has no {tier!r} tier; known: {sorted(tiers_for(provider))}"
        ) from exc


def default_policy() -> RoutingPolicy:
    return parse_policy(_config().get("default_policy", {}))


def parse_policy(config: object) -> RoutingPolicy:
    """Validate a tenant-supplied routing policy.

    Every tier named must exist for every configured provider — a policy that
    only works in mock mode would fail in production, which is the worst place
    to discover it.
    """
    if config is None:
        return RoutingPolicy(primary_tier="frontier")
    if not isinstance(config, dict):
        raise RoutingConfigError("model_routing must be an object")

    primary = str(config.get("primary_tier", "frontier"))
    escalate_to = config.get("escalate_to")
    escalate_to = str(escalate_to) if escalate_to else None
    triggers = tuple(str(t) for t in config.get("escalate_on", ()))

    unknown = sorted(set(triggers) - ESCALATION_TRIGGERS)
    if unknown:
        raise RoutingConfigError(
            f"unknown escalation trigger(s) {unknown}; allowed: {sorted(ESCALATION_TRIGGERS)}"
        )
    for tier in filter(None, (primary, escalate_to)):
        for provider in _config()["tiers"]:
            if tier not in tiers_for(provider):
                raise RoutingConfigError(
                    f"unknown model tier {tier!r} for provider {provider!r}; "
                    f"known: {sorted(tiers_for(provider))}"
                )
    if triggers and not escalate_to:
        raise RoutingConfigError("escalate_on is set but escalate_to names no tier")
    return RoutingPolicy(primary_tier=primary, escalate_to=escalate_to, escalate_on=triggers)


def escalation_trigger(
    policy: RoutingPolicy,
    *,
    extraction_failed: bool,
    validation_passed: bool | None,
    routing_decision: str | None,
) -> str | None:
    """The reason to escalate, or None to keep the first answer.

    Checked in severity order so the recorded reason is the most serious one.
    """
    if not policy.escalates:
        return None
    if extraction_failed and "extraction_failed" in policy.escalate_on:
        return "extraction_failed"
    if validation_passed is False and "validation_failed" in policy.escalate_on:
        return "validation_failed"
    if routing_decision == "needs_review" and "needs_review" in policy.escalate_on:
        return "needs_review"
    return None
