"""The template-swap experiment's moving parts, minus the rendering.

Rendering PDFs needs WeasyPrint's native libraries and takes minutes; the
experiment itself is a script you run deliberately. What is tested here is the
part that can silently go wrong between runs: that the new layout stayed out of
the default corpus, and that the measurement functions the write-up's numbers
come from mean what they say.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data" / "synth"))

import invoices
from docfactory_evals.drift_experiment import (
    Step,
    detection_index,
    first_breach_index,
    rolling_auto_approve_rate,
    rolling_auto_approved_precision,
    swap_index,
)


def step(index: int, *, phase="stable", approved=True, correct=10, total=10, flagged=()) -> Step:
    return Step(
        index=index,
        phase=phase,
        layout="classic",
        doc_id=f"inv-{index:05d}",
        routing_decision="approved" if approved else "needs_review",
        doc_confidence=0.9,
        validation_passed=True,
        escalated=False,
        cost_usd=0.001,
        fields_correct=correct,
        fields_total=total,
        all_correct=correct == total,
        drift_status="stable",
        flagged=list(flagged),
    )


class TestTheCorpusIsUnchanged:
    """The golden set is a hash-stable split of the corpus. A fourth layout in
    the default mix would move which documents are golden, change the invoice
    eval numbers, and turn the drift experiment into a corpus change."""

    def test_the_redesign_is_not_in_the_default_layouts(self):
        assert invoices.LAYOUTS == ("classic", "modern", "euro")
        assert "redesign" not in invoices.LAYOUTS
        assert invoices.DRIFT_LAYOUTS == ("redesign",)

    def test_the_default_corpus_is_byte_for_byte_what_it_was(self):
        """Seeded generation with no arguments must be exactly the old call.

        The signature below was taken from the generator BEFORE the drift
        experiment added its keyword arguments. If adding them perturbed the
        random stream by so much as one draw, every document after the first
        would differ and the golden split would move.
        """
        corpus = invoices.generate_corpus(6, seed=1337)
        signature = [(inv.doc_id, inv.layout, inv.scanned, str(inv.total)) for inv in corpus]
        assert signature == [
            ("inv-00001", "euro", False, "30998.51"),
            ("inv-00002", "classic", True, "1077.30"),
            ("inv-00003", "euro", False, "6719.28"),
            ("inv-00004", "modern", False, "36748.41"),
            ("inv-00005", "modern", False, "34344.68"),
            ("inv-00006", "modern", True, "24091.31"),
        ]

    def test_the_experiment_corpus_is_all_redesign_and_never_scanned(self):
        corpus = invoices.generate_corpus(
            12, seed=99, layouts=invoices.DRIFT_LAYOUTS, scan_fraction=0.0
        )
        assert {inv.layout for inv in corpus} == {"redesign"}
        assert not any(inv.scanned for inv in corpus)

    def test_the_redesign_is_internally_consistent(self):
        """The redesign changes labels, not arithmetic. If the totals stopped
        adding up in the DATA, the validation failures would be the generator's
        fault rather than the extractor's, and the experiment would measure
        nothing."""
        corpus = invoices.generate_corpus(
            20, seed=7, layouts=invoices.DRIFT_LAYOUTS, scan_fraction=0.0
        )
        for invoice in corpus:
            assert sum(item.amount for item in invoice.line_items) == invoice.subtotal
            assert invoice.subtotal + invoice.tax == invoice.total

    def test_the_redesign_template_exists(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "synth"
            / "templates"
            / "redesign.html.j2"
        )
        assert template.is_file()
        # The rendered markup only: the file's own comment names the old labels
        # while explaining what replaced them.
        body = template.read_text().split("<!DOCTYPE html>", 1)[1]
        for label in ("Net Amount", "VAT", "Balance Owing"):
            assert label in body
        for old in ("Subtotal", "Sales Tax", "Total Due"):
            assert old not in body


class TestMeasurement:
    def test_detection_and_swap_indices(self):
        steps = [step(i) for i in range(1, 6)] + [
            step(i, phase="drifted", flagged=("validation_failure",) if i >= 8 else ())
            for i in range(6, 11)
        ]
        assert swap_index(steps) == 6
        assert detection_index(steps) == 8

    def test_no_detection_reads_as_none(self):
        assert detection_index([step(i) for i in range(1, 5)]) is None

    def test_precision_counts_fields_not_documents(self):
        """Per-field, because that is the metric the operating point was chosen
        against. One wrong field in ten is 90% per-document and 99% per-field,
        and comparing the wrong one to a 99% floor invents a breach."""
        steps = [step(i, correct=10) for i in range(1, 10)] + [step(10, correct=9)]
        series = rolling_auto_approved_precision(steps, window=10)
        assert series == [(10, 99 / 100)]

    def test_documents_sent_to_review_are_excluded_from_precision(self):
        """A wrong document routed to a human is the system working. Only what
        was auto-approved counts against the floor."""
        steps = [step(i, correct=10) for i in range(1, 4)] + [
            step(4, approved=False, correct=0),
            step(5, correct=10),
        ]
        series = rolling_auto_approved_precision(steps, window=4)
        assert series == [(5, 1.0)]

    def test_a_short_stream_produces_no_precision_points(self):
        assert rolling_auto_approved_precision([step(1), step(2)], window=10) == []

    def test_breaches_before_the_swap_are_not_counted_as_damage(self):
        series = [(3, 0.95), (12, 0.80)]
        assert first_breach_index(series, 0.99) == 3
        assert first_breach_index(series, 0.99, at_or_after=10) == 12

    def test_the_auto_approve_rate_tracks_the_review_queue(self):
        steps = [step(i) for i in range(1, 5)] + [
            step(i, phase="drifted", approved=False) for i in range(5, 9)
        ]
        series = rolling_auto_approve_rate(steps, window=4)
        assert series[0] == (4, 1.0)
        assert series[-1] == (8, 0.0)


class TestSeedsArePinned:
    def test_the_experiment_seeds_are_constants(self):
        """The write-up's numbers are only reproducible if these never move."""
        from docfactory_evals import drift_experiment

        assert (drift_experiment.SEED_STABLE, drift_experiment.SEED_DRIFTED) == (
            20260819,
            20260820,
        )
        assert drift_experiment.SEED_CONTROL == 20260821


@pytest.mark.parametrize("layout", ["classic", "modern", "euro", "redesign"])
def test_every_layout_produces_a_template_name(layout):
    corpus = invoices.generate_corpus(1, seed=5, layouts=(layout,), scan_fraction=0.0)
    assert invoices.template_for(corpus[0]) == f"{layout}.html.j2"
