import logging

from ..clients.cds import cds_get

logger = logging.getLogger(__name__)

SCHEMAS = [
    {
        "name": "fetch_authors",
        "description": "List all authors/contributors for this publication with pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit": {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "fetch_author",
        "description": (
            "Get a single author by their numeric ID. "
            "identifier must be a numeric author ID. "
            "To find posts by a specific author, use fetch_published_posts with the contributors.id__eq filter. "
            "Do not guess IDs or pass non-numeric values."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Numeric author ID (e.g. \"42\")."},
            },
        },
    },
]


def fetch_authors(credentials: dict, args: dict):
    return cds_get(credentials, "/authors/", {"page": args.get("page"), "limit": args.get("limit")})


def fetch_author(credentials: dict, args: dict):
    identifier = str(args.get("identifier", "")).strip()
    if not identifier:
        return {"error": "invalid_input", "message": "identifier is required. Use fetch_authors to find valid numeric author IDs."}
    if not identifier.isdigit():
        logger.warning("fetch_author: non-numeric identifier=%r", identifier)
        return {
            "error": "invalid_input",
            "message": (
                f"Author identifier must be a numeric ID, got {identifier!r}. "
                "Use fetch_authors to discover valid author IDs."
            ),
        }
    try:
        return cds_get(credentials, f"/author/{identifier}/")
    except Exception as exc:
        err_str     = str(exc).lower()
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        if http_status == 404 or "unknown endpoint" in err_str or "not found" in err_str:
            return {
                "error": "not_found",
                "message": (
                    f"Author with ID {identifier} was not found. "
                    "Use fetch_authors to discover valid author IDs."
                ),
            }
        raise


HANDLERS = {
    "fetch_authors": fetch_authors,
    "fetch_author":  fetch_author,
}
