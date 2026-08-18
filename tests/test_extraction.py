"""Mock-mode extraction pipeline: parse fixture PDFs, run the shared
extraction runner, validate schema + canonicalization. No network, no stack.

The runner is schema-driven: what it asks for, what it accepts, and how it
canonicalizes all come from the pipeline definition passed in.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from docfactory_core.extraction import build_system_prompt, run_extraction
from docfactory_core.llm import MockLLMClient
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline import json_record
from docfactory_core.pipeline_registry import default_pipeline

FIXTURES = Path(__file__).parent / "fixtures"
DEFINITION = default_pipeline()


def test_digital_pdf_yields_text_and_scanned_does_not():
    digital = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    scanned = extract_pdf_text((FIXTURES / "scanned.pdf").read_bytes())
    assert len(digital) > 200
    assert scanned == ""


def test_mock_extraction_euro_fixture():
    text = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient(), DEFINITION)
    record = outcome.record
    assert record is not None, outcome.error
    assert outcome.attempts == 1
    assert outcome.model == "mock:mock-extractor-v1"
    # values are canonicalized: German formats became Decimal/date
    assert record["currency"] == "EUR"
    assert record["invoice_number"] == "RE-2024/9558"
    assert record["invoice_date"] == date(2024, 12, 25)
    assert record["total"] == Decimal("30998.51")
    assert record["vendor"] == "Baum"  # small-caps split ("B"+"aum") repaired
    assert len(record["line_items"]) == 5
    assert record["line_items"][1]["quantity"] == Decimal("6")


def test_mock_extraction_classic_fixture():
    text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient(), DEFINITION)
    record = outcome.record
    assert record is not None, outcome.error
    assert record["currency"] == "USD"
    assert record["invoice_number"] == "INV-2026-96748"
    assert record["subtotal"] == Decimal("1152.24")
    assert record["tax_rate"] == Decimal("0.0625")
    assert record["vendor"] == "Underwood Ltd"


def test_mock_is_deterministic():
    text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
    first = run_extraction(text, MockLLMClient(), DEFINITION)
    second = run_extraction(text, MockLLMClient(), DEFINITION)
    assert first.record == second.record


def test_extraction_serializes_decimals_as_strings():
    text = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient(), DEFINITION)
    dumped = json_record(outcome.record)
    assert dumped["total"] == "30998.51"
    assert dumped["invoice_date"] == "2024-12-25"
    assert isinstance(dumped["line_items"][0]["unit_price"], str)


class TestTheRunnerIsSchemaDriven:
    """Nothing in the runner knows what an invoice is."""

    def test_the_schema_sent_to_the_model_is_the_definition_s(self):
        seen = {}

        class RecordingClient(MockLLMClient):
            def complete(self, *, system, messages, output_schema=None, definition=None):
                seen["schema"] = output_schema
                seen["system"] = system
                return super().complete(
                    system=system,
                    messages=messages,
                    output_schema=output_schema,
                    definition=definition,
                )

        text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
        run_extraction(text, RecordingClient(), DEFINITION)
        assert seen["schema"] is DEFINITION.json_schema

    def test_the_prompt_is_built_from_the_definition(self):
        prompt = build_system_prompt(DEFINITION)
        assert prompt.startswith("You extract structured data from invoice documents.")
        # kind-derived rules
        assert "ISO-8601" in prompt
        # the definition's own field notes
        assert 'a printed "19%" becomes "0.19"' in prompt

    def test_output_failing_the_schema_is_retried_then_reported(self):
        class BrokenClient(MockLLMClient):
            def complete(self, *, system, messages, output_schema=None, definition=None):
                response = super().complete(
                    system=system,
                    messages=messages,
                    output_schema=output_schema,
                    definition=definition,
                )
                return type(response)(
                    content='{"vendor": "X"}',
                    model=response.model,
                    stop_reason=response.stop_reason,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )

        text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
        outcome = run_extraction(text, BrokenClient(), DEFINITION)
        assert outcome.record is None
        assert outcome.attempts == 2
        assert "ValidationError" in outcome.error

    def test_an_unparseable_value_is_a_retryable_error(self):
        class BadDateClient(MockLLMClient):
            def complete(self, *, system, messages, output_schema=None, definition=None):
                response = super().complete(
                    system=system,
                    messages=messages,
                    output_schema=output_schema,
                    definition=definition,
                )
                import json

                payload = json.loads(response.content) | {"invoice_date": "the ides of March"}
                return type(response)(
                    content=json.dumps(payload),
                    model=response.model,
                    stop_reason=response.stop_reason,
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                )

        text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
        outcome = run_extraction(text, BadDateClient(), DEFINITION)
        assert outcome.record is None
        assert "unparseable date" in outcome.error
