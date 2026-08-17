"""Integration tests against the local compose stack.

These auto-skip when the stack is down so `make test` is always runnable;
with the stack up they exercise the real MinIO/ElasticMQ code paths.
"""

import json
import socket
import uuid
from urllib.parse import urlparse

import pytest
from docfactory_core.bootstrap import ensure_infra
from docfactory_core.config import get_settings
from docfactory_core.queues import QueueBroker, dlq_name
from docfactory_core.storage import ObjectStore

pytestmark = pytest.mark.integration


def _reachable(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def require_stack():
    settings = get_settings()
    if not (settings.s3_endpoint_url and settings.sqs_endpoint_url):
        pytest.skip("local endpoints not configured (no .env)")
    if not (_reachable(settings.s3_endpoint_url) and _reachable(settings.sqs_endpoint_url)):
        pytest.skip("compose stack is not running")
    ensure_infra(attempts=3)


def test_ensure_infra_is_idempotent():
    # Running ensure twice must converge, not fail on already-exists.
    ensure_infra(attempts=1)
    ensure_infra(attempts=1)


def test_queues_exist_with_dlq_redrive_policy():
    settings = get_settings()
    broker = QueueBroker()
    for logical in (settings.parse_queue, settings.extract_queue):
        attrs = broker._sqs.get_queue_attributes(
            QueueUrl=broker.queue_url(logical), AttributeNames=["RedrivePolicy"]
        )["Attributes"]
        policy = json.loads(attrs["RedrivePolicy"])
        assert policy["maxReceiveCount"] == settings.max_receive_count
        assert policy["deadLetterTargetArn"].endswith(dlq_name(logical))


def test_object_store_roundtrip():
    store = ObjectStore()
    key = f"test/{uuid.uuid4()}.bin"
    payload = b"docfactory roundtrip"
    store.put_object(key, payload, content_type="application/octet-stream")
    try:
        assert store.get_object(key) == payload
        assert key in list(store.list_keys("test/"))
    finally:
        store._s3.delete_object(Bucket=store.bucket, Key=key)


def test_queue_send_receive_roundtrip():
    settings = get_settings()
    broker = QueueBroker()
    sent = {"probe": str(uuid.uuid4())}
    broker.send(settings.parse_queue, sent)
    url = broker.queue_url(settings.parse_queue)
    received = broker._sqs.receive_message(QueueUrl=url, WaitTimeSeconds=2)
    messages = received.get("Messages", [])
    assert messages, "sent message was not received"
    assert json.loads(messages[0]["Body"]) == sent
    broker._sqs.delete_message(QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"])
