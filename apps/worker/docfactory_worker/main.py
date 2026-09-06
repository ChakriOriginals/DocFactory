"""Worker entrypoint: one process, three consumer threads (ingest + parse + extract)."""

import logging
import signal
import threading
from pathlib import Path

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
    # Supervise rather than join. `join` would sit here quite happily while a
    # consumer thread was dead, which is the failure this replaces.
    _supervise(threads, stop, settings)
    for thread in threads:
        thread.join(timeout=30)


def _supervise(threads: list[threading.Thread], stop: threading.Event, settings) -> None:
    """Hold a heartbeat file fresh for exactly as long as every consumer lives.

    THE FAILURE THIS EXISTS FOR. The worker is one process running three
    consumer threads. If one of them dies — an exception escaping run_forever,
    a thread killed by something outside Python — the process stays up, the
    other two keep working, and the container looks perfectly healthy. The
    stack would carry on with two thirds of a pipeline and nothing anywhere
    would say so; documents on the dead consumer's queue would simply stop
    moving, and the first symptom would be a queue depth nobody was watching.

    ECS cannot see inside a process. What it can see is a container health
    check, so this converts "all three consumers are alive" into something a
    health check can read: a file whose mtime stops advancing the moment the
    claim stops being true.

    Deliberately not self-repair. Restarting a dead consumer in-process would
    paper over whatever killed it and leave a worker in a state no test covers.
    Letting the heartbeat go stale hands the task to ECS, which replaces it
    with a clean one — the platform is better at that than this function would
    be, and it is already paying for the capability.
    """
    heartbeat = Path(settings.worker_heartbeat_path)
    interval = max(settings.worker_heartbeat_interval_seconds, 1)

    while not stop.is_set():
        dead = [thread.name for thread in threads if not thread.is_alive()]
        if dead:
            # Stop touching the file and say why. The health check fails, ECS
            # replaces the task, and this line is what explains the restart.
            log.error(
                "consumer thread died; letting the heartbeat go stale so ECS replaces this task",
                extra={"dead_threads": dead, "heartbeat": str(heartbeat)},
            )
            return
        try:
            heartbeat.touch()
        except OSError:
            # An unwritable heartbeat path is a misconfiguration, not a reason
            # to kill a working worker. Log it and keep processing; the health
            # check will fail and ECS will replace the task, which is the
            # correct outcome for a container that cannot report its health.
            log.exception("could not write the heartbeat", extra={"path": str(heartbeat)})
        stop.wait(interval)


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
