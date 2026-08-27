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
