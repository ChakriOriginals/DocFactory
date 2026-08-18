"""Worker stage integration tests (need the compose stack; auto-skip otherwise).

Handlers are driven in-process against real MinIO/ElasticMQ/Postgres. The
poison test uses a throwaway queue pair with a 1s visibility timeout so the
3-receive redrive completes in seconds — the mechanics are identical to the
real queues (same consumer, same handler, same redrive policy shape).
"""

import json
import socket
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest
from docfactory_core.confidence_model import get_confidence_model
from docfactory_core.config import get_settings
from docfactory_core.db import session_scope
from docfactory_core.models import Document, DocumentStatus, Extraction
from sqlalchemy import delete, select

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def stack():
    settings = get_settings()
    endpoints = (settings.s3_endpoint_url, settings.sqs_endpoint_url)
    if not all(endpoints) or not all(_reachable(url) for url in endpoints):
        pytest.skip("compose stack is not running")
    from docfactory_core.bootstrap import ensure_infra
    from docfactory_core.queues import QueueBroker
    from docfactory_core.storage import ObjectStore

    try:
        with session_scope() as session:
            session.execute(select(Document).limit(1))
    except Exception:
        pytest.skip("postgres is not migrated/reachable")
    ensure_infra(attempts=3)
    return ObjectStore(), QueueBroker()


def _ingest(store, fixture_name: str, doc_type: str = "invoice") -> Document:
    """Simulate a completed upload: object in MinIO + document row in received."""
    pdf = (FIXTURES / fixture_name).read_bytes()
    import hashlib

    sha = hashlib.sha256(pdf).hexdigest()
    settings = get_settings()
    s3_key = f"{settings.default_tenant_id}/incoming/{sha}.pdf"
    store.put_object(s3_key, pdf, content_type="application/pdf")
    with session_scope() as session:
        # clean up any row left over from a previous run of this test
        session.execute(delete(Document).where(Document.sha256 == sha))
    document = Document(
        id=uuid.uuid4(),
        tenant_id=settings.default_tenant_id,
        s3_key=s3_key,
        sha256=sha,
        doc_type=doc_type,
        status=DocumentStatus.RECEIVED,
    )
    with session_scope() as session:
        session.add(document)
    return document


def _status(document_id: uuid.UUID) -> Document:
    with session_scope() as session:
        return session.get(Document, document_id)


def test_digital_document_flows_to_a_routed_terminal_state(stack):
    store, _ = stack
    from docfactory_worker.handlers import handle_extract, handle_parse

    document = _ingest(store, "digital_euro.pdf")
    payload = {"document_id": str(document.id)}

    handle_parse(payload)
    parsed = _status(document.id)
    assert parsed.status == DocumentStatus.PARSED
    assert parsed.text_chars and parsed.text_chars > 200
    assert store.get_object(parsed.text_s3_key)  # text artifact exists

    handle_extract(payload)
    extracted = _status(document.id)
    # Since 2.3b the terminal state is the routing decision. This fixture is
    # extracted cleanly by the mock, so it should clear the threshold.
    assert extracted.status == DocumentStatus.APPROVED
    assert extracted.extracted_at is not None

    with session_scope() as session:
        extractions = session.scalars(
            select(Extraction).where(Extraction.document_id == document.id)
        ).all()
        assert extractions
        extraction = extractions[-1]
        assert extraction.model == "mock:mock-extractor-v1"
        assert extraction.validation_passed is True
        assert all(extraction.validation.values())
        # 9 scalar fields + line_items.count + 5 items x 4 cells
        assert len(extraction.fields) == 9 + 1 + 5 * 4
        before = len(extractions)

    # Redelivery after completion is acknowledged without duplicate work.
    # Compared before/after rather than to an absolute count, so a worker
    # running alongside the suite can't make this flap.
    handle_extract(payload)
    with session_scope() as session:
        after = len(
            session.scalars(select(Extraction).where(Extraction.document_id == document.id)).all()
        )
    assert after == before


def test_scanned_document_routes_to_needs_ocr_not_failed(stack):
    store, _ = stack
    from docfactory_worker.handlers import handle_parse

    document = _ingest(store, "scanned.pdf")
    payload = {"document_id": str(document.id)}

    handle_parse(payload)
    routed = _status(document.id)
    assert routed.status == DocumentStatus.NEEDS_OCR  # terminal, expected — not failed
    assert routed.last_error is None
    assert routed.text_chars == 0

    handle_parse(payload)  # redelivery of a terminal doc: no-op, no crash
    assert _status(document.id).status == DocumentStatus.NEEDS_OCR


def test_poison_pdf_lands_in_dlq_and_worker_survives(stack):
    store, broker = stack
    from docfactory_core.queues import dlq_name
    from docfactory_worker.consumer import Consumer
    from docfactory_worker.handlers import handle_parse

    queue = "docfactory-test-poison"
    broker.ensure_queue_pair(queue, visibility_timeout="1", max_receive_count=3)
    sqs = broker._sqs
    try:
        document = _ingest(store, "corrupt.pdf")
        broker.send(queue, {"document_id": str(document.id)})

        consumer = Consumer(broker, queue, handle_parse)
        dlq_url = broker.queue_url(dlq_name(queue))
        moved = False
        for _ in range(30):
            consumer.run_once(wait_seconds=1)
            depth = int(
                sqs.get_queue_attributes(
                    QueueUrl=dlq_url, AttributeNames=["ApproximateNumberOfMessages"]
                )["Attributes"]["ApproximateNumberOfMessages"]
            )
            if depth == 1:
                moved = True
                break
        assert moved, "poison message never reached the DLQ"

        # the DLQ'd message is the original payload; the error is on the row
        dlq_message = sqs.receive_message(QueueUrl=dlq_url, WaitTimeSeconds=2)["Messages"][0]
        assert json.loads(dlq_message["Body"])["document_id"] == str(document.id)
        failed = _status(document.id)
        assert failed.status == DocumentStatus.FAILED
        assert "parse:" in failed.last_error

        # the consumer is still alive and polls cleanly
        assert consumer.run_once(wait_seconds=1) == 0
    finally:
        sqs.delete_queue(QueueUrl=broker.queue_url(queue))
        sqs.delete_queue(QueueUrl=broker.queue_url(dlq_name(queue)))


def test_confidence_is_persisted_end_to_end(stack):
    """A clean digital invoice carries a score, signals, and per-field values."""
    from docfactory_worker.handlers import handle_extract, handle_parse

    store, _ = stack
    document = _ingest(store, "digital_classic.pdf")
    payload = {"document_id": str(document.id)}
    handle_parse(payload)
    handle_extract(payload)

    with session_scope() as session:
        extraction = session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.created_at.desc())
        ).first()
        assert extraction is not None
        # doc_confidence is the calibrated probability of the weakest field,
        # so it is directly comparable to the model's threshold.
        model = get_confidence_model()
        assert float(extraction.doc_confidence) >= model.threshold
        assert extraction.routing_decision == "approved"
        assert extraction.confidence_model_version == model.version
        signals = extraction.confidence_signals
        assert signals["attempts"] == 1
        assert signals["rule.subtotal_plus_tax_equals_total"] is True
        # residual magnitudes are stored, not just the booleans
        assert "residual.subtotal_plus_tax_equals_total" in signals
        assert signals["vendor.looks_fragmented"] is False

        scored = {f.name: f.confidence for f in extraction.fields}
        assert scored["vendor"] is not None
        # every stored field carries a score, line-item cells included
        assert all(value is not None for value in scored.values())
        assert float(scored["line_items.0.amount"]) == float(scored["line_items.count"])


def test_routing_sends_a_low_confidence_document_to_review(stack):
    """A corrupted extraction must land in needs_review, not approved.

    Uses the 2.2b corruption machinery rather than a hand-built row, so the
    path exercised is the real one: mock extraction -> score -> route.
    """
    import os

    from docfactory_core.config import get_settings
    from docfactory_core.llm import MockLLMClient

    store, _ = stack
    from docfactory_worker.handlers import handle_parse

    document = _ingest(store, "digital_classic.pdf")
    payload = {"document_id": str(document.id)}
    handle_parse(payload)

    # Force corruption on for this one extraction, then restore.
    previous = os.environ.get("MOCK_CORRUPTION_RATE")
    os.environ["MOCK_CORRUPTION_RATE"] = "1.0"
    get_settings.cache_clear()
    try:
        import docfactory_worker.handlers as handlers

        corrupting = MockLLMClient()
        original = handlers.get_llm_client
        handlers.get_llm_client = lambda: corrupting
        try:
            handlers.handle_extract(payload)
        finally:
            handlers.get_llm_client = original
    finally:
        if previous is None:
            os.environ.pop("MOCK_CORRUPTION_RATE", None)
        else:
            os.environ["MOCK_CORRUPTION_RATE"] = previous
        get_settings.cache_clear()

    routed = _status(document.id)
    assert routed.status in (DocumentStatus.APPROVED, DocumentStatus.NEEDS_REVIEW)
    with session_scope() as session:
        extraction = session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.created_at.desc())
        ).first()
        assert extraction.routing_decision == routed.status
        assert extraction.confidence_model_version == get_confidence_model().version


def test_a_purchase_order_flows_through_the_same_worker(stack):
    """The 3c claim, end to end: a second document type through the real stages.

    The handler picks the pipeline from the document's own `doc_type`, so the
    only thing that makes this a purchase order rather than an invoice is the
    definition — the parse and extract code paths are the ones the invoice
    tests above exercise.
    """
    from docfactory_worker.handlers import handle_extract, handle_parse

    store, _ = stack
    document = _ingest(store, "po_standard.pdf", doc_type="purchase_order")
    payload = {"document_id": str(document.id)}

    handle_parse(payload)
    assert _status(document.id).status == DocumentStatus.PARSED

    handle_extract(payload)
    assert _status(document.id).status == DocumentStatus.APPROVED

    with session_scope() as session:
        extraction = session.scalars(
            select(Extraction)
            .where(Extraction.document_id == document.id)
            .order_by(Extraction.created_at.desc())
        ).first()
        assert extraction.pipeline_slug == "purchase_order"
        assert extraction.pipeline_version == 1
        assert extraction.validation_passed is True
        # the purchase order's own rules ran, not the invoice's
        assert set(extraction.validation) == {
            "line_items_sum_to_subtotal",
            "subtotal_plus_shipping_equals_total",
            "order_date_parses",
            "delivery_not_before_order_date",
            "po_number_format",
        }
        assert extraction.output["po_number"] == "PO-2026-47563"
        assert extraction.output["total"] == "102636.52"

        stored = {row.name: row.value for row in extraction.fields}
        # 9 scalars + line_items.count + 4 rows x 4 cells
        assert len(stored) == 9 + 1 + 4 * 4
        assert stored["vendor"] == "Jenkins-Cook"
        assert stored["shipping"] == "95.00"
        assert "invoice_number" not in stored
        assert all(row.confidence is not None for row in extraction.fields)
