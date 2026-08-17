"""Worker entrypoint: one process, two consumer threads (parse + extract)."""

import logging
import signal
import threading

from docfactory_core.bootstrap import ensure_infra
from docfactory_core.config import get_settings
from docfactory_core.logging import configure_logging
from docfactory_core.queues import QueueBroker
from docfactory_core.tracing import setup_tracing

from docfactory_worker.consumer import Consumer
from docfactory_worker.handlers import handle_extract, handle_parse

log = logging.getLogger(__name__)


def main() -> None:
    configure_logging("worker")
    setup_tracing("docfactory-worker")
    ensure_infra()
    settings = get_settings()
    stop = threading.Event()

    def request_stop(signum, frame):
        log.info("shutdown requested", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    consumers = (
        Consumer(QueueBroker(), settings.parse_queue, handle_parse, stop),
        Consumer(QueueBroker(), settings.extract_queue, handle_extract, stop),
    )
    threads = [
        threading.Thread(target=consumer.run_forever, name=f"consumer-{i}", daemon=False)
        for i, consumer in enumerate(consumers)
    ]
    for thread in threads:
        thread.start()
    log.info("worker running", extra={"queues": [settings.parse_queue, settings.extract_queue]})
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
