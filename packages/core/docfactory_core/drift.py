"""Drift detection over signals the pipeline has already computed.

THE CONSTRAINT THAT SHAPED THIS. Drift detection that costs a model call per
document doubles the inference bill of the thing it is monitoring, which is a
poor trade for a signal that fires once a quarter. So every input here is a
by-product of work already done: the calibrated confidence and the
deterministic validation result come from the extraction that just finished,
and the text profile is computed from the parsed text that is already in memory
at that moment. Detection adds one row update per document and no network.

THREE SIGNALS.

1. `doc_confidence` — the calibrated probability of the weakest field. A format
   the extractor handles worse depresses this first, before anything is
   provably wrong.
2. `validation_failure` — the Phase 2 arithmetic and date rules, as a 0/1 per
   document. This is the signal that says the output is not merely
   low-confidence but internally inconsistent.
3. `text_distance` — cosine distance between the document's lexical profile
   and the baseline centroid. The only one that can notice a format change the
   extractor happens to survive.

WHY A LEXICAL PROFILE AND NOT AN EMBEDDING. There is no embedding path in this
stack, and adding one means either a model call per document (see the
constraint above) or a local model — a large dependency, a download, and a
source of non-determinism across machines, all to detect that a page's wording
changed. A hashed token-frequency profile is deterministic, needs only the
standard library, costs microseconds, and answers exactly the question being
asked: does this document's text look like the documents this baseline was
built from? It cannot see semantic drift with unchanged vocabulary. For "the
vendor redesigned their invoice" — the case in the problem statement — the
vocabulary is precisely what changes.

`needs_ocr` rate is a reasonable fourth signal and is deliberately not here: it
is decided at the *parse* stage, where no extraction row exists yet, so it
would need its own observation point and its own transaction. Cheap to add;
not free, and out of scope for v1.

THE BASELINE IS FROZEN, NOT TRAILING. The first `baseline_n` documents of a
(tenant, doc_type) define the baseline; after that the statistics stop moving.
A trailing window has the property that slow drift is absorbed into the
baseline it is supposed to be measured against — the detector adapts to the
problem and goes quiet. Freezing avoids that, at the cost of needing an
explicit re-baseline after an acknowledged change; `window_key` exists so that
re-baselining opens `baseline-2` rather than mutating the history that
explained a past detection.
"""

import hashlib
import json
import logging
import math
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select

from docfactory_core.db import session_scope
from docfactory_core.models import DriftStat

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "drift.json"

# Which way a signal has to move to be bad. A confidence *rise* is not drift;
# a validation-failure *fall* is not drift. Testing both tails would double the
# false-positive rate to detect improvements nobody needs alerting about.
SIGNAL_DIRECTION = {
    "doc_confidence": "down",
    "validation_failure": "up",
    "text_distance": "up",
}

# Signals that are 0/1 per document. Their baseline sigma comes from the
# Bernoulli form rather than from Welford, because a baseline with zero
# failures has zero observed variance and would make every later failure an
# infinite z-score.
RATE_SIGNALS = frozenset({"validation_failure"})

STATUS_BASELINE = "baseline"
STATUS_STABLE = "stable"
STATUS_DRIFTING = "drifting"

_TOKEN = re.compile(r"[a-z0-9]+")


@lru_cache
def get_drift_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text())
    return {key: value for key, value in config.items() if not key.startswith("_")}


# --- the lexical profile ----------------------------------------------------


def _bucket(token: str, dim: int) -> int:
    """Stable hash. NOT Python's `hash()`, which is salted per process.

    A profile computed by the worker and a profile computed by a test must land
    in the same buckets or every distance is noise.
    """
    return (
        int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big") % dim
    )


def text_profile(text: str, dim: int | None = None) -> list[float]:
    """L2-normalised hashed token-frequency profile of a document's text.

    Sub-linear (log1p) term weighting so one repeated word — a line item
    description on a forty-row invoice — cannot dominate the profile and make
    two structurally identical documents look far apart.
    """
    dim = dim or get_drift_config()["profile_dim"]
    counts = [0.0] * dim
    for token in _TOKEN.findall(text.lower()):
        counts[_bucket(token, dim)] += 1.0

    weighted = [math.log1p(count) for count in counts]
    norm = math.sqrt(sum(value * value for value in weighted))
    if norm == 0.0:
        return weighted
    return [value / norm for value in weighted]


def cosine_distance(left: list[float], right: list[float]) -> float:
    """1 - cosine similarity, clamped to [0, 2]. Both inputs are L2-normalised
    profiles or means of them, so the dot product is the similarity directly."""
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    similarity = sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)
    return max(0.0, min(2.0, 1.0 - similarity))


# --- running statistics -----------------------------------------------------


def _welford_update(stat: dict, value: float) -> dict:
    """One Welford step: running mean and M2 with no stored history.

    Numerically stable in a way that sum-of-squares is not, which matters here
    because the variance of a confidence score clustered near 0.9 is the small
    difference of two large numbers.
    """
    n = stat.get("n", 0) + 1
    mean = stat.get("mean", 0.0)
    delta = value - mean
    mean += delta / n
    m2 = stat.get("m2", 0.0) + delta * (value - mean)
    return {"n": n, "mean": mean, "m2": m2, "consecutive": stat.get("consecutive", 0)}


def _baseline_sigma(signal: str, stat: dict, min_std: float) -> float:
    n = stat.get("n", 0)
    if signal in RATE_SIGNALS:
        # Laplace-smoothed Bernoulli sigma: never zero, and it widens
        # automatically when the baseline is small.
        smoothed = (stat.get("mean", 0.0) * n + 0.5) / (n + 1)
        return max(math.sqrt(smoothed * (1.0 - smoothed)), min_std)
    if n < 2:
        return min_std
    return max(math.sqrt(stat.get("m2", 0.0) / (n - 1)), min_std)


def _breaches(signal: str, z: float, threshold: float) -> bool:
    if SIGNAL_DIRECTION[signal] == "down":
        return z <= -threshold
    return z >= threshold


# --- the observation --------------------------------------------------------


@dataclass(frozen=True)
class DriftObservation:
    """What one document did to its tenant's drift state."""

    tenant_id: str
    doc_type: str
    status: str
    n_observed: int
    values: dict[str, float] = field(default_factory=dict)
    z_scores: dict[str, float] = field(default_factory=dict)
    breaching: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()
    newly_flagged: bool = False

    @property
    def is_baseline(self) -> bool:
        """True while the baseline is still being collected.

        THE COLD-START GUARD, as a property rather than a convention: no
        z-score is computed and no drift is claimed until a baseline exists.
        The first documents of a new tenant are what "normal" means for them;
        calling them drift would flag every new customer on their first day.
        """
        return self.status == STATUS_BASELINE


def observe(
    *,
    tenant_id: str,
    doc_type: str,
    doc_confidence: float | None,
    validation_passed: bool | None,
    text: str,
    document_id: uuid.UUID | None = None,
    session=None,
) -> DriftObservation:
    """Fold one finished extraction into its tenant's drift state.

    Runs inside the caller's transaction when a session is passed, so the
    observation and the extraction row it describes commit together — a
    document cannot end up counted in the baseline without existing, or exist
    without being counted.

    Everything this needs is already in hand at the call site. No queries
    beyond the one drift_stats row, no model call, no object-store read.
    """
    if session is not None:
        return _observe(
            session, tenant_id, doc_type, doc_confidence, validation_passed, text, document_id
        )
    with session_scope(tenant_id) as own_session:
        return _observe(
            own_session, tenant_id, doc_type, doc_confidence, validation_passed, text, document_id
        )


def _observe(
    session,
    tenant_id: str,
    doc_type: str,
    doc_confidence: float | None,
    validation_passed: bool | None,
    text: str,
    document_id: uuid.UUID | None,
) -> DriftObservation:
    config = get_drift_config()
    baseline_n = config["baseline_n"]
    threshold = config["z_threshold"]
    k = config["consecutive_k"]
    min_std = config["min_std"]

    row = session.scalar(
        select(DriftStat)
        .where(
            DriftStat.tenant_id == tenant_id,
            DriftStat.doc_type == doc_type,
            DriftStat.window_key == "baseline-1",
        )
        .with_for_update()
    )
    if row is None:
        row = DriftStat(
            tenant_id=tenant_id,
            doc_type=doc_type,
            window_key="baseline-1",
            status=STATUS_BASELINE,
            n_observed=0,
            stats={},
            centroid=None,
        )
        session.add(row)
        session.flush()

    # DEEP copy, and the depth matters. A shallow `dict(row.stats)` shares the
    # per-signal dicts with the instance SQLAlchemy loaded, so mutating one
    # in place mutates the committed state it would later be compared against:
    # the attribute is reassigned, the flush finds old == new, and no UPDATE is
    # emitted. The symptom is a detector whose breaches all work and whose
    # consecutive counter is silently always 1 — every document breaches, drift
    # is never declared. JSONB columns have no mutation tracking; copying is
    # the whole defence.
    stats = {signal: dict(stat) for signal, stat in (row.stats or {}).items()}
    centroid = list(row.centroid) if row.centroid else None
    profile = text_profile(text, config["profile_dim"])

    # Measured against the centroid as it stood BEFORE this document, so a
    # document is never compared to a baseline it is part of.
    values: dict[str, float] = {}
    if doc_confidence is not None:
        values["doc_confidence"] = float(doc_confidence)
    if validation_passed is not None:
        values["validation_failure"] = 0.0 if validation_passed else 1.0
    if centroid is not None and len(centroid) == len(profile):
        values["text_distance"] = cosine_distance(profile, centroid)
    elif centroid is not None:
        # profile_dim changed under a live baseline. The stored centroid is
        # not comparable, so the signal is UNAVAILABLE — which is a different
        # thing from "distance zero", and the difference matters: a zero
        # distance reads as "this document is identical to the baseline",
        # which is the most reassuring possible wrong answer. Dropping the
        # signal degrades detection to the other two; returning zero would
        # quietly guarantee it never fires.
        log.warning(
            "text profile dimension changed; text_distance is unavailable until re-baselined",
            extra={
                "tenant_id": tenant_id,
                "doc_type": doc_type,
                "stored_dim": len(centroid),
                "config_dim": len(profile),
            },
        )

    n_observed = row.n_observed + 1
    collecting = n_observed <= baseline_n

    z_scores: dict[str, float] = {}
    breaching: list[str] = []

    if collecting:
        # Baseline documents define normal; they are never judged against it.
        for signal, value in values.items():
            stats[signal] = _welford_update(stats.get(signal, {}), value)
        if centroid is None or len(centroid) != len(profile):
            # None on the first document; a length mismatch when profile_dim
            # changed under a live baseline. Either way the only sane centroid
            # is this document's profile — and the mismatch case must not raise,
            # because this runs inside the extraction transaction and an
            # exception here would fail a document over a monitoring detail.
            if centroid is not None:
                log.warning(
                    "text profile dimension changed; restarting the centroid",
                    extra={"tenant_id": tenant_id, "doc_type": doc_type},
                )
            centroid = profile
        else:
            centroid = [
                existing + (new - existing) / n_observed
                for existing, new in zip(centroid, profile, strict=True)
            ]
        row.status = STATUS_STABLE if n_observed >= baseline_n else STATUS_BASELINE
    else:
        for signal, value in values.items():
            stat = stats.get(signal)
            if not stat or stat.get("n", 0) < 2:
                continue
            sigma = _baseline_sigma(signal, stat, min_std)
            z = (value - stat["mean"]) / sigma
            z_scores[signal] = z
            stat["last_z"] = z
            if _breaches(signal, z, threshold):
                stat["consecutive"] = stat.get("consecutive", 0) + 1
                breaching.append(signal)
            else:
                # One ordinary document resets the run. Drift is a sustained
                # change; a single odd page is a single odd page.
                stat["consecutive"] = 0
            stats[signal] = stat

    flagged = tuple(
        sorted(signal for signal, stat in stats.items() if stat.get("consecutive", 0) >= k)
    )
    was_flagged = bool(row.flagged_signals)
    newly_flagged = bool(flagged) and not was_flagged

    row.n_observed = n_observed
    row.stats = stats
    row.centroid = centroid
    row.computed_at = datetime.now(UTC)
    if flagged:
        row.status = STATUS_DRIFTING
        row.flagged_signals = list(flagged)
        if row.first_flagged_at is None:
            row.first_flagged_at = row.computed_at
            row.flagged_document_id = document_id

    return DriftObservation(
        tenant_id=tenant_id,
        doc_type=doc_type,
        status=row.status,
        n_observed=n_observed,
        values=values,
        z_scores=z_scores,
        breaching=tuple(sorted(breaching)),
        flagged=flagged,
        newly_flagged=newly_flagged,
    )


# --- reading the state ------------------------------------------------------


@dataclass(frozen=True)
class DriftStatus:
    """Drift state for one tenant x document type, for the usage surface."""

    doc_type: str
    status: str
    n_observed: int
    baseline_n: int
    flagged_signals: tuple[str, ...]
    first_flagged_at: datetime | None
    signals: dict[str, dict]


def drift_status(tenant_id: str) -> list[DriftStatus]:
    """Current drift state per document type. Read-only; computes nothing.

    Drift is *observable* this phase, not alerted. Paging someone is the ops
    surface that waits for the deployed environment; a status a human can read
    on GET /usage is what this phase owes.
    """
    config = get_drift_config()
    with session_scope(tenant_id) as session:
        rows = session.scalars(
            select(DriftStat)
            .where(DriftStat.window_key == "baseline-1")
            .order_by(DriftStat.doc_type)
        ).all()
        return [
            DriftStatus(
                doc_type=row.doc_type,
                status=row.status,
                n_observed=row.n_observed,
                baseline_n=config["baseline_n"],
                flagged_signals=tuple(row.flagged_signals or ()),
                first_flagged_at=row.first_flagged_at,
                signals={
                    signal: {
                        "baseline_mean": round(stat.get("mean", 0.0), 6),
                        "baseline_n": stat.get("n", 0),
                        "last_z": round(stat["last_z"], 3) if "last_z" in stat else None,
                        "consecutive_breaches": stat.get("consecutive", 0),
                    }
                    for signal, stat in sorted((row.stats or {}).items())
                },
            )
            for row in rows
        ]
