"""Confidence scorer unit tests.

Mock mode produces near-perfect extractions, so the interesting paths only
exist if the tests build broken invoices deliberately. These pin behaviour
(ordering, attribution, clamping) rather than exact scores — the weights are
uncalibrated priors and are expected to move when the calibration study fits
them, at which point asserting on magnitudes would just be churn.
"""

from decimal import Decimal

import pytest
from docfactory_core.confidence import (
    RULE_FIELDS,
    failed_extraction_report,
    looks_fragmented,
    score_extraction,
)
from docfactory_core.schemas import SCALAR_FIELD_NAMES, Invoice
from docfactory_core.validation import validate_invoice


def make_invoice(**overrides) -> Invoice:
    """A clean, internally consistent invoice; override to break one thing."""
    base = {
        "vendor": "Fernandez-Harris",
        "invoice_number": "INV-2026-00042",
        "invoice_date": "2026-01-17",
        "due_date": "2026-02-16",
        "currency": "USD",
        "subtotal": "1000.00",
        "tax_rate": "0.075",
        "tax": "75.00",
        "total": "1075.00",
        "line_items": [
            {
                "description": "Consulting services",
                "quantity": "2",
                "unit_price": "400.00",
                "amount": "800.00",
            },
            {
                "description": "Cloud hosting",
                "quantity": "1",
                "unit_price": "200.00",
                "amount": "200.00",
            },
        ],
    }
    return Invoice.model_validate(base | overrides)


def score(invoice: Invoice, *, attempts: int = 1):
    return score_extraction(invoice, validate_invoice(invoice), attempts=attempts)


def test_clean_invoice_scores_full_confidence():
    report = score(make_invoice())
    assert report.doc_confidence == 1.0
    assert report.reasons == ()
    assert all(f.confidence == 1.0 for f in report.fields.values())


def test_every_scalar_field_and_line_items_are_scored():
    report = score(make_invoice())
    assert set(report.fields) == {*SCALAR_FIELD_NAMES, "line_items"}


def test_broken_totals_lower_document_confidence():
    broken = make_invoice(total="9999.00")
    assert score(broken).doc_confidence < score(make_invoice()).doc_confidence


def test_failed_rule_penalizes_only_the_fields_it_implicates():
    # subtotal + tax != total implicates subtotal, tax, total — not the dates.
    report = score(make_invoice(total="9999.00"))
    implicated = RULE_FIELDS["subtotal_plus_tax_equals_total"]
    for name in implicated:
        assert report.fields[name].confidence < 1.0, name
    for name in ("invoice_date", "due_date", "currency", "invoice_number"):
        assert report.fields[name].confidence == 1.0, name


def test_field_at_intersection_of_failures_scores_lowest():
    # Wrong subtotal breaks all three arithmetic rules; subtotal participates
    # in every one of them, so it must rank below tax_rate (only one rule).
    report = score(make_invoice(subtotal="12.00"))
    assert report.fields["subtotal"].confidence < report.fields["tax_rate"].confidence
    # lowest of every scored field: it sits in all three failing rules
    assert report.fields["subtotal"].confidence == min(f.confidence for f in report.fields.values())


def test_date_ordering_failure_is_penalized_more_gently_than_arithmetic():
    dates_wrong = score(make_invoice(due_date="2025-01-01"))
    totals_wrong = score(make_invoice(total="9999.00"))
    assert dates_wrong.doc_confidence > totals_wrong.doc_confidence
    assert dates_wrong.doc_confidence < 1.0


def test_schema_retry_lowers_confidence():
    assert score(make_invoice(), attempts=2).doc_confidence < 1.0
    assert "needed_schema_retry" in score(make_invoice(), attempts=2).reasons


def test_confidence_is_clamped_to_unit_interval():
    # Break everything at once; penalties sum past 1.0 but must not go negative.
    wrecked = make_invoice(
        vendor="B W S & C . Kg a",
        subtotal="12.00",
        total="-5.00",
        due_date="2020-01-01",
    )
    report = score(wrecked, attempts=2)
    assert report.doc_confidence == 0.0
    assert all(0.0 <= f.confidence <= 1.0 for f in report.fields.values())


def test_reasons_name_the_failing_rules():
    report = score(make_invoice(total="9999.00"))
    assert "failed:subtotal_plus_tax_equals_total" in report.reasons
    assert "failed:subtotal_plus_tax_equals_total" in report.fields["total"].reasons


def test_signals_carry_residual_magnitudes_not_just_booleans():
    report = score(make_invoice(total="1200.00"))
    assert report.signals["rule.subtotal_plus_tax_equals_total"] is False
    # 1000 + 75 - 1200 = -125
    assert report.signals["residual.subtotal_plus_tax_vs_total"] == pytest.approx(-125.0)
    assert report.signals["line_item_count"] == 2
    assert report.signals["attempts"] == 1


def test_near_miss_and_wild_miss_are_distinguishable_in_signals():
    near = score(make_invoice(total="1075.50"))
    wild = score(make_invoice(total="99999.00"))
    assert abs(near.signals["residual.subtotal_plus_tax_vs_total"]) < abs(
        wild.signals["residual.subtotal_plus_tax_vs_total"]
    )


def test_failed_extraction_scores_zero():
    report = failed_extraction_report("ValidationError: vendor missing")
    assert report.doc_confidence == 0.0
    assert report.signals["extraction_failed"] is True
    assert report.fields == {}


def test_line_item_cells_inherit_the_parent_score():
    report = score(make_invoice(subtotal="12.00"))
    parent = report.fields["line_items"].confidence
    assert report.field_confidence("line_items.0.amount") == parent
    assert report.field_confidence("line_items.count") == parent
    assert report.field_confidence("not_a_field") is None


class TestVendorFragmentation:
    """The euro small-caps artifact measured in Phase 1, as a live signal."""

    @pytest.mark.parametrize(
        "vendor",
        [
            "B W S & C . Kg a",  # observed pdfplumber output
            "auer einhold tiftung o a",  # its sibling run
            "A B C D Corp",
        ],
    )
    def test_flags_split_character_runs(self, vendor):
        assert looks_fragmented(vendor) is True

    @pytest.mark.parametrize(
        "vendor",
        [
            "Bohnbach Ullrich Stiftung & Co. KGaA",
            "Fernandez-Harris",
            "J P Morgan",  # ordinary initials must not trip it
            "Sanford-Cordova",
            "3M",
        ],
    )
    def test_accepts_ordinary_names(self, vendor):
        assert looks_fragmented(vendor) is False

    def test_fragmented_vendor_lowers_document_and_field_score(self):
        report = score(make_invoice(vendor="B W S & C . Kg a"))
        assert report.doc_confidence < 1.0
        assert report.fields["vendor"].confidence < 1.0
        assert "looks_fragmented" in report.fields["vendor"].reasons
        # arithmetic is untouched, so money fields stay clean
        assert report.fields["total"].confidence == 1.0


def test_scoring_is_pure_and_repeatable():
    invoice = make_invoice(total="9999.00")
    first, second = score(invoice), score(invoice)
    assert first == second


def test_short_vendor_is_flagged_as_implausible():
    report = score(make_invoice(vendor="X"))
    assert report.fields["vendor"].confidence < 1.0
    assert "implausibly_short" in report.fields["vendor"].reasons


def test_inconsistent_line_row_is_localized():
    # An empty description cannot reach the scorer (the schema rejects it), so
    # the reachable per-row signal is arithmetic: 2 x 400 != 750.
    invoice = make_invoice(
        line_items=[
            {
                "description": "Consulting services",
                "quantity": "2",
                "unit_price": "400.00",
                "amount": "750.00",
            },
            {
                "description": "Cloud hosting",
                "quantity": "1",
                "unit_price": "200.00",
                "amount": "200.00",
            },
        ],
        subtotal="950.00",
        tax="71.25",
        total="1021.25",
    )
    report = score(invoice)
    # row 0 named explicitly, row 1 exonerated
    assert report.signals["line_items.inconsistent_rows"] == [0]
    assert report.fields["line_items"].confidence < 1.0
    assert "row_arithmetic:[0]" in report.fields["line_items"].reasons


def test_decimal_precision_is_preserved_in_residuals():
    invoice = make_invoice(tax="75.01", total="1075.01")
    report = score(invoice)
    # 1000 * 0.075 = 75.00 vs 75.01 -> within tolerance, rule still passes
    assert report.signals["rule.tax_matches_rate"] is True
    assert report.signals["residual.tax_vs_rate"] == pytest.approx(float(Decimal("-0.01")))


class TestTruncatedVendorDetection:
    """The 2.2a measurement's real finding.

    Nine of the ten errors invisible to the 2.1 scorer were *truncations*, not
    hallucinations: the extractor kept only the small-caps capitals, turning
    "Bloch Bloch AG" into "B B AG". Groundedness cannot see these — the kept
    characters genuinely are in the source text — so the discriminator is
    token shape. Measured across all 500 ground-truth vendor names the lowest
    legitimate mean token length is 2.80, while every observed truncation is
    at or below 1.67, so the rule separates cleanly rather than being fitted
    to the golden set.
    """

    @pytest.mark.parametrize(
        "truncated,real",
        [
            ("B B AG", "Bloch Bloch AG"),
            ("P G H", "Pärtzelt GmbH"),
            ("R g", "Rogge GbR"),
            ("S K .G.", "Schleich Kostolzin e.G."),
            ("W AG", "Wulf AG"),
            ("B kG", "Becker KG"),
            ("H R .V.", "Heinz Rohleder e.V."),
            ("L W .V.", "Löchel Warmer e.V."),
            ("G S AG", "Gröttner Schottin AG"),
        ],
    )
    def test_observed_truncations_are_flagged(self, truncated, real):
        assert looks_fragmented(truncated) is True, truncated
        assert looks_fragmented(real) is False, real

    @pytest.mark.parametrize(
        "vendor",
        [
            "Hamann AG & Co. KG",  # lowest mean token length in the whole corpus
            "Jacob AG & Co. OHG",
            "Cox PLC",
            "Hein KG",
            "Wulf AG",
            "Seip AG",
            "J P Morgan",
            "3M",
        ],
    )
    def test_short_but_legitimate_names_are_not_flagged(self, vendor):
        assert looks_fragmented(vendor) is False

    def test_truncated_vendor_lowers_the_vendor_field_score(self):
        report = score(make_invoice(vendor="B B AG"))
        assert report.fields["vendor"].confidence < 1.0
        assert report.doc_confidence < 1.0

    def test_casing_only_errors_remain_undetectable(self):
        # "RöhRicht" for "Röhricht" — the tenth miss. No shape or groundedness
        # signal can see this; recorded so the limitation stays visible.
        assert looks_fragmented("RöhRicht") is False
        assert score(make_invoice(vendor="RöhRicht")).doc_confidence == 1.0
