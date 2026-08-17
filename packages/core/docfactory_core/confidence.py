"""Deterministic confidence scoring over extraction signals.

Two numbers come out of here: a document-level score (is this extraction
trustworthy as a whole?) and a per-field score (which cells should a reviewer
look at first?). Both are pure functions of the extraction plus the
deterministic validation results — no model self-report, no network, so the
score is identical in mock and anthropic mode and is reproducible offline.

Three things about the design are deliberate:

1. **Rule -> field attribution.** Every validation rule declares the fields it
   implicates. A failed rule lowers exactly those fields, so the field sitting
   at the intersection of several failures scores lowest — crude fault
   localization, which is what a reviewer actually needs. `subtotal`
   participates in three rules and is therefore the most sharply penalized
   field when the arithmetic disagrees.

2. **The raw signal vector is persisted, not just the score.** Residual
   *magnitudes* are recorded alongside the pass/fail booleans (being off by
   two cents is not being off by two thousand). The calibration study can then
   refit weights offline against the golden set instead of reprocessing every
   document.

3. **THE WEIGHTS BELOW ARE UNCALIBRATED PRIORS.** They are picked by
   judgement, not fitted to data, so the absolute number is not yet meaningful
   — only the ordering is. Fitting them, and choosing any review threshold, is
   the calibration study's job. Nothing in this module routes, gates, or
   compares against a threshold; that would be acting on a number we have not
   yet earned.
"""

from dataclasses import dataclass, field
from decimal import Decimal

from docfactory_core.schemas import SCALAR_FIELD_NAMES, Invoice

# --- Uncalibrated penalty priors (see note 3 above) -------------------------

PENALTY_ARITHMETIC_RULE = 0.30  # totals that don't add up: strong signal
PENALTY_DATE_RULE = 0.15  # date ordering: weaker, often a real oddity
PENALTY_RETRY = 0.10  # needed a second attempt to satisfy the schema
PENALTY_VENDOR_FRAGMENTED = 0.10  # split-run text artifact in the vendor name
PENALTY_NONPOSITIVE_TOTAL = 0.25
PENALTY_SUSPECT_FIELD_SHAPE = 0.20  # per-field: empty-ish / implausible value

# Which fields each validation rule implicates. Failing the rule lowers
# confidence for exactly these fields.
RULE_FIELDS: dict[str, tuple[str, ...]] = {
    "line_items_sum_to_subtotal": ("subtotal", "line_items"),
    "subtotal_plus_tax_equals_total": ("subtotal", "tax", "total"),
    "tax_matches_rate": ("subtotal", "tax_rate", "tax"),
    "invoice_date_parses": ("invoice_date",),
    "due_date_not_before_invoice_date": ("due_date", "invoice_date"),
}

_DATE_RULES = frozenset({"invoice_date_parses", "due_date_not_before_invoice_date"})

# A vendor name is "fragmented" when at least this fraction of its tokens are
# bare single characters — the signature of small-caps rendering, which splits
# "Bauer Weinhold" into runs like "B W" / "auer einhold" (see docs/backlog.md).
# The observed second run sits exactly at 0.4, hence >= rather than >.
_FRAGMENT_TOKEN_RATIO = 0.4
_FRAGMENT_MIN_TOKENS = 4

# Per-line consistency: the generator enforces amount == quantity x unit_price,
# so a line that breaks it localizes the error to that row.
_CENT = Decimal("0.01")


@dataclass(frozen=True)
class FieldConfidence:
    name: str
    confidence: float
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfidenceReport:
    doc_confidence: float
    fields: dict[str, FieldConfidence] = field(default_factory=dict)
    # Raw inputs to the score, persisted so weights can be refit offline.
    signals: dict[str, object] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    def field_confidence(self, name: str) -> float | None:
        """Confidence for a stored field path, e.g. 'line_items.0.amount'.

        Line-item cells inherit their parent's score: the rules constrain the
        item list as a whole, not individual cells within it.
        """
        if name in self.fields:
            return self.fields[name].confidence
        if name.startswith("line_items"):
            entry = self.fields.get("line_items")
            return entry.confidence if entry else None
        return None


def score_extraction(
    invoice: Invoice, validation: dict[str, bool], *, attempts: int = 1
) -> ConfidenceReport:
    signals: dict[str, object] = {"attempts": attempts}
    doc_penalty = 0.0
    doc_reasons: list[str] = []
    field_penalties: dict[str, float] = dict.fromkeys((*SCALAR_FIELD_NAMES, "line_items"), 0.0)
    field_reasons: dict[str, list[str]] = {name: [] for name in field_penalties}

    # --- validation rules -> document and field penalties ---
    for rule, passed in validation.items():
        signals[f"rule.{rule}"] = passed
        if passed:
            continue
        weight = PENALTY_DATE_RULE if rule in _DATE_RULES else PENALTY_ARITHMETIC_RULE
        doc_penalty += weight
        doc_reasons.append(f"failed:{rule}")
        for name in RULE_FIELDS.get(rule, ()):
            if name in field_penalties:
                field_penalties[name] += weight
                field_reasons[name].append(f"failed:{rule}")

    # --- residual magnitudes: how badly, not just whether ---
    line_sum = sum((item.amount for item in invoice.line_items), Decimal("0"))
    signals["residual.line_items_vs_subtotal"] = _as_float(line_sum - invoice.subtotal)
    signals["residual.subtotal_plus_tax_vs_total"] = _as_float(
        invoice.subtotal + invoice.tax - invoice.total
    )
    signals["residual.tax_vs_rate"] = _as_float(invoice.subtotal * invoice.tax_rate - invoice.tax)
    signals["residual.due_minus_invoice_days"] = (invoice.due_date - invoice.invoice_date).days
    signals["line_item_count"] = len(invoice.line_items)
    signals["total"] = _as_float(invoice.total)

    # --- extraction-process signals ---
    if attempts > 1:
        doc_penalty += PENALTY_RETRY
        doc_reasons.append("needed_schema_retry")

    # --- per-field shape heuristics ---
    fragmented = looks_fragmented(invoice.vendor)
    signals["vendor.looks_fragmented"] = fragmented
    signals["vendor.token_count"] = len(invoice.vendor.split())
    if fragmented:
        doc_penalty += PENALTY_VENDOR_FRAGMENTED
        doc_reasons.append("vendor_looks_fragmented")
        field_penalties["vendor"] += PENALTY_SUSPECT_FIELD_SHAPE
        field_reasons["vendor"].append("looks_fragmented")

    nonpositive_total = invoice.total <= 0
    signals["nonpositive_total"] = nonpositive_total
    if nonpositive_total:
        doc_penalty += PENALTY_NONPOSITIVE_TOTAL
        doc_reasons.append("nonpositive_total")
        field_penalties["total"] += PENALTY_SUSPECT_FIELD_SHAPE
        field_reasons["total"].append("nonpositive")

    for name in ("vendor", "invoice_number"):
        if len(getattr(invoice, name).strip()) < 2:
            field_penalties[name] += PENALTY_SUSPECT_FIELD_SHAPE
            field_reasons[name].append("implausibly_short")
            signals[f"{name}.implausibly_short"] = True

    # Descriptions cannot be empty here (the schema enforces min_length=1), so
    # the reachable per-line signal is arithmetic: which row fails qty x price.
    inconsistent_lines = [
        index
        for index, item in enumerate(invoice.line_items)
        if abs(item.quantity * item.unit_price - item.amount) > _CENT
    ]
    signals["line_items.inconsistent_rows"] = inconsistent_lines
    if inconsistent_lines:
        field_penalties["line_items"] += PENALTY_SUSPECT_FIELD_SHAPE
        field_reasons["line_items"].append(f"row_arithmetic:{inconsistent_lines}")

    fields = {
        name: FieldConfidence(name, _clamp(1.0 - penalty), tuple(field_reasons[name]))
        for name, penalty in field_penalties.items()
    }
    return ConfidenceReport(
        doc_confidence=_clamp(1.0 - doc_penalty),
        fields=fields,
        signals=signals,
        reasons=tuple(doc_reasons),
    )


def failed_extraction_report(error: str | None) -> ConfidenceReport:
    """No usable extraction: zero confidence, and say why."""
    return ConfidenceReport(
        doc_confidence=0.0,
        signals={"extraction_failed": True, "error": error},
        reasons=("extraction_failed",),
    )


def looks_fragmented(value: str) -> bool:
    """True when a string reads as split character runs ('B W S & C . Kg a').

    Requires several tokens so ordinary initials ("J P Morgan") don't trip it.
    """
    tokens = value.split()
    if len(tokens) < _FRAGMENT_MIN_TOKENS:
        return False
    singles = sum(1 for token in tokens if len(token) == 1 and token.isalnum())
    return singles / len(tokens) >= _FRAGMENT_TOKEN_RATIO


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def _as_float(value: Decimal) -> float:
    return float(round(value, 4))
