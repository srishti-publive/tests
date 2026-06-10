"""Shared helpers used by both the CDS and CMS HTTP clients."""
import base64

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def build_pooled_session(pool_maxsize: int = 10) -> requests.Session:
    """Return a Session with keep-alive connection pooling to the upstream hosts.

    Reusing TLS connections saves a full handshake per tool call. The connect-only
    retry is safe for non-idempotent requests: a connection failure means the
    request never reached the server. Never set auth or Authorization on this
    session — credentials differ per user and must stay per-request.
    """
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=pool_maxsize,
        max_retries=Retry(total=1, connect=1, read=0, redirect=0, status=0),
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def slugify_url_path(path: str) -> str:
    """Convert a URL path to a flat slug for NR transaction naming."""
    slug = path.strip("/").replace("/", "_")
    return slug or "root"


def build_base_url(template: str, credentials: dict) -> str:
    """Resolve a publisher-scoped base URL from credentials."""
    publisher_id = credentials.get("publisherId", "")
    if not publisher_id:
        raise Exception("No publisher ID in credentials — please re-authenticate")
    return template.format(publisher_id=publisher_id)


def build_basic_auth_headers(credentials: dict) -> dict:
    """Return Authorization + Content-Type headers for Basic Auth."""
    api_key    = credentials.get("apiKey", "")
    api_secret = credentials.get("apiSecret", "")
    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    }
