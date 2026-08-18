"""Groundedness: can an extracted value be traced back to the source text?

The 2.1 measurement found the confidence scorer blind to wrong `vendor`
values — every validation rule constrains money or dates, so a free-text field
is completely unguarded and a plausible-looking wrong answer scored a perfect
1.00. This module supplies the missing signal class by asking a question the
arithmetic rules cannot: does this string actually appear in the text
pdfplumber produced?

The subtlety is that a *correct* extraction frequently does not appear
verbatim. Observed in the corpus:

    "B" / "aum"   on separate lines   -> the vendor really is "Baum"
    "R ECHNU NG"  intra-word spacing  -> the word really is "RECHNUNG"

both caused by WeasyPrint rendering small-caps as separate text runs. A naive
`value in text` check therefore reports "ungrounded" for exactly the correct
answers this signal exists to protect. So whitespace is *removed* before
comparing, not merely collapsed.

The score is continuous, matching the convention 2.1 set for arithmetic
residuals: the calibration study needs magnitude, not a boolean.
"""

from difflib import SequenceMatcher

# "Most of the string traces back." An uncalibrated prior like the 2.1
# penalties — 2.2c fits it against labelled errors.
GROUNDEDNESS_THRESHOLD = 0.85

# Only runs of at least this many characters count as evidence in the fuzzy
# path. Single characters are noise: any short string can be assembled from
# scattered letters of a long document, which would ground a hallucination.
_MIN_EVIDENCE_RUN = 3


def _squash(value: str) -> str:
    """Case-fold and delete every whitespace character.

    Deleting rather than collapsing is what rejoins split character runs
    ("B\\naum" -> "baum") and repairs intra-word spacing ("R ECHNU NG" ->
    "rechnung").
    """
    return "".join(value.split()).casefold()


def groundedness(value: str, source_text: str) -> float:
    """0.0-1.0: how much of `value` can be traced into `source_text`.

    1.0 means the whole (whitespace-insensitive) string is present. Lower
    scores mean progressively less of it aligns, so a hallucinated value and a
    near-miss are distinguishable rather than both being "not found".
    """
    needle = _squash(value)
    haystack = _squash(source_text)
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0

    matcher = SequenceMatcher(None, needle, haystack, autojunk=False)
    evidence = sum(
        block.size for block in matcher.get_matching_blocks() if block.size >= _MIN_EVIDENCE_RUN
    )
    return round(min(1.0, evidence / len(needle)), 4)


def is_grounded(value: str, source_text: str, threshold: float = GROUNDEDNESS_THRESHOLD) -> bool:
    return groundedness(value, source_text) >= threshold
