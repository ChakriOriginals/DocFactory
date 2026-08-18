"""SQS-compatible queue client.

Locally this targets ElasticMQ via SQS_ENDPOINT_URL; with the endpoint unset,
boto3's default credential chain targets real SQS. Every logical queue gets a
companion DLQ wired by a redrive policy: after max_receive_count failed
receives the *queue service* moves the message aside — a poison document can
never block a queue, and no consumer code has to count failures.
"""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from docfactory_core.config import Settings, get_settings

log = logging.getLogger(__name__)


def dlq_name(queue: str) -> str:
    return f"{queue}-dlq"


class QueueBroker:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        kwargs: dict = {"region_name": self._settings.aws_region}
        if self._settings.sqs_endpoint_url:
            kwargs.update(
                endpoint_url=self._settings.sqs_endpoint_url,
                # ElasticMQ accepts any credentials; these never reach AWS.
                aws_access_key_id="local",
                aws_secret_access_key="local",
            )
        self._sqs = boto3.client("sqs", **kwargs)
        self._urls: dict[str, str] = {}

    def queue_url(self, name: str) -> str:
        if name not in self._urls:
            self._urls[name] = self._sqs.get_queue_url(QueueName=name)["QueueUrl"]
        return self._urls[name]

    def ensure_queues(self) -> None:
        # Visibility timeout must exceed worst-case processing time or a slow
        # message gets redelivered mid-flight: parse is fast (10s); extract
        # can include a real LLM call plus one retry (90s).
        visibility = {
            self._settings.parse_queue: "10",
            self._settings.extract_queue: "90",
            # Ingest fetches an object and writes one row; short and bounded.
            self._settings.ingest_queue: "30",
        }
        for logical in (
            self._settings.parse_queue,
            self._settings.extract_queue,
            self._settings.ingest_queue,
        ):
            self.ensure_queue_pair(
                logical,
                visibility_timeout=visibility[logical],
                max_receive_count=self._settings.max_receive_count,
            )
            log.info("queue ready", extra={"queue": logical, "dlq": dlq_name(logical)})

    def assert_queues(self) -> None:
        """Verify every queue exists. Never create one.

        `create_queue` is idempotent, but it still needs sqs:CreateQueue — a
        permission the deployed task roles deliberately do not have. Asserting
        turns "the task cannot create infrastructure" from a hope into a
        startup check with a clear message.
        """
        for logical in (
            self._settings.parse_queue,
            self._settings.extract_queue,
            self._settings.ingest_queue,
        ):
            for name in (logical, dlq_name(logical)):
                try:
                    self.queue_url(name)
                except ClientError as exc:
                    raise RuntimeError(
                        f"queue {name!r} is missing or unreadable by this role ({exc}). "
                        "Infrastructure is managed by Terraform in this environment; "
                        "the application does not create it."
                    ) from exc
            log.info("queue verified", extra={"queue": logical, "dlq": dlq_name(logical)})

    def ensure_queue_pair(
        self, logical: str, *, visibility_timeout: str, max_receive_count: int
    ) -> str:
        """Create (or converge) a queue + its DLQ with redrive. Returns DLQ url."""
        dlq_url = self._sqs.create_queue(QueueName=dlq_name(logical))["QueueUrl"]
        dlq_arn = self._sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
            "Attributes"
        ]["QueueArn"]
        attributes = {
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(max_receive_count)}
            ),
            "VisibilityTimeout": visibility_timeout,
            "ReceiveMessageWaitTimeSeconds": "10",
        }
        try:
            url = self._sqs.create_queue(QueueName=logical, Attributes=attributes)["QueueUrl"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in {"QueueAlreadyExists", "QueueNameExists"}:
                raise
            url = self.queue_url(logical)
        # Converge attributes unconditionally: an existing queue with stale
        # attributes is silently accepted by create_queue on some backends.
        self._sqs.set_queue_attributes(QueueUrl=url, Attributes=attributes)
        self._urls[logical] = url
        self._urls[dlq_name(logical)] = dlq_url
        return dlq_url

    def send(self, queue: str, payload: dict, *, delay_seconds: int = 0) -> str:
        """Enqueue a message, optionally invisible for a while.

        `delay_seconds` is how backpressure defers work: a message over a
        tenant's in-flight ceiling goes back to the queue with a delay instead
        of being processed, so the worker moves straight on to another
        tenant's message rather than spinning on this one.
        """
        kwargs = {"QueueUrl": self.queue_url(queue), "MessageBody": json.dumps(payload)}
        if delay_seconds:
            kwargs["DelaySeconds"] = min(delay_seconds, 900)  # SQS ceiling
        response = self._sqs.send_message(**kwargs)
        return response["MessageId"]
