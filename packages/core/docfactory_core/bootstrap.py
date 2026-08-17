"""Infra bootstrap: make the bucket and queues exist.

Called on service startup (api/worker) and by ``make up``. Idempotent. Retries
briefly because a container reporting healthy can precede its endpoint
accepting requests by a moment.
"""

import logging
import time

from botocore.exceptions import BotoCoreError, ClientError

from docfactory_core.logging import configure_logging
from docfactory_core.queues import QueueBroker
from docfactory_core.storage import ObjectStore

log = logging.getLogger(__name__)


def ensure_infra(attempts: int = 30, delay: float = 1.0) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            ObjectStore().ensure_bucket()
            QueueBroker().ensure_queues()
            log.info("infrastructure ready")
            return
        except (BotoCoreError, ClientError) as exc:
            last_error = exc
            log.warning(
                "infra not ready, retrying",
                extra={"attempt": attempt, "error": str(exc)},
            )
            time.sleep(delay)
    raise RuntimeError(f"infrastructure not reachable after {attempts} attempts") from last_error


if __name__ == "__main__":
    configure_logging("bootstrap")
    ensure_infra()
