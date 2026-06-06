# Thin re-export shim — use mcp_app.cds directly in new code.
from .cds import TOOLS, dispatch_cds_tool  # noqa: F401

call_tool = dispatch_cds_tool  # backward-compat alias
