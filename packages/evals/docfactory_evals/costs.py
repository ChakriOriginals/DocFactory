"""Unit-cost rollup from recorded usage.

`make eval` measures cost on the golden set in-process; this reads what the
running system actually spent — every `usage_events` row written by the worker
— and renders the per-document unit cost by document type and model tier.

Two numbers matter and both are here: the cost of a document, and how much of
it went to the expensive tier.
"""

import argparse
from pathlib import Path

from docfactory_core.config import get_settings
from docfactory_core.db import tenant_context
from docfactory_core.metering import cost_rows, unit_costs


def render(tenant_id: str) -> str:
    rows = cost_rows(tenant_id)
    units = unit_costs(tenant_id)
    if not rows:
        return (
            "# Unit costs\n\nNo usage recorded yet — run documents through the "
            "worker (`make worker`) and re-run `make costs`.\n"
        )

    lines = [
        "# Unit costs — from recorded usage events",
        "",
        "A snapshot: this is whatever the tenant has processed so far, so it moves",
        "with traffic. The reproducible per-document number lives in the eval report,",
        "which measures a fixed golden set.",
        "",
        f"- tenant: `{tenant_id}`",
        f"- calls metered: {sum(r.calls for r in rows)}",
        "",
        "## Per document type",
        "",
        "| document type | documents | calls | escalation rate | total | **per document** |",
        "|---|---|---|---|---|---|",
    ]
    for unit in units:
        lines.append(
            f"| {unit.pipeline_slug} | {unit.documents} | {unit.calls} | "
            f"{100 * unit.escalation_rate:.1f}% | ${unit.cost_usd:.6f} | "
            f"**${unit.cost_per_document:.6f}** |"
        )

    lines += [
        "",
        "## Per model tier",
        "",
        "| document type | tier | model | calls | input tokens | output tokens | cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row.pipeline_slug} | {row.model_tier} | `{row.model}` | {row.calls} | "
            f"{row.input_tokens:,} | {row.output_tokens:,} | ${row.cost_usd:.6f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="DocFactory unit-cost rollup")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--out", type=Path, default=Path("docs/unit_costs.md"))
    args = parser.parse_args()

    tenant_id = args.tenant or get_settings().default_tenant_id
    with tenant_context(tenant_id):
        report = render(tenant_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report)
    print(report)
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()
