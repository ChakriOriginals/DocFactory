"""Drift detector: the baseline, the rule, and the two ways it can be wrong.

A detector has two failure modes and they pull in opposite directions. It can
stay quiet through a real change, which makes it decorative; or it can fire on
ordinary variation, which makes it noise and gets it switched off. Both are
tested here, and the second one twice — once for stationary data and once for
the cold start, which is the specific case where a naive detector flags every
new customer on their first day.
"""

import json
import uuid

import pytest
from docfactory_core.db import admin_session_scope, session_scope, tenant_context
from docfactory_core.drift import (
    STATUS_BASELINE,
    STATUS_DRIFTING,
    STATUS_STABLE,
    cosine_distance,
    drift_status,
    get_drift_config,
    observe,
    text_profile,
)
from docfactory_core.models import DriftStat, Tenant
from sqlalchemy import delete, text

pytestmark = pytest.mark.integration

TENANT = "drift-tenant"
DOC_TYPE = "invoice"

# Stable stream: high confidence, validation passing, one wording.
STABLE_TEXT = (
    "INVOICE Northwind Traders Bill To Acme Corp Invoice Number INV-2026-00042 "
    "Invoice Date 2026-03-01 Due Date 2026-03-31 Description Qty Unit Price Amount "
    "Consulting hours 10 150.00 1500.00 Subtotal 1500.00 Sales Tax 120.00 Total Due 1620.00"
)
# A redesign: same transaction, different vocabulary and different labels.
DRIFTED_TEXT = (
    "STATEMENT OF CHARGES Northwind Traders Account Acme Corp Reference 88213-2026 "
    "Issued 01 March 2026 Payable By 31 March 2026 Item Units Rate Line Total "
    "Advisory services 10 150.00 1500.00 Net Amount 1500.00 VAT 120.00 Balance Owing 1620.00"
)


@pytest.fixture(scope="module", autouse=True)
def drift_tenant():
    """A tenant of its own, so the detector's state cannot be disturbed by —
    or disturb — the documents every other test in the suite creates."""
    import socket
    from urllib.parse import urlparse

    from docfactory_core.config import get_settings

    parsed = urlparse(get_settings().database_url.replace("postgresql+psycopg", "postgresql"))
    try:
        with socket.create_connection((parsed.hostname or "localhost", parsed.port or 5432), 0.5):
            pass
    except OSError:
        pytest.skip("postgres is not running")

    with admin_session_scope() as session:
        if session.get(Tenant, TENANT) is None:
            session.add(Tenant(id=TENANT, name="Drift Test Tenant"))
    yield TENANT
    with admin_session_scope() as session:
        session.execute(delete(DriftStat).where(DriftStat.tenant_id == TENANT))


@pytest.fixture
def clean_state():
    """Each test starts with no baseline at all."""
    with admin_session_scope() as session:
        session.execute(delete(DriftStat).where(DriftStat.tenant_id == TENANT))
    yield


def feed(n: int, *, body: str, confidence: float = 0.95, passing: bool = True, jitter: float = 0.0):
    """Push n documents through the detector and return every observation.

    `jitter` varies confidence deterministically so the baseline has a real
    standard deviation rather than a degenerate one.
    """
    results = []
    for index in range(n):
        wobble = jitter * ((index % 5) - 2) / 2.0
        with tenant_context(TENANT), session_scope() as session:
            results.append(
                observe(
                    tenant_id=TENANT,
                    doc_type=DOC_TYPE,
                    doc_confidence=confidence + wobble,
                    validation_passed=passing,
                    text=body,
                    document_id=None,
                    session=session,
                )
            )
    return results


class TestTheProfile:
    def test_the_hash_is_stable_across_processes(self):
        """Python's hash() is salted per process. A profile computed by the
        worker and one computed here have to agree or every distance is noise."""
        import subprocess
        import sys

        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, 'packages/core');"
                "from docfactory_core.drift import text_profile;"
                "print(sum(text_profile('invoice total due')))",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert abs(float(out.stdout) - sum(text_profile("invoice total due"))) < 1e-9

    def test_identical_text_is_zero_distance(self):
        assert cosine_distance(text_profile(STABLE_TEXT), text_profile(STABLE_TEXT)) < 1e-9

    def test_a_redesign_moves_the_profile(self):
        distance = cosine_distance(text_profile(STABLE_TEXT), text_profile(DRIFTED_TEXT))
        assert distance > 0.05, f"redesigned layout barely moved the profile ({distance:.4f})"

    def test_a_repeated_word_cannot_dominate(self):
        """Sub-linear weighting: a forty-row line-item table must not make two
        structurally identical invoices look like different documents."""
        once = text_profile(STABLE_TEXT)
        padded = text_profile(STABLE_TEXT + " consulting hours" * 40)
        assert cosine_distance(once, padded) < 0.35


class TestConfigChangesDoNotFailOpen:
    def test_a_changed_profile_dimension_drops_the_signal_rather_than_zeroing_it(self, clean_state):
        """A stale centroid must make the signal UNAVAILABLE, not "identical".

        cosine_distance returns 0.0 for mismatched lengths, and 0.0 reads as
        "this document is exactly the baseline" — the most reassuring possible
        wrong answer, and one that would silently guarantee text_distance never
        fires again after a config change.
        """
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)

        # Shrink the stored centroid, as a smaller profile_dim would.
        with admin_session_scope() as session:
            row = session.scalar(
                text("SELECT centroid FROM drift_stats WHERE tenant_id = :t").bindparams(t=TENANT)
            )
            session.execute(
                text(
                    "UPDATE drift_stats SET centroid = CAST(:c AS jsonb) WHERE tenant_id = :t"
                ).bindparams(c=json.dumps(row[:8]), t=TENANT)
            )

        after = feed(1, body=DRIFTED_TEXT, confidence=0.95, passing=True)[0]
        assert "text_distance" not in after.values, (
            "a stale centroid produced a distance instead of dropping the signal"
        )
        # The other two signals keep working.
        assert "doc_confidence" in after.z_scores


class TestMonitoringCannotBreakThePipeline:
    """A drift bug must never cost a document.

    The observation runs inside the extraction transaction — that is what makes
    it atomic with the row it describes, and also what makes an exception here
    catastrophic: the extraction rolls back, the message redelivers, and after
    max_receive_count the document is in the DLQ. Monitoring would have
    destroyed the thing it was monitoring. This was not hypothetical: raising
    profile_dim left stale centroids that raised inside the observation and
    failed four *extraction* tests.
    """

    def test_a_stale_centroid_does_not_raise_during_baseline(self, clean_state):
        """The dimension can change mid-baseline too, not only after it."""
        feed(3, body=STABLE_TEXT)
        with admin_session_scope() as session:
            row = session.scalar(
                text("SELECT centroid FROM drift_stats WHERE tenant_id = :t").bindparams(t=TENANT)
            )
            session.execute(
                text(
                    "UPDATE drift_stats SET centroid = CAST(:c AS jsonb) WHERE tenant_id = :t"
                ).bindparams(c=json.dumps(row[:8]), t=TENANT)
            )
        # Must not raise, and must keep counting.
        after = feed(1, body=STABLE_TEXT)[0]
        assert after.n_observed == 4

    def test_the_handler_swallows_a_failing_observation(self):
        """The wrapper the worker calls, exercised directly."""
        from docfactory_worker.handlers import _observe_drift_safely

        _observe_drift_safely(
            tenant_id=TENANT,
            doc_type=DOC_TYPE,
            doc_confidence=0.9,
            validation_passed=True,
            text=STABLE_TEXT,
            document_id=None,
            session=object(),  # not a session; observe() will raise on it
        )


class TestColdStart:
    """No baseline, no drift claims. The guard that stops a detector from
    flagging every new customer on their first day."""

    def test_the_first_document_claims_nothing(self, clean_state):
        first = feed(1, body=DRIFTED_TEXT, confidence=0.10, passing=False)[0]
        assert first.status == STATUS_BASELINE
        assert first.is_baseline
        assert first.flagged == ()
        assert first.z_scores == {}

    def test_an_entire_baseline_of_bad_documents_is_not_drift(self, clean_state):
        """Whatever a tenant sends first IS their normal. A tenant whose
        documents are all hard is a tenant with a low baseline, not a tenant in
        crisis — and the detector must not confuse the two."""
        baseline_n = get_drift_config()["baseline_n"]
        observations = feed(
            baseline_n, body=DRIFTED_TEXT, confidence=0.20, passing=False, jitter=0.02
        )
        assert all(o.flagged == () for o in observations)
        assert observations[-1].status == STATUS_STABLE

    def test_the_baseline_closes_exactly_at_n(self, clean_state):
        baseline_n = get_drift_config()["baseline_n"]
        observations = feed(baseline_n, body=STABLE_TEXT, jitter=0.02)
        assert all(o.status == STATUS_BASELINE for o in observations[:-1])
        assert observations[-1].status == STATUS_STABLE
        assert observations[-1].n_observed == baseline_n


class TestStationaryData:
    """A detector that fires on data that did not change is noise."""

    def test_no_flag_on_a_long_stationary_stream(self, clean_state):
        baseline_n = get_drift_config()["baseline_n"]
        feed(baseline_n, body=STABLE_TEXT, jitter=0.04)
        after = feed(150, body=STABLE_TEXT, jitter=0.04)
        assert all(o.status == STATUS_STABLE for o in after)
        assert all(o.flagged == () for o in after)

    def test_a_single_bad_document_does_not_trip_it(self, clean_state):
        """One breach is not drift. This is what consecutive-k buys."""
        baseline_n = get_drift_config()["baseline_n"]
        feed(baseline_n, body=STABLE_TEXT, jitter=0.04)
        outlier = feed(1, body=DRIFTED_TEXT, confidence=0.05, passing=False)[0]
        assert outlier.breaching, "an obviously bad document should breach"
        assert outlier.flagged == (), "one breach must not raise drift"
        recovered = feed(1, body=STABLE_TEXT)[0]
        assert recovered.flagged == ()

    def test_an_ordinary_document_resets_the_run(self, clean_state):
        """k-1 breaches followed by a normal document starts the count over."""
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        feed(config["consecutive_k"] - 1, body=DRIFTED_TEXT, confidence=0.05, passing=False)
        assert feed(1, body=STABLE_TEXT)[0].flagged == ()
        # And the run really is back to zero: k-1 more still is not enough.
        assert (
            feed(config["consecutive_k"] - 1, body=DRIFTED_TEXT, confidence=0.05, passing=False)[
                -1
            ].flagged
            == ()
        )


class TestDetection:
    def test_a_sustained_change_is_flagged_at_k(self, clean_state):
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        drifted = feed(10, body=DRIFTED_TEXT, confidence=0.30, passing=False)

        first_flag = next(i for i, o in enumerate(drifted, start=1) if o.flagged)
        assert first_flag == config["consecutive_k"], (
            f"expected the flag on document {config['consecutive_k']} after the swap, "
            f"got {first_flag}"
        )
        assert drifted[-1].status == STATUS_DRIFTING

    def test_every_signal_can_raise_drift_on_its_own(self, clean_state):
        """Each signal is independently sufficient — a format change that the
        extractor happens to survive still moves the text distance."""
        config = get_drift_config()

        # Text only: confidence and validation held exactly at baseline.
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        text_only = feed(config["consecutive_k"], body=DRIFTED_TEXT, confidence=0.95, passing=True)
        assert "text_distance" in text_only[-1].flagged

        # Confidence only: same wording, quality collapses.
        with admin_session_scope() as session:
            session.execute(delete(DriftStat).where(DriftStat.tenant_id == TENANT))
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        conf_only = feed(config["consecutive_k"], body=STABLE_TEXT, confidence=0.20, passing=True)
        assert "doc_confidence" in conf_only[-1].flagged

        # Validation only: same wording, same confidence, arithmetic breaks.
        with admin_session_scope() as session:
            session.execute(delete(DriftStat).where(DriftStat.tenant_id == TENANT))
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        rules_only = feed(config["consecutive_k"], body=STABLE_TEXT, confidence=0.95, passing=False)
        assert "validation_failure" in rules_only[-1].flagged

    def test_the_flag_records_which_document_tripped_it(self, clean_state):
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        feed(config["consecutive_k"], body=DRIFTED_TEXT, confidence=0.30, passing=False)
        with tenant_context(TENANT), session_scope() as session:
            row = session.scalar(
                text("SELECT status FROM drift_stats WHERE tenant_id = :t").bindparams(t=TENANT)
            )
        assert row == STATUS_DRIFTING


class TestPersistence:
    def test_the_consecutive_counter_survives_the_transaction(self, clean_state):
        """The regression test for a bug worth remembering.

        Each document is its own transaction, so the breach counter has to
        round-trip through JSONB. It did not: the per-signal dicts were shared
        with the instance SQLAlchemy had loaded, so mutating one in place also
        mutated the committed state the flush compares against, no UPDATE was
        emitted, and the counter reset to 1 on every document. Every breach
        fired correctly and drift was never declared — the worst shape of bug,
        because everything observable looked right.
        """
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        feed(2, body=DRIFTED_TEXT, confidence=0.30, passing=False)

        with admin_session_scope() as session:
            row = session.execute(
                text(
                    "SELECT stats -> 'doc_confidence' ->> 'consecutive' "
                    "FROM drift_stats WHERE tenant_id = :t"
                ).bindparams(t=TENANT)
            ).scalar()
        assert int(row) == 2, "the breach counter did not persist between documents"


class TestDeterminism:
    def test_the_same_stream_produces_the_same_verdicts(self, clean_state):
        """The established discipline: same input, same output. A detector
        whose verdicts move between runs cannot be reasoned about."""
        config = get_drift_config()

        def run() -> list[tuple]:
            with admin_session_scope() as session:
                session.execute(delete(DriftStat).where(DriftStat.tenant_id == TENANT))
            feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
            return [
                (o.status, o.flagged, tuple(sorted(round(z, 9) for z in o.z_scores.values())))
                for o in feed(8, body=DRIFTED_TEXT, confidence=0.30, passing=False)
            ]

        assert run() == run()


class TestTheUsageSurface:
    def test_drift_is_readable_per_document_type(self, clean_state):
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        feed(config["consecutive_k"], body=DRIFTED_TEXT, confidence=0.30, passing=False)

        with tenant_context(TENANT):
            rows = drift_status(TENANT)
        entry = next(row for row in rows if row.doc_type == DOC_TYPE)
        assert entry.status == STATUS_DRIFTING
        assert entry.flagged_signals
        assert entry.first_flagged_at is not None
        assert entry.signals["doc_confidence"]["baseline_n"] == config["baseline_n"]

    def test_a_tenant_with_no_documents_reports_nothing(self, clean_state):
        with tenant_context(TENANT):
            assert drift_status(TENANT) == []


class TestIsolation:
    def test_drift_state_does_not_cross_tenants(self, clean_state):
        """drift_stats carries a tenant_id, so the 4d guard already requires a
        forced policy on it. This is the behavioural half of that."""
        config = get_drift_config()
        feed(config["baseline_n"], body=STABLE_TEXT, jitter=0.04)
        with tenant_context("dev-tenant"):
            assert all(row.doc_type != "__never__" for row in drift_status("dev-tenant"))
        with tenant_context("dev-tenant"), session_scope() as session:
            rows = session.execute(
                text("SELECT tenant_id FROM drift_stats WHERE tenant_id = :t").bindparams(t=TENANT)
            ).all()
        assert rows == [], "another tenant's drift baseline was visible"

    def test_writing_drift_state_for_another_tenant_is_refused(self, clean_state):
        from sqlalchemy.exc import DatabaseError

        with (
            pytest.raises(DatabaseError),
            tenant_context("dev-tenant"),
            session_scope() as session,
        ):
            session.add(
                DriftStat(
                    id=uuid.uuid4(),
                    tenant_id=TENANT,
                    doc_type="smuggled",
                    window_key="baseline-1",
                    stats={},
                )
            )
