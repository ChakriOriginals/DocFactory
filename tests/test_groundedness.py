"""Groundedness signal tests — written before the implementation.

The 2.1 measurement showed the scorer was blind to wrong `vendor` values
because no arithmetic rule constrains free text. Groundedness asks a different
question: can this string be traced back to the text pdfplumber actually
produced?

The hard part is that a *correct* extraction often does not appear verbatim.
Real observed artifacts from the corpus:
    "B" / "aum"      on separate lines  -> the vendor is "Baum"
    "R ECHNU NG"     intra-word spacing -> the word is "RECHNUNG"
So whitespace must be removed before comparing, not merely collapsed;
otherwise the signal fires hardest on the correct answers it was built to
rescue.
"""

from pathlib import Path

import pytest
from docfactory_core.groundedness import groundedness, is_grounded

FIXTURES = Path(__file__).parent / "fixtures"

# Verbatim from the euro fixture (see tests/fixtures/digital_euro.pdf).
EURO_TEXT = (
    "B\n"
    "aum\n"
    "Kochallee 14 · 67096 Neuss\n"
    "USt-IdNr.: DE932364258 · zorbachramon@lorch.com\n"
    "R ECHNU NG\n"
    "Rechnungsempfänger Rechnungsnummer: RE-2024/9558\n"
)
CLASSIC_TEXT = "INVOICE\nU\nInvoice No. INV-2026-96748\nUnderwood Ltd\n44440 David Plaza Apt. 513\n"


class TestTheEuroFalseNegative:
    """The exact failure that motivated this signal."""

    def test_vendor_split_across_lines_is_fully_grounded(self):
        # "B" + "aum" -> "Baum". A correct extraction must NOT be penalized.
        assert groundedness("Baum", EURO_TEXT) == 1.0

    def test_naive_substring_matching_would_have_failed_here(self):
        # Documents why the normalization exists: the guard is meaningless if
        # the plain match ever starts succeeding.
        assert "Baum" not in EURO_TEXT
        assert groundedness("Baum", EURO_TEXT) == 1.0

    def test_intra_word_spacing_is_grounded(self):
        assert groundedness("RECHNUNG", EURO_TEXT) == 1.0

    def test_hallucinated_vendor_scores_low(self):
        assert groundedness("Acme Corporation", EURO_TEXT) < 0.6

    def test_correct_and_hallucinated_are_separable(self):
        assert groundedness("Baum", EURO_TEXT) > groundedness("Zenith Holdings", EURO_TEXT)


class TestNormalization:
    def test_exact_match_is_fully_grounded(self):
        assert groundedness("Underwood Ltd", CLASSIC_TEXT) == 1.0

    def test_matching_is_case_insensitive(self):
        assert groundedness("underwood ltd", CLASSIC_TEXT) == 1.0
        assert groundedness("UNDERWOOD LTD", CLASSIC_TEXT) == 1.0

    def test_matching_ignores_extra_internal_whitespace(self):
        assert groundedness("Underwood    Ltd", CLASSIC_TEXT) == 1.0

    def test_leading_and_trailing_whitespace_ignored(self):
        assert groundedness("  Underwood Ltd \n", CLASSIC_TEXT) == 1.0

    def test_invoice_number_is_grounded(self):
        assert groundedness("INV-2026-96748", CLASSIC_TEXT) == 1.0
        assert groundedness("RE-2024/9558", EURO_TEXT) == 1.0


class TestScoreIsContinuous:
    """The calibration study needs magnitude, not a boolean (same convention
    as the arithmetic residuals stored in 2.1)."""

    def test_partial_overlap_scores_between_the_extremes(self):
        partial = groundedness("Underwood Limited Partners", CLASSIC_TEXT)
        assert 0.0 < partial < 1.0

    def test_more_overlap_scores_higher(self):
        assert groundedness("Underwood Ltd", CLASSIC_TEXT) > groundedness(
            "Underwood Zzz", CLASSIC_TEXT
        )
        assert groundedness("Underwood Zzz", CLASSIC_TEXT) > groundedness("Qqq Zzz", CLASSIC_TEXT)

    def test_score_is_bounded_to_the_unit_interval(self):
        for value in ("Baum", "Acme Corporation", "", "x" * 500):
            assert 0.0 <= groundedness(value, EURO_TEXT) <= 1.0


class TestEdgeCases:
    def test_empty_value_is_not_grounded(self):
        assert groundedness("", CLASSIC_TEXT) == 0.0
        assert groundedness("   ", CLASSIC_TEXT) == 0.0

    def test_empty_source_text_is_not_grounded(self):
        assert groundedness("Underwood Ltd", "") == 0.0

    def test_is_grounded_applies_the_threshold(self):
        assert is_grounded("Baum", EURO_TEXT) is True
        assert is_grounded("Acme Corporation", EURO_TEXT) is False

    def test_is_pure_and_repeatable(self):
        assert groundedness("Baum", EURO_TEXT) == groundedness("Baum", EURO_TEXT)


class TestAgainstRealFixtures:
    """Same claims, but against the PDFs rather than transcribed strings."""

    @staticmethod
    @pytest.fixture(scope="class")
    def texts():
        from docfactory_core.parsing import extract_pdf_text

        return {
            "euro": extract_pdf_text((FIXTURES / "digital_euro.pdf").read_bytes()),
            "classic": extract_pdf_text((FIXTURES / "digital_classic.pdf").read_bytes()),
        }

    def test_real_euro_vendor_is_grounded(self, texts):
        assert groundedness("Baum", texts["euro"]) == 1.0

    def test_real_classic_vendor_is_grounded(self, texts):
        assert groundedness("Underwood Ltd", texts["classic"]) == 1.0

    def test_vendor_from_the_wrong_document_is_not_grounded(self, texts):
        # A plausible-looking name lifted from another invoice is exactly the
        # error class that scored a blind 1.00 in 2.1.
        assert is_grounded("Underwood Ltd", texts["euro"]) is False
        assert is_grounded("Baum", texts["classic"]) is False
