"""Calibration study logic — the pure parts.

The fit itself is exercised by `make calibrate`; these pin the decision rules
that turn a fitted model into a threshold, because those are what Phase 2.3
will act on. The infeasible-target case is tested explicitly: an earlier
version returned an approve-nothing threshold and produced a report full of
blanks instead of saying the target was unreachable.
"""

import numpy as np
import pytest
from docfactory_evals.calibrate import (
    MIN_USEFUL_RATE,
    frontier,
    operating_point,
    reliability,
    sweep,
)


def _scores(pairs):
    """pairs of (predicted, correct) -> arrays."""
    p = np.array([a for a, _ in pairs], dtype=float)
    y = np.array([b for _, b in pairs], dtype=float)
    return p, y


class TestSweep:
    def test_precision_and_rate_move_in_opposite_directions(self):
        p, y = _scores([(0.9, 1)] * 90 + [(0.1, 0)] * 10)
        curve = sweep(p, y)
        low = next(c for c in curve if c[0] == 0.0)
        high = [c for c in curve if c[1] > 0][-1]
        assert low[1] == 1.0  # approves everything
        assert high[2] >= low[2]  # stricter threshold is at least as precise

    def test_approving_nothing_reports_rate_zero(self):
        p, y = _scores([(0.5, 1), (0.4, 0)])
        assert sweep(p, y)[-1][1] == 0.0


class TestOperatingPoint:
    def test_picks_the_widest_coverage_that_meets_the_target(self):
        # clean fields at 0.9, errors at 0.2: a threshold above 0.2 is perfect
        p, y = _scores([(0.9, 1)] * 95 + [(0.2, 0)] * 5)
        (threshold, rate, precision), met = operating_point(sweep(p, y), 0.99)
        assert met is True
        assert precision >= 0.99
        assert rate == pytest.approx(0.95, abs=0.02)
        assert 0.2 < threshold <= 0.9

    def test_unreachable_target_is_reported_not_silently_met(self):
        # errors are indistinguishable from correct fields: no threshold helps
        p, y = _scores([(0.9, 1)] * 90 + [(0.9, 0)] * 10)
        point, met = operating_point(sweep(p, y), 0.99)
        assert met is False
        assert point[2] < 0.99

    def test_unreachable_target_still_returns_a_usable_point(self):
        p, y = _scores([(0.9, 1)] * 90 + [(0.9, 0)] * 10)
        (_, rate, _), met = operating_point(sweep(p, y), 0.99)
        assert met is False
        # the earlier bug: an approve-nothing threshold, which is not a point
        assert rate >= MIN_USEFUL_RATE

    def test_trivial_coverage_does_not_count_as_meeting_the_target(self):
        # A single lucky field scores 100% precision at 1% coverage. That is
        # not an operating point — a reviewer would still check everything.
        p, y = _scores([(1.0, 1)] + [(0.9, 1)] * 89 + [(0.9, 0)] * 10)
        _, met = operating_point(sweep(p, y), 0.99)
        assert met is False

    def test_coverage_above_the_floor_does_count(self):
        clean = [(1.0, 1)] * 10  # 10% of fields, comfortably above the floor
        p, y = _scores(clean + [(0.9, 1)] * 80 + [(0.9, 0)] * 10)
        _, met = operating_point(sweep(p, y), 0.99)
        assert met is True

    def test_frontier_is_monotone_in_coverage(self):
        p, y = _scores([(0.95, 1)] * 80 + [(0.6, 1)] * 10 + [(0.6, 0)] * 10)
        results = frontier(sweep(p, y), targets=(0.99, 0.95, 0.9))
        rates = [point[1] for _, point, _ in results]
        assert rates == sorted(rates), "looser targets must not buy less coverage"


class TestReliability:
    def test_perfectly_calibrated_scores_land_on_the_diagonal(self):
        pairs = [(0.9, 1)] * 90 + [(0.9, 0)] * 10
        points = reliability(*_scores(pairs))
        predicted, empirical, n = points[-1]
        assert n == 100
        assert predicted == pytest.approx(empirical, abs=0.01)

    def test_overconfident_scores_fall_below_the_diagonal(self):
        pairs = [(0.95, 1)] * 50 + [(0.95, 0)] * 50
        predicted, empirical, _ = reliability(*_scores(pairs))[-1]
        assert empirical < predicted

    def test_empty_bins_are_omitted(self):
        points = reliability(*_scores([(0.95, 1)] * 10))
        assert len(points) == 1
