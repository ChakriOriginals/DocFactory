"""A drop the system refuses still has to leave a trace.

Two silent-loss paths, found by reading the deploy runbook's own verification
step and discovering it asserted behaviour the code did not have.

  Runbook invariant 6 said: drop a corrupt PDF, expect a message in the parse
  DLQ and a document with status `failed`.
  What actually happened: ingest_object logged a warning and returned. Nothing
  raised, so the SQS message was deleted rather than redelivered — no row, no
  DLQ entry, no alarm. Measured: document count 3 before, 3 after.

  And separately, S3 suffix filters are case-sensitive. `INVOICE.PDF` generated
  no event at all, so the object sat in the bucket while the client believed it
  had been sent.

The client-visible shape of both is the same and it is the worst kind: they
sent 500, they can account for 497, and there is nothing to point at for the
missing three.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORAGE_TF = ROOT / "infra" / "terraform" / "data-plane" / "storage.tf"
RUNBOOK = ROOT / "docs" / "deploy_runbook.md"


class _FakeStore:
    """Returns whatever bytes the test planted, for one key."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def get_object(self, key: str) -> bytes:
        return self._body


class _RecordingBroker:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def send(self, queue: str, payload: dict) -> None:
        self.sent.append((queue, payload))


@pytest.mark.integration
class TestNonPdfDropIsRecorded:
    """Behaviour, not source text.

    An earlier version of these tests grepped ingest.py for "raise" and
    "DocumentStatus.FAILED". That made them pass or fail on prose — the comment
    explaining why the path deliberately does NOT raise contains the word
    "raised" — which is the same mistake as asserting a config value by
    matching a sentence that quotes it. Call the function instead.
    """

    @staticmethod
    def _ingest(body: bytes, key: str):
        from docfactory_core.ingest import ObjectRef, ingest_object

        broker = _RecordingBroker()
        result = ingest_object(
            ObjectRef(bucket="test-bucket", key=key, size=len(body)),
            _FakeStore(body),
            broker,
        )
        return result, broker

    def _cleanup(self, document_id) -> None:
        from docfactory_core.db import session_scope, tenant_context
        from docfactory_core.models import Document
        from sqlalchemy import delete

        if document_id is None:
            return
        with tenant_context("dev-tenant"), session_scope() as session:
            session.execute(delete(Document).where(Document.id == document_id))

    def test_it_creates_a_failed_document_rather_than_vanishing(self) -> None:
        import uuid as _uuid

        from docfactory_core.db import session_scope, tenant_context
        from docfactory_core.models import Document, DocumentStatus

        key = f"dev-tenant/dropbox/invoice/{_uuid.uuid4()}.pdf"
        result, _ = self._ingest(b"this is definitely not a pdf", key)
        try:
            assert result.document_id is not None, (
                "A non-PDF drop produced no document. Nothing raises, so the SQS "
                "message is deleted rather than redelivered: no row, no DLQ "
                "entry, no alarm. The object vanishes and the client has nothing "
                "to reconcile against."
            )
            assert result.skipped == "not-a-pdf"
            with tenant_context("dev-tenant"), session_scope() as session:
                document = session.get(Document, result.document_id)
                assert document.status == DocumentStatus.FAILED
                assert document.last_error and "PDF" in document.last_error, (
                    "The rejected document carries no usable last_error, so a "
                    "client filtering status=failed cannot tell why."
                )
        finally:
            self._cleanup(result.document_id)

    def test_it_does_not_enqueue_the_rejected_file(self) -> None:
        import uuid as _uuid

        key = f"dev-tenant/dropbox/invoice/{_uuid.uuid4()}.pdf"
        result, broker = self._ingest(b"not a pdf either", key)
        try:
            assert broker.sent == [], "A file that is not a PDF was enqueued for parsing anyway."
        finally:
            self._cleanup(result.document_id)

    def test_redelivery_costs_one_row_not_two(self) -> None:
        """S3 explicitly allows duplicate delivery."""
        import uuid as _uuid

        key = f"dev-tenant/dropbox/invoice/{_uuid.uuid4()}.pdf"
        body = b"the same corrupt bytes twice"
        first, _ = self._ingest(body, key)
        second, _ = self._ingest(body, key)
        try:
            assert second.duplicate is True
            assert first.document_id == second.document_id, (
                "A redelivered corrupt drop created a second failed row, so the "
                "client's failure count climbs on its own."
            )
        finally:
            self._cleanup(first.document_id)


def test_uppercase_pdf_extensions_still_generate_an_event() -> None:
    """S3 suffix filters are case-sensitive, and scanners emit uppercase."""
    tf = STORAGE_TF.read_text()
    suffixes = set(re.findall(r'filter_suffix\s*=\s*"([^"]+)"', tf))
    assert ".pdf" in suffixes, "the lowercase notification rule is gone"
    assert ".PDF" in suffixes, (
        "Only lowercase .pdf generates an ingest event. A client exporting "
        "INVOICE.PDF gets no message, no row and no log line — the object sits "
        "in the bucket while they believe it was sent."
    )


def test_the_suffix_filter_is_not_dropped_entirely() -> None:
    """Removing it would make the pipeline re-ingest its own output.

    Parsed text is written to {tenant}/parsed/{id}.txt; with no suffix filter
    every one of those writes raises an event the ingest consumer then has to
    receive and reject.
    """
    tf = STORAGE_TF.read_text()
    assert "filter_suffix" in tf, (
        "The notification has no suffix filter, so every parsed-text write "
        "raises an ingest event the pipeline spends a receive rejecting."
    )


@pytest.mark.parametrize("claim", ["parse DLQ", "docfactory-dev-parse-dlq"])
def test_the_runbook_no_longer_promises_a_dlq_message_for_a_corrupt_drop(claim) -> None:
    """The doc asserted behaviour the code did not have, on a safety path."""
    runbook = RUNBOOK.read_text()
    row = next((line for line in runbook.splitlines() if line.startswith("| 6 |")), None)
    assert row is not None, "runbook invariant 6 was renumbered; re-check what it claims"
    assert claim not in row, (
        f"Invariant 6 still tells the operator to expect {claim!r} for a corrupt "
        "drop. It does not happen: the file is recorded as failed and the "
        "message is deleted, deliberately."
    )
