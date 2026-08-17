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
        for logical in (self._settings.parse_queue, self._settings.extract_queue):
            dlq_url = self._sqs.create_queue(QueueName=dlq_name(logical))["QueueUrl"]
            dlq_arn = self._sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
                "Attributes"
            ]["QueueArn"]
            redrive_policy = json.dumps(
                {
                    "deadLetterTargetArn": dlq_arn,
                    "maxReceiveCount": str(self._settings.max_receive_count),
                }
            )
            try:
                url = self._sqs.create_queue(
                    QueueName=logical, Attributes={"RedrivePolicy": redrive_policy}
                )["QueueUrl"]
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                # Queue exists with different attributes (e.g. policy changed):
                # converge instead of failing, so ensure stays idempotent.
                if code not in {"QueueAlreadyExists", "QueueNameExists"}:
                    raise
                url = self.queue_url(logical)
                self._sqs.set_queue_attributes(
                    QueueUrl=url, Attributes={"RedrivePolicy": redrive_policy}
                )
            self._urls[logical] = url
            self._urls[dlq_name(logical)] = dlq_url
            log.info("queue ready", extra={"queue": logical, "dlq": dlq_name(logical)})

    def send(self, queue: str, payload: dict) -> str:
        response = self._sqs.send_message(
            QueueUrl=self.queue_url(queue), MessageBody=json.dumps(payload)
        )
        return response["MessageId"]
