# Thin re-export shim — use mcp_app.cms directly in new code.
from .cms import CMS_TOOLS, dispatch_cms_tool  # noqa: F401
call_cms_tool = dispatch_cms_tool  # backward-compat alias
