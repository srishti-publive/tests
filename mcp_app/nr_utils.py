"""Guarded wrappers around the New Relic Python agent API (no-op when agent absent)."""

import contextlib
import os

try:
    import newrelic.agent as _nr
except ImportError:
    _nr = None

# ── Deployment-level constants ────────────────────────────────────────────────
# Imported by views.py, prompt_capture.py, and auth_app/views.py so every
# custom event automatically carries environment and version tags.
SERVER_VERSION: str = os.environ.get("SERVER_VERSION", "1.0.0")
SERVER_ENV: str = os.environ.get(
    "RAILWAY_ENVIRONMENT",
    os.environ.get("DJANGO_ENV", "unknown"),
)


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


def add_span_attrs(pairs: list[tuple]) -> None:
    """Attach key-value pairs to the current active span (visible in trace waterfall).

    Unlike add_attrs() which attaches to the transaction (queryable via NRQL),
    span attributes appear in the Distributed Tracing waterfall on the individual
    span — useful for per-call context that would be too noisy as transaction attrs.
    Requires the NR agent to be active; no-ops when absent.
    """
    if _nr is None:
        return
    for key, value in pairs:
        _nr.add_custom_span_attribute(key, value)


def notice_err(exc=None, attrs=None) -> None:
    """Report an exception to New Relic with optional error-specific attributes.

    attrs are attached directly to the error event (visible in FROM TransactionError)
    via notice_error(attributes=...).  Previously they were added to the transaction
    only, which meant they were absent from error-event queries.
    """
    if not _current_transaction():
        return
    attr_dict = dict(attrs) if attrs else None
    _nr.notice_error(exc, attributes=attr_dict)


def record_event(event_type: str, params: dict) -> None:
    """Record a custom event (does not require an active transaction)."""
    if _nr is None:
        return
    _nr.record_custom_event(event_type, params)


def record_metric(name: str, value: float) -> None:
    """Record a custom metric (guarded no-op when agent absent).

    Prefer this over direct newrelic.agent.record_custom_metric() calls so that
    tests and local dev without the NR agent do not crash.
    """
    if _nr is None:
        return
    _nr.record_custom_metric(name, value)


def get_linking_metadata() -> dict:
    """Return current trace.id and span.id for custom event ↔ APM trace correlation.

    Returns empty dict when called outside a transaction (e.g. from event_stream()).
    Keys: "trace.id", "span.id", "entity.guid", "entity.name", "entity.type".
    """
    if _nr is None:
        return {}
    return _nr.get_linking_metadata()


def suppress_apdex() -> None:
    """Suppress Apdex scoring for the current transaction.

    Call for long-running or synthetic transactions (SSE sessions, health checks)
    that would permanently skew the Apdex score if counted.
    """
    if _current_transaction() and _nr is not None:
        _nr.suppress_apdex_metric()


def suppress_trace() -> None:
    """Suppress slow-transaction trace collection for the current transaction.

    Prevents long-lived SSE sessions from flooding the slow-transaction trace list.
    """
    if _current_transaction() and _nr is not None:
        _nr.suppress_transaction_trace()


@contextlib.contextmanager
def fn_trace(name: str, group: str = "Function"):
    txn = _current_transaction()
    if txn:
        with _nr.FunctionTrace(name=name, group=group):
            yield
    else:
        yield
