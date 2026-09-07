"""What the API exposes without an API key, and what guards it.

The tenant-binding middleware short-circuits for a small set of paths. Five of
them are read-only documentation and health endpoints. The sixth,
`/internal/storage-events`, accepts a POST and republishes its body onto the
ingest queue -- its caller is the object store, which has no tenant of its own,
so it authenticates with a shared secret instead of an API key.

That secret used to default to the literal "local-ingest-token", committed in
three files. Combined with `api_ingress_cidrs = ["0.0.0.0/0"]` and
`assign_public_ip = true`, applying the compute plane would have put a queue
writer on the public internet behind a password published in a public
repository. Nothing in the deployment ever set the variable, so the default was
what would have shipped. The endpoint had no test at all.

These tests pin the two things that made it possible:

  1. The secret fails closed when nobody configures it.
  2. The exempt-path set cannot grow by accident. Adding a route here is a real
     security decision, so it has to be a deliberate edit to this file.
"""

import asyncio
import re
from pathlib import Path
from typing import ClassVar

import pytest
from docfactory_api.main import _UNAUTHENTICATED_PATHS, app, storage_events
from docfactory_core.config import Settings
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]

# Deliberate, reviewed, and exhaustive. Growing this set means deciding that a
# new route is safe to reach with no API key.
EXPECTED_UNAUTHENTICATED = {
    "/healthz",
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
    "/internal/storage-events",
}


def test_the_unauthenticated_path_set_is_exactly_what_was_reviewed() -> None:
    assert _UNAUTHENTICATED_PATHS == EXPECTED_UNAUTHENTICATED, (
        "The set of paths reachable without an API key changed. This is a "
        "security decision, not a refactor: confirm the new route is safe to "
        "expose, then update EXPECTED_UNAUTHENTICATED."
    )


def test_only_one_unauthenticated_route_accepts_writes() -> None:
    """Read-only exemptions are cheap. A writable one needs its own auth."""
    writable = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) in _UNAUTHENTICATED_PATHS
        and getattr(route, "methods", set()) - {"GET", "HEAD", "OPTIONS"}
    }
    assert writable == {"/internal/storage-events"}, (
        f"Unauthenticated routes accepting writes: {sorted(writable)}. Each one "
        "needs its own authentication, like storage_events does."
    )


def test_the_ingest_secret_has_no_usable_default() -> None:
    """Empty is what makes the endpoint fail closed where nobody sets it."""
    assert Settings.model_fields["ingest_webhook_token"].default == "", (
        "ingest_webhook_token has a non-empty default again. A committed "
        "default for a shared secret is a password in a public repository, and "
        "the deployment never sets this variable."
    )


@pytest.mark.parametrize(
    "authorization",
    [
        pytest.param("", id="no-header"),
        pytest.param("Bearer ", id="bearer-empty"),
        pytest.param("Bearer local-ingest-token", id="the-old-committed-default"),
        pytest.param("Bearer ", id="bearer-space"),
    ],
)
def test_storage_events_rejects_everything_when_no_secret_is_configured(
    monkeypatch: pytest.MonkeyPatch, authorization: str
) -> None:
    """With no secret configured the route must refuse every caller.

    Including a caller presenting the old committed default -- which is what an
    attacker who read the repository would send.
    """
    import docfactory_api.main as api_main

    monkeypatch.setattr(api_main, "get_settings", lambda: Settings(ingest_webhook_token=""))

    class _Request:
        headers: ClassVar[dict[str, str]] = {"authorization": authorization}

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(storage_events(_Request()))
    assert excinfo.value.status_code == 401


def test_storage_events_still_accepts_the_configured_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The negative tests above would pass if the route were simply broken."""
    import docfactory_api.main as api_main

    monkeypatch.setattr(
        api_main, "get_settings", lambda: Settings(ingest_webhook_token="a-real-secret")
    )

    class _Request:
        headers: ClassVar[dict[str, str]] = {"authorization": "Bearer a-real-secret"}

        async def json(self):
            return {"Records": []}

    # No Records -> returns before touching the broker, so this needs no infra.
    assert asyncio.run(storage_events(_Request())) == {"accepted": 0}


def test_the_deployment_does_not_configure_the_ingest_bridge() -> None:
    """The endpoint is meant to be dead on AWS; S3 publishes to SQS directly.

    If a future change starts setting INGEST_WEBHOOK_TOKEN in the task
    definition, the endpoint goes live in production and this file's reasoning
    needs revisiting.
    """
    compute = (ROOT / "infra" / "terraform" / "compute-plane").glob("*.tf")
    setters = [
        f"{path.name}:{i}"
        for path in compute
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"INGEST_WEBHOOK_TOKEN", line) and not line.lstrip().startswith("#")
    ]
    assert not setters, (
        f"The compute plane now sets INGEST_WEBHOOK_TOKEN ({setters}), which makes "
        "/internal/storage-events live on AWS. Re-review the exposure first."
    )
