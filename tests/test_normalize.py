"""R2: normalizer unit tests across all three layouts' formats — written and
green BEFORE any accuracy eval runs, because a normalization bug is
indistinguishable from an extraction failure in the numbers."""

from datetime import date
from decimal import Decimal

import pytest
from docfactory_core.normalize import (
    normalize_amount,
    normalize_date,
    normalize_rate,
    normalize_text,
)

# --- amounts: euro layout (comma decimals, dot thousands, trailing €) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25.832,09 €", "25832.09"),
        ("1.984,90 €", "1984.90"),
        ("768,70 €", "768.70"),
        ("46,04 €", "46.04"),
        ("11,51\u00a0€", "11.51"),  # no-break space before symbol
        ("12.839,82", "12839.82"),
    ],
)
def test_euro_amounts(raw, expected):
    assert normalize_amount(raw) == Decimal(expected)


# --- amounts: classic/modern (US style) ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$1,152.24", "1152.24"),
        ("$22,819.50", "22819.50"),
        ("$72.02", "72.02"),
        ("1,234", "1234"),  # thousands-grouped integer
        ("1234.5", "1234.5"),
        ("$ 36,748.41", "36748.41"),
    ],
)
def test_us_amounts(raw, expected):
    assert normalize_amount(raw) == Decimal(expected)


def test_amount_passthrough_types():
    assert normalize_amount(Decimal("3.00")) == Decimal("3.00")
    assert normalize_amount(3) == Decimal("3")
    assert normalize_amount("3") == Decimal("3")


def test_amount_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_amount("N/A")
    with pytest.raises(ValueError):
        normalize_amount("€")


# --- rates ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("6.25%", "0.0625"),
        ("8.875%", "0.08875"),
        ("20%", "0.2"),
        ("19 %", "0.19"),
        ("0.19", "0.19"),
        ("19", "0.19"),  # bare number above 1 must be a percentage
        ("0", "0"),
    ],
)
def test_rates(raw, expected):
    assert normalize_rate(raw) == Decimal(expected)


# --- dates: all three layouts ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-11-10", date(2025, 11, 10)),  # already canonical
        ("10.11.2025", date(2025, 11, 10)),  # euro: day first
        ("06/06/2026", date(2026, 6, 6)),  # classic: month first
        ("01/17/2026", date(2026, 1, 17)),
        ("Jan 17, 2026", date(2026, 1, 17)),  # modern
        ("Mar 5, 2025", date(2025, 3, 5)),
        ("25.12.2024", date(2024, 12, 25)),
    ],
)
def test_dates(raw, expected):
    assert normalize_date(raw) == expected


def test_date_separator_disambiguates_day_month_order():
    # dot = European day-first, slash = US month-first — same digits, different dates
    assert normalize_date("04.03.2025") == date(2025, 3, 4)
    assert normalize_date("04/03/2025") == date(2025, 4, 3)


def test_date_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_date("sometime in march")


def test_text_collapses_whitespace():
    assert normalize_text("  Bohnbach \u00a0 Ullrich Stiftung ") == "Bohnbach Ullrich Stiftung"
