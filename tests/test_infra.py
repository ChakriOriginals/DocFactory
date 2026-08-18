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
    # Keys must live under the tenant prefix since Phase 3.
    key = f"{get_settings().default_tenant_id}/test/{uuid.uuid4()}.bin"
    payload = b"docfactory roundtrip"
    store.put_object(key, payload, content_type="application/octet-stream")
    try:
        assert store.get_object(key) == payload
        assert key in list(store.list_keys(f"{get_settings().default_tenant_id}/test/"))
    finally:
        store._s3.delete_object(Bucket=store.bucket, Key=key)


def test_queue_send_receive_roundtrip():
    # A throwaway queue, not the pipeline's: probing a real queue races any
    # running worker, which would consume the probe and fail this test.
    broker = QueueBroker()
    queue = f"docfactory-test-probe-{uuid.uuid4().hex[:8]}"
    broker.ensure_queue_pair(queue, visibility_timeout="30", max_receive_count=3)
    url = broker.queue_url(queue)
    try:
        sent = {"probe": str(uuid.uuid4())}
        broker.send(queue, sent)
        messages = broker._sqs.receive_message(QueueUrl=url, WaitTimeSeconds=2).get("Messages", [])
        assert messages, "sent message was not received"
        assert json.loads(messages[0]["Body"]) == sent
        broker._sqs.delete_message(QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"])
    finally:
        broker._sqs.delete_queue(QueueUrl=url)
        broker._sqs.delete_queue(QueueUrl=broker.queue_url(dlq_name(queue)))


class TestInfraOwnership:
    """Who is allowed to create the bucket and the queues.

    Locally the app creates them, because compose brings up empty backends and
    nothing else will. On AWS Terraform owns them and the task roles have no
    CreateQueue/CreateBucket rights at all — so the app must verify rather than
    create, and fail loudly when something is missing instead of retrying a
    call it will never be permitted to make.
    """

    def test_auto_asserts_when_no_endpoint_overrides_are_set(self, monkeypatch):
        """No endpoints set means real AWS, which means Terraform owns it."""
        from docfactory_core import bootstrap

        monkeypatch.setattr(
            bootstrap, "get_settings", lambda: _settings(s3=None, sqs=None, mode="auto")
        )
        calls = _record_calls(monkeypatch, bootstrap)
        bootstrap.ensure_infra(attempts=1)
        assert calls == ["assert_bucket", "assert_queues"]

    def test_auto_creates_when_pointed_at_local_backends(self, monkeypatch):
        from docfactory_core import bootstrap

        monkeypatch.setattr(
            bootstrap,
            "get_settings",
            lambda: _settings(s3="http://localhost:9000", sqs="http://localhost:9324"),
        )
        calls = _record_calls(monkeypatch, bootstrap)
        bootstrap.ensure_infra(attempts=1)
        assert calls == ["ensure_bucket", "ensure_queues"]

    def test_assert_mode_is_honoured_even_against_local_endpoints(self, monkeypatch):
        """An explicit setting beats inference: useful for testing the AWS path."""
        from docfactory_core import bootstrap

        monkeypatch.setattr(
            bootstrap,
            "get_settings",
            lambda: _settings(s3="http://localhost:9000", sqs="http://x", mode="assert"),
        )
        calls = _record_calls(monkeypatch, bootstrap)
        bootstrap.ensure_infra(attempts=1)
        assert calls == ["assert_bucket", "assert_queues"]

    def test_a_missing_queue_fails_with_an_actionable_message(self):
        """The deployed failure mode: Terraform did not run, or the role is wrong."""
        from botocore.exceptions import ClientError
        from docfactory_core.queues import QueueBroker

        broker = QueueBroker.__new__(QueueBroker)
        broker._settings = _settings(s3=None, sqs=None)
        broker._urls = {}

        def missing(name):
            raise ClientError(
                {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue"}}, "GetQueueUrl"
            )

        broker.queue_url = missing
        with pytest.raises(RuntimeError, match="managed by Terraform"):
            broker.assert_queues()


def _settings(*, s3, sqs, mode="auto"):
    from docfactory_core.config import Settings

    return Settings(
        s3_endpoint_url=s3,
        sqs_endpoint_url=sqs,
        infra_mode=mode,
        ingest_notify_target="",
    )


def _record_calls(monkeypatch, bootstrap):
    """Swap the clients for recorders so no backend is touched."""
    calls: list[str] = []

    class FakeStore:
        def assert_bucket(self):
            calls.append("assert_bucket")

        def ensure_bucket(self):
            calls.append("ensure_bucket")

        def ensure_bucket_notifications(self, *args, **kwargs):
            calls.append("notifications")

    class FakeBroker:
        def assert_queues(self):
            calls.append("assert_queues")

        def ensure_queues(self):
            calls.append("ensure_queues")

    monkeypatch.setattr(bootstrap, "ObjectStore", FakeStore)
    monkeypatch.setattr(bootstrap, "QueueBroker", FakeBroker)
    return calls
