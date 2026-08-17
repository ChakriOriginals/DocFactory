"""Deterministic post-extraction validation.

These re-check the exact consistency rules the synthetic generator enforces
by construction — so on a correct extraction of a well-formed invoice every
rule passes, and a failure localizes the error. Results are stored per-rule
on the extraction row; Phase 2's confidence scoring builds on these signals
(not built yet, deliberately).
"""

from decimal import Decimal

from docfactory_core.schemas import Invoice

_CENT = Decimal("0.01")
# subtotal * rate can differ from the printed tax by a rounding step
_RATE_TOLERANCE = Decimal("0.02")


def validate_invoice(invoice: Invoice) -> dict[str, bool]:
    line_sum = sum((item.amount for item in invoice.line_items), Decimal("0"))
    return {
        "line_items_sum_to_subtotal": abs(line_sum - invoice.subtotal) <= _CENT,
        "subtotal_plus_tax_equals_total": abs(invoice.subtotal + invoice.tax - invoice.total)
        <= _CENT,
        "tax_matches_rate": abs(invoice.subtotal * invoice.tax_rate - invoice.tax)
        <= _RATE_TOLERANCE,
        # canonicalization already proved these parse; recorded so the signal
        # is explicit on the extraction row
        "invoice_date_parses": invoice.invoice_date is not None,
        "due_date_not_before_invoice_date": invoice.due_date >= invoice.invoice_date,
    }
