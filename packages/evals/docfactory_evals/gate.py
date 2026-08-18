"""The eval gate: a regression in extraction quality must not reach the stack.

`make eval` measures; this decides. It re-runs the golden set in mock mode and
compares each document type against the floors in config/eval_thresholds.json,
exiting non-zero if any of them is breached. CI runs it between the tests and
the build, so a pull request that weakens a prompt, a rule or a field mapping
fails there rather than being discovered by a customer.

Three deliberate choices:

*Mock mode, always.* The gate must be free, deterministic, and runnable by
anyone including a fork's CI. Mock extraction is a pure function of the corpus,
so the same commit gives the same number on every machine — which is what makes
an exact floor meaningful instead of flaky.

*Both passes are gated.* The single-model pass is the Phase 1-3 regression
guard; the routed pass is what production actually runs. Guarding only one
leaves the other free to rot.

*Not every field gets a floor.* The euro `vendor` field is 11% by construction —
a real text-extraction artifact the backlog documents. Flooring it would mean
either a permanently red gate or a threshold set so low it asserts nothing.
Floors go on the fields whose failure would mean paying a wrong invoice.
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from docfactory_core.config import get_settings

from docfactory_evals.run import DocResult, evaluate_type, ground_truth_path

THRESHOLDS_PATH = Path("config/eval_thresholds.json")


@dataclass(frozen=True)
class Check:
    document_type: str
    name: str
    measured: float
    floor: float

    @property
    def passed(self) -> bool:
        return self.measured >= self.floor

    def render(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return (
            f"  [{mark}] {self.document_type:<15} {self.name:<24} "
            f"{self.measured:6.2f}%  (floor {self.floor:.2f}%)"
        )


def accuracy(results: list[DocResult], field: str | None = None) -> float:
    digital = [r for r in results if not r.needs_ocr]
    if field is None:
        cells = [ok for r in digital for ok in r.fields.values()]
    else:
        cells = [r.fields[field] for r in digital if field in r.fields]
    return 100.0 * sum(cells) / len(cells) if cells else 0.0


def check_type(slug: str, results: dict[str, list[DocResult]], thresholds: dict) -> list[Check]:
    spec = thresholds.get(slug)
    if spec is None:
        # An ungated document type is a hole in the guard, not a pass.
        return [Check(slug, "threshold configured", 0.0, 1.0)]

    checks = [
        Check(slug, "all fields (single model)", accuracy(results["frontier"]), spec["min_overall"])
    ]
    if "routed" in results:
        checks.append(
            Check(
                slug, "all fields (routed)", accuracy(results["routed"]), spec["min_overall_routed"]
            )
        )
    for field, floor in spec.get("min_fields", {}).items():
        checks.append(Check(slug, f"field: {field}", accuracy(results["frontier"], field), floor))
    return checks


def main() -> None:
    parser = argparse.ArgumentParser(description="Fail the build on an eval regression")
    parser.add_argument("--pdf-dir", type=Path, default=Path("data/synth/out"))
    parser.add_argument("--thresholds", type=Path, default=THRESHOLDS_PATH)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="grade only the first N golden docs per type — for a fast local check, NOT for CI",
    )
    args = parser.parse_args()

    config = json.loads(args.thresholds.read_text())
    thresholds = config["thresholds"]
    settings = get_settings()

    if settings.model_provider != "mock":
        raise SystemExit(
            f"the eval gate runs in mock mode; MODEL_PROVIDER is {settings.model_provider!r}. "
            "A gate that costs money per pull request is a gate that gets turned off."
        )

    print("Eval gate — golden-set floors per document type\n")
    checks: list[Check] = []
    for slug in thresholds:
        if not ground_truth_path(args.pdf_dir, slug).is_file():
            raise SystemExit(
                f"no corpus for {slug!r} in {args.pdf_dir}. Run `make seed` first — CI must "
                "generate the same corpus the thresholds were measured against."
            )
        _, results, _, _ = evaluate_type(
            slug,
            args.pdf_dir,
            golden_size=100,
            limit=args.limit,
            workers=args.workers,
            provider=settings.model_provider,
            min_chars=settings.min_parse_chars,
        )
        checks.extend(check_type(slug, results, thresholds))

    print()
    for check in checks:
        print(check.render())

    failures = [check for check in checks if not check.passed]
    print()
    if failures:
        print(f"EVAL GATE FAILED — {len(failures)} of {len(checks)} checks below floor.")
        for check in failures:
            print(
                f"  {check.document_type} {check.name}: {check.measured:.2f}% < {check.floor:.2f}%"
            )
        print("\nThis pull request degrades extraction quality. Fix it, or change the")
        print("floor deliberately in config/eval_thresholds.json and say why in the PR.")
        sys.exit(1)

    print(f"EVAL GATE PASSED — {len(checks)} checks, all at or above floor.")


if __name__ == "__main__":
    main()
