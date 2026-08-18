"""Deterministic corruption tests — written before the implementation.

Mock extraction is near-perfect (68/74 golden docs scored exactly 1.00 in
2.1), which is degenerate for calibration: a curve fitted on one point is not
a curve. This module manufactures labelled errors so 2.2c has something to
fit, spanning a deliberate range of detectability — from arithmetic drift
(caught by a validation rule) to a subtle date shift (caught by nothing).
"""

import json

import pytest
from docfactory_core.corruption import (
    ERROR_CLASS_FIELDS,
    ERROR_CLASSES,
    apply_corruption,
    document_key,
    plan_corruption,
)
from docfactory_core.extraction import validate_record
from docfactory_core.pipeline import evaluate_rules
from docfactory_core.pipeline_registry import default_pipeline

DEFINITION = default_pipeline()

CLEAN = {
    "vendor": "Bloch Bloch AG",
    "invoice_number": "RE-2025/8824",
    "invoice_date": "2025-11-10",
    "due_date": "2025-12-10",
    "currency": "EUR",
    "subtotal": "1000.00",
    "tax_rate": "0.19",
    "tax": "190.00",
    "total": "1190.00",
    "line_items": [
        {
            "description": "Beratungsleistungen",
            "quantity": "2",
            "unit_price": "300.00",
            "amount": "600.00",
        },
        {
            "description": "Cloud-Hosting",
            "quantity": "4",
            "unit_price": "100.00",
            "amount": "400.00",
        },
    ],
}
SOURCE_TEXT = "Bloch Bloch AG\nKochallee 14\nRE-2025/8824\nBeratungsleistungen\nCloud-Hosting\n"


def corrupt(text="doc-a", rate=1.0, seed=99):
    plan = plan_corruption(text, rate=rate, seed=seed)
    return plan, apply_corruption(json.loads(json.dumps(CLEAN)), plan, seed=seed)


class TestDeterminism:
    def test_same_document_and_seed_plans_identically(self):
        a = plan_corruption("doc-a", rate=0.5, seed=7)
        b = plan_corruption("doc-a", rate=0.5, seed=7)
        assert a == b

    def test_different_documents_can_plan_differently(self):
        plans = {plan_corruption(f"doc-{i}", rate=1.0, seed=7).error_class for i in range(40)}
        assert len(plans) > 1, "corruption should vary across documents"

    def test_seed_changes_the_plan(self):
        keys = {
            tuple(
                (plan_corruption(f"doc-{i}", rate=1.0, seed=s) or "").error_class for i in range(10)
            )
            for s in (1, 2, 3)
        }
        assert len(keys) > 1

    def test_applying_is_repeatable(self):
        _, first = corrupt()
        _, second = corrupt()
        assert first == second

    def test_plan_does_not_depend_on_processing_order(self):
        forward = [plan_corruption(f"d{i}", rate=0.4, seed=5) for i in range(20)]
        backward = [plan_corruption(f"d{i}", rate=0.4, seed=5) for i in reversed(range(20))]
        assert forward == list(reversed(backward))


class TestRate:
    def test_rate_zero_never_corrupts(self):
        assert all(plan_corruption(f"d{i}", rate=0.0, seed=3) is None for i in range(50))

    def test_rate_one_always_corrupts(self):
        assert all(plan_corruption(f"d{i}", rate=1.0, seed=3) is not None for i in range(50))

    def test_intermediate_rate_is_roughly_honoured(self):
        hits = sum(1 for i in range(400) if plan_corruption(f"d{i}", rate=0.35, seed=3))
        assert 0.25 < hits / 400 < 0.45


class TestCorruptionsAreRealisticAndLabelled:
    def test_every_class_is_reachable(self):
        seen = {plan_corruption(f"doc-{i}", rate=1.0, seed=11).error_class for i in range(300)}
        assert seen == set(ERROR_CLASSES)

    @pytest.mark.parametrize("error_class", ERROR_CLASSES)
    def test_output_still_validates_against_the_schema(self, error_class):
        # A corruption rejected at the schema boundary would never reach the
        # study — it must look like a plausible answer.
        payload = json.loads(json.dumps(CLEAN))
        plan = _plan_of_class(error_class)
        corrupted = apply_corruption(payload, plan, seed=4)
        validate_record(corrupted, DEFINITION)

    @pytest.mark.parametrize("error_class", ERROR_CLASSES)
    def test_labelled_fields_actually_change(self, error_class):
        payload = json.loads(json.dumps(CLEAN))
        plan = _plan_of_class(error_class)
        corrupted = apply_corruption(payload, plan, seed=4)
        changed = {k for k in CLEAN if corrupted[k] != CLEAN[k]}
        assert changed, f"{error_class} changed nothing"
        assert changed <= set(ERROR_CLASS_FIELDS[error_class]), (
            f"{error_class} changed {changed}, labelled {ERROR_CLASS_FIELDS[error_class]}"
        )

    def test_arithmetic_drift_breaks_the_totals(self):
        corrupted = apply_corruption(
            json.loads(json.dumps(CLEAN)), _plan_of_class("arithmetic_drift"), seed=4
        )
        record = validate_record(corrupted, DEFINITION)
        assert record["subtotal"] + record["tax"] != record["total"]

    def test_wrong_vendor_is_not_present_in_the_source_text(self):
        from docfactory_core.groundedness import is_grounded

        corrupted = apply_corruption(
            json.loads(json.dumps(CLEAN)), _plan_of_class("wrong_vendor"), seed=4
        )
        assert corrupted["vendor"] != CLEAN["vendor"]
        # this is the class that exercises the 2.2a groundedness signal
        assert is_grounded(corrupted["vendor"], SOURCE_TEXT) is False

    def test_truncated_vendor_mimics_the_observed_artifact(self):
        corrupted = apply_corruption(
            json.loads(json.dumps(CLEAN)), _plan_of_class("truncated_vendor"), seed=4
        )
        # "Bloch Bloch AG" -> "B B AG": initials kept, legal suffix preserved
        assert corrupted["vendor"] == "B B AG"

    def test_transposed_line_amounts_preserve_the_subtotal(self):
        # The permutation is sum-invariant, so line_items_sum_to_subtotal still
        # passes; only the per-row quantity x unit_price check can see it.
        corrupted = apply_corruption(
            json.loads(json.dumps(CLEAN)), _plan_of_class("transposed_line_amounts"), seed=4
        )
        from decimal import Decimal

        total = sum(Decimal(i["amount"]) for i in corrupted["line_items"])
        assert total == Decimal(CLEAN["subtotal"])
        assert [i["amount"] for i in corrupted["line_items"]] != [
            i["amount"] for i in CLEAN["line_items"]
        ]

    def test_shifted_date_keeps_every_validation_rule_passing(self):
        # Deliberately undetectable: the honest blind spot in the study.
        corrupted = apply_corruption(
            json.loads(json.dumps(CLEAN)), _plan_of_class("shifted_date"), seed=4
        )
        record = validate_record(corrupted, DEFINITION)
        assert all(evaluate_rules(record, DEFINITION).values())
        assert corrupted["invoice_date"] != CLEAN["invoice_date"]


def _plan_of_class(error_class: str):
    """First planned corruption of the requested class."""
    for i in range(2000):
        plan = plan_corruption(f"probe-{i}", rate=1.0, seed=11)
        if plan and plan.error_class == error_class:
            return plan
    raise AssertionError(f"never planned {error_class}")


def test_no_corruption_leaves_the_payload_untouched():
    payload = json.loads(json.dumps(CLEAN))
    assert apply_corruption(payload, None, seed=4) == CLEAN


class TestLocalizedMockOutput:
    """Corruption runs on raw mock output, which is localized, not canonical.

    The mock emits amounts and dates as printed ("$1,224.26", "25.832,09 €",
    "06/06/2026") because the runner canonicalizes them downstream. Corruption
    happens before that boundary, so it must normalize its own inputs.
    """

    @pytest.mark.parametrize(
        "total,currency", [("$1,224.26", "USD"), ("25.832,09 €", "EUR"), ("1224.26", "USD")]
    )
    def test_arithmetic_drift_handles_printed_amounts(self, total, currency):
        payload = json.loads(json.dumps(CLEAN)) | {"total": total, "currency": currency}
        corrupted = apply_corruption(payload, _plan_of_class("arithmetic_drift"), seed=4)
        validate_record(corrupted, DEFINITION)  # must remain schema-valid

    @pytest.mark.parametrize("issued", ["06/06/2026", "25.12.2024", "Jan 17, 2026", "2025-11-10"])
    def test_shifted_date_handles_printed_dates(self, issued):
        payload = json.loads(json.dumps(CLEAN)) | {"invoice_date": issued, "due_date": "2027-01-01"}
        corrupted = apply_corruption(payload, _plan_of_class("shifted_date"), seed=4)
        record = validate_record(corrupted, DEFINITION)
        assert record["due_date"] >= record["invoice_date"]


class TestDocumentKeyStability:
    """A mislabelling bug found in 2.2c: the prompt wrapper made the client and
    the study hash different strings for the same document."""

    def test_surrounding_whitespace_does_not_change_the_plan(self):
        text = "Underwood Ltd\nInvoice No. INV-2026-96748\n"
        wrapped = f"\n\n{text}"  # exactly what the prompt marker leaves behind
        assert document_key(text) == document_key(wrapped)
        assert plan_corruption(text, rate=1.0, seed=3) == plan_corruption(wrapped, rate=1.0, seed=3)

    def test_the_client_and_a_caller_agree_on_the_plan(self):
        from docfactory_core.extraction import build_user_message
        from docfactory_core.llm import MockLLMClient

        text = "Bloch Bloch AG\nRE-2025/8824\nGesamtbetrag 1.190,00 €\n"
        prompt = build_user_message(text, DEFINITION)
        recovered = MockLLMClient._document_text([{"role": "user", "content": prompt}])
        assert plan_corruption(recovered, rate=1.0, seed=9) == plan_corruption(
            text, rate=1.0, seed=9
        )
