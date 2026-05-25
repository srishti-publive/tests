from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_client = None
_trace_ids: dict[str, bool] = {}   # keyed by publisher_id


def _get_client():
    global _client
    if _client is None and os.environ.get("LANGFUSE_PUBLIC_KEY"):
        try:
            from langfuse import Langfuse
            _client = Langfuse(
                public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
        except Exception as exc:
            logger.warning("Langfuse init failed (tracing disabled): %s", exc)
    return _client


def start_mcp_trace(publisher_id: str) -> None:
    try:
        lf = _get_client()
        if not lf:
            return
        _trace_ids[publisher_id] = True
    except Exception as exc:
        logger.warning("start_mcp_trace failed (non-fatal): %s", exc)


def record_tool_call(
    publisher_id: str,
    tool_name: str,
    args: dict,
    result,
    error: str | None,
    duration_ms: float,
) -> None:
    # !! NEVER raise from here — a tracing failure must not kill the MCP response !!
    try:
        lf = _get_client()
        if not lf:
            return
        if publisher_id not in _trace_ids:
            start_mcp_trace(publisher_id)

        output_preview = str(result)[:500] if result is not None else None

        if hasattr(lf, "start_as_current_span"):
            # Langfuse v3
            with lf.start_as_current_span(name=f"tools/call.{tool_name}") as span:
                lf.update_current_trace(
                    name="mcp_session",
                    metadata={"publisher_id": publisher_id},
                    tags=["mcp", "publive-cds"],
                )
                span.update(
                    input={"tool": tool_name, "args_keys": sorted(args.keys())},
                    output={"preview": output_preview} if output_preview else None,
                    metadata={"duration_ms": duration_ms, "error": error},
                )
        elif hasattr(lf, "trace"):
            # Langfuse v2
            trace = lf.trace(
                name="mcp_session",
                metadata={"publisher_id": publisher_id},
                tags=["mcp", "publive-cds"],
            )
            trace.span(
                name=f"tools/call.{tool_name}",
                input={"tool": tool_name, "args_keys": sorted(args.keys())},
                output={"preview": output_preview} if output_preview else None,
                metadata={"duration_ms": duration_ms, "error": error},
                level="ERROR" if error else "DEFAULT",
            )
        else:
            logger.warning("Langfuse API not recognised — skipping trace")
            return

        lf.flush()

    except Exception as exc:
        logger.warning("record_tool_call tracing failed (non-fatal): %s", exc)
