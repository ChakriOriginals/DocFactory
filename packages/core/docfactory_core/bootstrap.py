"""Infra bootstrap: make the bucket and queues exist.

Called on service startup (api/worker) and by ``make up``. Idempotent. Retries
briefly because a container reporting healthy can precede its endpoint
accepting requests by a moment.
"""

import logging
import time

from botocore.exceptions import BotoCoreError, ClientError

from docfactory_core.config import get_settings
from docfactory_core.logging import configure_logging
from docfactory_core.queues import QueueBroker
from docfactory_core.storage import ObjectStore

log = logging.getLogger(__name__)


def ensure_infra(attempts: int = 30, delay: float = 1.0) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            store = ObjectStore()
            store.ensure_bucket()
            QueueBroker().ensure_queues()
            # Batch ingestion: object-created events under a tenant's drop
            # prefix start the pipeline without an API call. Best-effort —
            # a backend with no notification target configured is not an
            # error, it just means only the API path is live.
            settings = get_settings()
            if settings.s3_endpoint_url and settings.ingest_notify_target:
                store.ensure_bucket_notifications(settings.ingest_notify_target)
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
