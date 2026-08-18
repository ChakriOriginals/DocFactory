"""Upload API integration tests (need the compose stack; auto-skip otherwise).

The TestClient runs the app lifespan, so ensure_infra() executes exactly as it
would on real startup.
"""

import socket
import uuid
from urllib.parse import urlparse

import pytest
from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import Document, DocumentStatus
from sqlalchemy import delete, select

pytestmark = pytest.mark.integration


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def client():
    settings = get_settings()
    endpoints = (settings.s3_endpoint_url, settings.sqs_endpoint_url)
    if not all(endpoints) or not all(_reachable(url) for url in endpoints):
        pytest.skip("compose stack is not running")
    try:
        with session_scope() as session:
            session.execute(select(Document).limit(1))
    except Exception:
        pytest.skip("postgres is not migrated/reachable")

    from conftest import authenticated_client

    with authenticated_client() as test_client:
        yield test_client


@pytest.fixture
def unique_pdf_bytes():
    # valid magic bytes + unique content -> unique sha per test run; the parse
    # stage never runs here (no worker), so full PDF validity isn't needed
    payload = b"%PDF-1.7\n% docfactory api test " + uuid.uuid4().hex.encode() + b"\n%%EOF"
    created: list[str] = []
    yield payload, created
    with session_scope() as session:
        for sha in created:
            session.execute(delete(Document).where(Document.sha256 == sha))


def test_upload_is_idempotent(client, unique_pdf_bytes):
    payload, created = unique_pdf_bytes

    first = client.post("/documents", files={"file": ("invoice.pdf", payload, "application/pdf")})
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["duplicate"] is False

    second = client.post("/documents", files={"file": ("invoice.pdf", payload, "application/pdf")})
    assert second.status_code == 200
    assert second.json()["document_id"] == body["document_id"]
    assert second.json()["duplicate"] is True

    with session_scope() as session:
        rows = session.scalars(
            select(Document).where(Document.id == uuid.UUID(body["document_id"]))
        ).all()
    assert len(rows) == 1
    created.append(rows[0].sha256)

    status = client.get(f"/documents/{body['document_id']}")
    assert status.status_code == 200
    # Not pinned to "received": if a worker happens to be running it will have
    # advanced the document already. The claim under test is idempotency (one
    # row, same id), not a snapshot of pipeline progress.
    assert status.json()["status"] in {s.value for s in DocumentStatus}
    assert status.json()["sha256"] == rows[0].sha256


def test_upload_rejects_non_pdf(client):
    response = client.post(
        "/documents", files={"file": ("notes.txt", b"hello world", "text/plain")}
    )
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_upload_rejects_empty_file(client):
    response = client.post("/documents", files={"file": ("empty.pdf", b"", "application/pdf")})
    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


def test_get_unknown_document_404s(client):
    response = client.get(f"/documents/{uuid.uuid4()}")
    assert response.status_code == 404


class TestDocumentTypeSelectsThePipeline:
    """`doc_type` names the pipeline definition a document is processed under.

    It is caller-supplied, so it is checked here at the boundary rather than in
    a worker that would otherwise be holding a document it has no definition
    for. 3b made a definition untrusted input on write; this is the same
    standard applied to naming one.
    """

    def test_an_unknown_type_is_rejected_with_the_available_ones(self, client, unique_pdf_bytes):
        payload, _ = unique_pdf_bytes
        response = client.post(
            "/documents",
            params={"doc_type": "bill_of_lading"},
            files={"file": ("thing.pdf", payload, "application/pdf")},
        )
        assert response.status_code == 400
        assert "unknown doc_type" in response.json()["detail"]
        assert "purchase_order" in response.json()["detail"]

    def test_a_path_traversing_type_is_rejected(self, client, unique_pdf_bytes):
        payload, _ = unique_pdf_bytes
        response = client.post(
            "/documents",
            params={"doc_type": "../../etc/passwd"},
            files={"file": ("thing.pdf", payload, "application/pdf")},
        )
        assert response.status_code == 400

    def test_a_known_type_is_recorded_on_the_document(self, client, unique_pdf_bytes):
        payload, created = unique_pdf_bytes
        response = client.post(
            "/documents",
            params={"doc_type": "purchase_order"},
            files={"file": ("order.pdf", payload, "application/pdf")},
        )
        assert response.status_code == 202, response.text
        document_id = uuid.UUID(response.json()["document_id"])
        with session_scope() as session:
            document = session.get(Document, document_id)
            created.append(document.sha256)
            assert document.doc_type == "purchase_order"
        assert client.get(f"/documents/{document_id}").json()["doc_type"] == "purchase_order"

    def test_the_default_is_still_the_invoice_pipeline(self, client, unique_pdf_bytes):
        payload, created = unique_pdf_bytes
        response = client.post("/documents", files={"file": ("x.pdf", payload, "application/pdf")})
        document_id = uuid.UUID(response.json()["document_id"])
        with session_scope() as session:
            document = session.get(Document, document_id)
            created.append(document.sha256)
            assert document.doc_type == "invoice"
