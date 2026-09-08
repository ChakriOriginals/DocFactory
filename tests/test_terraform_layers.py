"""The two Terraform layers' contract, enforced without running Terraform.

`terraform validate` cannot check this: it type-checks one directory at a time,
and the compute layer's view of the data layer is a `terraform_remote_state`
object whose shape is only known once the other layer has actually been
applied. So a compute-plane reference to an output that does not exist is a
clean `validate` and a failed `plan` — discovered at deploy time, against a
real account, which is the worst place to discover it.

These are text assertions over the .tf files. They are cheap, they run in CI
with everything else, and they encode the two rules the split depends on:

  1. compute-plane may read only DECLARED outputs of data-plane;
  2. the dependency runs one way — nothing in data-plane knows this layer
     exists, which is what makes destroying compute-plane alone safe.
"""

import re
from pathlib import Path

import pytest

TERRAFORM = Path(__file__).resolve().parents[1] / "infra" / "terraform"
DATA_PLANE = TERRAFORM / "data-plane"
COMPUTE_PLANE = TERRAFORM / "compute-plane"

# Resource types the data layer owns. The compute layer must reach them through
# outputs, never by declaring its own resource or data source of the same kind.
DATA_PLANE_TYPES = (
    "aws_s3_bucket",
    "aws_sqs_queue",
    "aws_ssm_parameter",
    "aws_ecr_repository",
    "aws_iam_role",
    "aws_iam_policy",
)


def _strip_comments(text: str) -> str:
    """Drop `#` comment lines.

    These files carry a lot of prose, and the prose legitimately names the other
    layer. Only actual HCL counts as a reference.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _sources(directory: Path) -> dict[Path, str]:
    return {path: _strip_comments(path.read_text()) for path in sorted(directory.glob("*.tf"))}


def _declared_outputs(directory: Path) -> set[str]:
    pattern = re.compile(r'^output\s+"([^"]+)"', re.MULTILINE)
    return {name for text in _sources(directory).values() for name in pattern.findall(text)}


def test_both_layers_exist_as_separate_roots():
    for directory in (DATA_PLANE, COMPUTE_PLANE):
        assert (directory / "versions.tf").exists(), f"{directory.name} is not a root module"


def test_compute_plane_reads_only_declared_data_plane_outputs():
    declared = _declared_outputs(DATA_PLANE)
    referenced: dict[str, str] = {}
    for path, text in _sources(COMPUTE_PLANE).items():
        for name in re.findall(r"local\.data_plane\.([a-z_]+)", text):
            referenced.setdefault(name, path.name)

    assert referenced, "compute-plane reads nothing from the data layer — check the wiring"
    missing = {name: where for name, where in referenced.items() if name not in declared}
    assert not missing, (
        "compute-plane reads outputs the data layer does not declare: "
        f"{missing}. This passes `terraform validate` and fails `terraform plan` "
        "against a real account."
    )


@pytest.mark.parametrize("resource_type", DATA_PLANE_TYPES)
def test_compute_plane_never_reaches_around_the_outputs(resource_type: str):
    """No `resource`/`data` block in compute-plane for a type the data layer owns.

    Looking a bucket or a queue up by name from the compute layer would work,
    and would quietly delete the dependency this whole split is built on.
    """
    pattern = re.compile(rf'^\s*(resource|data)\s+"{resource_type}"', re.MULTILINE)
    offenders = [
        path.name for path, text in _sources(COMPUTE_PLANE).items() if pattern.search(text)
    ]
    assert not offenders, (
        f"compute-plane declares its own {resource_type} in {offenders}; "
        "cross-layer references must go through data-plane outputs."
    )


def test_the_dependency_runs_one_way():
    """data-plane must not know the compute layer exists.

    If it did, `terraform destroy` in compute-plane — the overnight cost park —
    would leave the data layer's next plan broken.
    """
    for path, text in _sources(DATA_PLANE).items():
        assert "terraform_remote_state" not in text, f"{path.name} reads another layer's state"
        assert "compute-plane" not in text, f"{path.name} references the compute layer in HCL"


def test_localstack_mode_cannot_use_real_credentials():
    """The emulator switch must move credentials, not just endpoints.

    Overriding the endpoints alone would leave a real profile in play, so a
    misconfigured endpoint would authenticate against a real account. Both
    layers pin the credentials in the same conditional as the endpoints.
    """
    for directory in (DATA_PLANE, COMPUTE_PLANE):
        versions = _strip_comments((directory / "versions.tf").read_text())
        assert 'access_key = local.localstack ? "test" : null' in versions, directory.name
        assert 'secret_key = local.localstack ? "test" : null' in versions, directory.name
        assert 'dynamic "endpoints"' in versions, directory.name


def test_the_localstack_var_file_is_not_auto_loaded():
    """It must be passed with -var-file, never picked up implicitly.

    A file named *.auto.tfvars in a root module is loaded by every command in
    that directory — including a real `terraform apply`.
    """
    localstack_dir = TERRAFORM.parent / "localstack"
    assert (localstack_dir / "data-plane.tfvars").exists()
    assert not list(DATA_PLANE.glob("*.auto.tfvars"))
    assert not list(COMPUTE_PLANE.glob("*.auto.tfvars"))


# AWS's own character class for security-group and rule descriptions. Several
# other resources use the same or a narrower set; an em-dash is outside all of
# them.
_AWS_SAFE = re.compile(r"^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*\-]*$")

# Arguments whose value AWS validates the characters of.
_VALIDATED_ARGUMENTS = ("description", "name", "alarm_name", "alarm_description", "sid")

# Only these block types send their arguments to AWS. `output` and `variable`
# descriptions are local metadata that never leaves the state file, which is
# why they are free to contain apostrophes, backticks and prose — an earlier
# version of this test flagged four of them and was wrong to.
_AWS_FACING_BLOCKS = ("resource", "data")


def _aws_facing_blocks(text: str) -> list[str]:
    """The text of every top-level resource/data block in a .tf file."""
    blocks, current, keep = [], [], False
    for line in text.splitlines():
        if re.match(r"^[a-z_]+\s", line) or re.match(r"^[a-z_]+$", line):
            if keep and current:
                blocks.append("\n".join(current))
            keep = line.split(None, 1)[0] in _AWS_FACING_BLOCKS
            current = [line] if keep else []
        elif keep:
            current.append(line)
    if keep and current:
        blocks.append("\n".join(current))
    return blocks


def test_no_aws_facing_string_contains_a_character_aws_rejects():
    """An em-dash in a security-group description fails the apply, not the plan.

    Found the hard way: `network.tf` carried "No inbound at all — they poll"
    from Phase 4c, and every check up to this point missed it.
    `terraform validate` does not inspect string contents, and LocalStack
    Community has no EC2, so the one tool that would have caught it could not
    run. It surfaced only when a plan reached the provider's client-side
    validation of an *ingress rule* description — the security group's own
    description would have waited for the real apply.

    Scoped to `resource` and `data` blocks. Output and variable descriptions
    never reach AWS, and the prose in them is worth more than ASCII purity.
    """
    offenders: list[str] = []
    pattern = re.compile(
        rf"^\s*({'|'.join(_VALIDATED_ARGUMENTS)})\s*=\s*\"([^\"]*)\"\s*$", re.MULTILINE
    )
    for directory in (DATA_PLANE, COMPUTE_PLANE):
        for path in sorted(directory.glob("*.tf")):
            for block in _aws_facing_blocks(path.read_text()):
                for argument, value in pattern.findall(block):
                    # Interpolations resolve at apply time; only the literal
                    # parts are ours to police.
                    literal = re.sub(r"\$\{[^}]*\}", "", value)
                    if not _AWS_SAFE.match(literal):
                        bad = sorted({c for c in literal if not _AWS_SAFE.match(c)})
                        offenders.append(f"{path.name}: {argument} = {value!r} contains {bad}")

    assert not offenders, (
        "AWS rejects these characters in validated string arguments:\n  "
        + "\n  ".join(offenders)
        + "\nUse ASCII in the value; keep the prose in the comment above it."
    )


# --- ECR retention: two changes that are only safe together -------------------
#
# The lifecycle policy expires images by recency. That is safe if and only if
# `latest` always rides on the newest image, because Terraform's task
# definitions reference `:latest` — if the tag stopped moving it would sink down
# the list and eventually be expired out from under them, and the next
# `terraform apply` would register a task definition pointing at an image that
# no longer exists.
#
# Before the fix, `latest` was a hand-pushed image CI never touched: it was
# already the third-newest tag in a five-image repository and sinking with every
# deploy. Adding the count rule alone would have armed a delayed failure.
#
# These two tests exist to keep the pair together. Removing the `latest` push
# from CI must fail loudly rather than quietly making the lifecycle rule unsafe.

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"


def test_the_lifecycle_policy_bounds_image_count() -> None:
    """Unbounded tagged images cost storage forever; CI tags every push."""
    ecr = (DATA_PLANE / "ecr.tf").read_text()
    assert "imageCountMoreThan" in ecr, (
        "The ECR lifecycle policy no longer bounds how many images are kept. "
        "CI tags every image with a commit SHA, so nothing it pushes is ever "
        "untagged and an untagged-only rule never removes anything."
    )


def test_ci_keeps_latest_on_the_newest_image() -> None:
    """The count rule above is only safe while this holds."""
    workflow = WORKFLOW.read_text()
    ecr = (DATA_PLANE / "ecr.tf").read_text()

    if "imageCountMoreThan" not in ecr:
        pytest.skip("no count-based expiry, so `latest` sinking is harmless")

    assert ':latest"' in workflow or ":latest'" in workflow or ":latest\n" in workflow, (
        "The deploy workflow no longer pushes `:latest`, but the ECR lifecycle "
        "policy still expires images by recency. `latest` will sink below the "
        "retention count and be deleted, and Terraform's task definitions "
        "reference it. Either restore the `latest` push or drop the count rule."
    )
    assert "docker push" in workflow and "latest" in workflow


def test_retention_keeps_enough_images_to_roll_back() -> None:
    """The ECS circuit breaker rolls back to the previous task definition.

    That image has to still exist, so a retention count of 1 would leave a
    rollback with nothing to pull.
    """
    variables = (DATA_PLANE / "variables.tf").read_text()
    match = re.search(
        r'variable\s+"ecr_image_retention_count".*?default\s*=\s*(\d+)', variables, re.DOTALL
    )
    assert match, "ecr_image_retention_count is no longer declared with a default"
    assert int(match.group(1)) >= 2, (
        "Retention below 2 breaks rollback: the circuit breaker pulls the "
        "previous task definition's image, which would already be expired."
    )


# --- The orphan check has two scopes, and the default is dangerous after a park

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
RUNBOOK = Path(__file__).resolve().parents[1] / "docs" / "deploy_runbook.md"


def test_the_orphan_check_distinguishes_a_park_from_a_teardown() -> None:
    """Without a scope flag it calls the retained data layer an orphan.

    The park -- destroy compute, keep the bucket, queues, images and SSM
    parameters -- is the normal overnight operation. Run against that, the
    unscoped check listed the whole data layer under ORPHAN and printed
    "Delete them, or re-run terraform destroy". Following that deletes the
    parameters holding the production database credentials.
    """
    script = (SCRIPTS / "aws_orphan_check.sh").read_text()
    assert "--park" in script, (
        "aws_orphan_check.sh no longer understands --park. Without it the check "
        "cannot tell a park from a full teardown, and reports the retained data "
        "layer as orphans to be deleted."
    )
    assert 'SCOPE="full"' in script, "the default scope should stay the full teardown"


def test_the_runbook_passes_park_when_it_parks() -> None:
    """A correct script that the runbook invokes wrongly is still the bug."""
    runbook = RUNBOOK.read_text()
    park_section = runbook[runbook.index('cd "$TF_COMPUTE" && terraform destroy') :][:2000]
    assert "--park" in park_section, (
        "The runbook's park procedure calls the orphan check without --park, so "
        "following it reports the data layer as orphans and tells the operator "
        "to delete it."
    )


# --- A push while the stack is parked must not build anything ----------------


def _deploy_steps() -> list[dict]:
    yaml = pytest.importorskip("yaml")
    workflow = yaml.safe_load(WORKFLOW.read_text())
    return workflow["jobs"]["build-and-deploy"]["steps"]


def test_the_parked_check_runs_before_anything_is_built() -> None:
    """Ordering is the whole point, not just the presence of a check.

    Parking is `terraform destroy` in compute-plane/, and a push while parked is
    normal. Before the gate, such a run built both images, pushed them,
    registered a migrate task definition, and only then failed on
    ClusterNotFoundException. The images are the real damage: pushed against a
    retention of 5, a couple of parked runs evict the images a rollback needs.
    """
    steps = _deploy_steps()
    ids = [s.get("id") or s.get("name", "") for s in steps]

    gate = next((i for i, s in enumerate(steps) if s.get("id") == "cluster"), None)
    assert gate is not None, (
        "The deploy job no longer checks whether the compute plane is up. A push "
        "while parked will build and push images for a cluster that does not "
        "exist, then fail on ClusterNotFoundException."
    )

    build = next((i for i, n in enumerate(ids) if "Build and push" in str(n)), None)
    assert build is not None, "the build step was renamed; update this test"
    assert gate < build, (
        f"The parked-state check (index {gate}) must run BEFORE the build "
        f"(index {build}). Building first spends CI time and ECR retention "
        "budget on a stack that does not exist."
    )


def test_every_cluster_touching_step_is_gated_on_the_check() -> None:
    steps = _deploy_steps()
    gate = "steps.cluster.outputs.up"
    ungated = [
        s.get("name") or s.get("id") or s.get("uses")
        for s in steps
        if any(
            k in str(s.get("name", ""))
            for k in ("Build and push", "Migrate", "Roll the", "stabilise")
        )
        and gate not in str(s.get("if", ""))
    ]
    assert not ungated, (
        f"These steps touch the cluster but are not gated on the parked check: "
        f"{ungated}. They will run against a destroyed cluster."
    )
