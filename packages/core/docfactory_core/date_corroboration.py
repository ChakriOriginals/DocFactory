"""Date corroboration: does an extracted date actually appear on the page?

The 2.2c calibration study found `shifted_date` responsible for 10 of the 12
errors that survived auto-approval. Moving an invoice date 3-20 days earlier
leaves every validation rule satisfied — the date still parses, and due_date is
still after it — so the scorer had no way to see it. Arithmetic rules constrain
money; nothing constrained dates against the document itself.

This asks the question the rules cannot: is this date one of the dates printed
on the page? A correct extraction matches exactly. A shifted one matches
nothing, because the shift never lands on another date the document contains.

Two corroborations, both grounded in what the corpus actually prints:

*Surface match.* All three layouts are parsed — classic "06/06/2026",
euro "25.12.2024", modern "Jan 17, 2026" — and the score decays with distance
to the nearest date on the page, so a near-miss and a wild miss are
distinguishable (the residual-magnitude convention from 2.1).

*Stated payment term.* Euro invoices print "Zahlbar innerhalb von 30 Tagen",
which pins the invoice-to-due interval. Classic and modern print no term, so
nothing is inferred for them — an invented constraint would fire on correct
documents.

Everything here is biased against false positives: a signal that dings correct
dates is worse than the gap it closes, so anything unverifiable scores 1.0.
"""

import re
from datetime import date

# Separators may be padded, and pdfplumber can split runs mid-token, so spaces
# are tolerated between date components rather than assumed absent.
_NUMERIC_DATE = re.compile(r"\b(\d{1,2})\s*([./])\s*(\d{1,2})\s*\2\s*(\d{4})\b")
_MONTH_NAME_DATE = re.compile(r"\b([A-Za-zÄÖÜäöü]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")

# "Zahlbar innerhalb von 30 Tagen" (euro layout) and the English form, in case
# a future template prints it. Nothing else in the corpus states a term.
_PAYMENT_TERMS = (
    re.compile(r"innerhalb\s+von\s+(\d{1,3})\s+Tagen", re.IGNORECASE),
    re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE),
)

_MONTHS = {
    "jan": 1, "january": 1, "januar": 1,
    "feb": 2, "february": 2, "februar": 2,
    "mar": 3, "march": 3, "mär": 3, "maerz": 3, "märz": 3,
    "apr": 4, "april": 4,
    "may": 5, "mai": 5,
    "jun": 6, "june": 6, "juni": 6,
    "jul": 7, "july": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12, "dez": 12, "dezember": 12,
}  # fmt: skip


def dates_in_text(text: str) -> set[date]:
    """Every date the document plausibly prints, in any corpus locale.

    Ambiguous numeric forms are read both ways (06/07 could be 6 July or
    7 June) and both readings are kept. That deliberately over-collects: a
    superfluous candidate can only make corroboration more forgiving, which is
    the safe direction when the cost of a false positive is flagging a correct
    document.
    """
    found: set[date] = set()

    for first, separator, second, year in _NUMERIC_DATE.findall(text):
        a, b, y = int(first), int(second), int(year)
        # "/" is month-first in this corpus, "." is day-first; keep both anyway.
        for month, day in ((a, b), (b, a)):
            try:
                found.add(date(y, month, day))
            except ValueError:
                continue

    for name, day, year in _MONTH_NAME_DATE.findall(text):
        month = _MONTHS.get(name.casefold())
        if month:
            try:
                found.add(date(int(year), month, int(day)))
            except ValueError:
                continue

    for year, month, day in _ISO_DATE.findall(text):
        try:
            found.add(date(int(year), int(month), int(day)))
        except ValueError:
            continue

    return found


def corroborate_date(value: date, text_dates: set[date]) -> float:
    """1.0 when the date is printed on the page, decaying with distance.

    ``1 / (1 + days)`` — one formula, no tuned constants: an exact match scores
    1.0, one day out scores 0.5, and the deliberate 3-20 day corruption lands
    between 0.25 and 0.05.

    An empty candidate set means the page yielded no parsable dates at all, so
    the claim cannot be checked. That returns 1.0 rather than 0.0: unverifiable
    is not the same as wrong, and penalizing it would flag correct documents.
    """
    if not text_dates:
        return 1.0
    nearest = min(abs((value - candidate).days) for candidate in text_dates)
    return round(1.0 / (1.0 + nearest), 4)


def stated_payment_term(text: str) -> int | None:
    """Payment term in days if the document states one, else None."""
    for pattern in _PAYMENT_TERMS:
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def corroborate_payment_term(
    invoice_date: date, due_date: date, term_days: int | None
) -> float | None:
    """How well the extracted interval matches the term the document states.

    None when no term is stated — most of the corpus — which the caller must
    treat as "no opinion" rather than as a failure.
    """
    if term_days is None:
        return None
    drift = abs((due_date - invoice_date).days - term_days)
    return round(1.0 / (1.0 + drift), 4)
