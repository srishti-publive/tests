"""CDS read tools for content metadata: types, ad slots, forms, and URL identification."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "identify_content",
        "description": "Resolve a URL path to its content type: post, category, tag, author, redirect, or not_found.",
        "inputSchema": {
            "type": "object",
            "required": ["legacy_url"],
            "properties": {
                "legacy_url": {"type": "string", "description": "Path to resolve e.g. /guides/getting-started"},
            },
        },
    },
    {
        "name": "get_active_slots",
        "description": "Get configured advertisement slots with dimensions, HTML content, and slot type information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_content_types",
        "description": "Get all content types configured for this publication (e.g. Article, Video, Web Story) with their API and collection slugs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_form_schema",
        "description": "Get a form schema by ID, including field definitions, validation rules, field groups, and captcha configuration.",
        "inputSchema": {
            "type": "object",
            "required": ["schema_id"],
            "properties": {
                "schema_id":   {"type": "string", "description": "24-character hex form schema ID"},
                "page_source": {"type": "string", "description": "Optional context used by the serializer"},
            },
        },
    },
]


def identify_content(credentials: dict, args: dict):
    return cds_get(credentials, "/identify_url/", {"legacy_url": args["legacy_url"]})


def get_active_slots(credentials: dict, args: dict):
    return cds_get(credentials, "/active-slots/")


def get_content_types(credentials: dict, args: dict):
    return cds_get(credentials, "/content-types/")


def get_form_schema(credentials: dict, args: dict):
    return cds_get(credentials, f"/form-schemas/{args['schema_id']}/", {"page_source": args.get("page_source")})


HANDLERS = {
    "identify_content": identify_content,
    "get_active_slots": get_active_slots,
    "get_content_types": get_content_types,
    "get_form_schema":  get_form_schema,
}
