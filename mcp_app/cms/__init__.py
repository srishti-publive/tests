"""CMS tool package — aggregates schemas and dispatches tool calls by name."""
import collections
import logging
import threading

import newrelic.agent

from ..nr_utils            import add_attrs, fn_trace, notice_err
from .categories           import HANDLERS as _CAT_H,    SCHEMAS as _CAT_S
from .custom_components    import HANDLERS as _CC_H,     SCHEMAS as _CC_S
from .custom_content_types import HANDLERS as _CCT_H,    SCHEMAS as _CCT_S
from .forms                import HANDLERS as _FORMS_H,  SCHEMAS as _FORMS_S
from .live_blog            import HANDLERS as _LB_H,     SCHEMAS as _LB_S
from .media                import HANDLERS as _MEDIA_H,  SCHEMAS as _MEDIA_S
from .posts                import HANDLERS as _POSTS_H,  SCHEMAS as _POSTS_S
from .tags                 import HANDLERS as _TAGS_H,   SCHEMAS as _TAGS_S
from .validators           import HANDLERS as _VAL_H,    SCHEMAS as _VAL_S

logger = logging.getLogger(__name__)

CMS_TOOLS: list[dict] = (
    _CAT_S + _TAGS_S + _POSTS_S + _LB_S
    + _CC_S + _CCT_S + _VAL_S + _MEDIA_S
    + _FORMS_S
)

_HANDLER_REGISTRY: dict = {
    **_CAT_H, **_TAGS_H, **_POSTS_H, **_LB_H,
    **_CC_H,  **_CCT_H,  **_VAL_H,   **_MEDIA_H,
    **_FORMS_H,
}

# Public set used by the dispatcher to route tool calls without brittle prefix checks.
CMS_TOOL_NAMES: frozenset = frozenset(_HANDLER_REGISTRY.keys())

_active_calls:      dict[str, int] = collections.defaultdict(int)
_active_calls_lock: threading.Lock = threading.Lock()


@newrelic.agent.function_trace(name="dispatch_cms_tool", group="Tool")
def dispatch_cms_tool(credentials: dict, name: str, args: dict):
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
            logger.warning("dispatch_cms_tool: unknown tool=%s", name)
            raise Exception(f"Unknown CMS tool: {name}")

        with fn_trace(name, group="Tool"):
            return handler(credentials, args)

    except Exception as exc:
        notice_err(exc, [("error.layer", "cms_tool"), ("error.tool_name", name)])
        raise
    finally:
        with _active_calls_lock:
            _active_calls[name] = max(0, _active_calls[name] - 1)
