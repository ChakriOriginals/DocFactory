"""Deterministic validation rules — pass on consistent data, localize errors.

The rules themselves are config now (`config/pipelines/invoice_v1.json`), so
these run the shared rule engine against the seeded invoice definition. The
cases are the ones the hardcoded `validate_invoice` was written against; they
are kept because what they assert is a property of the *invoice pipeline*, not
of the function that used to implement it.
"""

from docfactory_core.pipeline import evaluate_rules
from docfactory_core.pipeline_registry import default_pipeline

DEFINITION = default_pipeline()


def _rules(**overrides) -> dict[str, bool]:
    base = {
        "vendor": "Acme GmbH",
        "invoice_number": "RE-2025/1234",
        "invoice_date": "2025-03-01",
        "due_date": "2025-03-31",
        "currency": "EUR",
        "subtotal": "100.00",
        "tax_rate": "0.19",
        "tax": "19.00",
        "total": "119.00",
        "line_items": [
            {"description": "A", "quantity": "2", "unit_price": "25.00", "amount": "50.00"},
            {"description": "B", "quantity": "1", "unit_price": "50.00", "amount": "50.00"},
        ],
    }
    return evaluate_rules(DEFINITION.normalize(base | overrides), DEFINITION)


def test_consistent_invoice_passes_all_rules():
    assert all(_rules().values())


def test_wrong_total_fails_only_the_total_rule():
    result = _rules(total="120.00")
    assert result["subtotal_plus_tax_equals_total"] is False
    assert result["line_items_sum_to_subtotal"] is True


def test_wrong_line_item_fails_sum_rule():
    result = _rules(
        line_items=[
            {"description": "A", "quantity": "2", "unit_price": "25.00", "amount": "51.00"},
            {"description": "B", "quantity": "1", "unit_price": "50.00", "amount": "50.00"},
        ]
    )
    assert result["line_items_sum_to_subtotal"] is False


def test_due_before_invoice_date_fails_rule():
    result = _rules(due_date="2025-02-01")
    assert result["due_date_not_before_invoice_date"] is False


def test_rounding_within_one_cent_tolerated():
    # 33.33 + 33.33 + 33.34 = 100.00; tax 8.875% -> 8.88 (rounded)
    result = _rules(tax_rate="0.08875", tax="8.88", total="108.88")
    assert result["subtotal_plus_tax_equals_total"] is True
    assert result["tax_matches_rate"] is True
