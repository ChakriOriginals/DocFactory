"""Mock-mode extraction pipeline: parse fixture PDFs, run the shared
extraction runner, validate schema + canonicalization. No network, no stack."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from docfactory_core.extraction import run_extraction
from docfactory_core.llm import MockLLMClient
from docfactory_core.parsing import extract_pdf_text

FIXTURES = Path(__file__).parent / "fixtures"


def test_digital_pdf_yields_text_and_scanned_does_not():
    digital = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    scanned = extract_pdf_text((FIXTURES / "scanned.pdf").read_bytes())
    assert len(digital) > 200
    assert scanned == ""


def test_mock_extraction_euro_fixture():
    text = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient())
    invoice = outcome.invoice
    assert invoice is not None, outcome.error
    assert outcome.attempts == 1
    assert outcome.model == "mock:mock-extractor-v1"
    # values are canonicalized: German formats became Decimal/date
    assert invoice.currency == "EUR"
    assert invoice.invoice_number == "RE-2024/9558"
    assert invoice.invoice_date == date(2024, 12, 25)
    assert invoice.total == Decimal("30998.51")
    assert invoice.vendor == "Baum"  # small-caps split ("B"+"aum") repaired
    assert len(invoice.line_items) == 5
    assert invoice.line_items[1].quantity == Decimal("6")


def test_mock_extraction_classic_fixture():
    text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient())
    invoice = outcome.invoice
    assert invoice is not None, outcome.error
    assert invoice.currency == "USD"
    assert invoice.invoice_number == "INV-2026-96748"
    assert invoice.subtotal == Decimal("1152.24")
    assert invoice.tax_rate == Decimal("0.0625")
    assert invoice.vendor == "Underwood Ltd"


def test_mock_is_deterministic():
    text = extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes())
    first = run_extraction(text, MockLLMClient())
    second = run_extraction(text, MockLLMClient())
    assert first.invoice == second.invoice


def test_extraction_serializes_decimals_as_strings():
    text = extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes())
    outcome = run_extraction(text, MockLLMClient())
    dumped = outcome.invoice.model_dump(mode="json")
    assert dumped["total"] == "30998.51"
    assert dumped["invoice_date"] == "2024-12-25"
    assert isinstance(dumped["line_items"][0]["unit_price"], str)
