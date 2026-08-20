"""Error taxonomy, bounded retry, and a circuit breaker for the model provider.

THE DISTINCTION EVERYTHING HERE RESTS ON. A failure is either *transient* — the
same call would probably succeed a moment later — or *permanent* — the same
call will fail identically forever. Treating one as the other is expensive in
both directions:

- retrying a permanent failure burns the redrive budget on a document that was
  never going to parse, and delays its arrival in the DLQ where a human can see
  it;
- giving up on a transient failure throws away a perfectly good document
  because a socket closed.

The queue's redrive policy cannot tell them apart: it counts receives. So the
classification happens here, in process, and the two get different machinery.
Transient failures are retried in-process with backoff, which is what keeps
them from consuming the poison-message budget at all — a message that never
goes back to the queue never increments its receive count.

WHY A CIRCUIT BREAKER AND NOT JUST RETRIES. Retries assume the failure is
local. When a provider is down, every worker retrying independently turns an
outage into a thundering herd against a service that is already unwell, and —
because reservations are taken before each call — it burns budget on calls that
cannot succeed. The breaker makes the fleet notice collectively-visible failure
and stop, which is both self-healing and cost protection.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

log = logging.getLogger(__name__)

Verdict = Literal["transient", "permanent"]


class TransientError(Exception):
    """Retry me. The same call would probably work in a moment."""


class PermanentError(Exception):
    """Do not retry me. The same call will fail the same way forever."""


class CircuitOpenError(TransientError):
    """The provider is being given a rest. Transient by definition: it closes."""


# --- classification ---------------------------------------------------------
#
# Matched on names rather than imported types, deliberately. Importing
# botocore, psycopg, sqlalchemy and anthropic here to reference their exception
# classes would make this module drag the whole dependency tree into anything
# that wants to classify an error, including the API process that has no
# business importing an LLM SDK. Names are stable across those libraries'
# versions in a way that class paths are not.

_TRANSIENT_NAMES = frozenset(
    {
        # botocore / boto3
        "ConnectionError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "IncompleteReadError",
        "ResponseStreamingError",
        # SQLAlchemy / psycopg
        "OperationalError",
        "InterfaceError",
        "DBAPIError",
        "TimeoutError",
        # anthropic SDK
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
        "OverloadedError",
        # stdlib
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
        "socket.timeout",
    }
)

_PERMANENT_NAMES = frozenset(
    {
        # anthropic SDK: the request itself is wrong, or was refused
        "BadRequestError",
        "UnprocessableEntityError",
        "PermissionDeniedError",
        "NotFoundError",
        "LLMRefusalError",
        # the document is not a document
        "PDFSyntaxError",
        "PdfminerException",
        "PSEOF",
        # the database said no on the merits
        "IntegrityError",
        "CheckViolation",
        "ProgrammingError",
        "DataError",
    }
)

# HTTP-ish status codes, wherever we can find one on the exception.
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509})

# AWS error codes that mean "later", not "no".
_TRANSIENT_AWS_CODES = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestThrottled",
        "RequestThrottledException",
        "ProvisionedThroughputExceededException",
        "ServiceUnavailable",
        "InternalError",
        "InternalFailure",
        "SlowDown",
        "RequestTimeout",
        "RequestTimeoutException",
        "KMSThrottlingException",
    }
)


def _status_of(exc: BaseException) -> int | None:
    for attribute in ("status_code", "status"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        metadata = response.get("ResponseMetadata") or {}
        code = metadata.get("HTTPStatusCode")
        if isinstance(code, int):
            return code
    return None


def _aws_code_of(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str):
            return code
    return None


def classify(exc: BaseException) -> Verdict:
    """Transient or permanent. Defaults to PERMANENT when it cannot tell.

    The default direction is the important decision. An unknown error retried
    forever is a queue that never drains and a bill that never stops; an
    unknown error sent to the DLQ is one document a human looks at. Failing
    towards "stop and show someone" is the cheaper mistake, and the DLQ exists
    precisely to make it a cheap one.
    """
    if isinstance(exc, TransientError):
        return "transient"
    if isinstance(exc, PermanentError):
        return "permanent"

    # PERMANENT ANYWHERE IN THE CHAIN WINS. SQLAlchemy and boto3 both wrap, and
    # deciding on the outermost exception alone gets it backwards: a bad
    # request wrapped in a connection error is still a bad request, and
    # retrying it three times proves nothing. Scanning the whole chain and
    # preferring the permanent verdict is the same conservative direction as
    # the default below.
    verdicts = [_verdict_of(error) for error in (exc, *_causes(exc))]
    if "permanent" in verdicts:
        return "permanent"
    if "transient" in verdicts:
        return "transient"
    return "permanent"


def _verdict_of(error: BaseException) -> Verdict | None:
    """What one exception says about itself, or None if it says nothing.

    The internal order matters and is not arbitrary. An AWS throttle arrives as
    an HTTP 400, and 400s are otherwise permanent, so the error CODE has to be
    consulted before the status or every throttle becomes a dead letter.
    """
    name = type(error).__name__
    if name in _PERMANENT_NAMES:
        return "permanent"

    code = _aws_code_of(error)
    if code in _TRANSIENT_AWS_CODES:
        return "transient"

    status = _status_of(error)
    if status in _TRANSIENT_STATUS:
        return "transient"
    if status is not None and 400 <= status < 500:
        return "permanent"

    if name in _TRANSIENT_NAMES:
        return "transient"
    return None


def _causes(exc: BaseException, limit: int = 5) -> list[BaseException]:
    """The __cause__/__context__ chain. SQLAlchemy and boto3 both wrap."""
    chain: list[BaseException] = []
    seen = {id(exc)}
    current = exc
    while len(chain) < limit:
        nxt = current.__cause__ or current.__context__
        if nxt is None or id(nxt) in seen:
            break
        chain.append(nxt)
        seen.add(id(nxt))
        current = nxt
    return chain


# --- bounded retry ----------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    # Full jitter. Without it, every worker that failed at the same moment
    # retries at the same moment, which is how a recovering service is knocked
    # over by the clients waiting for it.
    jitter: bool = True

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        capped = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        if not self.jitter:
            return capped
        return (rng or random).uniform(0.0, capped)


def retry_transient[T](
    call: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Run `call`, retrying only what is worth retrying.

    Permanent failures are re-raised on the first attempt — the whole point of
    classifying is not to spend eight seconds proving that a corrupt PDF is
    still corrupt.

    `sleep` and `rng` are injectable so tests are deterministic and instant;
    nothing about the retry behaviour is random in CI.
    """
    policy = policy or RetryPolicy()
    last: BaseException | None = None

    for attempt in range(1, policy.attempts + 1):
        try:
            return call()
        except BaseException as exc:  # classified immediately below
            if classify(exc) == "permanent":
                raise
            last = exc
            if attempt == policy.attempts:
                break
            delay = policy.delay_for(attempt, rng)
            if on_retry:
                on_retry(attempt, exc, delay)
            log.warning(
                "transient failure; retrying",
                extra={
                    "attempt": attempt,
                    "of": policy.attempts,
                    "delay_s": round(delay, 3),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            sleep(delay)

    assert last is not None
    raise last


# --- circuit breaker --------------------------------------------------------


@dataclass
class BreakerState:
    failures: int = 0
    opened_at: datetime | None = None
    half_open_probe: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class CircuitBreaker:
    """Closed -> open on repeated transient failure -> half-open probe -> closed.

    Process-local, and that is a real limitation stated up front: six worker
    tasks have six breakers, so a provider outage is noticed six times rather
    than once. A shared breaker would need the database on the hot path of
    every model call, which costs more than it saves at this fleet size. It
    still achieves the thing that matters — each worker stops hammering a dead
    provider within `threshold` failures instead of retrying until the budget
    is gone.

    Only TRANSIENT failures count towards opening. A provider that rejects a
    malformed request is not a provider that is down, and counting those would
    open the breaker on our own bug and hide it.
    """

    def __init__(
        self,
        *,
        name: str = "model",
        threshold: int = 5,
        cooldown_seconds: float = 60.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = timedelta(seconds=cooldown_seconds)
        self._now = now or (lambda: datetime.now(UTC))
        self._state = BreakerState()

    @property
    def state(self) -> Literal["closed", "open", "half_open"]:
        with self._state.lock:
            return self._state_unlocked()

    def _state_unlocked(self) -> Literal["closed", "open", "half_open"]:
        if self._state.opened_at is None:
            return "closed"
        if self._now() - self._state.opened_at >= self.cooldown:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """May a call go out right now?

        In half-open, exactly ONE caller is allowed through as a probe. The
        others are refused, because sending the whole fleet at a provider the
        moment its cooldown expires is the thundering herd the breaker exists
        to prevent, just delayed by a minute.
        """
        with self._state.lock:
            state = self._state_unlocked()
            if state == "closed":
                return True
            if state == "open":
                return False
            if self._state.half_open_probe:
                return False
            self._state.half_open_probe = True
            return True

    def record_success(self) -> None:
        with self._state.lock:
            was_open = self._state.opened_at is not None
            self._state.failures = 0
            self._state.opened_at = None
            self._state.half_open_probe = False
        if was_open:
            log.info("circuit closed: provider recovered", extra={"breaker": self.name})

    def record_failure(self, exc: BaseException) -> None:
        if classify(exc) != "transient":
            return
        with self._state.lock:
            # A failed probe re-opens immediately and restarts the cooldown,
            # rather than letting the next caller probe again at once.
            if self._state.half_open_probe:
                self._state.half_open_probe = False
                self._state.opened_at = self._now()
                opened = True
            else:
                self._state.failures += 1
                opened = self._state.failures >= self.threshold and self._state.opened_at is None
                if opened:
                    self._state.opened_at = self._now()
        if opened:
            log.error(
                "circuit opened: pausing calls to the provider",
                extra={
                    "breaker": self.name,
                    "threshold": self.threshold,
                    "cooldown_s": self.cooldown.total_seconds(),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    def call[T](self, fn: Callable[[], T]) -> T:
        if not self.allow():
            raise CircuitOpenError(
                f"circuit '{self.name}' is open; not calling the provider "
                f"(cooldown {self.cooldown.total_seconds():.0f}s)"
            )
        try:
            result = fn()
        except BaseException as exc:  # recorded, then re-raised unchanged
            self.record_failure(exc)
            raise
        self.record_success()
        return result

    def reset(self) -> None:
        """Tests and operators only."""
        with self._state.lock:
            self._state.failures = 0
            self._state.opened_at = None
            self._state.half_open_probe = False


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(name: str = "model", **kwargs) -> CircuitBreaker:
    """One breaker per named dependency, per process."""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(name=name, **kwargs)
        return _breakers[name]


def reset_breakers() -> None:
    with _breakers_lock:
        for breaker in _breakers.values():
            breaker.reset()
