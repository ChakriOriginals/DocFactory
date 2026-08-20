"""Error taxonomy, bounded retry, and the circuit breaker.

Everything here is deterministic. Faults are injected by counting, not by
sampling; `sleep` and the jitter RNG are both injected, so the suite is instant
and cannot flake. A chaos test that fails once a fortnight teaches a team to
re-run CI, which is worse than having no chaos test.
"""

import random

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from docfactory_core.resilience import (
    CircuitBreaker,
    CircuitOpenError,
    PermanentError,
    RetryPolicy,
    TransientError,
    classify,
    retry_transient,
)


def aws_error(code: str, status: int = 400, operation: str = "GetObject") -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, operation
    )


class FakeHTTPError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status


class TestClassification:
    @pytest.mark.parametrize(
        "exc",
        [
            aws_error("ThrottlingException", 400),
            aws_error("ServiceUnavailable", 503),
            aws_error("InternalError", 500),
            aws_error("SlowDown", 503),
            EndpointConnectionError(endpoint_url="https://sqs.us-east-1.amazonaws.com"),
            ConnectionResetError(),
            FakeHTTPError(429),
            FakeHTTPError(503),
            TransientError("explicit"),
        ],
    )
    def test_transient(self, exc):
        assert classify(exc) == "transient"

    @pytest.mark.parametrize(
        "exc",
        [
            aws_error("AccessDenied", 403),
            aws_error("NoSuchKey", 404),
            FakeHTTPError(400),
            FakeHTTPError(422),
            ValueError("malformed extraction"),
            PermanentError("explicit"),
        ],
    )
    def test_permanent(self, exc):
        assert classify(exc) == "permanent"

    def test_an_unknown_error_is_permanent(self):
        """The default direction is a decision, not an accident.

        An unknown error retried forever is a queue that never drains and a
        bill that never stops. An unknown error sent to the DLQ is one document
        a human looks at. Fail towards the cheap mistake.
        """

        class SomethingNew(Exception):
            pass

        assert classify(SomethingNew()) == "permanent"

    def test_an_aws_throttle_beats_its_own_4xx_status(self):
        """Throttling arrives as a 400, and a 400 is otherwise permanent. The
        AWS error code has to win or every throttle becomes a dead letter."""
        assert classify(aws_error("ThrottlingException", 400)) == "transient"

    def test_a_permanent_cause_beats_a_transient_wrapper(self):
        """SQLAlchemy and boto3 both wrap. A 400 inside something retryable is
        still a bad request."""
        try:
            try:
                raise aws_error("InvalidParameterValue", 400)
            except ClientError as inner:
                raise ConnectionResetError("wrapped") from inner
        except ConnectionResetError as outer:
            assert classify(outer) == "permanent"

    def test_classification_walks_a_bounded_chain(self):
        """A cyclic or very deep __cause__ chain must not hang the classifier."""
        first = ValueError("a")
        second = ValueError("b")
        first.__cause__ = second
        second.__cause__ = first  # cycle
        assert classify(first) == "permanent"


class TestRetry:
    def test_a_transient_failure_is_retried_then_succeeds(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise aws_error("ServiceUnavailable", 503)
            return "ok"

        slept: list[float] = []
        assert (
            retry_transient(
                flaky,
                policy=RetryPolicy(attempts=3, base_delay=1.0, jitter=False),
                sleep=slept.append,
            )
            == "ok"
        )
        assert len(calls) == 3
        assert slept == [1.0, 2.0], "backoff should be exponential"

    def test_a_permanent_failure_is_not_retried_at_all(self):
        """The point of classifying: not spending eight seconds proving that a
        corrupt PDF is still corrupt."""
        calls = []

        def broken():
            calls.append(1)
            raise ValueError("this document is not a PDF")

        slept: list[float] = []
        with pytest.raises(ValueError):
            retry_transient(broken, policy=RetryPolicy(attempts=5), sleep=slept.append)
        assert calls == [1]
        assert slept == []

    def test_exhausting_the_attempts_reraises_the_last_error(self):
        def always_down():
            raise aws_error("ServiceUnavailable", 503)

        with pytest.raises(ClientError):
            retry_transient(
                always_down, policy=RetryPolicy(attempts=3, jitter=False), sleep=lambda _: None
            )

    def test_backoff_is_capped(self):
        policy = RetryPolicy(attempts=10, base_delay=1.0, max_delay=4.0, jitter=False)
        assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 4.0, 4.0]

    def test_jitter_spreads_retries_but_never_exceeds_the_cap(self):
        """Without jitter every worker that failed together retries together,
        which is how a recovering service is knocked over by its own clients."""
        policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=True)
        rng = random.Random(12345)
        delays = [policy.delay_for(3, rng) for _ in range(200)]
        assert all(0.0 <= d <= 4.0 for d in delays)
        assert len(set(delays)) > 100, "jitter should actually spread"


class TestCircuitBreaker:
    def _breaker(self, clock: list[float], **kwargs) -> CircuitBreaker:
        from datetime import UTC, datetime

        return CircuitBreaker(
            threshold=3,
            cooldown_seconds=60,
            now=lambda: datetime.fromtimestamp(clock[0], tz=UTC),
            **kwargs,
        )

    def test_it_opens_after_the_threshold_and_refuses_calls(self):
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(3):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("ServiceUnavailable", 503)))
        assert breaker.state == "open"
        with pytest.raises(CircuitOpenError):
            breaker.call(lambda: "should not run")

    def test_permanent_failures_never_open_it(self):
        """A provider rejecting a malformed request is not a provider that is
        down. Counting those would open the breaker on our own bug and hide it.
        """
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(10):
            with pytest.raises(ValueError):
                breaker.call(lambda: (_ for _ in ()).throw(ValueError("bad request")))
        assert breaker.state == "closed"

    def test_a_success_resets_the_count(self):
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(2):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        breaker.call(lambda: "fine")
        for _ in range(2):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        assert breaker.state == "closed", "two failures either side of a success is not three"

    def test_it_half_opens_after_the_cooldown_and_closes_on_a_good_probe(self):
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(3):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        assert breaker.state == "open"

        clock[0] = 61.0
        assert breaker.state == "half_open"
        assert breaker.call(lambda: "recovered") == "recovered"
        assert breaker.state == "closed"

    def test_only_one_probe_is_allowed_through_in_half_open(self):
        """Sending the whole fleet the instant a cooldown expires is the
        thundering herd the breaker exists to prevent, just delayed."""
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(3):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        clock[0] = 61.0
        assert breaker.allow() is True
        assert breaker.allow() is False
        assert breaker.allow() is False

    def test_a_failed_probe_reopens_and_restarts_the_cooldown(self):
        clock = [0.0]
        breaker = self._breaker(clock)
        for _ in range(3):
            with pytest.raises(ClientError):
                breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        clock[0] = 61.0
        with pytest.raises(ClientError):
            breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))
        assert breaker.state == "open", "a failed probe must not leave it half-open"
        clock[0] = 100.0
        assert breaker.state == "open", "the cooldown should have restarted"
        clock[0] = 122.0
        assert breaker.state == "half_open"

    def test_it_is_thread_safe_under_concurrent_failures(self):
        """Six worker tasks fail at once; the breaker must open once, not six
        times, and must not lose count."""
        import threading

        clock = [0.0]
        breaker = self._breaker(clock)
        barrier = threading.Barrier(6)

        def hammer():
            import contextlib

            barrier.wait()
            for _ in range(5):
                with contextlib.suppress(Exception):
                    breaker.call(lambda: (_ for _ in ()).throw(aws_error("InternalError", 500)))

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert breaker.state == "open"
