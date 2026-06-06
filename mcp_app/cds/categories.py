from mcp_app.clients.cds import cds_get

SCHEMAS = [
    {
        "name": "fetch_published_categories",
        "description": (
            "List all published categories with hierarchical structure. "
            "If the user only needs a quick count or names, return a summary and offer more details. "
            "If the user needs unpublished categories or management operations, suggest list_editorial_categories instead."
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
        "name": "fetch_published_category",
        "description": (
            "Get a single published category by ID or slug including SEO metadata and child categories. "
            "If the user only needs basic info (name, slug), return that and offer more. "
            "If the user needs management fields or plans to update, suggest get_editorial_category instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Category ID or slug"},
            },
        },
    },
]


def fetch_published_categories(credentials: dict, args: dict):
    return cds_get(credentials, "/categories/", {"page": args.get("page"), "limit": args.get("limit")})


def fetch_published_category(credentials: dict, args: dict):
    return cds_get(credentials, f"/category/{args['identifier']}/")


HANDLERS = {
    "fetch_published_categories": fetch_published_categories,
    "fetch_published_category":   fetch_published_category,
}
