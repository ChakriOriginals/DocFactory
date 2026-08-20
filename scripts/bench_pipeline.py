"""Per-stage cost and batch throughput for the document pipeline.

Committed because docs/performance.md cites its numbers, and a cited number
that cannot be reproduced is an anecdote.

    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib uv run python scripts/bench_pipeline.py

Needs the synthetic corpus (`make seed`). Mock mode only: the point is to
measure the code, and a real model call would drown every number here in
network latency.
"""

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from docfactory_core.confidence import score_extraction
from docfactory_core.confidence_model import confidence_model_for, route_extraction
from docfactory_core.drift import text_profile
from docfactory_core.extraction import run_extraction, validate_record
from docfactory_core.groundedness import groundedness
from docfactory_core.llm import get_llm_client, heuristic_extract, load_hints, load_profile
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.pipeline import evaluate_rules
from docfactory_core.pipeline_registry import load_from_file

ROOT = Path(__file__).resolve().parents[1]
PDF_DIR = ROOT / "data" / "synth" / "out"
DEFINITION = load_from_file("invoice", 1)


def load_blobs(limit: int) -> list[bytes]:
    path = PDF_DIR / "ground_truth_invoice.jsonl"
    if not path.is_file():
        raise SystemExit(f"no corpus at {path} — run `make seed` first")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    return [(PDF_DIR / r["file"]).read_bytes() for r in records if not r["scanned"]][:limit]


def bench(label: str, fn, items, reps: int = 3) -> float:
    """Best-of-N mean. Best rather than average: the minimum is the run least
    disturbed by whatever else the machine was doing."""
    fn(items[0])
    samples = []
    for _ in range(reps):
        start = time.perf_counter()
        for item in items:
            fn(item)
        samples.append((time.perf_counter() - start) / len(items) * 1000)
    best = min(samples)
    print(f"  {label:<34} {best:8.3f} ms/doc")
    return best


def stages(blobs: list[bytes]) -> None:
    """Where the non-model CPU actually goes."""
    texts = [extract_pdf_text(blob) for blob in blobs]
    hints, profile = load_hints("invoice"), load_profile("mock-extractor-v1")
    raw = [heuristic_extract(text, hints, profile) for text in texts]

    normalized, validations = [], []
    for record in raw:
        try:
            clean = validate_record(record, DEFINITION)
        except Exception:
            clean = None
        normalized.append(clean)
        validations.append(evaluate_rules(clean, DEFINITION) if clean else {})

    triples = list(zip(normalized, validations, texts, strict=True))
    reports = [
        score_extraction(n, v, attempts=1, source_text=t, definition=DEFINITION) if n else None
        for n, v, t in triples
    ]
    model = confidence_model_for(DEFINITION)

    print(f"\nper-document cost, {len(blobs)} real invoices, model latency excluded:\n")
    total = 0.0
    total += bench("extract_pdf_text (pdfplumber)", extract_pdf_text, blobs)
    total += bench(
        "heuristic_extract (mock model)", lambda t: heuristic_extract(t, hints, profile), texts
    )
    total += bench("validate_record + normalize", lambda r: validate_record(r, DEFINITION), raw)
    total += bench(
        "evaluate_rules", lambda n: evaluate_rules(n, DEFINITION) if n else {}, normalized
    )
    total += bench(
        "score_extraction (confidence)",
        lambda p: (
            score_extraction(p[0], p[1], attempts=1, source_text=p[2], definition=DEFINITION)
            if p[0]
            else None
        ),
        triples,
    )
    total += bench(
        "route_extraction",
        lambda r: route_extraction(r.signals, model, DEFINITION) if r else None,
        reports,
    )
    total += bench(
        "groundedness (one field)", lambda t: groundedness("Northwind Traders", t), texts
    )
    total += bench("text_profile (drift)", text_profile, texts)
    print(f"\n  {'TOTAL non-model CPU':<34} {total:8.3f} ms/doc")
    print(f"  {'single-thread CPU ceiling':<34} {3600 / (total / 1000):8,.0f} docs/hour")


def one_document(blob: bytes) -> bool:
    text = extract_pdf_text(blob)
    if len(text) < 100:
        return False
    outcome = run_extraction(text, get_llm_client(model="mock-extractor-small-v1"), DEFINITION)
    if outcome.record is None:
        return False
    validation = evaluate_rules(outcome.record, DEFINITION)
    report = score_extraction(
        outcome.record,
        validation,
        attempts=outcome.attempts,
        source_text=text,
        definition=DEFINITION,
    )
    route_extraction(report.signals, confidence_model_for(DEFINITION), DEFINITION)
    return True


def flood(blobs: list[bytes]) -> None:
    """Does it scale with consumer threads, or is something serialising?"""
    one_document(blobs[0])
    print(f"\nthreaded flood, {len(blobs)} documents, mock latency INCLUDED:\n")
    print(f"  {'threads':<10}{'wall':>9}{'docs/hour':>14}{'scaling':>10}")
    baseline = None
    for threads in (1, 3, 6, 12):
        start = time.perf_counter()
        with ThreadPoolExecutor(max_workers=threads) as pool:
            list(pool.map(one_document, blobs))
        elapsed = time.perf_counter() - start
        rate = len(blobs) / elapsed * 3600
        baseline = baseline or rate
        print(f"  {threads:<10}{elapsed:>8.2f}s{rate:>14,.0f}{rate / baseline:>9.1f}x")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=int, default=60)
    args = parser.parse_args()
    blobs = load_blobs(args.documents)
    stages(blobs)
    flood(blobs)


if __name__ == "__main__":
    main()
