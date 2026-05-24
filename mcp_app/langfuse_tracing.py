from __future__ import annotations

import os

_client = None
_traces = {}   # keyed by publisher_id so session spans survive across HTTP requests


def _get_client():
    global _client
    if _client is None and os.environ.get("LANGFUSE_PUBLIC_KEY"):
        from langfuse import Langfuse
        _client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
    return _client


def start_mcp_trace(publisher_id: str) -> None:
    lf = _get_client()
    if not lf:
        return
    _traces[publisher_id] = lf.trace(
        name="mcp_session",
        metadata={"publisher_id": publisher_id},
        tags=["mcp", "publive-cds"],
    )


def record_tool_call(
    publisher_id: str,
    tool_name: str,
    args: dict,
    result,
    error: str | None,
    duration_ms: float,
) -> None:
    lf = _get_client()
    if not lf:
        return
    if publisher_id not in _traces:
        start_mcp_trace(publisher_id)

    output_preview = str(result)[:500] if result is not None else None
    _traces[publisher_id].span(
        name=f"tools/call.{tool_name}",
        input={"tool": tool_name, "args_keys": sorted(args.keys())},
        output={"preview": output_preview} if output_preview else None,
        metadata={"duration_ms": duration_ms, "error": error},
        level="ERROR" if error else "DEFAULT",
    )
    lf.flush()
