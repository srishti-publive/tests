"""Guarded wrappers around the New Relic Python agent API (no-op when agent absent)."""

import contextlib

try:
    import newrelic.agent as _nr
except ImportError:
    _nr = None


def _current_transaction():
    if _nr is None:
        return None
    return _nr.current_transaction()


def set_txn_name(name: str, group: str = "Python") -> None:
    if _current_transaction():
        _nr.set_transaction_name(name, group)


def add_attrs(pairs: list[tuple]) -> None:
    if _current_transaction():
        _nr.add_custom_attributes(pairs)


def notice_err(exc=None, attrs=None) -> None:
    if not _current_transaction():
        return
    if attrs:
        _nr.add_custom_attributes(attrs)
    _nr.notice_error(exc)


def record_event(event_type: str, params: dict) -> None:
    """Record a custom event (does not require an active transaction)."""
    if _nr is None:
        return
    _nr.record_custom_event(event_type, params)


def get_linking_metadata() -> dict:
    """Return current trace.id and span.id for custom event ↔ APM trace correlation.

    Returns empty dict when called outside a transaction (e.g. from event_stream()).
    Keys: "trace.id", "span.id", "entity.guid", "entity.name", "entity.type".
    """
    if _nr is None:
        return {}
    return _nr.get_linking_metadata()


@contextlib.contextmanager
def fn_trace(name: str, group: str = "Function"):
    txn = _current_transaction()
    if txn:
        with _nr.FunctionTrace(name=name, group=group):
            yield
    else:
        yield
