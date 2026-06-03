"""Shared helpers used by both the CDS and CMS HTTP clients."""
import base64


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
