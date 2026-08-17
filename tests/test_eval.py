"""Eval harness tests: deterministic split (R4) + mock-mode smoke run.

Needs the generated corpus (data/synth/out); auto-skips on a fresh clone
before `make seed`. No services required — the eval is in-process.
"""

import json
import random
import sys
from pathlib import Path

import pytest

CORPUS = Path("data/synth/out")
GROUND_TRUTH = CORPUS / "ground_truth.jsonl"

pytestmark = pytest.mark.skipif(
    not GROUND_TRUTH.exists(), reason="synthetic corpus not generated (run `make seed`)"
)


def _records() -> list[dict]:
    return [json.loads(line) for line in GROUND_TRUTH.open()]


def test_golden_split_is_deterministic_and_order_independent():
    from docfactory_evals.run import golden_split

    records = _records()
    golden = golden_split(records, 100)
    assert len(golden) == 100

    shuffled = records.copy()
    random.Random(0).shuffle(shuffled)
    assert [r["doc_id"] for r in golden_split(shuffled, 100)] == [r["doc_id"] for r in golden]
    # split keys off doc_id, not file bytes — regenerated PDFs don't move it
    assert golden[0]["doc_id"] == golden_split(records, 100)[0]["doc_id"]


def test_golden_split_contains_scanned_docs():
    from docfactory_evals.run import golden_split

    scanned = sum(1 for r in golden_split(_records(), 100) if r["scanned"])
    assert 10 <= scanned <= 50  # ~30% of a fair split


def test_eval_smoke_run_mock(tmp_path, monkeypatch):
    from docfactory_evals import run

    out = tmp_path / "eval_results.md"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run", "--limit", "8", "--workers", "2", "--out", str(out)],
    )
    run.main()
    table = out.read_text()
    assert "provider/model: `mock:mock-extractor-v1`" in table
    assert "needs_ocr" in table
    assert "| **all fields** |" in table
