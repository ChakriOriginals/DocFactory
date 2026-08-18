"""Pipeline definitions: parsing, the rule engine, and the untrusted boundary.

Tenants author these through the API, so a malformed definition must be
rejected on write with a clear error rather than failing inside a worker where
the blast radius is a stuck document and an opaque traceback.

The behaviour-preservation claim also lives here: the rule engine, driven by
the seeded invoice config, must reproduce what the hardcoded validator it
replaced returned. That validator is gone (3c — one definition of the invoice
schema, not two), so its answers are frozen below as literal expectations.
"""

import json

import pytest
from docfactory_core.pipeline import (
    FieldKind,
    PipelineConfigError,
    evaluate_rules,
    inconsistent_rows,
    parse_definition,
    residuals,
)
from docfactory_core.pipeline_registry import default_pipeline

RECORD = {
    "vendor": "Underwood Ltd",
    "invoice_number": "INV-2026-00042",
    "invoice_date": "2026-06-06",
    "due_date": "2026-07-06",
    "currency": "USD",
    "subtotal": "1000.00",
    "tax_rate": "0.075",
    "tax": "75.00",
    "total": "1075.00",
    "line_items": [
        {"description": "Consulting", "quantity": "2", "unit_price": "400.00", "amount": "800.00"},
        {"description": "Hosting", "quantity": "1", "unit_price": "200.00", "amount": "200.00"},
    ],
}


def minimal_config(**overrides) -> dict:
    base = {
        "document_type": "thing",
        "fields": {"name": {"kind": "text"}},
        "extraction_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
    }
    return base | overrides


class TestFieldKindsDriveSignals:
    """The 2.3 finding made structural: signals follow declared kinds."""

    def test_the_invoice_pipeline_declares_its_kinds(self):
        definition = default_pipeline()
        assert definition.text_fields == ("vendor", "invoice_number")
        assert definition.date_fields == ("invoice_date", "due_date")
        assert definition.table_fields == ("line_items",)

    def test_rule_to_field_attribution_comes_from_config(self):
        rule_fields = default_pipeline().rule_fields
        assert rule_fields["subtotal_plus_tax_equals_total"] == ("subtotal", "tax", "total")
        assert "subtotal" in rule_fields["line_items_sum_to_subtotal"]

    def test_kinds_drive_normalization(self):
        definition = default_pipeline()
        record = definition.normalize(
            {**RECORD, "invoice_date": "06.06.2026", "total": "1.075,00 €"}
        )
        assert str(record["invoice_date"]) == "2026-06-06"
        assert str(record["total"]) == "1075.00"


# Frozen output of the hardcoded `validate_invoice`, recorded before it was
# deleted in 3c: same rule names, same booleans, same tolerances.
FROZEN_RULE_RESULTS = {
    "clean": (
        {},
        {
            "line_items_sum_to_subtotal": True,
            "subtotal_plus_tax_equals_total": True,
            "tax_matches_rate": True,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": True,
        },
    ),
    "wrong_total": (
        {"total": "9999.00"},
        {
            "line_items_sum_to_subtotal": True,
            "subtotal_plus_tax_equals_total": False,
            "tax_matches_rate": True,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": True,
        },
    ),
    "wrong_tax": (
        {"tax": "500.00"},
        {
            "line_items_sum_to_subtotal": True,
            "subtotal_plus_tax_equals_total": False,
            "tax_matches_rate": False,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": True,
        },
    ),
    "wrong_subtotal": (
        {"subtotal": "12.00"},
        {
            "line_items_sum_to_subtotal": False,
            "subtotal_plus_tax_equals_total": False,
            "tax_matches_rate": False,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": True,
        },
    ),
    "due_before_issue": (
        {"due_date": "2025-01-01"},
        {
            "line_items_sum_to_subtotal": True,
            "subtotal_plus_tax_equals_total": True,
            "tax_matches_rate": True,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": False,
        },
    ),
    "wrong_rate": (
        {"tax_rate": "0.5"},
        {
            "line_items_sum_to_subtotal": True,
            "subtotal_plus_tax_equals_total": True,
            "tax_matches_rate": False,
            "invoice_date_parses": True,
            "due_date_not_before_invoice_date": True,
        },
    ),
}


class TestRuleEngineReproducesTheValidatorItReplaced:
    """The regression guard for the whole refactor.

    Expectations are the output of the hardcoded `validate_invoice` recorded
    before it was deleted: same rule names, same booleans, same tolerances.
    """

    @pytest.mark.parametrize("case", sorted(FROZEN_RULE_RESULTS))
    def test_frozen_rule_results(self, case):
        override, expected = FROZEN_RULE_RESULTS[case]
        definition = default_pipeline()
        assert evaluate_rules(definition.normalize(RECORD | override), definition) == expected

    def test_per_row_rules_stay_out_of_the_document_level_results(self):
        # `row_arithmetic` is reported per row by inconsistent_rows, which
        # localizes to a line; entering it here as well would double-count the
        # same evidence in the confidence model's feature vector.
        definition = default_pipeline()
        assert any(rule.name == "row_arithmetic" for rule in definition.rules)
        assert "row_arithmetic" not in evaluate_rules(definition.normalize(RECORD), definition)

    def test_residuals_are_reported_per_rule(self):
        definition = default_pipeline()
        record = definition.normalize(RECORD | {"total": "1200.00"})
        assert residuals(record, definition)["subtotal_plus_tax_equals_total"] == pytest.approx(
            -125.0
        )

    def test_row_arithmetic_localizes_the_bad_row(self):
        definition = default_pipeline()
        broken = {
            **RECORD,
            "line_items": [
                {"description": "A", "quantity": "2", "unit_price": "400.00", "amount": "750.00"},
                {"description": "B", "quantity": "1", "unit_price": "200.00", "amount": "200.00"},
            ],
        }
        assert inconsistent_rows(definition.normalize(broken), definition)["line_items"] == [0]

    def test_an_unevaluable_rule_fails_rather_than_passes(self):
        # A missing field means the consistency check cannot be run, which is
        # exactly when a human should look — so it must not read as "passed".
        definition = default_pipeline()
        record = definition.normalize(RECORD)
        record["total"] = None
        assert evaluate_rules(record, definition)["subtotal_plus_tax_equals_total"] is False


class TestUntrustedConfigIsRejectedOnWrite:
    def test_a_field_without_a_kind_is_rejected(self):
        with pytest.raises(PipelineConfigError, match="must declare a kind"):
            parse_definition("t", "s", 1, minimal_config(fields={"name": {}}))

    def test_an_unknown_kind_is_rejected_with_the_valid_set(self):
        with pytest.raises(PipelineConfigError, match="unknown kind"):
            parse_definition("t", "s", 1, minimal_config(fields={"name": {"kind": "wizard"}}))

    def test_an_unknown_rule_type_is_rejected(self):
        config = minimal_config(
            validation_rules=[{"name": "r", "type": "exec_python", "fields": ["name"]}]
        )
        with pytest.raises(PipelineConfigError, match="unknown rule type"):
            parse_definition("t", "s", 1, config)

    def test_a_rule_referencing_an_unknown_field_is_rejected(self):
        config = minimal_config(
            validation_rules=[{"name": "r", "type": "required", "fields": ["nope"]}]
        )
        with pytest.raises(PipelineConfigError, match="unknown fields"):
            parse_definition("t", "s", 1, config)

    def test_a_rule_naming_no_fields_is_rejected(self):
        # Without implicated fields there is no fault localization.
        config = minimal_config(validation_rules=[{"name": "r", "type": "required"}])
        with pytest.raises(PipelineConfigError, match="must name the fields"):
            parse_definition("t", "s", 1, config)

    def test_a_schema_property_with_no_declared_kind_is_rejected(self):
        config = minimal_config(
            extraction_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}, "ghost": {"type": "string"}},
            }
        )
        with pytest.raises(PipelineConfigError, match="no field kind"):
            parse_definition("t", "s", 1, config)

    def test_a_table_without_columns_is_rejected(self):
        with pytest.raises(PipelineConfigError, match="item_fields"):
            parse_definition("t", "s", 1, minimal_config(fields={"rows": {"kind": "table"}}))

    def test_an_enum_without_values_is_rejected(self):
        with pytest.raises(PipelineConfigError, match="must declare values"):
            parse_definition("t", "s", 1, minimal_config(fields={"c": {"kind": "enum"}}))

    def test_an_invalid_regex_is_rejected(self):
        config = minimal_config(fields={"name": {"kind": "text", "pattern": "([unclosed"}})
        with pytest.raises(PipelineConfigError, match="invalid pattern"):
            parse_definition("t", "s", 1, config)

    def test_missing_required_keys_are_rejected(self):
        with pytest.raises(PipelineConfigError, match="missing required key"):
            parse_definition("t", "s", 1, {"document_type": "x"})

    def test_a_non_object_schema_is_rejected(self):
        config = minimal_config(extraction_schema={"type": "array"})
        with pytest.raises(PipelineConfigError, match="JSON Schema object"):
            parse_definition("t", "s", 1, config)


class TestSeededPipelineMatchesTheFile:
    def test_the_database_seed_and_the_runtime_file_agree(self):
        """One source of truth: the migration seeds from the file the app loads."""
        from pathlib import Path

        from docfactory_core.pipeline_registry import CONFIG_DIR

        on_disk = json.loads((CONFIG_DIR / "invoice_v1.json").read_text())
        assert Path(CONFIG_DIR / "invoice_v1.json").exists()
        definition = parse_definition("dev-tenant", "invoice", 1, on_disk)
        assert definition.document_type == "invoice"
        assert set(definition.fields) == set(on_disk["fields"])
        assert definition.fields["line_items"].kind is FieldKind.TABLE

    def test_the_definition_is_the_only_source_of_the_invoice_schema(self):
        """3c: `INVOICE_JSON_SCHEMA` and `SCALAR_FIELD_NAMES` are gone."""
        import importlib

        for module in ("docfactory_core.schemas", "docfactory_core.validation"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module)

        definition = default_pipeline()
        schema = definition.json_schema
        assert set(schema["properties"]) == set(definition.fields)
        assert set(schema["required"]) == set(definition.fields)


class TestPromptConfigIsPartOfTheDefinition:
    def test_field_notes_for_an_unknown_field_are_rejected(self):
        config = minimal_config(prompt={"field_notes": {"ghost": "beware"}})
        with pytest.raises(PipelineConfigError, match="unknown fields"):
            parse_definition("t", "s", 1, config)

    def test_a_positive_constraint_on_a_text_field_is_rejected(self):
        config = minimal_config(fields={"name": {"kind": "text", "positive": True}})
        with pytest.raises(PipelineConfigError, match="cannot be positive"):
            parse_definition("t", "s", 1, config)
