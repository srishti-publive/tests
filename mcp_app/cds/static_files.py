"""CDS static file tools — publisher-specific files served from S3."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "fetch_static_file",
        "description": (
            "Get a publisher-specific static file. "
            "ads.txt and robots.txt are always present. "
            "service-worker.js and the push notification HTML files (izooto.html, helper-iframe.html, permission-dialog.html) "
            "return 404 if push notifications are not enabled for this publisher."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["filename"],
            "properties": {
                "filename": {
                    "type": "string",
                    "enum": [
                        "ads.txt",
                        "robots.txt",
                        "service-worker.js",
                        "izooto.html",
                        "helper-iframe.html",
                        "permission-dialog.html",
                    ],
                    "description": "Name of the static file to fetch",
                },
            },
        },
    },
]


def fetch_static_file(credentials: dict, args: dict):
    return cds_get(credentials, f"/static/{args['filename']}/")


HANDLERS = {
    "fetch_static_file": fetch_static_file,
}
