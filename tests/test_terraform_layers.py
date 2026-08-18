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
    "aws_secretsmanager_secret",
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
