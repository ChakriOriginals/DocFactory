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

from docfactory_core.date_corroboration import (
    corroborate_date,
    corroborate_payment_term,
    dates_in_text,
    stated_payment_term,
)
from docfactory_core.groundedness import (
    GROUNDEDNESS_THRESHOLD,
    groundedness,
)
from docfactory_core.groundedness import (
    squash_for_matching as _squash_for_enum,
)
from docfactory_core.pipeline import FieldKind, PipelineDefinition

# --- Uncalibrated penalty priors (see note 3 above) -------------------------

PENALTY_ARITHMETIC_RULE = 0.30  # totals that don't add up: strong signal
PENALTY_DATE_RULE = 0.15  # date ordering: weaker, often a real oddity
PENALTY_RETRY = 0.10  # needed a second attempt to satisfy the schema
PENALTY_TEXT_FRAGMENTED = 0.10  # split-run artifact in a free-text field
PENALTY_NONPOSITIVE_AMOUNT = 0.25
PENALTY_SUSPECT_FIELD_SHAPE = 0.20  # per-field: empty-ish / implausible value
# Free-text fields have no arithmetic guard at all, so a value that cannot be
# traced back to the source text is the only evidence available that it was
# invented. Weighted accordingly — still a prior until 2.2c fits it.
PENALTY_UNGROUNDED_FIELD = 0.35
# An enum value the document does not mention anywhere. Same weight as an
# ungrounded free-text field, and for the same reason: nothing else guards it.
# Arithmetic rules are currency-blind — subtotal + tax = total holds just as
# well in the wrong currency — so without this signal a mislabelled invoice
# scores a clean 1.00 and auto-approves.
PENALTY_UNGROUNDED_ENUM = 0.35

# Dates that do not appear anywhere on the page. This closes the gap the 2.2c
# study measured: `shifted_date` accounted for 10 of the 12 errors that
# survived auto-approval, because every rule it broke was one we do not have.
# Measured at zero false positives across all 345 clean digital documents
# before adoption, so it can afford to be strict.
PENALTY_UNCORROBORATED_DATE = 0.35
# With 1/(1+days) only an exact match clears this; the constant exists so the
# calibration can loosen it rather than requiring a code change.
DATE_CORROBORATION_THRESHOLD = 0.9

# Which fields get which signals is no longer knowledge this module holds: the
# pipeline definition declares each field's kind, and text/date/table views are
# derived from that. Groundedness applies to free text only — money and dates
# are already constrained by rules, and their canonical form ("25832.09")
# deliberately differs from the printed form ("25.832,09 €"), so a text search
# would report a false negative on a correct extraction.

# Rule types whose failure is a softer signal than an arithmetic mismatch.
_SOFT_RULE_TYPES = frozenset({"date_order", "required"})

# Small-caps rendering splits a vendor name into character runs, and the
# extractor can go wrong in two distinguishable ways. Both are shape defects,
# so one predicate covers them:
#
#   scattered  "B W S & C . Kg a"  -> many bare single-character tokens
#   truncated  "B B AG"            -> only the capitals kept, stubby tokens
#
# Truncation is what the 2.2a measurement found dominating: nine of the ten
# errors the arithmetic scorer missed. Groundedness cannot see them (the kept
# characters really are in the source text), so token shape is the
# discriminator. Across all 500 ground-truth vendor names the lowest
# legitimate mean token length is 2.80 ("Hamann AG & Co. KG") while every
# observed truncation is <= 1.67, so 2.0 sits in open space between them.
_FRAGMENT_TOKEN_RATIO = 0.4
_FRAGMENT_MIN_TOKENS = 4
_MIN_MEAN_TOKEN_LENGTH = 2.0

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
        """Confidence for a stored field path, e.g. '<table>.0.<column>'.

        Table cells inherit their parent's score: the rules constrain the row
        list as a whole, not individual cells within it.
        """
        if name in self.fields:
            return self.fields[name].confidence
        # Table cells inherit their parent's score: the rules constrain the
        # row list as a whole, not individual cells within it.
        table = name.split(".", 1)[0]
        entry = self.fields.get(table)
        return entry.confidence if entry else None


def score_extraction(
    record: dict,
    validation: dict[str, bool],
    *,
    attempts: int = 1,
    source_text: str | None = None,
    definition: PipelineDefinition | None = None,
) -> ConfidenceReport:
    """Score an extracted record against its pipeline definition.

    `record` is the canonicalized extraction — a plain dict keyed by field
    name, with values already in the kind's canonical type (Decimal, date).
    Nothing here reads an attribute off a document-type-specific model, so the
    same code scores any pipeline.

    `source_text` is the parsed document text. When supplied, free-text fields
    are additionally checked for groundedness — without it the scorer is blind
    to invented string values (the 2.1 finding), so callers in the pipeline
    should always pass it. It stays optional so the arithmetic signals can be
    scored standalone.
    """
    if definition is None:
        from docfactory_core.pipeline_registry import default_pipeline

        definition = default_pipeline()
    rule_fields = definition.rule_fields
    soft_rules = {rule.name for rule in definition.rules if rule.type in _SOFT_RULE_TYPES}

    signals: dict[str, object] = {"attempts": attempts}
    doc_penalty = 0.0
    doc_reasons: list[str] = []
    field_penalties: dict[str, float] = dict.fromkeys(definition.scored_fields, 0.0)
    field_reasons: dict[str, list[str]] = {name: [] for name in field_penalties}

    # --- validation rules -> document and field penalties ---
    for rule, passed in validation.items():
        signals[f"rule.{rule}"] = passed
        if passed:
            continue
        weight = PENALTY_DATE_RULE if rule in soft_rules else PENALTY_ARITHMETIC_RULE
        doc_penalty += weight
        doc_reasons.append(f"failed:{rule}")
        for name in rule_fields.get(rule, ()):
            if name in field_penalties:
                field_penalties[name] += weight
                field_reasons[name].append(f"failed:{rule}")

    # --- residual magnitudes: how badly, not just whether ---
    from docfactory_core.pipeline import residuals as _residuals

    for rule_name, value in _residuals(record, definition).items():
        signals[f"residual.{rule_name}"] = value
    for table in definition.table_fields:
        signals[f"{table}.count"] = len(record.get(table) or [])
    # Scale for relative residuals: the largest money field present.
    money_values = [
        abs(_as_float(record[name]))
        for name, spec in definition.fields.items()
        if spec.kind is FieldKind.MONEY and record.get(name) is not None
    ]
    signals["scale"] = max(money_values) if money_values else 1.0

    # --- extraction-process signals ---
    if attempts > 1:
        doc_penalty += PENALTY_RETRY
        doc_reasons.append("needed_schema_retry")

    # --- per-field shape heuristics ---
    # Shape heuristics apply to every free-text field the pipeline declares,
    # not to a field named "vendor".
    for name in definition.text_fields:
        value = str(record.get(name) or "")
        fragmented = looks_fragmented(value)
        signals[f"{name}.looks_fragmented"] = fragmented
        signals[f"{name}.token_count"] = len(value.split())
        if fragmented:
            doc_penalty += PENALTY_TEXT_FRAGMENTED
            doc_reasons.append(f"{name}_looks_fragmented")
            field_penalties[name] += PENALTY_SUSPECT_FIELD_SHAPE
            field_reasons[name].append("looks_fragmented")

    # Which amounts must be positive is declared per field: an invoice total
    # qualifies, its tax does not.
    for name in definition.positive_fields:
        value = record.get(name)
        nonpositive = value is None or _as_float(value) <= 0
        signals[f"{name}.nonpositive"] = nonpositive
        if nonpositive:
            doc_penalty += PENALTY_NONPOSITIVE_AMOUNT
            doc_reasons.append(f"nonpositive_{name}")
            field_penalties[name] += PENALTY_SUSPECT_FIELD_SHAPE
            field_reasons[name].append("nonpositive")

    for name in definition.text_fields:
        if len(str(record.get(name) or "").strip()) < 2:
            field_penalties[name] += PENALTY_SUSPECT_FIELD_SHAPE
            field_reasons[name].append("implausibly_short")
            signals[f"{name}.implausibly_short"] = True

    # Per-row arithmetic, from whatever product rules the pipeline declares.
    from docfactory_core.pipeline import inconsistent_rows as _inconsistent_rows

    for table, bad_rows in _inconsistent_rows(record, definition).items():
        signals[f"{table}.inconsistent_rows"] = bad_rows
        if bad_rows:
            field_penalties[table] += PENALTY_SUSPECT_FIELD_SHAPE
            field_reasons[table].append(f"row_arithmetic:{bad_rows}")

    # --- groundedness: the only guard on unconstrained free text ---
    if source_text is not None:
        for name in definition.text_fields:
            score = groundedness(record.get(name), source_text)
            signals[f"groundedness.{name}"] = score
            if score < GROUNDEDNESS_THRESHOLD:
                doc_penalty += PENALTY_UNGROUNDED_FIELD
                doc_reasons.append(f"ungrounded:{name}")
                field_penalties[name] += PENALTY_UNGROUNDED_FIELD
                field_reasons[name].append(f"ungrounded:{score}")

        # Free-text columns of a table are checked the same way, row by row;
        # which columns those are comes from the declared item kinds.
        for table in definition.table_fields:
            columns = _text_columns(definition, table)
            row_scores = [
                groundedness(row.get(column), source_text)
                for row in (record.get(table) or [])
                for column in columns
            ]
            weakest = min(row_scores, default=1.0)
            signals[f"groundedness.{table}_min"] = weakest
            signals[f"groundedness.{table}_mean"] = (
                round(sum(row_scores) / len(row_scores), 4) if row_scores else 1.0
            )
            if weakest < GROUNDEDNESS_THRESHOLD:
                doc_penalty += PENALTY_UNGROUNDED_FIELD
                doc_reasons.append(f"ungrounded:{table}_text")
                field_penalties[table] += PENALTY_UNGROUNDED_FIELD
                field_reasons[table].append(f"ungrounded_row_text:{weakest}")

    # --- date corroboration: the only guard on dates against the page ---
    # --- enum values the document never mentions ------------------------------
    #
    # An enum constrains what the model MAY say; nothing checked that it said
    # the right thing. On a GBP invoice a model restricted to {USD, EUR}
    # returns one of them with the correct number, every arithmetic rule
    # passes, and the record auto-approves reading $12,000 for £12,000.
    #
    # Whitespace-insensitive because the currency symbol is frequently split
    # from its amount by the text extractor, the same artifact _squash exists
    # to absorb for free text.
    if source_text is not None:
        squashed_source = _squash_for_enum(source_text)
        for name in definition.grounded_enum_fields:
            value = record.get(name)
            forms = definition.fields[name].surface_forms.get(value or "", ())
            if not value or not forms:
                # A value outside the declared set, or one with no forms: the
                # schema layer already rejects the first, and _parse_field
                # refuses partial coverage, so reaching here means the record
                # is malformed rather than merely ungrounded.
                continue
            found = any(_squash_for_enum(form) in squashed_source for form in forms)

            # Emitted as `groundedness.{field}`, deliberately, and not under a
            # key of its own. confidence_model.features_for reads exactly that
            # key and defaults it to 1.0 — so a new signal name would be
            # invisible to the fitted model, the heuristic doc_confidence would
            # drop, and ROUTING would not change at all. Reusing the key means
            # the already-calibrated groundedness weight does the work and no
            # refit is needed. Binary here where free text is continuous; the
            # scale and the meaning are the same.
            signals[f"groundedness.{name}"] = 1.0 if found else 0.0
            if not found:
                doc_penalty += PENALTY_UNGROUNDED_ENUM
                doc_reasons.append(f"enum_not_in_document:{name}={value}")
                field_penalties[name] += PENALTY_UNGROUNDED_ENUM
                field_reasons[name].append(f"not_in_document:{value}")

    if source_text is not None:
        text_dates = dates_in_text(source_text)
        signals["date_corroboration.text_dates_found"] = len(text_dates)
        for name in definition.date_fields:
            score = corroborate_date(record.get(name), text_dates)
            signals[f"date_corroboration.{name}"] = score
            if score < DATE_CORROBORATION_THRESHOLD:
                doc_penalty += PENALTY_UNCORROBORATED_DATE
                doc_reasons.append(f"uncorroborated_date:{name}")
                field_penalties[name] += PENALTY_UNCORROBORATED_DATE
                field_reasons[name].append(f"not_on_page:{score}")

        # A stated term ("Net 30") pins the interval between two dates. Which
        # two is not knowledge this module holds: the date_order rules already
        # declare the pairs the document type cares about.
        term = stated_payment_term(source_text)
        term_score: float | None = None
        for rule in definition.rules:
            if rule.type != "date_order":
                continue
            score = corroborate_payment_term(
                record.get(rule.params["earlier"]), record.get(rule.params["later"]), term
            )
            if score is None:
                continue
            term_score = score if term_score is None else min(term_score, score)
            if score < DATE_CORROBORATION_THRESHOLD:
                doc_penalty += PENALTY_UNCORROBORATED_DATE
                doc_reasons.append("payment_term_mismatch")
                # The stated term pins the interval but not which end moved.
                for name in rule.fields:
                    if name in definition.date_fields:
                        field_penalties[name] += PENALTY_UNCORROBORATED_DATE / 2
                        field_reasons[name].append(f"term_mismatch:{score}")
        signals["date_corroboration.payment_term"] = term_score

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


def _text_columns(definition: PipelineDefinition, table: str) -> tuple[str, ...]:
    spec = definition.fields[table]
    return tuple(name for name, column in spec.item_fields.items() if column.is_free_text)


def failed_extraction_report(error: str | None) -> ConfidenceReport:
    """No usable extraction: zero confidence, and say why."""
    return ConfidenceReport(
        doc_confidence=0.0,
        signals={"extraction_failed": True, "error": error},
        reasons=("extraction_failed",),
    )


def looks_fragmented(value: str) -> bool:
    """True when a string reads as split character runs rather than a name.

    Catches both observed forms: stubby tokens throughout ("B B AG", "R g"),
    and longer strings peppered with bare single characters
    ("B W S & C . Kg a"). Ordinary initials ("J P Morgan", mean 2.67) and
    genuinely short real names ("Cox PLC", "3M") clear both rules.
    """
    tokens = value.split()
    if len(tokens) < 2:
        return False
    if sum(len(token) for token in tokens) / len(tokens) < _MIN_MEAN_TOKEN_LENGTH:
        return True
    if len(tokens) < _FRAGMENT_MIN_TOKENS:
        return False
    singles = sum(1 for token in tokens if len(token) == 1 and token.isalnum())
    return singles / len(tokens) >= _FRAGMENT_TOKEN_RATIO


def _clamp(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def _as_float(value: Decimal) -> float:
    return float(round(value, 4))
