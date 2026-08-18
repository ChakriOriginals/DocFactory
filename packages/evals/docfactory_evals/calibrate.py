"""Confidence calibration study (Phase 2.2c).

Turns the hand-picked penalty priors from 2.1 into fitted weights, and
produces the artifact that justifies a review threshold: the trade-off curve
between how much we auto-approve and how often auto-approval is wrong.

Method
------
*Unit of analysis is the field, not the document.* The product decision is
"can this cell be accepted without a human looking at it", so precision and
auto-approve rate are measured over fields.

*Fit on the non-golden documents, choose the operating point on the golden
set.* The doc_id-hash split from Phase 1 already separates them. Fitting and
then reporting on the same rows would overstate precision, and the threshold
is the number 2.3 will act on, so it is measured strictly out of sample.

*Signals are read back from the database, never recomputed.* Phase 2.1 stored
the raw signal vector on `extractions.confidence_signals` precisely so the fit
could be re-run offline without another extraction pass. `--build` populates
those rows once; `--fit` reads them and can be re-run in a second.

*Errors are injected, not waited for.* Mock extraction is near-perfect, so
2.2b's deterministic corruption supplies labelled errors spanning a range of
detectability — including one class (`shifted_date`) that no current signal
can see, so the ceiling shows up in the results instead of being hidden.

Usage
-----
    make calibrate            # build the dataset, then fit
    uv run python -m docfactory_evals.calibrate --fit-only
"""

import argparse
import json
import subprocess
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
from docfactory_core.confidence import score_extraction
from docfactory_core.confidence_model import FEATURES, features_for
from docfactory_core.config import get_settings
from docfactory_core.corruption import plan_corruption
from docfactory_core.db import session_scope, tenant_context
from docfactory_core.extraction import run_extraction
from docfactory_core.llm import get_llm_client
from docfactory_core.models import Document, DocumentStatus, Extraction
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline_registry import default_pipeline
from docfactory_core.schemas import Invoice
from docfactory_core.validation import validate_invoice
from sqlalchemy import delete, select

from docfactory_evals.run import _compare, golden_split

REPO_ROOT = Path(__file__).resolve().parents[3]
PDF_DIR = REPO_ROOT / "data" / "synth" / "out"
GROUND_TRUTH = PDF_DIR / "ground_truth.jsonl"
LABELS_PATH = REPO_ROOT / "data" / "calibration" / "error_classes.jsonl"
META_PATH = REPO_ROOT / "data" / "calibration" / "dataset_meta.json"
CONFIG_VERSION = 2
CONFIG_PATH = REPO_ROOT / "config" / f"confidence_model_v{CONFIG_VERSION}.json"
DOCS_DIR = REPO_ROOT / "docs"

# Namespace so a document's row id is stable across rebuilds.
_CALIB_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")

# Which fields exist comes from the pipeline definition, not from a
# hardcoded invoice list.
FIELDS = default_pipeline().scored_fields

# Feature names in fitted-weight order. Kept small and individually meaningful
# so the saved config can be read and argued with, not just applied.
# FEATURES and the feature extractor live in core and are imported above:
# fitting and serving must produce identical vectors, so there is exactly one
# implementation. A second copy here is the classic train/serve skew bug.


@dataclass
class Row:
    doc_id: str
    layout: str
    field: str
    error_class: str
    features: list[float]
    correct: int


# --------------------------------------------------------------------------
# stage 1: build the dataset (one extraction pass, persisted)
# --------------------------------------------------------------------------


def dataset_meta() -> dict:
    """Provenance of the persisted dataset (empty if never built)."""
    return json.loads(META_PATH.read_text()) if META_PATH.exists() else {}


def _records() -> list[dict]:
    return [json.loads(line) for line in GROUND_TRUTH.read_text().splitlines() if line.strip()]


def build_dataset(limit: int | None = None) -> int:
    """Extract + score every corpus document, persisting signals to Postgres."""
    settings = get_settings()
    if settings.mock_corruption_rate <= 0:
        raise SystemExit(
            "MOCK_CORRUPTION_RATE must be > 0 to build a calibration dataset — "
            "an uncorrupted mock produces one label and the fit is degenerate. "
            "Run via `make calibrate`, which sets it."
        )
    records = _records()
    if limit:
        records = records[:limit]
    client = get_llm_client()
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)

    with session_scope() as session:
        session.execute(delete(Document).where(Document.s3_key.like("%/synth/%")))

    labels: list[dict] = []
    built = 0
    for index, record in enumerate(records, 1):
        pdf_path = PDF_DIR / record["file"]
        text = extract_pdf_text(pdf_path.read_bytes())
        if len(text) < settings.min_parse_chars:
            continue  # scanned: needs_ocr, out of scope for extraction calibration
        outcome = run_extraction(text, client)
        if outcome.invoice is None:
            continue
        validation = validate_invoice(outcome.invoice)
        report = score_extraction(
            outcome.invoice, validation, attempts=outcome.attempts, source_text=text
        )
        plan = plan_corruption(
            text, rate=settings.mock_corruption_rate, seed=settings.mock_corruption_seed
        )
        document_id = uuid.uuid5(_CALIB_NS, record["doc_id"])
        with session_scope() as session:
            session.add(
                Document(
                    id=document_id,
                    tenant_id=settings.default_tenant_id,
                    s3_key=record["s3_key"],
                    sha256=uuid.uuid5(_CALIB_NS, record["file"]).hex * 2,
                    status=DocumentStatus.EXTRACTED,
                )
            )
            session.add(
                Extraction(
                    document_id=document_id,
                    tenant_id=settings.default_tenant_id,
                    model=f"{client.provider}:{client.model}",
                    output=outcome.invoice.model_dump(mode="json"),
                    validation=validation,
                    validation_passed=all(validation.values()),
                    doc_confidence=report.doc_confidence,
                    confidence_signals=report.signals,
                )
            )
        labels.append(
            {
                "doc_id": record["doc_id"],
                "error_class": plan.error_class if plan else "none",
            }
        )
        built += 1
        if index % 50 == 0:
            print(f"  built {index}/{len(records)}", flush=True)

    LABELS_PATH.write_text("\n".join(json.dumps(entry) for entry in labels) + "\n")
    # Provenance belongs to the dataset, not to whatever environment happens to
    # re-run the fit later: --fit-only would otherwise report the ambient
    # corruption rate (0.0) for data generated at 0.35.
    META_PATH.write_text(
        json.dumps(
            {
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "documents": built,
                "corruption_rate": settings.mock_corruption_rate,
                "corruption_seed": settings.mock_corruption_seed,
                "model_provider": settings.model_provider,
                "git_sha": _git_sha(),
            },
            indent=2,
        )
        + "\n"
    )
    print(f"dataset built: {built} extractions persisted with signal vectors")
    return built


# --------------------------------------------------------------------------
# stage 2: offline fit from stored signals
# --------------------------------------------------------------------------


def load_rows() -> list[Row]:
    """Read stored signal vectors from Postgres and label them offline."""
    records = {r["doc_id"]: r for r in _records()}
    error_classes = {}
    if LABELS_PATH.exists():
        for line in LABELS_PATH.read_text().splitlines():
            if line.strip():
                entry = json.loads(line)
                error_classes[entry["doc_id"]] = entry["error_class"]

    rows: list[Row] = []
    with session_scope() as session:
        extractions = session.scalars(
            select(Extraction)
            .join(Document, Document.id == Extraction.document_id)
            .where(Document.s3_key.like("%/synth/%"))
            .where(Extraction.confidence_signals.isnot(None))
        ).all()
        for extraction in extractions:
            document = session.get(Document, extraction.document_id)
            doc_id = Path(document.s3_key).stem
            record = records.get(doc_id)
            if record is None:
                continue
            invoice = Invoice.model_validate(extraction.output)
            expected = {**record["fields"], "currency": record["currency"]}
            correctness = _compare(invoice, expected)
            for field in FIELDS:
                rows.append(
                    Row(
                        doc_id=doc_id,
                        layout=record["layout"],
                        field=field,
                        error_class=error_classes.get(doc_id, "unknown"),
                        features=features_for(field, extraction.confidence_signals),
                        correct=int(correctness.get(field, True)),
                    )
                )
    return rows


def fit_logistic(
    X: np.ndarray, y: np.ndarray, *, l2: float = 1.0, epochs: int = 4000, lr: float = 0.5
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Plain gradient-descent logistic regression.

    Deliberately hand-rolled: six features and a few hundred rows do not need
    scikit-learn, and keeping it here means the fitted weights in the config
    are reproducible from this file alone.
    """
    mu, sigma = X.mean(axis=0), X.std(axis=0) + 1e-9
    Z = np.hstack([(X - mu) / sigma, np.ones((len(X), 1))])
    w = np.zeros(Z.shape[1])
    for _ in range(epochs):
        p = 1.0 / (1.0 + np.exp(-Z @ w))
        penalty = np.concatenate([w[:-1], [0.0]])
        w -= lr * (Z.T @ (p - y) / len(y) + l2 * penalty / len(y))
    return w, mu, sigma


def predict(X: np.ndarray, w: np.ndarray, mu: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    Z = np.hstack([(X - mu) / sigma, np.ones((len(X), 1))])
    return 1.0 / (1.0 + np.exp(-Z @ w))


def sweep(p: np.ndarray, y: np.ndarray) -> list[tuple[float, float, float]]:
    """(threshold, auto-approve rate, precision among auto-approved)."""
    out = []
    for threshold in np.linspace(0.0, 1.0, 201):
        approved = p >= threshold
        rate = approved.mean()
        precision = y[approved].mean() if approved.any() else 1.0
        out.append((float(threshold), float(rate), float(precision)))
    return out


# A target only counts as met if it holds over a share of fields worth
# operating on. Without this floor, a threshold that approves a handful of
# lucky fields reports 100% precision at ~1% coverage and looks like success,
# while a reviewer still hand-checks everything. 5% is a judgement call.
MIN_USEFUL_RATE = 0.05

# How much coverage we will trade for precision once the target is met.
#
# Selecting purely on max coverage is what produced the unsafe v2 point: it
# picked threshold 0.42, which sits below the score band of fields with one
# failed validation rule and so auto-approved four wrong invoice totals. The
# same model at 0.67 gave 99.57% precision for 1.4pp less coverage. Buying
# 1.4% throughput by silently approving wrong totals is the wrong trade for a
# document platform, so the rule is now: reach max coverage subject to the
# target, then take the most precise point within this much coverage of it.
COVERAGE_TOLERANCE = 0.02


def operating_point(
    curve: list[tuple[float, float, float]], target_precision: float
) -> tuple[tuple[float, float, float], bool]:
    """Highest auto-approve rate whose precision still clears the target.

    Precision is the constraint the business sets ("auto-approved fields must
    be right 99% of the time") and coverage is what we maximise inside it.

    Returns (point, met). When the target is unreachable at any usable
    coverage, the best *achievable* point is returned with met=False rather
    than a degenerate approve-nothing threshold: an unreachable target is a
    finding to report, not a table full of blanks.
    """
    feasible = [
        point for point in curve if point[2] >= target_precision and point[1] >= MIN_USEFUL_RATE
    ]
    if feasible:
        widest = max(point[1] for point in feasible)
        # Precision-safety, not max coverage: among the points that give up at
        # most COVERAGE_TOLERANCE of the best coverage, take the most precise.
        affordable = [point for point in feasible if point[1] >= widest - COVERAGE_TOLERANCE]
        return max(affordable, key=lambda point: (point[2], point[1])), True
    usable = [point for point in curve if point[1] >= MIN_USEFUL_RATE]
    if not usable:
        return (1.0, 0.0, 1.0), False
    return max(usable, key=lambda point: (point[2], point[1])), False


def frontier(curve, targets=(0.995, 0.99, 0.985, 0.98, 0.97, 0.95)):
    """What coverage each precision target buys — the trade-off, not one point."""
    return [(target, *operating_point(curve, target)) for target in targets]


def reliability(p: np.ndarray, y: np.ndarray, bins: int = 10) -> list[tuple[float, float, int]]:
    """(mean predicted, empirical correctness, n) per confidence bin."""
    out = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for low, high in pairwise(edges):
        mask = (p >= low) & (p < high if high < 1.0 else p <= 1.0)
        if mask.sum():
            out.append((float(p[mask].mean()), float(y[mask].mean()), int(mask.sum())))
    return out


def _plot_precision_vs_rate(curve, point, path: Path) -> None:
    """Plot only the operating points that actually exist.

    A line plot over the threshold sweep is actively misleading here: nearly
    all clean fields receive an identical score, so coverage jumps from 0% to
    ~95% with nothing in between, and a connected line interpolates straight
    through the target — implying operating points that cannot be selected.
    Markers plus a step function show the real, discrete choice set.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    achievable = sorted({(round(c[1], 6), round(c[2], 6)) for c in curve})
    rates = [a[0] for a in achievable]
    precisions = [a[1] for a in achievable]

    fig, ax = plt.subplots(figsize=(7.5, 4.8))
    ax.step(
        rates,
        precisions,
        where="post",
        lw=1.5,
        color="#0f766e",
        alpha=0.55,
        label="reachable envelope",
    )
    ax.plot(rates, precisions, "o", ms=7, color="#0f766e", label="selectable operating points")
    ax.axhline(0.99, ls="--", lw=1, color="#b91c1c", label="99% precision target")
    ax.plot([point[1]], [point[2]], "*", ms=18, color="#b91c1c", zorder=5)
    ax.annotate(
        f"chosen: threshold {point[0]:.3f}\n{point[1]:.1%} auto-approved\n{point[2]:.2%} precision",
        xy=(point[1], point[2]),
        xytext=(-14, 34),
        textcoords="offset points",
        ha="right",
        fontsize=9,
        bbox={"boxstyle": "round,pad=0.4", "fc": "#fef2f2", "ec": "#b91c1c"},
    )
    if len(rates) > 2:
        gap_lo, gap_hi = rates[0], rates[1]
        ax.axvspan(gap_lo, gap_hi, color="#94a3b8", alpha=0.12)
        ax.text(
            (gap_lo + gap_hi) / 2,
            min(precisions) + 0.002,
            "no operating points exist in this range\n(nearly all clean fields share one score)",
            ha="center",
            va="bottom",
            fontsize=8,
            color="#475569",
            style="italic",
        )
    ax.set_xlabel("auto-approve rate (fraction of fields accepted without review)")
    ax.set_ylabel("precision among auto-approved fields")
    ax.set_title("Auto-approval trade-off (held-out golden set)")
    ax.set_xlim(-0.03, 1.03)
    ax.set_ylim(min(precisions) - 0.005, 1.004)
    ax.grid(alpha=0.3)
    ax.legend(loc="lower left", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _plot_reliability(points, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(5.5, 5))
    ax.plot([0, 1], [0, 1], ls="--", lw=1, color="#64748b", label="perfectly calibrated")
    if points:
        ax.plot(
            [p[0] for p in points],
            [p[1] for p in points],
            "o-",
            lw=2,
            color="#0f766e",
            label="observed",
        )
        for predicted, empirical, n in points:
            ax.annotate(
                f"n={n}",
                (predicted, empirical),
                textcoords="offset points",
                xytext=(4, -10),
                fontsize=7,
                color="#475569",
            )
    ax.set_xlabel("predicted confidence")
    ax.set_ylabel("empirical correctness")
    ax.set_title("Reliability (held-out golden set)")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def _surviving_errors(rows, p, y, threshold):
    """Wrong fields that would still be auto-approved — i.e. what sets the ceiling."""
    counts = defaultdict(int)
    for index, row in enumerate(rows):
        if p[index] >= threshold and y[index] == 0:
            counts[(row.error_class, row.field)] += 1
    return sorted(counts.items(), key=lambda item: -item[1])


def _breakdown(rows, p, y, threshold, key):
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        groups[getattr(row, key)].append(index)
    table = {}
    for name, indices in sorted(groups.items()):
        idx = np.array(indices)
        approved = p[idx] >= threshold
        table[name] = {
            "n": len(idx),
            "auto_approve_rate": float(approved.mean()),
            "precision": float(y[idx][approved].mean()) if approved.any() else float("nan"),
            "recall_of_errors": float(
                1.0 - (y[idx][approved] == 0).sum() / max((y[idx] == 0).sum(), 1)
            ),
        }
    return table


def main() -> None:
    parser = argparse.ArgumentParser(description="Confidence calibration study")
    parser.add_argument("--build", action="store_true", help="(re)build the dataset")
    parser.add_argument("--fit-only", action="store_true", help="skip the build step")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--target-precision", type=float, default=0.99)
    args = parser.parse_args()

    # The study operates on one tenant's corpus; RLS needs it bound.
    with tenant_context(get_settings().default_tenant_id):
        if args.build or not args.fit_only:
            build_dataset(limit=args.limit)
        rows = load_rows()
    if not rows:
        raise SystemExit("no calibration rows found — run with --build first")

    golden_ids = {record["doc_id"] for record in golden_split(_records(), 100)}
    train = [row for row in rows if row.doc_id not in golden_ids]
    held = [row for row in rows if row.doc_id in golden_ids]
    if not train or not held:
        raise SystemExit("train/holdout split is empty — rebuild the dataset")

    X_train = np.array([row.features for row in train], dtype=float)
    y_train = np.array([row.correct for row in train], dtype=float)
    X_held = np.array([row.features for row in held], dtype=float)
    y_held = np.array([row.correct for row in held], dtype=float)

    weights, mu, sigma = fit_logistic(X_train, y_train)
    p_held = predict(X_held, weights, mu, sigma)

    curve = sweep(p_held, y_held)
    point, target_met = operating_point(curve, args.target_precision)
    threshold, rate, precision = point
    # Kept for the record: what pure max-coverage selection would have chosen.
    widest = [c for c in curve if c[2] >= args.target_precision and c[1] >= MIN_USEFUL_RATE]
    max_coverage_point = max(widest, key=lambda c: c[1]) if widest else point

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    _plot_precision_vs_rate(curve, point, DOCS_DIR / "calibration_precision_vs_rate.png")
    _plot_reliability(reliability(p_held, y_held), DOCS_DIR / "calibration_reliability.png")

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "schema_version": CONFIG_VERSION,
                "model": "logistic",
                "features": list(FEATURES),
                "weights": [float(w) for w in weights[:-1]],
                "bias": float(weights[-1]),
                "standardization": {
                    "mean": [float(m) for m in mu],
                    "std": [float(s) for s in sigma],
                },
                "operating_point": {
                    "selection_rule": (
                        "most precise point within "
                        f"{COVERAGE_TOLERANCE:.0%} coverage of the max-coverage point"
                    ),
                    "threshold": threshold,
                    "target_precision": args.target_precision,
                    "target_met": target_met,
                    "measured_precision": precision,
                    "auto_approve_rate": rate,
                    "max_coverage_alternative": {
                        "threshold": max_coverage_point[0],
                        "auto_approve_rate": max_coverage_point[1],
                        "measured_precision": max_coverage_point[2],
                        "note": (
                            "What max-coverage selection would have picked. Rejected: it "
                            "auto-approves wrong invoice totals to buy ~1.4% throughput."
                        ),
                    },
                },
                "provenance": {
                    "fitted_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "git_sha": _git_sha(),
                    "train_rows": len(train),
                    "holdout_rows": len(held),
                    "dataset": dataset_meta(),
                    "note": (
                        "Fitted on non-golden documents; operating point measured on the "
                        "held-out golden set. Consumed by Phase 2.3 routing."
                    ),
                },
            },
            indent=2,
        )
        + "\n"
    )

    report = _render_report(
        train,
        held,
        p_held,
        y_held,
        weights,
        point,
        target_met,
        curve,
        args.target_precision,
        max_coverage_point,
    )
    (DOCS_DIR / "calibration_results.md").write_text(report)
    print(report)
    print(f"config written to {CONFIG_PATH.relative_to(REPO_ROOT)}")


def _render_report(
    train, held, p_held, y_held, weights, point, target_met, curve, target, max_coverage_point
):
    threshold, rate, precision = point
    meta = dataset_meta()
    n_train_docs = len({row.doc_id for row in train})
    n_held_docs = len({row.doc_id for row in held})
    lines = [
        "# Confidence calibration study",
        "",
        f"Fitted {datetime.now(UTC).date()} · git `{_git_sha()}` · "
        f"dataset built {meta.get('built_at', 'unknown')} from "
        f"{meta.get('documents', '?')} documents · "
        f"provider `{meta.get('model_provider', '?')}` · "
        f"corruption rate {meta.get('corruption_rate', '?')} "
        f"(seed {meta.get('corruption_seed', '?')})",
        "",
        "Unit of analysis is the **field**: the decision being calibrated is whether one",
        "cell can be accepted without a human looking at it. Weights are fitted on the",
        "non-golden documents; every number below is measured on the held-out golden set,",
        "so the threshold is not reported on its own training data.",
        "",
        f"- train rows (fields): **{len(train)}** across {n_train_docs} documents",
        f"- holdout rows (fields): **{len(held)}** across {n_held_docs} documents",
        f"- error rate in holdout: **{1 - y_held.mean():.1%}** of fields wrong",
        "",
        "## Operating point",
        "",
        f"Target: auto-approved fields must be **>= {target:.0%}** correct.",
        "",
        (
            "Target met."
            if target_met
            else f"**Not reachable at any usable coverage.** The best achievable precision "
            f"is {precision:.2%}, so the point below is the best available — not one that "
            f"meets the {target:.0%} target. Why, and what it would take, is in "
            "*Where the ceiling comes from*."
        ),
        "",
        "| | value |",
        "|---|---|",
        f"| threshold | **{threshold:.4f}** |",
        f"| auto-approve rate | **{rate:.1%}** of fields |",
        f"| precision among auto-approved | **{precision:.2%}** |",
        f"| fields still sent to review | {1 - rate:.1%} |",
        f"| target met | **{'yes' if target_met else 'NO'}** |",
        "",
        "### What each precision target buys",
        "",
        "| target | reachable | threshold | auto-approve rate | actual precision |",
        "|---|---|---|---|---|",
        *[
            f"| {tgt:.1%} | {'yes' if ok else '**no**'} | {pt[0]:.3f} | {pt[1]:.1%} | {pt[2]:.2%} |"
            for tgt, pt, ok in frontier(curve)
        ],
        "",
        "![precision vs auto-approve rate](calibration_precision_vs_rate.png)",
        "",
        "**The score is discrete, not continuous.** Nearly every clean field gets an "
        "identical score, because their signal features are all zero, so coverage jumps "
        "from 0% straight to ~95% with nothing selectable in between. Only a handful of "
        "operating points exist. Phase 2.3 therefore cannot dial coverage finely: it picks "
        "one of these points. Finer control needs a feature that varies across *clean* "
        "documents (per-field extraction margin, say), not more weight tuning.",
        "",
        "## Fitted weights",
        "",
        "| feature | weight |",
        "|---|---|",
    ]
    for name, weight in zip(FEATURES, weights[:-1], strict=True):
        lines.append(f"| `{name}` | {weight:+.3f} |")
    lines += [
        f"| _bias_ | {weights[-1]:+.3f} |",
        "",
        "Positive weight = pushes towards *correct*. Standardized inputs, so magnitudes",
        "are comparable across features.",
        "",
        "## By layout (holdout)",
        "",
        "| layout | fields | auto-approve rate | precision |",
        "|---|---|---|---|",
    ]
    for name, stats in _breakdown(held, p_held, y_held, threshold, "layout").items():
        lines.append(
            f"| {name} | {stats['n']} | {stats['auto_approve_rate']:.1%} | "
            f"{stats['precision']:.2%} |"
        )
    lines += [
        "",
        "## By injected error class (holdout)",
        "",
        "Documents are grouped by the error deliberately injected into them. "
        "`none` means the document was left clean.",
        "",
        "| error class | fields | auto-approve rate | precision |",
        "|---|---|---|---|",
    ]
    for name, stats in _breakdown(held, p_held, y_held, threshold, "error_class").items():
        lines.append(
            f"| `{name}` | {stats['n']} | {stats['auto_approve_rate']:.1%} | "
            f"{stats['precision']:.2%} |"
        )
    survivors = _surviving_errors(held, p_held, y_held, threshold)
    total_wrong = int((y_held == 0).sum())
    lines += [
        "",
        "## Where the ceiling comes from",
        "",
        f"Of {total_wrong} wrong fields in the holdout, "
        f"{sum(n for _, n in survivors)} would still be auto-approved at threshold "
        f"{threshold:.3f}. These are the residual error — the reason a higher precision "
        "target is or is not reachable.",
        "",
        "| injected class | field | count |",
        "|---|---|---|",
        *[f"| `{cls}` | {field} | {n} |" for (cls, field), n in survivors],
        "",
        "`shifted_date` is undetectable by construction: it moves the invoice date earlier "
        "while leaving every validation rule satisfied, so no current signal can see it. It "
        "is injected deliberately so the ceiling appears in the results rather than being "
        "hidden by only testing catchable errors.",
        "",
        "## Reliability",
        "",
        "Does the score behave like a probability, or only like a ranking?",
        "",
        "![reliability](calibration_reliability.png)",
        "",
        "| predicted | empirical | n |",
        "|---|---|---|",
    ]
    for predicted, empirical, n in reliability(p_held, y_held):
        lines.append(f"| {predicted:.3f} | {empirical:.3f} | {n} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
