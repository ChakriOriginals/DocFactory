"""Invariants of the two runtime images, checked without building them.

Building both images takes minutes on a laptop and emulates a foreign
architecture, so it does not belong in the unit suite. But the properties that
matter are all textual, and each one here is a bug this project actually hit
or came close to hitting:

  1. The images run as a non-root user. This is a claim the project makes out
     loud, and a `USER` line is one careless edit away from disappearing.
  2. The base image tag is pinned to a Debian release, not `latest` or a bare
     `slim`. The CVE review in docs/container_cves.md reasons about specific
     package versions; a floating base makes that document fiction.
  3. Any apt package installed on top of the base is pinned to an exact
     version. A bare `apt-get upgrade` makes the image a function of its build
     date: two builds of one commit differ, and a rollback can quietly ship
     different libraries than the tag it rolled back to.
  4. The calibration harness stays out of the runtime images. It is the only
     code in the repo that calls `subprocess`, and the perl findings in the
     CVE review are argued unreachable partly on that basis.
"""

import re
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parents[1] / "infra" / "docker"
DOCKERFILES = sorted(DOCKER_DIR.glob("Dockerfile.*"))


def _lines(path: Path) -> list[str]:
    """Instruction lines, comments dropped and continuations joined."""
    joined = path.read_text().replace("\\\n", " ")
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_there_are_dockerfiles_to_check() -> None:
    # Without this the whole module passes vacuously if the directory moves.
    assert len(DOCKERFILES) == 2, [p.name for p in DOCKERFILES]


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_image_drops_to_a_non_root_user(dockerfile: Path) -> None:
    users = [ln.split(None, 1)[1].strip() for ln in _lines(dockerfile) if ln.startswith("USER ")]
    assert users, f"{dockerfile.name} never switches away from root"
    assert users[-1] not in ("root", "0"), f"{dockerfile.name} ends as {users[-1]}"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_base_image_is_pinned_to_a_debian_release(dockerfile: Path) -> None:
    froms = [ln for ln in _lines(dockerfile) if ln.startswith("FROM ")]
    assert froms, dockerfile.name
    for line in froms:
        image = line.split()[1]
        if image.startswith("$") or "@sha256:" in image:
            # A build-arg base, or one already pinned by digest.
            continue
        assert ":" in image, f"{dockerfile.name}: unpinned base {image!r}"
        tag = image.rsplit(":", 1)[1]
        assert tag != "latest", f"{dockerfile.name}: base pinned to :latest"
        if image.startswith("python:"):
            assert "bookworm" in tag or "@sha256" in image, (
                f"{dockerfile.name}: base tag {tag!r} does not name a Debian release, "
                "so docs/container_cves.md cannot reason about its packages"
            )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_apt_installs_are_version_pinned_and_never_blanket_upgrades(dockerfile: Path) -> None:
    for line in _lines(dockerfile):
        if not line.startswith("RUN ") or "apt-get" not in line:
            continue

        assert not re.search(r"apt-get\s+(-\S+\s+)*(upgrade|dist-upgrade)", line), (
            f"{dockerfile.name}: blanket apt-get upgrade makes the image a "
            "function of its build date; pin the package instead"
        )

        for install in re.finditer(r"apt-get\s+(?:-\S+\s+)*install\s+(.*?)(?:&&|$)", line):
            packages = [token for token in install.group(1).split() if not token.startswith("-")]
            assert packages, f"{dockerfile.name}: apt-get install with no packages"
            for package in packages:
                assert "=" in package, (
                    f"{dockerfile.name}: {package!r} is installed without a version pin"
                )


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_the_calibration_harness_is_not_copied_into_the_image(dockerfile: Path) -> None:
    for line in _lines(dockerfile):
        if not line.startswith("COPY ") or "--from=" in line:
            continue
        sources = line.split()[1:-1]
        for source in sources:
            assert source not in ("packages/", "packages"), (
                f"{dockerfile.name}: `COPY {source}` pulls in packages/evals, the only "
                "code in the repo that shells out; copy packages/core/ instead"
            )
            assert not (source.startswith("packages/evals") and not source.endswith(".toml")), (
                f"{dockerfile.name}: copies evals source ({source})"
            )
