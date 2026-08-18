"""The second document type, and the claim it exists to test.

Purchase orders were added as a pipeline definition, a mock-hints file, a
generator module and templates. No extraction, scoring, routing or worker code
was written for them. These tests assert that from the outside: the same
runner extracts a PO, the same scorer produces PO signals chosen by the PO's
declared field kinds, and the same rule engine checks the PO's own arithmetic.

A purchase order is deliberately not a renamed invoice — it has a PO number
and an order/delivery pair rather than an invoice number and a due date, a
second free-text party (the buyer issues it), a freight line instead of a tax
rate, and therefore different rules.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from docfactory_core.confidence import score_extraction
from docfactory_core.confidence_model import get_confidence_model, route_extraction
from docfactory_core.extraction import build_system_prompt, run_extraction
from docfactory_core.llm import MockHintsError, MockLLMClient, load_hints
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline import FieldKind, evaluate_rules, inconsistent_rows
from docfactory_core.pipeline_registry import available_slugs, default_pipeline, load_from_file

FIXTURES = Path(__file__).parent / "fixtures"
PO = load_from_file("purchase_order")
INVOICE = default_pipeline()

# The euro fixture, po-00001, as the generator labelled it.
EURO_LABELS = {
    "buyer": "Heydrich",
    "vendor": "Staude KG",
    "po_number": "BA-2025/9221",
    "order_date": date(2025, 8, 31),
    "delivery_date": date(2025, 9, 14),
    "currency": "EUR",
    "subtotal": Decimal("30230.59"),
    "shipping": Decimal("0.00"),
    "total": Decimal("30230.59"),
}


def _extract(fixture: str):
    text = extract_pdf_text((FIXTURES / fixture).read_bytes())
    return text, run_extraction(text, MockLLMClient(), PO)


class TestTheDefinitionIsGenuinelyADifferentDocument:
    def test_it_declares_its_own_fields_and_kinds(self):
        assert PO.text_fields == ("buyer", "vendor", "po_number")
        assert PO.date_fields == ("order_date", "delivery_date")
        assert PO.table_fields == ("line_items",)
        assert PO.positive_fields == ("total",)
        # no rate field at all: a PO has freight, not a tax rate
        assert not [n for n, s in PO.fields.items() if s.kind is FieldKind.RATE]

    def test_its_rules_are_its_own_arithmetic(self):
        rules = {rule.name: rule.type for rule in PO.rules}
        assert rules["line_items_sum_to_subtotal"] == "sum_equals"
        assert rules["subtotal_plus_shipping_equals_total"] == "terms_equal"
        assert rules["po_number_format"] == "regex"
        assert "tax_matches_rate" not in rules

    def test_it_shares_no_field_names_it_should_not(self):
        assert "invoice_number" not in PO.fields
        assert "due_date" not in PO.fields
        assert "tax" not in PO.fields

    def test_the_deployment_offers_both_types(self):
        assert set(available_slugs()) == {"invoice", "purchase_order"}


class TestExtractionNeedsNoNewCode:
    @pytest.mark.parametrize("fixture", ["po_euro.pdf", "po_standard.pdf"])
    def test_a_purchase_order_extracts_and_canonicalizes(self, fixture):
        _, outcome = _extract(fixture)
        assert outcome.record is not None, outcome.error
        assert outcome.attempts == 1
        record = outcome.record
        assert set(record) == set(PO.fields)
        assert isinstance(record["order_date"], date)
        assert isinstance(record["total"], Decimal)

    def test_the_euro_fixture_matches_its_labels(self):
        _, outcome = _extract("po_euro.pdf")
        for name, expected in EURO_LABELS.items():
            assert outcome.record[name] == expected, name
        assert len(outcome.record["line_items"]) == 2

    def test_the_prompt_is_the_purchase_order_prompt(self):
        prompt = build_system_prompt(PO)
        assert prompt.startswith("You extract structured data from purchase order documents.")
        assert "Bestellung" in prompt
        # kind-derived rules still apply, and nothing invoice-specific leaks in
        assert "ISO-8601" in prompt
        assert "tax_rate" not in prompt

    def test_the_mock_cannot_fake_a_type_it_has_no_hints_for(self):
        with pytest.raises(MockHintsError, match="no mock extraction hints"):
            load_hints("nonexistent_type")


class TestSignalsFollowTheDeclaredKinds:
    """The point of field kinds: the right signal for each field, chosen by the
    definition rather than by a document-type branch in the scorer."""

    @pytest.fixture(scope="class")
    @classmethod
    def report(cls):
        text, outcome = _extract("po_euro.pdf")
        return score_extraction(
            outcome.record,
            evaluate_rules(outcome.record, PO),
            attempts=outcome.attempts,
            source_text=text,
            definition=PO,
        )

    def test_text_fields_get_groundedness(self, report):
        for name in PO.text_fields:
            assert report.signals[f"groundedness.{name}"] == 1.0
        assert report.signals["groundedness.line_items_min"] == 1.0

    def test_date_fields_get_corroboration(self, report):
        for name in PO.date_fields:
            assert report.signals[f"date_corroboration.{name}"] == 1.0

    def test_the_stated_interval_is_corroborated_from_the_date_order_rule(self, report):
        # "Lieferung innerhalb von 14 Tagen" pins order -> delivery, exactly as
        # an invoice's payment term pins invoice -> due. Same code, different
        # date pair, taken from the pipeline's own date_order rule.
        assert report.signals["date_corroboration.payment_term"] == 1.0

    def test_money_fields_get_arithmetic_residuals(self, report):
        assert report.signals["residual.line_items_sum_to_subtotal"] == 0.0
        assert report.signals["residual.subtotal_plus_shipping_equals_total"] == 0.0
        assert report.signals["total.nonpositive"] is False

    def test_the_table_gets_row_arithmetic(self, report):
        assert report.signals["line_items.inconsistent_rows"] == []
        assert report.signals["line_items.count"] == 2

    def test_no_invoice_signals_are_produced(self, report):
        assert not [key for key in report.signals if "invoice" in key or "tax" in key]

    def test_a_clean_order_scores_and_routes(self, report):
        assert report.doc_confidence == 1.0
        routing = route_extraction(report.signals, get_confidence_model(), PO)
        assert routing.decision == "approved"
        assert set(routing.field_confidence) == set(PO.fields)


# A clean purchase order, by construction, to break one thing at a time.
CLEAN_ORDER = {
    "buyer": "Northwind Traders",
    "vendor": "Ohlmann KG",
    "po_number": "PO-2026-01234",
    "order_date": "2026-03-02",
    "delivery_date": "2026-03-16",
    "currency": "USD",
    "subtotal": "1000.00",
    "shipping": "48.00",
    "total": "1048.00",
    "line_items": [
        {
            "description": "Standing desk frame",
            "quantity": "2",
            "unit_price": "400.00",
            "amount": "800.00",
        },
        {
            "description": "USB-C docking station",
            "quantity": "1",
            "unit_price": "200.00",
            "amount": "200.00",
        },
    ],
}


class TestTheRulesLocalizeFaultsInAPurchaseOrder:
    def _score(self, **overrides):
        record = PO.normalize(CLEAN_ORDER | overrides)
        return record, score_extraction(record, evaluate_rules(record, PO), definition=PO)

    def test_a_consistent_order_passes_every_rule(self):
        record, report = self._score()
        assert all(evaluate_rules(record, PO).values())
        assert report.doc_confidence == 1.0

    def test_freight_that_does_not_reconcile_implicates_its_own_fields(self):
        _, report = self._score(total="2000.00")
        for name in ("subtotal", "shipping", "total"):
            assert report.fields[name].confidence < 1.0, name
        for name in ("order_date", "delivery_date", "po_number", "buyer", "vendor"):
            assert report.fields[name].confidence == 1.0, name

    def test_a_malformed_po_number_fails_its_regex_rule(self):
        record = PO.normalize(CLEAN_ORDER | {"po_number": "12345"})
        assert evaluate_rules(record, PO)["po_number_format"] is False

    def test_delivery_before_order_is_the_softer_date_failure(self):
        _, early = self._score(delivery_date="2026-01-01")
        _, arithmetic = self._score(total="2000.00")
        assert early.doc_confidence < 1.0
        assert early.doc_confidence > arithmetic.doc_confidence

    def test_a_broken_row_is_localized_to_that_row(self):
        record = PO.normalize(
            CLEAN_ORDER
            | {
                "line_items": [
                    CLEAN_ORDER["line_items"][0] | {"amount": "750.00"},
                    CLEAN_ORDER["line_items"][1],
                ],
                "subtotal": "950.00",
                "total": "998.00",
            }
        )
        assert inconsistent_rows(record, PO)["line_items"] == [0]


class TestTheDefinitionCrossesTheUntrustedBoundary:
    """Tenants author these; the PO path is validated on write like any other."""

    def test_the_seeded_config_parses(self):
        import json

        from docfactory_core.pipeline import parse_definition
        from docfactory_core.pipeline_registry import CONFIG_DIR

        config = json.loads((CONFIG_DIR / "purchase_order_v1.json").read_text())
        definition = parse_definition("dev-tenant", "purchase_order", 1, config)
        assert definition.document_type == "purchase order"
        assert definition.sla_hours == 48

    def test_a_purchase_order_config_with_an_unknown_rule_is_rejected(self):
        import json

        from docfactory_core.pipeline import PipelineConfigError, parse_definition
        from docfactory_core.pipeline_registry import CONFIG_DIR

        config = json.loads((CONFIG_DIR / "purchase_order_v1.json").read_text())
        config["validation_rules"].append(
            {"name": "sneaky", "type": "exec_python", "fields": ["total"]}
        )
        with pytest.raises(PipelineConfigError, match="unknown rule type"):
            parse_definition("dev-tenant", "purchase_order", 1, config)

    def test_a_slug_that_is_not_a_definition_is_rejected_before_any_path_is_built(self):
        from docfactory_core.pipeline import PipelineConfigError

        with pytest.raises(PipelineConfigError, match="invalid pipeline slug"):
            load_from_file("../../etc/passwd")
        with pytest.raises(PipelineConfigError, match="no pipeline definition"):
            load_from_file("bill_of_lading")
