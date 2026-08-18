"""The fitted confidence model, and routing decisions taken from it.

This is the serving half of the calibration study. The study fits weights and
writes `config/confidence_model_vN.json`; this module loads that file and
applies it, so the threshold is configuration rather than a constant and a
refit changes behaviour without a code change.

**Feature extraction lives here, not in the study.** The study imports it from
this module so that the vector fitted offline and the vector scored in the
pipeline are produced by the same code. If those two drifted, the threshold
would be measured in one feature space and applied in another — the failure is
silent, and every precision number would be quietly wrong.

Routing rule, stated explicitly: a field is auto-approved when its calibrated
probability is at or above the threshold. A *document* is auto-approved only
when every one of its fields is; a single field below threshold sends the
document to review, with that field named. The conservative direction is
deliberate — the cost of a missed error is a wrong invoice paid, while the
cost of a false alarm is one glance from a reviewer at exactly the cell that
looked wrong.
"""

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from docfactory_core.confidence import RULE_FIELDS
from docfactory_core.config import get_settings
from docfactory_core.schemas import SCALAR_FIELD_NAMES

# Feature names in fitted-weight order. Small and individually meaningful so a
# saved config can be read and argued with rather than merely applied.
FEATURES = (
    "implicating_rules_failed",  # how many failed rules touch this field
    "log_residual_magnitude",  # size of the worst arithmetic disagreement
    "groundedness",  # traceability to source text (1.0 when N/A)
    "shape_suspect",  # fragmented/truncated/implausible value
    "row_arithmetic_broken",  # a line row where qty x price != amount
    "needed_retry",  # extraction took a second attempt
    "date_corroboration",  # does this date appear on the page (2.3a)
)

SCORED_FIELDS = (*SCALAR_FIELD_NAMES, "line_items")

# Each arithmetic rule has its own residual, so a field is scored by the
# disagreement that actually implicates it rather than the loudest anywhere.
_RULE_RESIDUAL = {
    "line_items_sum_to_subtotal": "residual.line_items_vs_subtotal",
    "subtotal_plus_tax_equals_total": "residual.subtotal_plus_tax_vs_total",
    "tax_matches_rate": "residual.tax_vs_rate",
}

_DATE_FIELDS = ("invoice_date", "due_date")


def features_for(field: str, signals: dict) -> list[float]:
    """Feature vector for one field of one extraction.

    Shared by the calibration fit and the pipeline — see the module docstring.
    """
    failed = [
        key.removeprefix("rule.")
        for key, passed in signals.items()
        if key.startswith("rule.") and passed is False
    ]
    implicating = [rule for rule in failed if field in RULE_FIELDS.get(rule, ())]

    residual = 0.0
    scale = max(abs(float(signals.get("total") or 1.0)), 1.0)
    for rule in implicating:
        key = _RULE_RESIDUAL.get(rule)
        if key:
            residual = max(residual, abs(float(signals.get(key) or 0.0)))

    grounded = float(signals.get(f"groundedness.{field}", 1.0))
    if field == "line_items":
        grounded = float(signals.get("groundedness.line_items_min", 1.0))

    shape = 0.0
    if field == "vendor" and signals.get("vendor.looks_fragmented"):
        shape = 1.0
    if field == "total" and signals.get("nonpositive_total"):
        shape = 1.0

    rows_broken = (
        1.0 if (field == "line_items" and signals.get("line_items.inconsistent_rows")) else 0.0
    )

    # Dates carry their own corroboration; other fields have no opinion and
    # take 1.0, so the weight simply does not act on them.
    if field in _DATE_FIELDS:
        date_score = float(signals.get(f"date_corroboration.{field}", 1.0))
        term = signals.get("date_corroboration.payment_term")
        if term is not None:
            date_score = min(date_score, float(term))
    else:
        date_score = 1.0

    return [
        float(len(implicating)),
        float(math.log1p(residual / scale)),
        grounded,
        shape,
        rows_broken,
        1.0 if float(signals.get("attempts", 1)) > 1 else 0.0,
        date_score,
    ]


@dataclass(frozen=True)
class ConfidenceModel:
    version: int
    features: tuple[str, ...]
    weights: tuple[float, ...]
    bias: float
    mean: tuple[float, ...]
    std: tuple[float, ...]
    threshold: float
    source: str

    @classmethod
    def load(cls, path: str | Path) -> "ConfidenceModel":
        payload = json.loads(Path(path).read_text())
        features = tuple(payload["features"])
        if features != FEATURES:
            # A config fitted on a different feature set would be applied to
            # vectors it was never trained on; fail loudly rather than serve
            # silently-wrong probabilities.
            raise ValueError(
                f"config {path} was fitted on {features}, but this build produces {FEATURES}"
            )
        return cls(
            version=int(payload["schema_version"]),
            features=features,
            weights=tuple(float(w) for w in payload["weights"]),
            bias=float(payload["bias"]),
            mean=tuple(float(m) for m in payload["standardization"]["mean"]),
            std=tuple(float(s) for s in payload["standardization"]["std"]),
            threshold=float(payload["operating_point"]["threshold"]),
            source=str(path),
        )

    def probability(self, features: list[float]) -> float:
        z = self.bias
        for value, mean, std, weight in zip(
            features, self.mean, self.std, self.weights, strict=True
        ):
            z += weight * ((value - mean) / (std or 1e-9))
        return round(1.0 / (1.0 + math.exp(-z)), 6)


@dataclass(frozen=True)
class RoutingDecision:
    decision: str  # "approved" | "needs_review"
    doc_confidence: float
    field_confidence: dict[str, float]
    flagged_fields: tuple[str, ...]
    model_version: int
    threshold: float


def route_extraction(signals: dict, model: ConfidenceModel) -> RoutingDecision:
    """Score every field and decide whether the document can skip review."""
    field_confidence = {
        field: model.probability(features_for(field, signals)) for field in SCORED_FIELDS
    }
    flagged = tuple(field for field, score in field_confidence.items() if score < model.threshold)
    return RoutingDecision(
        decision="needs_review" if flagged else "approved",
        # Comparable to the threshold by construction: the document clears it
        # exactly when its weakest field does.
        doc_confidence=min(field_confidence.values()),
        field_confidence=field_confidence,
        flagged_fields=flagged,
        model_version=model.version,
        threshold=model.threshold,
    )


@lru_cache
def get_confidence_model() -> ConfidenceModel:
    """The configured model, loaded once per process."""
    return ConfidenceModel.load(get_settings().confidence_model_path)
