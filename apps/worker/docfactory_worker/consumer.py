"""Generic queue consumer.

At-least-once semantics: a message is deleted only after its handler returns.
On handler failure the message is left invisible until the visibility timeout
expires, redelivers, and after max_receive_count failed receives the queue's
redrive policy moves it to the DLQ — the consumer itself never dies on a bad
message, and no consumer code counts failures.
"""

import json
import logging
import threading

from docfactory_core.config import get_settings
from docfactory_core.logging import document_id_var
from docfactory_core.queues import QueueBroker

log = logging.getLogger(__name__)

# Handler contract: handler(payload, receive_count=..., final_attempt=...) -> None.
# Raise to signal failure (message redelivers); return to acknowledge.


class Consumer:
    def __init__(
        self,
        broker: QueueBroker,
        queue_name: str,
        handler,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._broker = broker
        self._queue_name = queue_name
        self._handler = handler
        self._stop = stop_event or threading.Event()
        self._max_receive_count = get_settings().max_receive_count

    def run_once(self, wait_seconds: int = 5) -> int:
        """Poll once, process up to one message. Returns messages processed OK."""
        response = self._broker._sqs.receive_message(
            QueueUrl=self._broker.queue_url(self._queue_name),
            MaxNumberOfMessages=1,
            WaitTimeSeconds=wait_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        processed = 0
        for message in response.get("Messages", []):
            payload = json.loads(message["Body"])
            receive_count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            token = document_id_var.set(payload.get("document_id"))
            try:
                self._handler(
                    payload,
                    receive_count=receive_count,
                    final_attempt=receive_count >= self._max_receive_count,
                )
            except Exception:
                # Deliberately broad: one poison message must never kill the
                # consumer. Not deleting it hands retry/DLQ to the queue layer.
                log.exception(
                    "handler failed; leaving message for redelivery",
                    extra={"queue": self._queue_name, "receive_count": receive_count},
                )
            else:
                self._broker._sqs.delete_message(
                    QueueUrl=self._broker.queue_url(self._queue_name),
                    ReceiptHandle=message["ReceiptHandle"],
                )
                processed += 1
            finally:
                document_id_var.reset(token)
        return processed

    def run_forever(self) -> None:
        log.info("consumer started", extra={"queue": self._queue_name})
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # e.g. transient queue connectivity — back off briefly
                log.exception("poll failed", extra={"queue": self._queue_name})
                self._stop.wait(2)
        log.info("consumer stopped", extra={"queue": self._queue_name})
