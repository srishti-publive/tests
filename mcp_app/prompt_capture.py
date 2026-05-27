"""
Extract user/LLM prompt text from MCP requests and record to New Relic.

Clients can send prompts via (first match wins):
  - HTTP header X-MCP-Prompt (or X-User-Prompt, X-Prompt-Text, X-LLM-Prompt)
  - JSON-RPC _meta.prompt / _meta.userMessage on the request or params
  - params.prompt
  - arguments._prompt or arguments.prompt (stripped before the tool runs)
  - Fallback: JSON snapshot of tool arguments (source: tool_args)
"""

from __future__ import annotations

import json
import uuid
from typing import Optional, Tuple

from .nr_utils import add_attrs, get_linking_metadata, record_event

MAX_PROMPT_LEN = 2000

_PROMPT_HEADERS = (
    "HTTP_X_MCP_PROMPT",
    "HTTP_X_USER_PROMPT",
    "HTTP_X_PROMPT_TEXT",
    "HTTP_X_LLM_PROMPT",
)

_META_PROMPT_KEYS = ("prompt", "userMessage", "user_message", "message")


def _truncate(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= MAX_PROMPT_LEN:
        return text
    return text[: MAX_PROMPT_LEN - 3] + "..."


def _prompt_from_meta(meta) -> Optional[Tuple[str, str]]:
    if not isinstance(meta, dict):
        return None
    for key in _META_PROMPT_KEYS:
        val = meta.get(key)
        if val is not None and str(val).strip():
            return str(val).strip(), f"meta.{key}"
    return None


def extract_prompt_for_tool_call(request, body: dict, params: dict) -> tuple[str, str, str, dict]:
    """
    Return (prompt_id, prompt_text, prompt_source, arguments).
    arguments may have _prompt / prompt removed so tools do not receive them.
    """
    prompt_id = str(uuid.uuid4())
    args = dict(params.get("arguments") or {})

    if request is not None:
        for header in _PROMPT_HEADERS:
            raw = request.META.get(header)
            if raw and str(raw).strip():
                return prompt_id, _truncate(str(raw)), "header", args

    for meta in (body.get("_meta"), params.get("_meta")):
        found = _prompt_from_meta(meta)
        if found:
            return prompt_id, _truncate(found[0]), found[1], args

    raw = params.get("prompt")
    if raw is not None and str(raw).strip():
        return prompt_id, _truncate(str(raw)), "params.prompt", args

    for key in ("_prompt", "prompt"):
        if key in args:
            text = str(args.pop(key)).strip()
            if text:
                return prompt_id, _truncate(text), f"arguments.{key}", args

    if args:
        preview = json.dumps(args, ensure_ascii=False, default=str)
        return prompt_id, _truncate(preview), "tool_args", args

    # No arguments and no prompt from any source.
    # The client (e.g. Claude desktop) called this tool without forwarding any
    # user prompt context — this is expected MCP behaviour, not an error.
    # Label is "client_not_provided" (not "none") so dashboards read clearly.
    tool_name = params.get("name", "unknown_tool")
    return prompt_id, f"[{tool_name}]", "client_not_provided", args


def record_prompt_observability(
    *,
    prompt_id: str,
    prompt_text: str,
    prompt_source: str,
    session_id: str,
    tool_name: str,
    jsonrpc_id,
    request,
    credentials: Optional[dict],
) -> None:
    """Attach prompt attrs to the transaction and emit MCPPrompt custom event."""
    publisher_id = (credentials or {}).get("publisherId", "unknown")
    client_name = "unknown"
    if request is not None:
        client_name = request.META.get("HTTP_USER_AGENT", "unknown")[:200]

    # Proxy for AI token usage: character count ÷ 4 ≈ token count (GPT/Claude heuristic).
    # This server is a tool server — actual token consumption happens in the AI client.
    # These fields give a cost-proxy without requiring a tokenizer dependency.
    char_count = len(prompt_text)
    estimated_tokens = max(1, char_count // 4)

    pairs = [
        ("mcp.prompt_id", prompt_id),
        ("mcp.prompt_text", prompt_text),
        ("mcp.prompt_source", prompt_source),
        ("mcp.session_id", session_id or ""),
        ("mcp.tool_name", tool_name),
        ("mcp.prompt_char_count", char_count),
        ("mcp.estimated_prompt_tokens", estimated_tokens),
    ]
    if jsonrpc_id is not None:
        pairs.append(("mcp.jsonrpc_id", str(jsonrpc_id)))

    add_attrs(pairs)

    # Linking metadata: attaches this event to its NR APM trace so you can click
    # from an MCPPrompt event directly to the transaction trace waterfall.
    linking = get_linking_metadata()

    record_event("MCPPrompt", {
        "prompt_id": prompt_id,
        "prompt_text": prompt_text,
        "prompt_source": prompt_source,
        "session_id": session_id or "",
        "tool_name": tool_name,
        "publisher_id": publisher_id,
        "jsonrpc_id": str(jsonrpc_id) if jsonrpc_id is not None else "",
        "client_name": client_name,
        "prompt_char_count": char_count,
        "estimated_prompt_tokens": estimated_tokens,
        "trace_id": linking.get("trace.id", ""),
        "span_id": linking.get("span.id", ""),
    })
