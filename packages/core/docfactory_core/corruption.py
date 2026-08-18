"""Deterministic, labelled error injection for calibration.

Mock extraction is near-perfect — 68 of 74 golden documents scored exactly
1.00 in 2.1 — and a calibration curve fitted on a single point is not a
curve. This module manufactures errors with known labels so the study has a
y-variable, without spending API budget and without hand-labelling.

Two properties make it usable as an experimental control:

*Deterministic.* The plan is a pure function of (seed, document identity), so
re-running the study produces the same corrupted corpus, and the study can
recompute a document's error class independently instead of having labels
threaded back through the LLM interface. Same convention as the Phase 0
corpus generator: seed the RNG from a stable document key, never from
processing order.

*Spanning a range of detectability.* The classes are chosen so the fitted
model has something to discriminate rather than one trivially-caught error
type — from arithmetic drift, which a validation rule catches outright, to a
subtle date shift that no current signal can see. That last one is included
deliberately: a study that only injects catchable errors reports a flattering
number and hides the ceiling.
"""

import hashlib
import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

ERROR_CLASSES = (
    "arithmetic_drift",  # total no longer equals subtotal + tax
    "wrong_vendor",  # plausible name absent from the document (tests groundedness)
    "truncated_vendor",  # small-caps artifact: "Bloch Bloch AG" -> "B B AG"
    "transposed_line_amounts",  # sum-invariant swap; only per-row arithmetic sees it
    "shifted_date",  # every rule still passes — the deliberate blind spot
)

# What each class makes wrong. Fixed per class so the plan stays a pure
# function of the document key and never has to inspect the payload.
ERROR_CLASS_FIELDS: dict[str, tuple[str, ...]] = {
    "arithmetic_drift": ("total",),
    "wrong_vendor": ("vendor",),
    "truncated_vendor": ("vendor",),
    "transposed_line_amounts": ("line_items",),
    "shifted_date": ("invoice_date",),
}

# Plausible-looking companies that do not appear anywhere in the corpus, so a
# groundedness check has a fair chance of catching the substitution.
_IMPOSTOR_VENDORS = (
    "Meridian Logistics GmbH",
    "Halcyon Systems Ltd",
    "Northgate Industrial PLC",
    "Calder & Vance AG",
    "Tessellate Partners KG",
    "Ironwood Supply Co.",
)

# Legal-form suffixes survive the small-caps artifact intact, so the
# truncation mimic keeps them rather than reducing them to an initial.
_LEGAL_SUFFIXES = frozenset(
    {"ag", "kg", "gmbh", "ohg", "plc", "ltd", "inc", "llc", "e.v.", "e.g.", "co.", "&"}
)

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class Corruption:
    error_class: str
    fields: tuple[str, ...]


def document_key(text: str) -> str:
    """Stable identity for a document, independent of processing order."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan_corruption(text: str, *, rate: float, seed: int) -> Corruption | None:
    """Decide whether and how to corrupt — pure in (text, rate, seed)."""
    if rate <= 0:
        return None
    rng = random.Random(f"{seed}:plan:{document_key(text)}")
    if rng.random() >= rate:
        return None
    error_class = rng.choice(ERROR_CLASSES)
    return Corruption(error_class, ERROR_CLASS_FIELDS[error_class])


def apply_corruption(payload: dict, corruption: Corruption | None, *, seed: int) -> dict:
    """Apply a planned corruption in place and return the payload.

    The result must stay schema-valid: an extraction that fails Pydantic is
    rejected before scoring and would never reach the study.
    """
    if corruption is None:
        return payload
    rng = random.Random(f"{seed}:apply:{corruption.error_class}:{payload.get('invoice_number')}")
    handler = _HANDLERS[corruption.error_class]
    handler(payload, rng)
    return payload


def _drift_total(payload: dict, rng: random.Random) -> None:
    total = Decimal(str(payload["total"]))
    # Big enough to clear the validator's one-cent tolerance, small enough to
    # look like a misread digit rather than a different document.
    magnitude = max(total * Decimal(str(rng.uniform(0.005, 0.05))), Decimal("1.00"))
    drift = magnitude if rng.random() < 0.5 else -magnitude
    payload["total"] = str((total + drift).quantize(_CENT))


def _swap_vendor(payload: dict, rng: random.Random) -> None:
    current = str(payload["vendor"])
    choices = [v for v in _IMPOSTOR_VENDORS if v != current]
    payload["vendor"] = rng.choice(choices)


def _truncate_vendor(payload: dict, rng: random.Random) -> None:
    tokens = str(payload["vendor"]).split()
    if not tokens:
        return
    truncated = [token if token.casefold() in _LEGAL_SUFFIXES else token[0] for token in tokens]
    payload["vendor"] = " ".join(truncated)


def _transpose_line_amounts(payload: dict, rng: random.Random) -> None:
    items = payload["line_items"]
    if len(items) >= 2:
        i, j = rng.sample(range(len(items)), 2)
        items[i]["amount"], items[j]["amount"] = items[j]["amount"], items[i]["amount"]
    else:
        # Single-row invoice: transpose within the row instead, which breaks
        # quantity x unit_price the same way.
        row = items[0]
        row["quantity"], row["unit_price"] = row["unit_price"], row["quantity"]


def _shift_date(payload: dict, rng: random.Random) -> None:
    # Shift the invoice date *earlier* only: due_date stays later, so
    # due_date >= invoice_date still holds and no rule fires.
    issued = date.fromisoformat(str(payload["invoice_date"]))
    payload["invoice_date"] = (issued - timedelta(days=rng.randint(3, 20))).isoformat()


_HANDLERS = {
    "arithmetic_drift": _drift_total,
    "wrong_vendor": _swap_vendor,
    "truncated_vendor": _truncate_vendor,
    "transposed_line_amounts": _transpose_line_amounts,
    "shifted_date": _shift_date,
}
