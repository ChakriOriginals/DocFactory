"""Batch ingestion from storage events, and its convergence with the API path.

A document dropped into a tenant's prefix must become the same document an
upload would have created — same row, same pipeline, same dedupe — or the
system has two ingestion stories and only one of them is tested.
"""

import hashlib
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from docfactory_core.config import get_settings
from docfactory_core.db import session_scope, tenant_context
from docfactory_core.ingest import ObjectRef, ingest_object, parse_s3_events, route_key
from docfactory_core.models import Document
from sqlalchemy import delete, select

FIXTURES = Path(__file__).parent / "fixtures"


def s3_event(bucket: str, key: str, size: int = 1024, *, event: str = "ObjectCreated:Put") -> dict:
    """An object-created notification.

    `event` defaults to the form REAL S3 delivers to SQS. MinIO prefixes the
    same event with "s3:", so both spellings reach this parser depending on
    which storage backend is in front of it — see MINIO_PREFIXED below.
    """
    return {
        "Records": [
            {
                "eventVersion": "2.1",
                "eventSource": "aws:s3",
                "eventName": event,
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key, "size": size},
                },
            }
        ]
    }


# The two producers, spelled exactly as each emits. Verified against LocalStack
# (AWS shape, Phase 4c.5b) and against the compose MinIO (prefixed shape).
AWS_SHAPE = "ObjectCreated:Put"
MINIO_PREFIXED = "s3:ObjectCreated:Put"


class TestEventParsing:
    """Pure parsing — no services needed."""

    @pytest.mark.parametrize("event_name", [AWS_SHAPE, MINIO_PREFIXED])
    def test_object_created_events_yield_a_reference(self, event_name):
        """Both producers' spellings must parse.

        Only MinIO's was covered before 4c.5b, which is why the AWS form went
        unnoticed until the data plane was applied to LocalStack: an unmatched
        event is not an error, it is silence, and the batch path would simply
        have done nothing in the cloud.
        """
        refs = parse_s3_events(s3_event("docfactory", "dev-tenant/dropbox/x.pdf", event=event_name))
        assert [(r.bucket, r.key) for r in refs] == [("docfactory", "dev-tenant/dropbox/x.pdf")]

    @pytest.mark.parametrize(
        "event_name", ["ObjectRemoved:Delete", "s3:ObjectRemoved:Delete", "ObjectRestore:Post"]
    )
    def test_other_event_types_are_ignored(self, event_name):
        event = s3_event("docfactory", "dev-tenant/dropbox/x.pdf", event=event_name)
        assert parse_s3_events(event) == []

    def test_percent_encoded_keys_are_decoded(self):
        refs = parse_s3_events(s3_event("docfactory", "dev-tenant/dropbox/an+invoice%21.pdf"))
        assert refs[0].key == "dev-tenant/dropbox/an invoice!.pdf"

    def test_the_key_carries_the_tenant_and_the_document_type(self):
        assert route_key("dev-tenant/dropbox/purchase_order/po.pdf") == (
            "dev-tenant",
            "purchase_order",
        )
        assert route_key("dev-tenant/dropbox/inv.pdf") == ("dev-tenant", None)

    def test_the_pipeline_s_own_artifacts_are_not_re_ingested(self):
        # Uploads and parsed text live in the same bucket under other prefixes;
        # ingesting them would be an infinite loop.
        assert route_key("dev-tenant/incoming/abc.pdf") == (None, None)
        assert route_key("dev-tenant/parsed/abc.txt") == (None, None)
        assert route_key("no-prefix.pdf") == (None, None)

    def test_an_unknown_document_type_segment_is_not_treated_as_one(self):
        tenant, doc_type = route_key("dev-tenant/dropbox/some-folder/x.pdf")
        assert tenant == "dev-tenant"
        assert doc_type is None  # falls back to the tenant's default


def _stack_or_skip():
    settings = get_settings()
    for url in (settings.s3_endpoint_url, settings.sqs_endpoint_url):
        parsed = urlparse(url or "")
        try:
            with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
                pass
        except OSError:
            pytest.skip("compose stack is not running")
    return settings


@pytest.mark.integration
class TestDroppedObjectsBecomeDocuments:
    @pytest.fixture
    def dropped(self):
        from docfactory_core.queues import QueueBroker
        from docfactory_core.storage import ObjectStore

        settings = _stack_or_skip()
        store, broker = ObjectStore(), QueueBroker()
        pdf = (FIXTURES / "digital_classic.pdf").read_bytes()
        # Unique per run so the dedupe assertions are about this test's bytes.
        pdf = pdf + b"\n%% " + uuid.uuid4().hex.encode()
        sha = hashlib.sha256(pdf).hexdigest()
        key = f"{settings.default_tenant_id}/{settings.ingest_prefix}/{sha[:12]}.pdf"
        store.put_object(key, pdf, content_type="application/pdf")
        yield store, broker, key, sha
        with tenant_context(settings.default_tenant_id), session_scope() as session:
            session.execute(delete(Document).where(Document.sha256 == sha))

    def test_a_dropped_object_starts_the_pipeline(self, dropped):
        store, broker, key, sha = dropped
        settings = get_settings()

        with tenant_context(settings.default_tenant_id):
            result = ingest_object(ObjectRef(settings.s3_bucket, key), store, broker)

        assert result.document_id is not None
        assert result.duplicate is False
        with tenant_context(settings.default_tenant_id), session_scope() as session:
            document = session.get(Document, result.document_id)
            assert document.sha256 == sha
            assert document.status == "received"
            assert document.s3_key == key

    def test_a_redelivered_event_does_not_create_a_second_document(self, dropped):
        """S3 explicitly allows duplicate delivery; it must cost one row."""
        store, broker, key, sha = dropped
        settings = get_settings()
        ref = ObjectRef(settings.s3_bucket, key)

        with tenant_context(settings.default_tenant_id):
            first = ingest_object(ref, store, broker)
            second = ingest_object(ref, store, broker)

        assert second.duplicate is True
        assert second.document_id == first.document_id
        with tenant_context(settings.default_tenant_id), session_scope() as session:
            rows = session.scalars(select(Document).where(Document.sha256 == sha)).all()
        assert len(rows) == 1

    def test_the_batch_and_api_paths_dedupe_against_each_other(self, dropped):
        """The same bytes by both routes are one document, not two."""
        from conftest import authenticated_client

        store, broker, key, _sha = dropped
        settings = get_settings()
        with tenant_context(settings.default_tenant_id):
            dropped_result = ingest_object(ObjectRef(settings.s3_bucket, key), store, broker)

        with authenticated_client() as client:
            response = client.post(
                "/documents",
                files={"file": ("same.pdf", store.get_object(key), "application/pdf")},
            )
        assert response.status_code == 200, response.text
        assert response.json()["duplicate"] is True
        assert response.json()["document_id"] == str(dropped_result.document_id)

    def test_a_non_pdf_drop_is_ignored_rather_than_queued(self, dropped):
        store, broker, _, _ = dropped
        settings = get_settings()
        key = f"{settings.default_tenant_id}/{settings.ingest_prefix}/notes.txt"
        store.put_object(key, b"just some text", content_type="text/plain")
        with tenant_context(settings.default_tenant_id):
            result = ingest_object(ObjectRef(settings.s3_bucket, key), store, broker)
        assert result.skipped == "not-a-pdf"
        assert result.document_id is None

    def test_the_worker_handler_consumes_a_raw_storage_event(self, dropped):
        """End to end on the message shape AWS will actually deliver."""
        from docfactory_worker.handlers import handle_ingest

        _, _, key, sha = dropped
        settings = get_settings()
        handle_ingest(s3_event(settings.s3_bucket, key))

        with tenant_context(settings.default_tenant_id), session_scope() as session:
            rows = session.scalars(select(Document).where(Document.sha256 == sha)).all()
        assert len(rows) == 1, "the raw event must produce exactly one document"
