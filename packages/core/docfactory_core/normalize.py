"""Locale-aware canonicalization (R2).

One shared normalizer used by BOTH the extraction schema (Pydantic validators)
and the eval comparator. Canonical forms: ISO-8601 dates, Decimal amounts with
'.' separator, no thousands separators, no currency symbols, tax rates as
fractions. Centralizing this is deliberate: a normalization bug that lives in
two places masquerades as an extraction failure and wastes hours.

Known limitation (documented, not corpus-relevant): a bare "1.234" is read as
US decimal 1.234, not EU thousands 1234 — euro amounts in this corpus always
carry a ",XX" decimal part, which disambiguates.
"""

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY_TOKENS = ("USD", "EUR", "GBP", "$", "€", "£")
# regular, no-break, narrow no-break, thin spaces
_WHITESPACE = re.compile(r"[\s   ]+")
_US_GROUPED = re.compile(r"[+-]?\d{1,3}(,\d{3})+")

_DATE_FORMATS = (
    "%m/%d/%Y",  # classic: 06/06/2026
    "%d.%m.%Y",  # euro: 10.11.2025
    "%b %d, %Y",  # modern: Jan 17, 2026
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
)


def normalize_text(value: str) -> str:
    return _WHITESPACE.sub(" ", str(value)).strip()


def normalize_amount(value: str | int | float | Decimal) -> Decimal:
    """'25.832,09 €' -> 25832.09; '$1,152.24' -> 1152.24; '3' -> 3."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    s = str(value)
    for token in _CURRENCY_TOKENS:
        s = s.replace(token, "")
    s = _WHITESPACE.sub("", s)
    if not s:
        raise ValueError(f"no numeric content in amount: {value!r}")

    has_comma, has_dot = "," in s, "." in s
    if has_comma and has_dot:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # EU: 1.234,56
        else:
            s = s.replace(",", "")  # US: 1,234.56
    elif has_comma:
        if _US_GROUPED.fullmatch(s):
            s = s.replace(",", "")  # US thousands only: 1,234
        else:
            s = s.replace(",", ".")  # EU decimal comma: 768,70
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount: {value!r}") from exc


def normalize_rate(value: str | int | float | Decimal) -> Decimal:
    """'6.25%' -> 0.0625; '19 %' -> 0.19; '0.19' -> 0.19; '19' -> 0.19."""
    if isinstance(value, str) and "%" in value:
        return normalize_amount(value.replace("%", "")) / 100
    rate = normalize_amount(value)
    # a rate above 1 can only sensibly be a percentage
    return rate / 100 if rate > 1 else rate


def normalize_date(value: str | date | datetime) -> date:
    """'10.11.2025' -> 2025-11-10; '06/06/2026' -> 2026-06-06; 'Jan 17, 2026' -> 2026-01-17."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = normalize_text(value)
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {value!r}")
