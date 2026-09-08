"""An enum constrains what the model may say, not whether it said the right thing.

`currency` was `enum: ["USD", "EUR"]`, and no validation rule referenced it.
A GBP invoice therefore produced the correct numbers with the wrong currency
code, passed every arithmetic rule — subtotal + tax = total holds in any
currency — and auto-approved. A client posts £12,000 into their ledger as
$12,000 and nothing anywhere flags it. That is the worst error shape this
system can produce: internally consistent and wrong.

The fix is two-sided. The value set is wide enough to express the currency
(otherwise the model has no correct answer available), and the choice has to be
traceable to the document (otherwise a wide set just moves the guess around).
"""

import json
from pathlib import Path

import pytest
from docfactory_core.confidence import score_extraction
from docfactory_core.confidence_model import FEATURES, confidence_model_for, features_for
from docfactory_core.pipeline import PipelineConfigError, evaluate_rules, parse_definition

ROOT = Path(__file__).resolve().parents[1]
PIPELINES = sorted((ROOT / "config" / "pipelines").glob("*.json"))

GBP_TEXT = """INVOICE
Baum Industries Ltd
Invoice INV-2026-00042  Date 2026-03-01  Due 2026-03-31
Widget A  2 x £100.00  £200.00
Subtotal £200.00
VAT 20% £40.00
Total £240.00"""

_RECORD = {
    "vendor": "Baum Industries Ltd",
    "invoice_number": "INV-2026-00042",
    "invoice_date": "2026-03-01",
    "due_date": "2026-03-31",
    "subtotal": "200.00",
    "tax_rate": "0.20",
    "tax": "40.00",
    "total": "240.00",
    "line_items": [
        {"description": "Widget A", "quantity": "2", "unit_price": "100.00", "amount": "200.00"}
    ],
}


def _invoice():
    return parse_definition(
        "dev-tenant",
        "invoice",
        1,
        json.loads((ROOT / "config/pipelines/invoice_v1.json").read_text()),
    )


def _score(currency: str, text: str = GBP_TEXT):
    definition = _invoice()
    record = definition.normalize({**_RECORD, "currency": currency})
    report = score_extraction(
        record, evaluate_rules(record, definition), source_text=text, definition=definition
    )
    return definition, report


def test_a_currency_the_document_never_mentions_is_not_auto_approved() -> None:
    """The property, not the number.

    Asserting "confidence == 0.65" would pass while the document still
    auto-approved, because routing is the fitted model's decision and not the
    heuristic score. Assert what the client actually experiences.
    """
    definition, report = _score("USD")
    model = confidence_model_for(definition)
    probability = model.probability(features_for("currency", report.signals, definition))
    assert probability < model.threshold, (
        f"A GBP invoice labelled USD scored {probability:.4f} against a threshold "
        f"of {model.threshold} and would auto-approve. Every arithmetic rule "
        "passes on it, so this signal is the only thing standing between the "
        "client and a wrong currency in their ledger."
    )


def test_the_correct_currency_still_auto_approves() -> None:
    """The negative test above passes trivially if everything is refused."""
    definition, report = _score("GBP")
    model = confidence_model_for(definition)
    probability = model.probability(features_for("currency", report.signals, definition))
    assert probability >= model.threshold, (
        f"A correctly-labelled GBP invoice scored {probability:.4f}, below the "
        f"{model.threshold} threshold. The check is rejecting correct answers."
    )


def test_the_signal_uses_the_key_the_fitted_model_actually_reads() -> None:
    """This is the mistake that made the first implementation useless.

    confidence_model.features_for reads `groundedness.{field}` and defaults it
    to 1.0. Emitting the result under any other key leaves the fitted model
    seeing a perfectly grounded field: the heuristic doc_confidence drops, the
    reasons list mentions it, and the routing decision does not change at all.
    A test on doc_confidence alone would not have caught that.
    """
    assert "groundedness" in FEATURES, "the feature this signal rides on is gone"
    _, report = _score("USD")
    assert report.signals.get("groundedness.currency") == 0.0, (
        "The ungrounded currency is not reported under `groundedness.currency`, "
        "which is the only key features_for consults. Wherever it is being "
        "written now, the fitted model cannot see it."
    )
    _, ok = _score("GBP")
    assert ok.signals.get("groundedness.currency") == 1.0


def test_symbols_count_as_evidence_not_just_iso_codes() -> None:
    """Documents print "£", almost never "GBP"."""
    _, report = _score("GBP", text="Total £240.00")
    assert report.signals.get("groundedness.currency") == 1.0, (
        "A document showing only the symbol was treated as not mentioning the "
        "currency. Requiring the ISO code would fail nearly every real invoice."
    )


@pytest.mark.parametrize("path", PIPELINES, ids=lambda p: p.name)
def test_every_allowed_currency_declares_how_it_appears(path: Path) -> None:
    """Partial coverage is worse than none.

    A value with no surface forms skips the check silently — and it is exactly
    the value a constrained model reaches for when the right answer is not
    available.
    """
    config = json.loads(path.read_text())
    spec = config["fields"].get("currency")
    if spec is None:
        pytest.skip(f"{path.name} has no currency field")
    values = set(spec["values"])
    forms = set(spec.get("surface_forms", {}))
    assert values == forms, (
        f"{path.name}: currency allows {sorted(values - forms)} with no declared "
        "surface forms. Those values would never be checked."
    )
    assert values > {"USD", "EUR"}, (
        f"{path.name}: currency is back to a two-value enum. A model forced to "
        "choose between USD and EUR on a GBP invoice returns one of them, and "
        "no arithmetic rule notices."
    )


def test_partial_surface_form_coverage_is_a_config_error() -> None:
    """Caught at load, not at scoring time."""
    config = json.loads((ROOT / "config/pipelines/invoice_v1.json").read_text())
    config["fields"]["currency"]["surface_forms"].pop("EUR")
    with pytest.raises(PipelineConfigError, match="surface_forms"):
        parse_definition("dev-tenant", "invoice", 1, config)
