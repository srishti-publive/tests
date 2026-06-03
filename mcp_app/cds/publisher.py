"""CDS read tools for publisher configuration: branding, navigation, and newsletters."""
import logging

import newrelic.agent

from ..clients.cds import cds_get
from ..nr_utils import add_attrs

logger = logging.getLogger(__name__)

SCHEMAS = [
    {
        "name": "fetch_publisher_profile",
        "description": "Get publisher profile: branding, logo, accent colors, social links, app store URLs, and site metadata. Always call this first for any branding or publisher identity question — it automatically falls back to footer data if the primary endpoint is unavailable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_site_navigation",
        "description": "Get the navigation menu configuration including nested menu items and links.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_site_footer",
        "description": "Get the footer layout: menus, links, copyright text, app store URLs, social links, and logo. Use this for footer structure and navigation links. For publisher branding questions (logo, colors, identity), prefer fetch_publisher_profile which aggregates from multiple sources.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_newsletter_groups",
        "description": (
            "Get all configured newsletter groups with their metadata, logos, and descriptions. "
            "NOTE: this only works for publishers that have a newsletter email configured. "
            "If the publisher has no newsletter set up, this tool returns a not_configured error — "
            "do not retry in that case."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def fetch_publisher_profile(credentials: dict, args: dict):
    try:
        return cds_get(credentials, "/publisher-data/")
    except Exception as exc:
        err_str     = str(exc).lower()
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        is_missing  = (
            http_status in (400, 404)
            or "unknown endpoint" in err_str
            or "not found"        in err_str
            or "http 404"         in err_str
            or "http 400"         in err_str
            or "no such"          in err_str
        )
        if is_missing:
            publisher_id = (credentials or {}).get("publisherId", "unknown")
            logger.warning("fetch_publisher_profile: /publisher-data/ unavailable for publisher=%s — falling back to /footer/", publisher_id)
            add_attrs([("mcp.tool_fallback", "footer"), ("mcp.tool_fallback_reason", "endpoint_unavailable")])
            newrelic.agent.record_custom_metric("Custom/MCP/fallback_count", 1)
            return cds_get(credentials, "/footer/")
        raise


def fetch_site_navigation(credentials: dict, args: dict):
    return cds_get(credentials, "/navbar/")


def fetch_site_footer(credentials: dict, args: dict):
    return cds_get(credentials, "/footer/")


def fetch_newsletter_groups(credentials: dict, args: dict):
    try:
        return cds_get(credentials, "/newsletter-groups/")
    except Exception as exc:
        err_str          = str(exc).lower()
        http_status      = getattr(getattr(exc, "response", None), "status_code", None)
        is_not_configured = (
            http_status in (400, 404)
            or "newsletter"       in err_str
            or "email"            in err_str
            or "not configured"   in err_str
            or "unknown endpoint" in err_str
        )
        if is_not_configured:
            publisher_id = (credentials or {}).get("publisherId", "unknown")
            logger.warning("fetch_newsletter_groups: publisher=%s has no newsletter configured", publisher_id)
            return {"error": "not_configured", "message": "This publisher has no newsletter email configured. Newsletter groups are unavailable."}
        raise


HANDLERS = {
    "fetch_publisher_profile":  fetch_publisher_profile,
    "fetch_site_navigation":    fetch_site_navigation,
    "fetch_site_footer":        fetch_site_footer,
    "fetch_newsletter_groups":  fetch_newsletter_groups,
}
