"""Eval harness: field accuracy on the golden set.

Golden split (R4): all 500 doc_ids ordered by sha256(doc_id), first 100. The
hash is over the *id*, not file bytes — PDFs embed timestamps and are not
byte-stable across regenerations, while doc_ids are.

Runs the extraction path in-process (same extract_pdf_text + run_extraction
the worker uses) rather than through the API/queues, so it needs no services
and runs in CI in mock mode. Pipeline transport is covered by the integration
tests; this measures extraction quality.

Comparison: both sides pass through the R2 normalizer, then Decimal/date/text
equality. Scanned docs (below the text threshold) are counted as needs_ocr
and excluded from the accuracy denominators (R1); extraction failures stay IN
the denominators as all-fields-wrong — accuracy numbers must not silently
drop failures.
"""

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docfactory_core.config import get_settings
from docfactory_core.extraction import run_extraction
from docfactory_core.llm import get_llm_client
from docfactory_core.normalize import (
    normalize_amount,
    normalize_date,
    normalize_rate,
    normalize_text,
)
from docfactory_core.parsing import extract_pdf_text
from docfactory_core.schemas import SCALAR_FIELD_NAMES, Invoice

LAYOUTS = ("classic", "modern", "euro")
ALL_FIELDS = (*SCALAR_FIELD_NAMES, "line_items")


@dataclass
class DocResult:
    doc_id: str
    layout: str
    needs_ocr: bool = False
    extraction_failed: bool = False
    fields: dict[str, bool] = field(default_factory=dict)


def golden_split(records: list[dict], size: int) -> list[dict]:
    return sorted(records, key=lambda r: hashlib.sha256(r["doc_id"].encode()).hexdigest())[:size]


def _eq_text(extracted: str, expected: str) -> bool:
    return normalize_text(extracted) == normalize_text(expected)


def _eq_amount(extracted, expected) -> bool:
    try:
        return normalize_amount(extracted) == normalize_amount(expected)
    except ValueError:
        return False


def _eq_rate(extracted, expected) -> bool:
    try:
        return normalize_rate(extracted) == normalize_rate(expected)
    except ValueError:
        return False


def _eq_date(extracted, expected) -> bool:
    try:
        left = extracted if isinstance(extracted, date) else normalize_date(extracted)
        return left == normalize_date(expected)
    except ValueError:
        return False


def _compare(invoice: Invoice, expected: dict) -> dict[str, bool]:
    results = {
        "vendor": _eq_text(invoice.vendor, expected["vendor"]),
        "invoice_number": _eq_text(invoice.invoice_number, expected["invoice_number"]),
        "invoice_date": _eq_date(invoice.invoice_date, expected["invoice_date"]),
        "due_date": _eq_date(invoice.due_date, expected["due_date"]),
        "currency": invoice.currency == expected.get("currency", ""),
        "subtotal": _eq_amount(invoice.subtotal, expected["subtotal"]),
        "tax_rate": _eq_rate(invoice.tax_rate, expected["tax_rate"]),
        "tax": _eq_amount(invoice.tax, expected["tax"]),
        "total": _eq_amount(invoice.total, expected["total"]),
    }
    expected_items = expected["line_items"]
    results["line_items"] = len(invoice.line_items) == len(expected_items) and all(
        _eq_text(got.description, want["description"])
        and _eq_amount(got.quantity, want["quantity"])
        and _eq_amount(got.unit_price, want["unit_price"])
        and _eq_amount(got.amount, want["amount"])
        for got, want in zip(invoice.line_items, expected_items, strict=True)
    )
    return results


def evaluate_document(record: dict, pdf_dir: Path, client, min_chars: int) -> DocResult:
    result = DocResult(doc_id=record["doc_id"], layout=record["layout"])
    text = extract_pdf_text((pdf_dir / record["file"]).read_bytes())
    if len(text) < min_chars:
        result.needs_ocr = True
        return result
    outcome = run_extraction(text, client)
    if outcome.invoice is None:
        result.extraction_failed = True
        result.fields = dict.fromkeys(ALL_FIELDS, False)
        return result
    # record["currency"] lives at the top level of the ground-truth record
    expected = {**record["fields"], "currency": record["currency"]}
    result.fields = _compare(outcome.invoice, expected)
    return result


def render_table(results: list[DocResult], model_label: str, golden_size: int, total: int) -> str:
    digital = [r for r in results if not r.needs_ocr]
    needs_ocr = sum(1 for r in results if r.needs_ocr)
    failed = sum(1 for r in results if r.extraction_failed)

    def pct(subset: list[DocResult], field_name: str | None) -> str:
        if field_name is None:
            cells = [ok for r in subset for ok in r.fields.values()]
        else:
            cells = [r.fields[field_name] for r in subset]
        return f"{100 * sum(cells) / len(cells):.1f}%" if cells else "—"

    by_layout = {layout: [r for r in digital if r.layout == layout] for layout in LAYOUTS}
    header = " | ".join(f"{layout} (n={len(by_layout[layout])})" for layout in LAYOUTS)
    lines = [
        "# Eval results — field accuracy on the golden set",
        "",
        f"- date: {date.today().isoformat()}",
        f"- provider/model: `{model_label}`",
        f"- golden set: {len(results)} of {total} docs, deterministic sha256(doc_id) split"
        + (f" (limited from {golden_size})" if len(results) < golden_size else ""),
        f"- digital evaluated: {len(digital)} · needs_ocr (excluded, no text layer): {needs_ocr}"
        f" · extraction failures (counted as wrong): {failed}",
        "",
        f"| field | {header} | overall |",
        "|---|---|---|---|---|",
    ]
    for field_name in ALL_FIELDS:
        label = "line_items (exact list)" if field_name == "line_items" else field_name
        row = " | ".join(pct(by_layout[layout], field_name) for layout in LAYOUTS)
        lines.append(f"| {label} | {row} | {pct(digital, field_name)} |")
    all_row = " | ".join(f"**{pct(by_layout[layout], None)}**" for layout in LAYOUTS)
    lines.append(f"| **all fields** | {all_row} | **{pct(digital, None)}** |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="DocFactory eval harness")
    parser.add_argument(
        "--ground-truth", type=Path, default=Path("data/synth/out/ground_truth.jsonl")
    )
    parser.add_argument("--pdf-dir", type=Path, default=Path("data/synth/out"))
    parser.add_argument("--golden-size", type=int, default=100)
    parser.add_argument(
        "--limit", type=int, default=None, help="evaluate only the first N golden docs"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("docs/eval_results.md"))
    args = parser.parse_args()

    settings = get_settings()
    records = [json.loads(line) for line in args.ground_truth.open()]
    golden = golden_split(records, args.golden_size)
    if args.limit:
        golden = golden[: args.limit]

    client = get_llm_client()
    model_label = f"{client.provider}:{client.model}"
    print(f"evaluating {len(golden)} golden docs with {model_label} ...")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(
            pool.map(
                lambda record: evaluate_document(
                    record, args.pdf_dir, client, settings.min_parse_chars
                ),
                golden,
            )
        )

    table = render_table(results, model_label, args.golden_size, total=len(records))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(table)
    print()
    print(table)
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
