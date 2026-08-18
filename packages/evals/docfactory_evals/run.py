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
from decimal import Decimal
from pathlib import Path

from docfactory_core.confidence import score_extraction
from docfactory_core.confidence_model import confidence_model_for, route_extraction
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
from docfactory_core.pipeline import FieldKind, FieldSpec, PipelineDefinition, evaluate_rules
from docfactory_core.pipeline_registry import available_slugs, load_from_file
from docfactory_core.pricing import call_cost_usd
from docfactory_core.routing import escalation_trigger, model_for_tier

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
    # Metering: what this document cost to extract, and whether routing had to
    # escalate it to the expensive model.
    cost_usd: Decimal = Decimal("0")
    escalated: bool = False
    calls: int = 0


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


def _one_call(text: str, definition: PipelineDefinition, provider: str, tier: str):
    """One extraction on a named tier, with what it cost."""
    client = get_llm_client(model=model_for_tier(provider, tier))
    outcome = run_extraction(text, client, definition)
    label = f"{client.provider}:{client.model}"
    return outcome, call_cost_usd(label, outcome.input_tokens, outcome.output_tokens)


def evaluate_document(
    record: dict,
    pdf_dir: Path,
    min_chars: int,
    definition: PipelineDefinition,
    *,
    provider: str,
    routed: bool,
) -> DocResult:
    """Extract one document, either on the frontier model or under the routing policy.

    `routed=False` is the single-model baseline — the measurement Phases 1-3
    reported, kept as the regression guard. `routed=True` runs the pipeline's
    own policy: the cheap tier first, escalating only when the deterministic
    checks say the cheap answer is suspect.
    """
    result = DocResult(doc_id=record["doc_id"], layout=record["layout"])
    text = extract_pdf_text((pdf_dir / record["file"]).read_bytes())
    if len(text) < min_chars:
        result.needs_ocr = True
        return result

    policy = definition.model_routing
    tier = policy.primary_tier if (routed and policy) else "frontier"
    outcome, cost = _one_call(text, definition, provider, tier)
    result.calls, result.cost_usd = 1, cost

    if routed and policy and policy.escalates:
        trigger = escalation_trigger(policy, **_assessment(outcome, text, definition))
        if trigger:
            escalated, escalation_cost = _one_call(text, definition, provider, policy.escalate_to)
            result.calls, result.escalated = 2, True
            result.cost_usd += escalation_cost
            if escalated.record is not None:
                outcome = escalated

    if outcome.record is None:
        result.extraction_failed = True
        result.fields = dict.fromkeys(definition.scored_fields, False)
        return result
    result.fields = compare(outcome.record, expected_fields(record, definition), definition)
    return result


def _assessment(outcome, text: str, definition: PipelineDefinition) -> dict:
    """The deterministic verdicts routing escalates on."""
    if outcome.record is None:
        return {"extraction_failed": True, "validation_passed": None, "routing_decision": None}
    validation = evaluate_rules(outcome.record, definition)
    report = score_extraction(
        outcome.record,
        validation,
        attempts=outcome.attempts,
        source_text=text,
        definition=definition,
    )
    decision = route_extraction(
        report.signals, confidence_model_for(definition), definition
    ).decision
    return {
        "extraction_failed": False,
        "validation_passed": all(validation.values()),
        "routing_decision": decision,
    }


def render_section(
    definition: PipelineDefinition,
    results: list[DocResult],
    requested_size: int,
    capped_size: int,
    total: int,
    heading: str | None = None,
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
        heading or f"## {definition.document_type} (`{definition.slug}` v{definition.version})",
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


def render_cost_note(baseline: list[DocResult], routed: list[DocResult]) -> str:
    """What routing bought, in dollars per document."""
    digital = [r for r in routed if not r.needs_ocr]
    base_digital = [r for r in baseline if not r.needs_ocr]
    if not digital or not base_digital:
        return ""

    base_cost = sum((r.cost_usd for r in base_digital), Decimal("0")) / len(base_digital)
    routed_cost = sum((r.cost_usd for r in digital), Decimal("0")) / len(digital)
    escalated = sum(1 for r in digital if r.escalated)
    saving = (1 - routed_cost / base_cost) * 100 if base_cost else Decimal("0")

    def accuracy(rows: list[DocResult]) -> float:
        cells = [ok for r in rows for ok in r.fields.values()]
        return 100 * sum(cells) / len(cells) if cells else 0.0

    # Break-even: above this escalation rate, routing costs more than it saves.
    per_call_base = base_cost
    small_share = routed_cost / per_call_base if per_call_base else Decimal("0")
    return "\n".join(
        [
            "",
            f"- escalated: {escalated} of {len(digital)} digital docs"
            f" ({100 * escalated / len(digital):.1f}%)",
            f"- cost per document: **${routed_cost:.6f} routed** vs"
            f" ${base_cost:.6f} single-model — **{saving:.1f}% cheaper**"
            f" (routed spend is {small_share:.2f}x the single-model spend)",
            f"- accuracy: {accuracy(digital):.1f}% routed vs {accuracy(base_digital):.1f}%"
            " single-model, all fields",
            "",
        ]
    )


def evaluate_type(
    slug: str,
    pdf_dir: Path,
    golden_size: int,
    limit: int | None,
    workers: int,
    provider: str,
    min_chars: int,
) -> tuple[PipelineDefinition, dict[str, list[DocResult]], int, int]:
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
    # Two passes when the pipeline routes: the single-model baseline is the
    # regression guard the earlier phases reported, and the routed pass is what
    # production actually runs. Reporting only one of them would either hide a
    # regression or hide the cost saving.
    modes = ["frontier"]
    if definition.model_routing and definition.model_routing.escalates:
        modes.append("routed")

    results: dict[str, list[DocResult]] = {}
    for mode in modes:
        print(f"evaluating {len(golden)} golden {slug} docs ({mode}) ...")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results[mode] = list(
                pool.map(
                    lambda record, mode=mode, provider=provider: evaluate_document(
                        record,
                        pdf_dir,
                        min_chars,
                        definition,
                        provider=provider,
                        routed=mode == "routed",
                    ),
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
    provider = settings.model_provider
    tier_models = {
        tier: f"{provider}:{model_for_tier(provider, tier)}" for tier in ("small", "frontier")
    }

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
        f"- provider/model: `{tier_models['frontier']}` · small tier: `{tier_models['small']}`",
        "- costs are the models' published per-token rates applied to the tokens"
        " each call actually used",
        "",
    ]
    for slug in slugs:
        definition, results, total, capped = evaluate_type(
            slug,
            args.pdf_dir,
            args.golden_size,
            args.limit,
            args.workers,
            provider,
            settings.min_parse_chars,
        )
        sections.append(
            f"## {definition.document_type} (`{definition.slug}` v{definition.version})"
        )
        sections.append("")
        sections.append(
            render_section(
                definition,
                results["frontier"],
                args.golden_size,
                capped,
                total,
                heading="### single model — the Phase 1-3 regression guard",
            )
        )
        if "routed" in results:
            sections.append("")
            sections.append(
                render_section(
                    definition,
                    results["routed"],
                    args.golden_size,
                    capped,
                    total,
                    heading="### routed — cheap tier first, escalating on a failed check",
                )
            )
            sections.append(render_cost_note(results["frontier"], results["routed"]))
        sections.append("")

    report = "\n".join(sections).rstrip() + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print()
    print(report)
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
