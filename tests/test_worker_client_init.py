"""A transient failure building the clients must not become permanent.

Found by running the stack rather than by reading it. The worker logged:

    AttributeError: 'NoneType' object has no attribute 'send'
    ... then, on redelivery: "skipping duplicate delivery", status=parsed

`_clients()` guarded on `if _store is None` while assigning two globals. When
QueueBroker() raised after ObjectStore() had succeeded, `_store` was left set
and `_broker` left None — and every later call then skipped initialisation,
because the guard only looked at `_store`. One transient blip at startup broke
the process for its whole life.

The failure shape is worse than a crash. handle_parse extracts the text, writes
it to S3, and commits status=parsed BEFORE calling broker.send. So the work is
done and durable when the AttributeError fires; on redelivery the status guard
sees `parsed`, returns cleanly, and the message is deleted. No DLQ, no alarm,
no error state — a document stranded at `parsed` forever with nothing reporting
anything.
"""

import pytest


class _Boom(Exception):
    pass


@pytest.fixture
def handlers():
    import docfactory_worker.handlers as module

    saved = (module._store, module._broker)
    module._store = module._broker = None
    yield module
    module._store, module._broker = saved


def test_a_failed_broker_does_not_poison_the_next_call(handlers, monkeypatch) -> None:
    monkeypatch.setattr(
        handlers, "QueueBroker", lambda *a, **k: (_ for _ in ()).throw(_Boom("transient"))
    )
    with pytest.raises(_Boom):
        handlers._clients()

    # The blip is over.
    monkeypatch.undo()
    store, broker = handlers._clients()
    assert broker is not None, (
        "The broker is still None after the transient failure cleared. The "
        "cached half-initialised state makes one startup blip permanent, and "
        "every parse then strands its document at `parsed` with no DLQ message "
        "and no alarm."
    )
    assert store is not None


def test_a_half_built_pair_is_never_published(handlers, monkeypatch) -> None:
    """The globals must not be visible mid-construction."""
    monkeypatch.setattr(
        handlers, "QueueBroker", lambda *a, **k: (_ for _ in ()).throw(_Boom("transient"))
    )
    with pytest.raises(_Boom):
        handlers._clients()
    assert handlers._store is None and handlers._broker is None, (
        f"_store={handlers._store!r} _broker={handlers._broker!r} after a failed "
        "construction. Assigning the store before the broker exists is what "
        "made the guard skip re-initialisation."
    )


def test_the_guard_checks_both_globals(handlers, monkeypatch) -> None:
    """Belt and braces: even if one is cleared by hand, both are rebuilt."""
    handlers._clients()
    handlers._broker = None
    _, broker = handlers._clients()
    assert broker is not None, (
        "With _store set and _broker cleared, _clients returned a None broker — "
        "the guard is looking at only one of the two values it assigns."
    )
