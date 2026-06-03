"""CDS static file tools — publisher-specific files served from S3."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "get_static_ads_txt",
        "description": "Get the publisher's ads.txt file content. Returns raw text wrapped in the CDS JSON envelope.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_static_robots_txt",
        "description": "Get the publisher's robots.txt file content.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_static_service_worker",
        "description": "Get the push notification service worker JavaScript file. Returns 404 if push notifications are not enabled.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_static_push_notification_html",
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


def get_static_ads_txt(credentials: dict, args: dict):
    return cds_get(credentials, "/static/ads.txt/")


def get_static_robots_txt(credentials: dict, args: dict):
    return cds_get(credentials, "/static/robots.txt/")


def get_static_service_worker(credentials: dict, args: dict):
    return cds_get(credentials, "/static/service-worker.js/")


def get_static_push_notification_html(credentials: dict, args: dict):
    filename = args["filename"]
    return cds_get(credentials, f"/static/{filename}/")


HANDLERS = {
    "get_static_ads_txt":               get_static_ads_txt,
    "get_static_robots_txt":            get_static_robots_txt,
    "get_static_service_worker":        get_static_service_worker,
    "get_static_push_notification_html": get_static_push_notification_html,
}
