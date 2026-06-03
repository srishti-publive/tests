from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "fetch_published_tags",
        "description": (
            "List all published tags. "
            "If the user only needs a quick count or names, return a summary and offer more. "
            "If the user needs unpublished tags or management operations, suggest list_editorial_tags instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "fetch_published_tag",
        "description": (
            "Get a single published tag by ID or slug. "
            "If the user needs management fields or plans to update, suggest get_editorial_tag instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Tag ID or slug"},
            },
        },
    },
]


def fetch_published_tags(credentials: dict, args: dict):
    return cds_get(credentials, "/tags/", {"page": args.get("page"), "limit": args.get("limit")})


def fetch_published_tag(credentials: dict, args: dict):
    return cds_get(credentials, f"/tag/{args['identifier']}/")


HANDLERS = {
    "fetch_published_tags": fetch_published_tags,
    "fetch_published_tag":  fetch_published_tag,
}
