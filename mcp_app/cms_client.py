# Thin re-export shim — use mcp_app.clients.cms directly in new code.
from .clients.cms import (  # noqa: F401
    classify_cms_error,
    cms_delete,
    cms_get,
    cms_patch,
    cms_post,
    normalize_cms_error,
)
