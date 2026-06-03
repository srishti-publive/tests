"""CDS static file tools — publisher-specific files served from S3."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "fetch_ads_txt",
        "description": "Get the publisher's ads.txt file content. Returns raw text wrapped in the CDS JSON envelope.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_robots_txt",
        "description": "Get the publisher's robots.txt file content.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_service_worker_js",
        "description": "Get the push notification service worker JavaScript file. Returns 404 if push notifications are not enabled.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_push_notification_html",
        "description": (
            "Get one of the three HTML files required for the push notification permission UI. "
            "Returns 404 if push notifications are not enabled for this publisher."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["filename"],
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "One of: izooto.html, helper-iframe.html, permission-dialog.html",
                },
            },
        },
    },
]


def fetch_ads_txt(credentials: dict, args: dict):
    return cds_get(credentials, "/static/ads.txt/")


def fetch_robots_txt(credentials: dict, args: dict):
    return cds_get(credentials, "/static/robots.txt/")


def fetch_service_worker_js(credentials: dict, args: dict):
    return cds_get(credentials, "/static/service-worker.js/")


def fetch_push_notification_html(credentials: dict, args: dict):
    filename = args["filename"]
    return cds_get(credentials, f"/static/{filename}/")


HANDLERS = {
    "fetch_ads_txt":               fetch_ads_txt,
    "fetch_robots_txt":            fetch_robots_txt,
    "fetch_service_worker_js":     fetch_service_worker_js,
    "fetch_push_notification_html": fetch_push_notification_html,
}
