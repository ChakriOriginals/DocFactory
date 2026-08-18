"""Eval harness: field accuracy on the golden set, per document type.

Golden split (R4): all doc_ids of a type ordered by sha256(doc_id), first N.
The hash is over the *id*, not file bytes — PDFs embed timestamps and are not
byte-stable across regenerations, while doc_ids are. The split is taken within
a type, so adding a document type cannot move another type's golden set.

Runs the extraction path in-process (same extract_pdf_text + run_extraction
the worker uses) rather than through the API/queues, so it needs no services
and runs in CI in mock mode. Pipeline transport is covered by the integration
tests; this measures extraction quality.

Nothing here knows what an invoice is: which fields exist, and how each is
compared, comes from the pipeline definition's field kinds. Both sides pass
through the R2 normalizer, then Decimal/date/text equality. Scanned docs
(below the text threshold) are counted as needs_ocr and excluded from the
accuracy denominators (R1); extraction failures stay IN the denominators as
all-fields-wrong — accuracy numbers must not silently drop failures.
"""

import argparse
import hashlib
import json
from collections.abc import Callable
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
from docfactory_core.pipeline import FieldKind, FieldSpec, PipelineDefinition
from docfactory_core.pipeline_registry import available_slugs, load_from_file

DEFAULT_GOLDEN_SIZE = 100


def ground_truth_path(pdf_dir: Path, slug: str) -> Path:
    return pdf_dir / f"ground_truth_{slug}.jsonl"


@dataclass
class DocResult:
    doc_id: str
    layout: str
    needs_ocr: bool = False
    extraction_failed: bool = False
    fields: dict[str, bool] = field(default_factory=dict)


def golden_split(records: list[dict], size: int) -> list[dict]:
    return sorted(records, key=lambda r: hashlib.sha256(r["doc_id"].encode()).hexdigest())[:size]


def _eq_text(extracted, expected) -> bool:
    if extracted is None or expected is None:
        return False
    return normalize_text(extracted) == normalize_text(expected)


def _eq_amount(extracted, expected) -> bool:
    try:
        return normalize_amount(extracted) == normalize_amount(expected)
    except (ValueError, TypeError):
        return False


def _eq_rate(extracted, expected) -> bool:
    try:
        return normalize_rate(extracted) == normalize_rate(expected)
    except (ValueError, TypeError):
        return False


def _eq_date(extracted, expected) -> bool:
    try:
        left = extracted if isinstance(extracted, date) else normalize_date(extracted)
        return left == normalize_date(expected)
    except (ValueError, TypeError):
        return False


def _eq_exact(extracted, expected) -> bool:
    return extracted is not None and str(extracted) == str(expected)


# How a field is compared follows from what it *is*, so a new document type
# needs no new comparison code.
COMPARATORS: dict[FieldKind, Callable[[object, object], bool]] = {
    FieldKind.TEXT: _eq_text,
    FieldKind.DATE: _eq_date,
    FieldKind.MONEY: _eq_amount,
    FieldKind.QUANTITY: _eq_amount,
    FieldKind.RATE: _eq_rate,
    FieldKind.ENUM: _eq_exact,
}


def _eq_table(extracted, expected, spec: FieldSpec) -> bool:
    """Exact list match: same length, every cell equal by its column's kind."""
    rows, wanted = extracted or [], expected or []
    if len(rows) != len(wanted):
        return False
    return all(
        COMPARATORS[column.kind](row.get(name), want.get(name))
        for row, want in zip(rows, wanted, strict=True)
        for name, column in spec.item_fields.items()
    )


def compare(record: dict, expected: dict, definition: PipelineDefinition) -> dict[str, bool]:
    results = {}
    for name, spec in definition.fields.items():
        if spec.kind is FieldKind.TABLE:
            results[name] = _eq_table(record.get(name), expected.get(name), spec)
        else:
            results[name] = COMPARATORS[spec.kind](record.get(name), expected.get(name))
    return results


def expected_fields(record: dict, definition: PipelineDefinition) -> dict:
    """Ground-truth labels for the declared fields.

    Labels normally live under "fields"; any top-level key that names a
    declared field is folded in too, which is where the invoice corpus keeps
    `currency`.
    """
    top_level = {key: value for key, value in record.items() if key in definition.fields}
    return {**record.get("fields", {}), **top_level}


def evaluate_document(
    record: dict, pdf_dir: Path, client, min_chars: int, definition: PipelineDefinition
) -> DocResult:
    result = DocResult(doc_id=record["doc_id"], layout=record["layout"])
    text = extract_pdf_text((pdf_dir / record["file"]).read_bytes())
    if len(text) < min_chars:
        result.needs_ocr = True
        return result
    outcome = run_extraction(text, client, definition)
    if outcome.record is None:
        result.extraction_failed = True
        result.fields = dict.fromkeys(definition.scored_fields, False)
        return result
    result.fields = compare(outcome.record, expected_fields(record, definition), definition)
    return result


def render_section(
    definition: PipelineDefinition,
    results: list[DocResult],
    requested_size: int,
    capped_size: int,
    total: int,
) -> str:
    digital = [r for r in results if not r.needs_ocr]
    needs_ocr = sum(1 for r in results if r.needs_ocr)
    failed = sum(1 for r in results if r.extraction_failed)
    layouts = sorted({r.layout for r in results})

    def pct(subset: list[DocResult], field_name: str | None) -> str:
        if field_name is None:
            cells = [ok for r in subset for ok in r.fields.values()]
        else:
            cells = [r.fields[field_name] for r in subset]
        return f"{100 * sum(cells) / len(cells):.1f}%" if cells else "—"

    by_layout = {layout: [r for r in digital if r.layout == layout] for layout in layouts}
    header = " | ".join(f"{layout} (n={len(by_layout[layout])})" for layout in layouts)
    lines = [
        f"## {definition.document_type} (`{definition.slug}` v{definition.version})",
        "",
        f"- golden set: {len(results)} of {total} docs, deterministic sha256(doc_id) split"
        + (
            f", capped at a third of the corpus (asked {requested_size})"
            if capped_size < requested_size
            else ""
        )
        + (f", limited to the first {len(results)}" if len(results) < capped_size else ""),
        f"- digital evaluated: {len(digital)} · needs_ocr (excluded, no text layer): {needs_ocr}"
        f" · extraction failures (counted as wrong): {failed}",
        "",
        f"| field | {header} | overall |",
        "|---|" + "---|" * (len(layouts) + 1),
    ]
    for field_name, spec in definition.fields.items():
        label = f"{field_name} (exact list)" if spec.kind is FieldKind.TABLE else field_name
        row = " | ".join(pct(by_layout[layout], field_name) for layout in layouts)
        lines.append(f"| {label} | {row} | {pct(digital, field_name)} |")
    all_row = " | ".join(f"**{pct(by_layout[layout], None)}**" for layout in layouts)
    lines.append(f"| **all fields** | {all_row} | **{pct(digital, None)}** |")
    return "\n".join(lines) + "\n"


def evaluate_type(
    slug: str,
    pdf_dir: Path,
    golden_size: int,
    limit: int | None,
    workers: int,
    client,
    min_chars: int,
) -> tuple[PipelineDefinition, list[DocResult], int]:
    definition = load_from_file(slug)
    records = [
        json.loads(line)
        for line in ground_truth_path(pdf_dir, slug).read_text().splitlines()
        if line.strip()
    ]
    # A golden set is never more than a third of a corpus: the rest has to
    # stay available as holdout for the calibration study. That is what keeps
    # a smaller second corpus from being almost entirely golden, without
    # per-type configuration.
    capped = min(golden_size, max(1, len(records) // 3))
    golden = golden_split(records, capped)
    if limit:
        golden = golden[:limit]
    print(f"evaluating {len(golden)} golden {slug} docs ...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda record: evaluate_document(record, pdf_dir, client, min_chars, definition),
                golden,
            )
        )
    return definition, results, len(records), capped


def main() -> None:
    parser = argparse.ArgumentParser(description="DocFactory eval harness")
    parser.add_argument("--pdf-dir", type=Path, default=Path("data/synth/out"))
    parser.add_argument(
        "--type",
        dest="types",
        action="append",
        help="document type to evaluate (repeatable); default: every type with a corpus",
    )
    parser.add_argument("--golden-size", type=int, default=DEFAULT_GOLDEN_SIZE)
    parser.add_argument(
        "--limit", type=int, default=None, help="evaluate only the first N golden docs per type"
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=Path("docs/eval_results.md"))
    args = parser.parse_args()

    settings = get_settings()
    client = get_llm_client()
    model_label = f"{client.provider}:{client.model}"

    slugs = args.types or [
        slug for slug in available_slugs() if ground_truth_path(args.pdf_dir, slug).is_file()
    ]
    if not slugs:
        raise SystemExit(
            f"no corpus found in {args.pdf_dir} — run `make seed` to generate one "
            f"(looked for ground_truth_<type>.jsonl for: {list(available_slugs())})"
        )

    sections = [
        "# Eval results — field accuracy on the golden set",
        "",
        f"- date: {date.today().isoformat()}",
        f"- provider/model: `{model_label}`",
        "",
    ]
    for slug in slugs:
        definition, results, total, capped = evaluate_type(
            slug,
            args.pdf_dir,
            args.golden_size,
            args.limit,
            args.workers,
            client,
            settings.min_parse_chars,
        )
        sections.append(render_section(definition, results, args.golden_size, capped, total))
        sections.append("")

    report = "\n".join(sections).rstrip() + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print()
    print(report)
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
