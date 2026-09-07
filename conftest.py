# The synth generator is a script directory (dev tooling), not an installed
# package — put it on sys.path so tests can import its modules.
import contextlib
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).parent / "data" / "synth"))


import pytest


@pytest.fixture(scope="session", autouse=True)
def _bind_dev_tenant():
    """Bind the development tenant for every test.

    Since Phase 3 every query passes through row level security, which reads
    `app.tenant_id` from the transaction. Tests that exercise the pipeline act
    as the dev tenant; the isolation suite nests its own tenant_context to act
    as someone else.

    Session-scoped so it is bound before module-scoped fixtures run — those
    open sessions of their own, and a narrower scope would leave them unbound.
    """
    from docfactory_core.config import get_settings
    from docfactory_core.db import tenant_context

    with tenant_context(get_settings().default_tenant_id):
        yield


DEV_API_KEY = "dev-local-key"


@pytest.fixture(scope="session", autouse=True)
def _seed_dev_key(_bind_dev_tenant):
    """Ensure the development API key exists before any request is made."""
    from docfactory_core.auth import AuthError, issue_api_key, resolve_tenant

    with contextlib.suppress(Exception):
        # A missing database is fine here: the integration tests skip on their
        # own, and the unit tests never touch it.
        try:
            resolve_tenant(DEV_API_KEY)
        except AuthError:
            issue_api_key("dev-tenant", "test suite", plaintext=DEV_API_KEY)


def object_store_reachable() -> bool:
    """Is the compose stack's S3/SQS up?

    Entering a TestClient runs the app lifespan, and the lifespan calls
    ensure_infra(), which creates the bucket and queues. Without MinIO and
    ElasticMQ that call does not fail fast -- it retries thirty times and then
    raises RuntimeError, and in CI (no endpoint override, no credentials) it
    burns thirty seconds first.

    So every test that enters a TestClient has to check this. The suites that
    only touch Postgres do not, which is what lets CI run the RLS assertions
    against a real database with no object store present.
    """
    from docfactory_core.config import get_settings

    settings = get_settings()
    endpoints = (settings.s3_endpoint_url, settings.sqs_endpoint_url)
    if not all(endpoints):
        # No endpoint override means boto would talk to real AWS. Never do that
        # from a test.
        return False
    for url in endpoints:
        parsed = urlparse(url)
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                pass
        except OSError:
            return False
    return True


@pytest.fixture
def requires_object_store():
    """Skip a test that needs a live TestClient when the stack is not up."""
    if not object_store_reachable():
        pytest.skip("compose stack (MinIO/ElasticMQ) is not running")


def authenticated_client(api_key: str = DEV_API_KEY):
    """A TestClient that presents an API key on every request."""
    from docfactory_api.main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    client.headers.update({"x-api-key": api_key})
    return client
