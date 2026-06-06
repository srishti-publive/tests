"""CDS tool package — aggregates schemas and dispatches tool calls by name."""
import collections
import logging
import threading

import newrelic.agent

from mcp_app.nr_utils import add_attrs, fn_trace, notice_err

from .authors import HANDLERS as _AUTHORS_HANDLERS
from .authors import SCHEMAS as _AUTHORS_SCHEMAS
from .categories import HANDLERS as _CATEGORIES_HANDLERS
from .categories import SCHEMAS as _CATEGORIES_SCHEMAS
from .content import HANDLERS as _CONTENT_HANDLERS
from .content import SCHEMAS as _CONTENT_SCHEMAS
from .posts import HANDLERS as _POSTS_HANDLERS
from .posts import SCHEMAS as _POSTS_SCHEMAS
from .publisher import HANDLERS as _PUBLISHER_HANDLERS
from .publisher import SCHEMAS as _PUBLISHER_SCHEMAS
from .sitemaps import HANDLERS as _SITEMAPS_HANDLERS
from .sitemaps import SCHEMAS as _SITEMAPS_SCHEMAS
from .static_files import HANDLERS as _STATIC_HANDLERS
from .static_files import SCHEMAS as _STATIC_SCHEMAS
from .tags import HANDLERS as _TAGS_HANDLERS
from .tags import SCHEMAS as _TAGS_SCHEMAS

logger = logging.getLogger(__name__)

TOOLS: list[dict] = (
    _POSTS_SCHEMAS
    + _CATEGORIES_SCHEMAS
    + _TAGS_SCHEMAS
    + _AUTHORS_SCHEMAS
    + _PUBLISHER_SCHEMAS
    + _CONTENT_SCHEMAS
    + _SITEMAPS_SCHEMAS
    + _STATIC_SCHEMAS
)

_HANDLER_REGISTRY: dict = {
    **_POSTS_HANDLERS,
    **_CATEGORIES_HANDLERS,
    **_TAGS_HANDLERS,
    **_AUTHORS_HANDLERS,
    **_PUBLISHER_HANDLERS,
    **_CONTENT_HANDLERS,
    **_SITEMAPS_HANDLERS,
    **_STATIC_HANDLERS,
}

_active_calls:      dict[str, int] = collections.defaultdict(int)
_active_calls_lock: threading.Lock = threading.Lock()


@newrelic.agent.function_trace(name="dispatch_cds_tool", group="Tool")
def dispatch_cds_tool(credentials: dict, name: str, args: dict):
    """Resolve name → handler and execute; centralises concurrency tracking and error handling."""
    add_attrs([("mcp.tool_name", name)])
    args = args or {}

    with _active_calls_lock:
        _active_calls[name] += 1
        concurrency = _active_calls[name]
    add_attrs([("mcp.tool_concurrency", concurrency)])
    newrelic.agent.record_custom_metric(f"Custom/Tool/{name}/active_calls", concurrency)

    try:
        handler = _HANDLER_REGISTRY.get(name)
        if handler is None:
            logger.warning("dispatch_cds_tool: unknown tool=%s", name)
            raise Exception(f"Unknown tool: {name}")

        with fn_trace(name, group="Tool"):
            return handler(credentials, args)

    except Exception as exc:
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        if http_status == 401:
            logger.warning("dispatch_cds_tool: CDS rejected credentials (HTTP 401): tool=%s", name)
            add_attrs([("mcp.tool_auth_error", True), ("error.layer", "tool"), ("error.tool_name", name)])
            return {
                "error": "auth_expired",
                "message": (
                    "Your CDS credentials were rejected (HTTP 401). "
                    "Please re-authenticate: visit /connect or re-run the OAuth flow."
                ),
            }
        logger.error("dispatch_cds_tool error: tool=%s error=%s", name, exc, exc_info=True)
        notice_err(exc, [("error.layer", "tool"), ("error.tool_name", name)])
        raise
    finally:
        with _active_calls_lock:
            _active_calls[name] = max(0, _active_calls[name] - 1)
