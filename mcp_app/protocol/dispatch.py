"""JSON-RPC dispatcher — routes initialize / tools/list / tools/call to the right handler."""
import json
import logging
import time

from mcp_app.cds import TOOLS, dispatch_cds_tool
from mcp_app.cms import CMS_TOOL_NAMES, CMS_TOOLS, dispatch_cms_tool
from mcp_app.nr_utils import (
    SERVER_ENV,
    SERVER_VERSION,
    add_attrs,
    get_linking_metadata,
    record_event,
    record_metric,
    set_txn_name,
)
from mcp_app.prompt_capture import extract_prompt_for_tool_call, record_prompt_observability

from .session import SESSION_PROTOCOL_KEY, should_emit_prompt_event
from .session_store import session_stats, session_stats_lock

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"

# ── Input schema validation ───────────────────────────────────────────────────

# Pre-built name → inputSchema lookup for all 71 tools
_SCHEMA_REGISTRY: dict = {
    tool["name"]: tool.get("inputSchema", {})
    for tool in (TOOLS + CMS_TOOLS)
}

_JSON_TYPE_MAP: dict = {
    "string":  str,
    "integer": int,
    "boolean": bool,
    "object":  dict,
    "array":   list,
    "number":  (int, float),
}


def _validate_tool_args(name: str, args: dict) -> dict:
    """Validate args against the tool's inputSchema. Returns an error dict or None."""
    schema = _SCHEMA_REGISTRY.get(name)
    if not schema:
        return None

    properties: dict = schema.get("properties", {})
    required: list   = schema.get("required", [])

    # Required field check
    for field in required:
        if field not in args or args[field] is None or args[field] == "":
            return {
                "error_type": "invalid_params",
                "message": f"Required field '{field}' is missing.",
                "retryable": False,
            }

    # Type + constraint check for every provided field
    for field, value in args.items():
        if value is None:
            continue
        prop = properties.get(field)
        if not prop:
            continue  # extra fields are tolerated

        expected_type = prop.get("type")
        if expected_type and expected_type in _JSON_TYPE_MAP:
            expected = _JSON_TYPE_MAP[expected_type]
            # bool is a subclass of int in Python — reject bool for integer fields
            if expected_type == "integer" and isinstance(value, bool):
                return {
                    "error_type": "invalid_params",
                    "message": f"Field '{field}' must be an integer, got boolean.",
                    "retryable": False,
                }
            if not isinstance(value, expected):
                return {
                    "error_type": "invalid_params",
                    "message": (
                        f"Field '{field}' must be of type {expected_type}, "
                        f"got {type(value).__name__}."
                    ),
                    "retryable": False,
                }

        # minLength for string fields
        min_length = prop.get("minLength")
        if min_length is not None and isinstance(value, str) and len(value) < min_length:
            return {
                "error_type": "invalid_params",
                "message": f"Field '{field}' must be at least {min_length} character(s) long.",
                "retryable": False,
            }

    return None

_UNIMPLEMENTED_METHODS: frozenset[str] = frozenset({
    "sampling/createMessage",
    "roots/list",
    "resources/list",
    "resources/read",
    "resources/subscribe",
    "resources/unsubscribe",
    "prompts/list",
    "prompts/get",
    "completion/complete",
    "logging/setLevel",
})


def jsonrpc_ok(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def jsonrpc_error(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def classify_tool_error(exc) -> str:
    """Map a tool exception to a standard error.category string."""
    http_status = getattr(getattr(exc, "response", None), "status_code", None)
    exc_lower   = str(exc).lower()
    if http_status == 408 or "timeout" in exc_lower or "timed out" in exc_lower:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status and 400 <= http_status < 500:
        return "client_error"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"


_CMS_READ_PREFIXES = ("list_", "get_", "validate_")

def _is_cms_write(name: str) -> bool:
    """True for CMS tools that mutate data (create / update / delete / register / add / submit)."""
    return name in CMS_TOOL_NAMES and not any(name.startswith(p) for p in _CMS_READ_PREFIXES)


def _record_client_attrs(request) -> None:
    from .auth import identify_mcp_client
    if request is None:
        return
    client_name, client_version = identify_mcp_client(request)
    add_attrs([("mcp.client_name", client_name), ("mcp.client_version", client_version)])


def _record_session_protocol(request) -> None:
    if request is None:
        return
    protocol_version = request.session.get(SESSION_PROTOCOL_KEY)
    if protocol_version:
        add_attrs([("mcp.protocol_version", protocol_version)])


def dispatch_jsonrpc(body: dict, credentials: dict, request=None, session_id=None, token_expires_at=None):
    """Route a single JSON-RPC body to the correct handler and return the response dict."""
    method = body.get("method", "")
    id_    = body.get("id")

    if request is not None:
        _record_client_attrs(request)
        _record_session_protocol(request)

    if id_ is None:
        logger.debug("MCP notification: method=%s (no response)", method)
        return None

    # ── initialize ────────────────────────────────────────────────────────────
    if method == "initialize":
        set_txn_name("MCP/initialize", group="MCP")
        add_attrs([("mcp.protocol_version", PROTOCOL_VERSION)])
        if request is not None:
            request.session[SESSION_PROTOCOL_KEY] = PROTOCOL_VERSION
        logger.info("MCP initialize: session=%s protocol=%s", session_id, PROTOCOL_VERSION)
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities":    {"tools": {}},
            "serverInfo":      {"name": "publive-cds", "version": "1.0.0"},
        }
        if token_expires_at is not None:
            result["tokenExpiresAt"] = token_expires_at.isoformat()
        return jsonrpc_ok(id_, result)

    # ── tools/list ────────────────────────────────────────────────────────────
    if method == "tools/list":
        set_txn_name("MCP/tools_list", group="MCP")
        all_tools = TOOLS + CMS_TOOLS
        logger.debug("MCP tools/list: session=%s count=%d", session_id, len(all_tools))
        return jsonrpc_ok(id_, {"tools": all_tools})

    # ── tools/call ────────────────────────────────────────────────────────────
    if method == "tools/call":
        return _handle_tool_call(body, credentials, request, session_id, id_)

    # ── ping ──────────────────────────────────────────────────────────────────
    if method == "ping":
        set_txn_name("MCP/ping", group="MCP")
        logger.debug("MCP ping: session=%s", session_id)
        return jsonrpc_ok(id_, {})

    # ── known-but-unimplemented ───────────────────────────────────────────────
    if method in _UNIMPLEMENTED_METHODS:
        logger.debug("MCP method not implemented (expected): method=%s session=%s", method, session_id)
        add_attrs([("mcp.jsonrpc_error_code", -32601)])
        return jsonrpc_error(id_, -32601, f"Method not found: {method}")

    # ── truly unknown ─────────────────────────────────────────────────────────
    logger.warning("MCP unknown method: method=%s session=%s id=%s", method, session_id, id_)
    add_attrs([("mcp.jsonrpc_error_code", -32601), ("mcp.unknown_method", method)])
    record_event("MCPUnknownMethod", {
        "method":     method,
        "session_id": session_id or "",
        "jsonrpc_id": str(id_) if id_ is not None else "",
        "env":            SERVER_ENV,
        "server_version": SERVER_VERSION,
    })
    return jsonrpc_error(id_, -32601, f"Method not found: {method}")


def _handle_tool_call(body: dict, credentials: dict, request, session_id, id_) -> dict:
    """Execute a single tools/call request; emit full NR observability."""
    params = body.get("params", {})
    name   = params.get("name", "")
    prompt_id, prompt_text, prompt_source, args = extract_prompt_for_tool_call(request, body, params)

    logger.info(
        "MCP tools/call: tool=%s session=%s prompt_source=%s args_count=%d",
        name, session_id, prompt_source, len(args) if args else 0,
    )

    if should_emit_prompt_event():
        record_prompt_observability(
            prompt_id=prompt_id, prompt_text=prompt_text, prompt_source=prompt_source,
            session_id=session_id or "", tool_name=name, jsonrpc_id=id_,
            request=request, credentials=credentials,
        )
    else:
        add_attrs([
            ("mcp.prompt_id",     prompt_id),
            ("mcp.prompt_text",   prompt_text),
            ("mcp.prompt_source", prompt_source),
            ("mcp.session_id",    session_id or ""),
            ("mcp.tool_name",     name),
        ])
        record_metric("Custom/MCP/prompt_event_dropped_count", 1)
        logger.warning("MCPPrompt event dropped (rate limit): tool=%s session=%s", name, session_id)

    tool_input = json.dumps(args, ensure_ascii=False, default=str)[:500] if args else ""
    add_attrs([("mcp.tool_name", name), ("mcp.tool_input", tool_input)])
    record_metric(f"Custom/Tool/{name}/call_count", 1)
    record_metric("Custom/MCP/tool_call_count",    1)

    # Validate arguments against the tool's inputSchema before dispatching
    validation_error = _validate_tool_args(name, args or {})
    if validation_error:
        add_attrs([("mcp.tool_result_status", "invalid_params"), ("mcp.tool_is_error", True)])
        record_metric("Custom/MCP/tool_validation_error_count", 1)
        logger.warning(
            "MCP tools/call validation error: tool=%s session=%s message=%s",
            name, session_id, validation_error.get("message"),
        )
        return jsonrpc_ok(id_, {
            "content": [{"type": "text", "text": json.dumps(validation_error)}],
            "isError": True,
        })

    # Session timeline (SSE sessions only — HTTP sessions have no entry)
    start_offset_ms = ai_think_ms = None
    prompt_tokens   = max(1, len(prompt_text) // 4)
    session_trace_id = ""

    with session_stats_lock:
        stats = session_stats.get(session_id or "")
        if stats is not None:
            sess_start = stats.get("session_start_time")
            last_end   = stats.get("last_tool_end_perf")
            if sess_start is not None:
                start_offset_ms = round((time.perf_counter() - sess_start) * 1000, 2)
            if last_end is not None:
                ai_think_ms = round((time.perf_counter() - last_end) * 1000, 2)
            if stats.get("client_name") is None and request is not None:
                stats["client_name"] = request.META.get("HTTP_USER_AGENT", "unknown")[:200]
            session_trace_id = stats.get("session_trace_id", "")

    if start_offset_ms is not None:
        add_attrs([("mcp.tool_start_offset_ms", start_offset_ms)])
    if ai_think_ms is not None:
        add_attrs([("mcp.ai_think_time_ms", ai_think_ms)])

    # Per-session CMS write-op rate limit (SSE sessions only)
    if _is_cms_write(name):
        with session_stats_lock:
            write_stats = session_stats.get(session_id or "")
            if write_stats is not None:
                write_stats["write_op_count"] += 1
                write_op_count = write_stats["write_op_count"]
            else:
                write_op_count = 0
        if write_op_count > 50:
            return jsonrpc_ok(id_, {"content": [{"type": "text", "text": json.dumps({
                "error_type": "rate_limit",
                "message": "Write operation limit (50) reached for this session. Start a new session to continue making changes.",
                "retryable": False,
            })}]})

    t0 = time.perf_counter()
    try:
        result      = dispatch_cms_tool(credentials, name, args) if name in CMS_TOOL_NAMES else dispatch_cds_tool(credentials, name, args)
        duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        set_txn_name(f"MCP/{name}", group="MCP")
        output_text   = json.dumps(result, indent=2) if result else ""
        result_size   = len(output_text)
        output_tokens = max(1, result_size // 4)
        add_attrs([("mcp.estimated_output_tokens", output_tokens)])

        linking   = get_linking_metadata()
        trace_id  = linking.get("trace.id", "")
        span_id   = linking.get("span.id", "")

        degraded_reason = (result.get("error") or result.get("error_type")) if isinstance(result, dict) else None
        is_degraded     = bool(degraded_reason)

        with session_stats_lock:
            upd = session_stats.get(session_id or "")
            if upd is not None:
                upd["total_tool_duration_ms"]         = upd.get("total_tool_duration_ms", 0.0) + duration_ms
                upd["total_estimated_input_tokens"]   = upd.get("total_estimated_input_tokens", 0) + prompt_tokens
                upd["total_estimated_output_tokens"]  = upd.get("total_estimated_output_tokens", 0) + output_tokens
                upd["last_tool_end_perf"]             = time.perf_counter()
                upd.setdefault("tool_sequence", []).append(name)
                if is_degraded:
                    upd["degraded_count"] = upd.get("degraded_count", 0) + 1

        if is_degraded:
            add_attrs([
                ("mcp.tool_result_status",   "degraded"),
                ("mcp.tool_is_degraded",     True),
                ("mcp.tool_is_error",        False),
                ("mcp.degraded_reason",      degraded_reason),
                ("mcp.tool_args_count",      len(args) if args else 0),
                ("mcp.tool_response_size",   result_size),
                ("mcp.tool_duration_ms",     duration_ms),
                ("mcp.tool_output_preview",  output_text[:500]),
                ("mcp.tool_output_char_count", result_size),
            ])
            record_metric(f"Custom/Tool/{name}/degraded_count", 1)
            record_metric("Custom/MCP/tool_degraded_count",      1)
            record_metric(f"Custom/Tool/{name}/duration_ms",      duration_ms)
            record_event("MCPToolDegraded", {
                "tool_name":           name,
                "publisher_id":        (credentials or {}).get("publisherId", "unknown"),
                "degraded_reason":     degraded_reason,
                "session_id":          session_id or "",
                "session_trace_id":    session_trace_id,
                "prompt_id":           prompt_id,
                "duration_ms":         duration_ms,
                "tool_input":          tool_input,
                "trace_id":            trace_id,
                "span_id":             span_id,
                "tool_start_offset_ms": start_offset_ms or 0,
                "ai_think_time_ms":    ai_think_ms or 0,
                "env":                 SERVER_ENV,
                "server_version":      SERVER_VERSION,
            })
            logger.warning("MCP tools/call degraded: tool=%s reason=%s duration_ms=%.2f", name, degraded_reason, duration_ms)
        else:
            add_attrs([
                ("mcp.tool_result_status",   "success"),
                ("mcp.tool_is_error",        False),
                ("mcp.tool_args_count",      len(args) if args else 0),
                ("mcp.tool_response_size",   result_size),
                ("mcp.tool_duration_ms",     duration_ms),
                ("mcp.tool_output_preview",  output_text[:500]),
                ("mcp.tool_output_char_count", result_size),
            ])
            record_metric(f"Custom/Tool/{name}/duration_ms",  duration_ms)
            record_metric("Custom/MCP/tool_success_count",     1)
            logger.info("MCP tools/call success: tool=%s duration_ms=%.2f response_size=%d", name, duration_ms, result_size)

        return jsonrpc_ok(id_, {"content": [{"type": "text", "text": output_text}]})

    except Exception as exc:
        duration_ms    = round((time.perf_counter() - t0) * 1000, 2)
        set_txn_name(f"MCP/{name}", group="MCP")
        error_category = classify_tool_error(exc)
        linking        = get_linking_metadata()
        trace_id       = linking.get("trace.id", "")
        span_id        = linking.get("span.id",  "")

        add_attrs([
            ("mcp.tool_result_status",  "error"),
            ("mcp.tool_is_error",       True),
            ("mcp.tool_args_count",     len(args) if args else 0),
            ("mcp.tool_response_size",  0),
            ("mcp.tool_duration_ms",    duration_ms),
            ("mcp.tool_output_preview", str(exc)[:500]),
            ("mcp.error_category",      error_category),
        ])

        with session_stats_lock:
            upd = session_stats.get(session_id or "")
            if upd is not None:
                upd["total_tool_duration_ms"]        = upd.get("total_tool_duration_ms", 0.0) + duration_ms
                upd["total_estimated_input_tokens"]  = upd.get("total_estimated_input_tokens", 0) + prompt_tokens
                upd["last_tool_end_perf"]            = time.perf_counter()
                upd.setdefault("tool_sequence", []).append(name)

        record_metric("Custom/MCP/tool_error_count",             1)
        record_metric(f"Custom/Tool/{name}/error_count",          1)
        record_metric(f"Custom/Tool/{name}/error_duration_ms",    duration_ms)
        record_event("MCPToolError", {
            "tool_name":           name,
            "publisher_id":        (credentials or {}).get("publisherId", "unknown"),
            "error_type":          type(exc).__name__,
            "error_message":       str(exc)[:500],
            "error_category":      error_category,
            "session_id":          session_id or "",
            "session_trace_id":    session_trace_id,
            "prompt_id":           prompt_id,
            "prompt_text":         prompt_text[:500],
            "duration_ms":         duration_ms,
            "tool_input":          tool_input,
            "trace_id":            trace_id,
            "span_id":             span_id,
            "tool_start_offset_ms": start_offset_ms or 0,
            "ai_think_time_ms":    ai_think_ms or 0,
            "env":                 SERVER_ENV,
            "server_version":      SERVER_VERSION,
        })
        logger.error(
            "MCP tools/call error: tool=%s session=%s category=%s error=%s duration_ms=%.2f",
            name, session_id, error_category, exc, duration_ms, exc_info=True,
        )
        return jsonrpc_ok(id_, {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True})
