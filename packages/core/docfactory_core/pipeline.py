"""Pipeline definitions: what a document type is, as data rather than code.

Everything that used to be invoice-specific — the extraction schema, which
fields exist, what each field *is*, and which consistency rules must hold —
lives in a versioned record owned by a tenant.

The load-bearing idea is **field kinds**. The confidence signals are
schema-shaped: groundedness only makes sense for free text, date corroboration
only for dates, and the arithmetic rules only for money and quantities. So a
definition declares what each field is, not merely what it is called, and the
scorer reads those kinds instead of carrying hardcoded knowledge that the
vendor is text and the invoice date is a date.

Validation rules come from a small allowlisted vocabulary rather than
arbitrary code. Tenants author these through the API, so the rule set is an
untrusted-input boundary: a definition that names an unknown rule, or points a
rule at a field that does not exist, is rejected on write rather than blowing
up in a worker later.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from docfactory_core.normalize import (
    normalize_amount,
    normalize_date,
    normalize_rate,
    normalize_text,
)


class FieldKind(StrEnum):
    """What a field *is*, which decides which signals apply to it."""

    TEXT = "text"  # free text -> groundedness
    DATE = "date"  # -> date corroboration, date-order rules
    MONEY = "money"  # -> arithmetic rules
    RATE = "rate"  # a decimal fraction (0.19), not a percentage
    QUANTITY = "quantity"  # -> row arithmetic
    ENUM = "enum"  # membership in a fixed set
    TABLE = "table"  # repeated rows of sub-fields


# Rule names a tenant may use. Anything else is rejected on write.
RULE_TYPES = frozenset(
    {"sum_equals", "terms_equal", "product_equals", "date_order", "regex", "required"}
)

_CENT = Decimal("0.01")


class PipelineConfigError(ValueError):
    """A pipeline definition is malformed. Raised on write, never at runtime."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    required: bool = True
    # TABLE only: the columns of each row.
    item_fields: dict[str, "FieldSpec"] = field(default_factory=dict)
    # ENUM only.
    values: tuple[str, ...] = ()
    pattern: str | None = None

    @property
    def is_free_text(self) -> bool:
        return self.kind is FieldKind.TEXT

    @property
    def is_date(self) -> bool:
        return self.kind is FieldKind.DATE


@dataclass(frozen=True)
class RuleSpec:
    name: str
    type: str
    # Fields this rule implicates — drives per-field confidence attribution,
    # the fault localization introduced in 2.1.
    fields: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineDefinition:
    tenant_id: str
    slug: str
    version: int
    document_type: str
    fields: dict[str, FieldSpec]
    rules: tuple[RuleSpec, ...]
    json_schema: dict
    confidence_model_path: str | None = None
    sla_hours: float | None = None

    # --- views the scorer uses instead of hardcoded field knowledge ---

    @property
    def scored_fields(self) -> tuple[str, ...]:
        return tuple(self.fields)

    @property
    def text_fields(self) -> tuple[str, ...]:
        return tuple(n for n, spec in self.fields.items() if spec.is_free_text)

    @property
    def date_fields(self) -> tuple[str, ...]:
        return tuple(n for n, spec in self.fields.items() if spec.is_date)

    @property
    def table_fields(self) -> tuple[str, ...]:
        return tuple(n for n, spec in self.fields.items() if spec.kind is FieldKind.TABLE)

    @property
    def rule_fields(self) -> dict[str, tuple[str, ...]]:
        """rule name -> fields it implicates (the 2.1 attribution map)."""
        return {rule.name: rule.fields for rule in self.rules}

    def normalize(self, record: dict) -> dict:
        """Canonicalize every value according to its declared kind."""
        return {name: self.normalize_field(name, record.get(name)) for name in self.fields}

    def normalize_field(self, name: str, value: Any) -> Any:
        spec = self.fields.get(name)
        if spec is None or value is None:
            return value
        return _normalize_by_kind(spec, value)


def _normalize_by_kind(spec: FieldSpec, value: Any) -> Any:
    if spec.kind is FieldKind.MONEY or spec.kind is FieldKind.QUANTITY:
        return normalize_amount(value)
    if spec.kind is FieldKind.RATE:
        return normalize_rate(value)
    if spec.kind is FieldKind.DATE:
        return normalize_date(value)
    if spec.kind is FieldKind.TABLE:
        return [
            {
                column: _normalize_by_kind(sub, row[column])
                for column, sub in spec.item_fields.items()
                if column in row
            }
            for row in (value or [])
        ]
    return normalize_text(str(value))


# --------------------------------------------------------------------------
# parsing and validation of tenant-supplied definitions
# --------------------------------------------------------------------------


def parse_definition(tenant_id: str, slug: str, version: int, config: dict) -> PipelineDefinition:
    """Build a definition from stored/submitted config, rejecting anything malformed.

    This is the untrusted-input boundary: tenants POST these.
    """
    if not isinstance(config, dict):
        raise PipelineConfigError("config must be an object")
    for required in ("document_type", "fields", "extraction_schema"):
        if required not in config:
            raise PipelineConfigError(f"missing required key: {required}")

    raw_fields = config["fields"]
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise PipelineConfigError("fields must be a non-empty object")

    fields: dict[str, FieldSpec] = {}
    for name, spec in raw_fields.items():
        fields[name] = _parse_field(name, spec)

    rules = tuple(_parse_rule(entry, fields) for entry in config.get("validation_rules", []))

    schema = config["extraction_schema"]
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise PipelineConfigError("extraction_schema must be a JSON Schema object")
    unknown = set(schema.get("properties", {})) - set(fields)
    if unknown:
        raise PipelineConfigError(
            f"extraction_schema declares properties with no field kind: {sorted(unknown)}"
        )

    return PipelineDefinition(
        tenant_id=tenant_id,
        slug=slug,
        version=version,
        document_type=str(config["document_type"]),
        fields=fields,
        rules=rules,
        json_schema=schema,
        confidence_model_path=config.get("confidence", {}).get("model_path"),
        sla_hours=config.get("sla_hours"),
    )


def _parse_field(name: str, spec: Any) -> FieldSpec:
    if not isinstance(spec, dict) or "kind" not in spec:
        raise PipelineConfigError(f"field {name!r} must declare a kind")
    try:
        kind = FieldKind(spec["kind"])
    except ValueError as exc:
        raise PipelineConfigError(
            f"field {name!r} has unknown kind {spec['kind']!r}; "
            f"valid kinds: {sorted(k.value for k in FieldKind)}"
        ) from exc

    item_fields: dict[str, FieldSpec] = {}
    if kind is FieldKind.TABLE:
        columns = spec.get("item_fields")
        if not isinstance(columns, dict) or not columns:
            raise PipelineConfigError(f"table field {name!r} must declare item_fields")
        item_fields = {column: _parse_field(column, sub) for column, sub in columns.items()}

    values = tuple(spec.get("values", ()))
    if kind is FieldKind.ENUM and not values:
        raise PipelineConfigError(f"enum field {name!r} must declare values")

    pattern = spec.get("pattern")
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise PipelineConfigError(f"field {name!r} has an invalid pattern: {exc}") from exc

    return FieldSpec(
        name=name,
        kind=kind,
        required=bool(spec.get("required", True)),
        item_fields=item_fields,
        values=values,
        pattern=pattern,
    )


def _parse_rule(entry: Any, fields: dict[str, FieldSpec]) -> RuleSpec:
    if not isinstance(entry, dict) or "name" not in entry or "type" not in entry:
        raise PipelineConfigError("each validation rule needs a name and a type")
    rule_type = entry["type"]
    if rule_type not in RULE_TYPES:
        raise PipelineConfigError(f"unknown rule type {rule_type!r}; allowed: {sorted(RULE_TYPES)}")
    implicated = tuple(entry.get("fields", ()))
    unknown = [name for name in implicated if name not in fields]
    if unknown:
        raise PipelineConfigError(f"rule {entry['name']!r} references unknown fields: {unknown}")
    if not implicated:
        raise PipelineConfigError(f"rule {entry['name']!r} must name the fields it implicates")
    return RuleSpec(
        name=str(entry["name"]),
        type=rule_type,
        fields=implicated,
        params={k: v for k, v in entry.items() if k not in {"name", "type", "fields"}},
    )


# --------------------------------------------------------------------------
# the rule engine
# --------------------------------------------------------------------------


def evaluate_rules(record: dict, definition: PipelineDefinition) -> dict[str, bool]:
    """Run the pipeline's validation rules over a normalized record."""
    return {rule.name: _evaluate(rule, record, definition) for rule in definition.rules}


def residuals(record: dict, definition: PipelineDefinition) -> dict[str, float]:
    """Signed magnitude by which each arithmetic rule misses, for calibration."""
    out: dict[str, float] = {}
    for rule in definition.rules:
        if rule.type in {"sum_equals", "terms_equal"} or (
            rule.type == "product_equals" and "items" not in rule.params
        ):
            try:
                out[rule.name] = float(_arithmetic_residual(rule, record))
            except (InvalidOperation, TypeError, ValueError, KeyError):
                out[rule.name] = 0.0
    return out


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return normalize_amount(value)


def _arithmetic_residual(rule: RuleSpec, record: dict) -> Decimal:
    if rule.type == "product_equals":
        factors = rule.params["factors"]
        product = _decimal(record[factors[0]]) * _decimal(record[factors[1]])
        return product - _decimal(record[rule.params["result"]])
    if rule.type == "sum_equals":
        rows = record.get(rule.params["items"]) or []
        column = rule.params["item_field"]
        total = sum((_decimal(row[column]) for row in rows), Decimal("0"))
        return total - _decimal(record[rule.params["target"]])
    terms = sum((_decimal(record[name]) for name in rule.params["terms"]), Decimal("0"))
    return terms - _decimal(record[rule.params["target"]])


def _evaluate(rule: RuleSpec, record: dict, definition: PipelineDefinition) -> bool:
    try:
        if rule.type in {"sum_equals", "terms_equal"}:
            tolerance = Decimal(str(rule.params.get("tolerance", _CENT)))
            return abs(_arithmetic_residual(rule, record)) <= tolerance

        if rule.type == "product_equals":
            tolerance = Decimal(str(rule.params.get("tolerance", _CENT)))
            factors = rule.params["factors"]
            result = rule.params["result"]
            if "items" not in rule.params:
                # Scalar form: field_a x field_b == field_c, e.g. an invoice's
                # subtotal x tax_rate == tax.
                product = _decimal(record[factors[0]]) * _decimal(record[factors[1]])
                return abs(product - _decimal(record[result])) <= tolerance
            # Per row of a table: factor_a x factor_b == result, e.g. the
            # quantity x unit_price == amount check from 2.1.
            rows = record.get(rule.params["items"]) or []
            return all(
                abs(_decimal(row[factors[0]]) * _decimal(row[factors[1]]) - _decimal(row[result]))
                <= tolerance
                for row in rows
            )

        if rule.type == "date_order":
            earlier = record[rule.params["earlier"]]
            later = record[rule.params["later"]]
            return earlier is not None and later is not None and later >= earlier

        if rule.type == "regex":
            value = record.get(rule.params["field"])
            return (
                value is not None and re.fullmatch(rule.params["pattern"], str(value)) is not None
            )

        if rule.type == "required":
            return all(
                record.get(name) not in (None, "", [])
                for name in rule.params.get("fields", rule.fields)
            )
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        # A rule that cannot be evaluated has failed, not passed: an
        # unevaluable consistency check is exactly when a human should look.
        # ValueError included deliberately — normalization raises it on a
        # missing or unparseable value, which is precisely that case.
        return False
    return False


def inconsistent_rows(record: dict, definition: PipelineDefinition) -> dict[str, list[int]]:
    """Row indices failing a per-row product rule, per table field."""
    out: dict[str, list[int]] = {}
    for rule in definition.rules:
        if rule.type != "product_equals" or "items" not in rule.params:
            continue
        table = rule.params["items"]
        rows = record.get(table) or []
        tolerance = Decimal(str(rule.params.get("tolerance", _CENT)))
        factors, result = rule.params["factors"], rule.params["result"]
        bad = []
        for index, row in enumerate(rows):
            try:
                miss = _decimal(row[factors[0]]) * _decimal(row[factors[1]]) - _decimal(row[result])
            except (KeyError, TypeError, ValueError, InvalidOperation):
                bad.append(index)
                continue
            if abs(miss) > tolerance:
                bad.append(index)
        out[table] = bad
    return out
