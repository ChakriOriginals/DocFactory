"""Date corroboration tests — written before the implementation.

The 2.2c study found `shifted_date` responsible for 10 of the 12 errors that
survived auto-approval: moving the invoice date 3-20 days earlier leaves every
validation rule satisfied, so nothing saw it. This signal asks the question the
rules cannot — does the extracted date actually appear on the page?

Three surface forms exist in the corpus, all verified against real fixtures:
    classic  "06/06/2026"     MM/DD/YYYY
    euro     "25.12.2024"     dd.mm.yyyy
    modern   "Jan 17, 2026"
Euro invoices additionally state the payment term in words ("Zahlbar innerhalb
von 30 Tagen"), which pins the invoice-to-due interval; classic and modern
state no term, so nothing is inferred for them.

The overriding constraint is false positives: a signal that dings correct dates
is worse than the gap it closes, so anything it cannot check scores as fine.
"""

from datetime import date

import pytest
from docfactory_core.date_corroboration import (
    corroborate_date,
    corroborate_payment_term,
    dates_in_text,
    stated_payment_term,
)

EURO_TEXT = (
    "Baum\nRechnungsempfänger Rechnungsnummer: RE-2024/9558\n"
    "Rechnungsdatum: 25.12.2024\nFällig am: 24.01.2025\n"
    "Zahlbar innerhalb von 30 Tagen ohne Abzug. · IBAN: DE09823701359624637032\n"
)
CLASSIC_TEXT = (
    "INVOICE\nUnderwood Ltd\nInvoice No. INV-2026-96748\n"
    "Invoice Date 06/06/2026\nDue Date 07/06/2026\nTotal Due $1,224.26\n"
)
MODERN_TEXT = (
    "TAX INVOICE\nINVOICE NUMBER ISSUED DUE\n"
    "202601-3581 Jan 17, 2026 Feb 16, 2026\nAmount Due $36,748.41\n"
)


class TestDatesAreFoundInEveryLayout:
    def test_euro_dotted_dates(self):
        found = dates_in_text(EURO_TEXT)
        assert date(2024, 12, 25) in found
        assert date(2025, 1, 24) in found

    def test_classic_slashed_dates(self):
        found = dates_in_text(CLASSIC_TEXT)
        assert date(2026, 6, 6) in found
        assert date(2026, 7, 6) in found

    def test_modern_month_name_dates(self):
        found = dates_in_text(MODERN_TEXT)
        assert date(2026, 1, 17) in found
        assert date(2026, 2, 16) in found

    def test_tolerates_whitespace_inside_a_date(self):
        # pdfplumber splits runs ("R ECHNU NG"); dates can suffer the same.
        assert date(2026, 6, 6) in dates_in_text("Invoice Date 06 / 06 / 2026")

    def test_amounts_are_not_mistaken_for_dates(self):
        # "5.166,42 €" and "1,224.26" must not parse as dates
        found = dates_in_text("MwSt. 20% 5.166,42 € Total Due $1,224.26 Subtotal 25.832,09 €")
        assert found == set()


class TestCorroboration:
    def test_a_date_on_the_page_corroborates_fully(self):
        assert corroborate_date(date(2024, 12, 25), dates_in_text(EURO_TEXT)) == 1.0
        assert corroborate_date(date(2026, 6, 6), dates_in_text(CLASSIC_TEXT)) == 1.0
        assert corroborate_date(date(2026, 1, 17), dates_in_text(MODERN_TEXT)) == 1.0

    @pytest.mark.parametrize("shift", [3, 7, 12, 20])
    def test_shifted_dates_score_low(self, shift):
        # exactly the corruption class that defeated the 2.2c scorer
        from datetime import timedelta

        shifted = date(2024, 12, 25) - timedelta(days=shift)
        assert corroborate_date(shifted, dates_in_text(EURO_TEXT)) < 0.5

    def test_bigger_shifts_score_lower(self):
        from datetime import timedelta

        found = dates_in_text(EURO_TEXT)
        near = corroborate_date(date(2024, 12, 25) - timedelta(days=3), found)
        far = corroborate_date(date(2024, 12, 25) - timedelta(days=20), found)
        assert far < near < 1.0

    def test_score_is_continuous_and_bounded(self):
        from datetime import timedelta

        found = dates_in_text(CLASSIC_TEXT)
        for days in range(0, 40):
            score = corroborate_date(date(2026, 6, 6) + timedelta(days=days), found)
            assert 0.0 <= score <= 1.0

    def test_unverifiable_dates_are_not_penalized(self):
        # No dates parsed from the page: we cannot check, so we must not ding.
        # False positives here are worse than the gap this signal closes.
        assert corroborate_date(date(2026, 6, 6), set()) == 1.0


class TestPaymentTerm:
    def test_reads_the_german_term(self):
        assert stated_payment_term(EURO_TEXT) == 30

    def test_layouts_without_a_stated_term_return_none(self):
        assert stated_payment_term(CLASSIC_TEXT) is None
        assert stated_payment_term(MODERN_TEXT) is None

    def test_matching_interval_corroborates(self):
        assert corroborate_payment_term(date(2024, 12, 25), date(2025, 1, 24), 30) == 1.0

    def test_shifted_invoice_date_breaks_the_stated_interval(self):
        # invoice date pulled 13 days earlier -> interval reads 43, not 30
        assert corroborate_payment_term(date(2024, 12, 12), date(2025, 1, 24), 30) < 1.0

    def test_no_stated_term_means_no_opinion(self):
        assert corroborate_payment_term(date(2024, 12, 25), date(2025, 1, 24), None) is None


class TestAgainstRealFixtures:
    @staticmethod
    @pytest.fixture(scope="class")
    def texts():
        from pathlib import Path

        from docfactory_core.parsing import extract_pdf_text

        fixtures = Path(__file__).parent / "fixtures"
        return {
            name: extract_pdf_text((fixtures / f"digital_{name}.pdf").read_bytes())
            for name in ("euro", "classic")
        }

    def test_real_documents_corroborate_their_own_dates(self, texts):
        for text in texts.values():
            found = dates_in_text(text)
            assert found, "no dates parsed from a real invoice"
            for value in found:
                assert corroborate_date(value, found) == 1.0
