"""
Shared utilities for mcp_app.

Consolidates helpers that were previously copy-pasted across
cds_client.py, cms_client.py, views.py, and auth_app/services.py.

Imports from this module:
    from .utils import (
        CDS_BASE_URL,
        classify_error_category,
        extract_bearer_token,
        make_basic_token,
        require_publisher_id,
        slugify_path,
    )
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Optional

import requests

if TYPE_CHECKING:
    from django.http import HttpRequest


# ── Shared constants ──────────────────────────────────────────────────────────

# Single source of truth for the CDS base URL.
# Used by cds_client.py (all tool calls) AND auth_app/services.py (credential
# validation).  Change this one string to switch both callers to a new host.
CDS_BASE_URL = "https://cds-beta.thepublive.com/publisher/{publisher_id}"

_NO_PUBLISHER_ID_MSG = "No publisher ID in credentials — please re-authenticate"


# ── Auth helpers ──────────────────────────────────────────────────────────────

def make_basic_token(api_key: str, api_secret: str) -> str:
    """Return a Base64-encoded Basic auth token from API key + secret.

    All three call sites (cds_client, cms_client, auth_app/services) previously
    inlined this formula.  One definition means one place to change if the auth
    scheme is ever updated.
    """
    return base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()


def require_publisher_id(credentials: dict) -> str:
    """Return publisher_id from credentials, or raise with a standard message.

    Previously duplicated verbatim in cds_client.cds_get and cms_client._base_url,
    including the identical exception string.
    """
    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception(_NO_PUBLISHER_ID_MSG)
    return publisher_id


def extract_bearer_token(request: "HttpRequest") -> Optional[str]:
    """Return the raw Bearer token from the Authorization header, or None.

    Previously duplicated in views._get_credentials and views._get_session_id.
    """
    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return None


# ── Path helpers ──────────────────────────────────────────────────────────────

def slugify_path(path: str) -> str:
    """Convert a URL path to a flat NR transaction-name segment.

    Example: /posts/trending/ → posts_trending

    Previously duplicated verbatim in cds_client and cms_client.
    """
    slug = path.strip("/").replace("/", "_")
    return slug or "root"


# ── Error classification ──────────────────────────────────────────────────────

def classify_error_category(exc: Exception, http_status: Optional[int] = None) -> str:
    """Map an exception to a standard error.category string.

    Single source of truth used by cds_client, cms_client, and views so that
    NRQL queries can FACET on one consistent error.category value across all layers.

    Previously three separate functions existed with divergent labels:
      • views._classify_tool_error   → "client_error" for all 4xx
      • cds_client._cds_error_category → "client_error" for all 4xx
      • cms_client._cms_error_category → "bad_request" / "not_found" for 4xx

    Unified categories (pick the more specific when relevant):
      timeout        — requests.Timeout, HTTP 408, or message contains "timeout"
      auth_error     — HTTP 401
      not_found      — HTTP 404
      client_error   — HTTP 4xx (excluding 401, 404, 408)
      upstream_error — HTTP 5xx
      system_error   — anything else
    """
    if http_status is None:
        http_status = getattr(getattr(exc, "response", None), "status_code", None)

    if isinstance(exc, requests.exceptions.Timeout) or http_status == 408:
        return "timeout"
    exc_str = str(exc).lower()
    if "timeout" in exc_str or "timed out" in exc_str:
        return "timeout"
    if http_status == 401:
        return "auth_error"
    if http_status == 404:
        return "not_found"
    if http_status and 400 <= http_status < 500:
        return "client_error"
    if http_status and 500 <= http_status < 600:
        return "upstream_error"
    return "system_error"
