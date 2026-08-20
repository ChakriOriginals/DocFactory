"""Worker entrypoint: one process, three consumer threads (ingest + parse + extract)."""

import logging
import signal
import threading

from docfactory_core.bootstrap import ensure_infra
from docfactory_core.config import get_settings
from docfactory_core.healing import heal
from docfactory_core.logging import configure_logging
from docfactory_core.queues import QueueBroker
from docfactory_core.tracing import setup_tracing

from docfactory_worker.consumer import Consumer
from docfactory_worker.handlers import handle_extract, handle_ingest, handle_parse

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

    if settings.heal_interval_seconds > 0:
        threading.Thread(
            target=_healing_loop,
            args=(stop, settings),
            name="healer",
            daemon=True,
        ).start()

    consumers = (
        # Batch ingestion and the API upload path feed the same parse queue.
        Consumer(QueueBroker(), settings.ingest_queue, handle_ingest, stop),
        Consumer(QueueBroker(), settings.parse_queue, handle_parse, stop),
        Consumer(QueueBroker(), settings.extract_queue, handle_extract, stop),
    )
    threads = [
        threading.Thread(target=consumer.run_forever, name=f"consumer-{i}", daemon=False)
        for i, consumer in enumerate(consumers)
    ]
    for thread in threads:
        thread.start()
    log.info(
        "worker running",
        extra={"queues": [settings.ingest_queue, settings.parse_queue, settings.extract_queue]},
    )
    for thread in threads:
        thread.join()


def _healing_loop(stop: threading.Event, settings) -> None:
    """Sweep for work the queue cannot recover on its own.

    A daemon thread rather than a separate scheduled task: the sweep is cheap,
    it needs exactly the credentials and network the worker already has, and a
    fleet that scales to zero has nobody to run a cron. Every running worker
    sweeps; the operations are idempotent, so overlapping sweeps cost duplicate
    no-op receives and nothing else.

    NOTHING HERE MAY KILL THE WORKER. A sweeper that crashes the process it
    lives in has done more damage than the stranded documents it was looking
    for — the 4e lesson, applied again.
    """
    from datetime import timedelta

    from docfactory_core.queues import QueueBroker

    broker = QueueBroker()
    stale_after = timedelta(seconds=settings.heal_stale_after_seconds)

    # Wait first. Starting a sweep in the same instant as the consumers means
    # racing the documents they are about to pick up.
    while not stop.wait(settings.heal_interval_seconds):
        try:
            summary = heal(broker, stale_after=stale_after)
        except Exception:
            log.exception("healing sweep failed; the worker continues")
            continue
        if any(summary.values()):
            log.info("healing sweep", extra=summary)


if __name__ == "__main__":
    main()
