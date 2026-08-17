"""The generator's consistency invariants are Phase 1's validation contract —
these tests pin them down."""

from decimal import Decimal

from invoices import LAYOUTS, SCAN_FRACTION, generate_corpus


def test_arithmetic_consistency():
    for invoice in generate_corpus(150, seed=99):
        for item in invoice.line_items:
            assert item.amount == (item.quantity * item.unit_price).quantize(Decimal("0.01"))
        assert invoice.subtotal == sum(item.amount for item in invoice.line_items)
        assert invoice.subtotal + invoice.tax == invoice.total
        assert invoice.due_date > invoice.invoice_date


def test_layout_and_scan_mix():
    corpus = generate_corpus(300, seed=7)
    layouts = {invoice.layout for invoice in corpus}
    assert layouts == set(LAYOUTS)
    scanned_fraction = sum(1 for invoice in corpus if invoice.scanned) / len(corpus)
    assert abs(scanned_fraction - SCAN_FRACTION) < 0.1
    # the multi-row stress layout really is multi-row
    assert all(len(i.line_items) >= 6 for i in corpus if i.layout == "modern")


def test_corpus_is_deterministic():
    assert generate_corpus(20, seed=42) == generate_corpus(20, seed=42)
    assert generate_corpus(20, seed=42) != generate_corpus(20, seed=43)


def test_euro_layout_has_vat_identity():
    for invoice in generate_corpus(100, seed=5):
        if invoice.layout == "euro":
            assert invoice.currency == "EUR"
            assert invoice.vendor.vat_id and invoice.vendor.vat_id.startswith("DE")
        else:
            assert invoice.currency == "USD"
