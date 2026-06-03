# Thin re-export shim — use mcp_app.clients.cds directly in new code.
from .clients.cds import cds_get, classify_cds_error, is_retryable_cds_error  # noqa: F401
