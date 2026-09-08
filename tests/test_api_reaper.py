"""The reaper could not run in the one situation it was written for.

The worker's healing loop reasons that "a fleet that scales to zero has nobody
to run a cron, so every running worker sweeps." That is true and it leaves a
hole exactly the shape of the problem:

  worker_min_count = 0
    -> a stranded document has no message
    -> so no queue has depth
    -> so the backlog alarm never fires
    -> so no worker ever starts
    -> so the sweep that would have found it never runs

The document waits for an unrelated upload to wake a worker. On a quiet day
that is tomorrow; on a quiet week, Monday. The API is desired_count 1 and
always awake, so the sweep belongs there too.

It cannot simply run heal(): the API task role may send to parse and ingest and
deliberately NOT to extract, and cannot consume from any queue at all, so a
DLQ redrive is not available to it either. The stage filter is what keeps a
recovery mechanism from becoming a stream of AccessDenied.
"""

from pathlib import Path

import pytest
from docfactory_core.healing import ReapReport, reap_stuck_documents

ROOT = Path(__file__).resolve().parents[1]
API_MAIN = ROOT / "apps" / "api" / "docfactory_api" / "main.py"
IAM_TF = ROOT / "infra" / "terraform" / "data-plane" / "iam.tf"


def test_the_api_runs_a_reaper_at_all() -> None:
    import docfactory_api.main as api

    assert hasattr(api, "_reaper_loop"), (
        "The API no longer sweeps for stranded documents. With worker_min_count "
        "at 0 nothing else is awake to do it: a stranded document has no "
        "message, so no alarm fires and no worker starts."
    )
    assert hasattr(api, "_API_REAPABLE_STAGES")


def test_the_api_only_reaps_stages_its_role_can_enqueue() -> None:
    """Widening this set without widening IAM produces AccessDenied, not recovery."""
    import docfactory_api.main as api

    assert frozenset({"parse"}) == api._API_REAPABLE_STAGES, (
        f"The API would attempt to requeue {sorted(api._API_REAPABLE_STAGES)}. "
        "Its task role may send only to parse and ingest — extract is "
        "deliberately withheld because the API never drives extraction. Either "
        "narrow this back to parse, or change iam.tf first and understand what "
        "that widens."
    )


def test_the_iam_boundary_this_relies_on_still_holds() -> None:
    """The filter is only correct while the role is actually this narrow.

    If the API were later granted extract, the filter would be needlessly
    conservative rather than wrong — but if `parse` were REVOKED, the sweep
    would fail on every document it tried to rescue.
    """
    iam = IAM_TF.read_text()
    api_block_start = iam.find('data "aws_iam_policy_document" "api_task"')
    assert api_block_start != -1, "the API task policy document was renamed"
    block = iam[api_block_start : iam.find("data ", api_block_start + 10)]
    assert "sqs:SendMessage" in block, (
        "The API task role no longer sends to any queue, so the reaper cannot "
        "requeue anything it finds."
    )


def test_the_stage_filter_excludes_rather_than_attempts() -> None:
    """A document the caller may not requeue must be counted, not attempted.

    Silently passing over it makes a sweeper that is permitted to fix nothing
    look identical to one with nothing to fix.
    """
    assert "skipped_stage" in ReapReport.__dataclass_fields__, (
        "ReapReport lost skipped_stage, so a filtered-out document is invisible "
        "in the sweep summary."
    )
    import inspect

    assert "stages" in inspect.signature(reap_stuck_documents).parameters, (
        "reap_stuck_documents no longer accepts a stages filter, so the API "
        "would attempt extract sends its role forbids."
    )


def test_the_worker_still_runs_the_full_heal() -> None:
    """The API's sweep is additional, not a replacement.

    DLQ redrive and extract-stage recovery need permissions the API does not
    have and should not get; they stay with the worker, where an outage-shaped
    DLQ implies traffic implies a running fleet anyway.
    """
    worker_main = (ROOT / "apps" / "worker" / "docfactory_worker" / "main.py").read_text()
    assert "_healing_loop" in worker_main and "heal(" in worker_main, (
        "The worker no longer runs the full healing sweep. The API's reaper "
        "covers only parse-stage strandings and cannot drain a DLQ."
    )


@pytest.mark.parametrize(
    "status,stage",
    [("received", "parse"), ("parsing", "parse"), ("parsed", "extract"), ("extracting", "extract")],
)
def test_the_documented_stage_mapping_is_what_the_filter_assumes(status, stage) -> None:
    """The filter is a claim about _REQUEUE_STAGE; pin them together."""
    from docfactory_core.healing import _REQUEUE_STAGE
    from docfactory_core.models import DocumentStatus

    assert _REQUEUE_STAGE[DocumentStatus(status)] == stage, (
        f"{status} now requeues to a different stage. The API's filter assumes "
        "received/parsing go to parse and everything else to extract."
    )
