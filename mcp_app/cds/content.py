"""CDS read tools for content metadata: types, ad slots, forms, and URL identification."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "resolve_url_to_content_type",
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
        "name": "fetch_ad_slots",
        "description": "Get configured advertisement slots with dimensions, HTML content, and slot type information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_content_type_definitions",
        "description": "Get all content types configured for this publication (e.g. Article, Video, Web Story) with their API and collection slugs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_form_schema",
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


def resolve_url_to_content_type(credentials: dict, args: dict):
    return cds_get(credentials, "/identify_url/", {"legacy_url": args["legacy_url"]})


def fetch_ad_slots(credentials: dict, args: dict):
    return cds_get(credentials, "/active-slots/")


def fetch_content_type_definitions(credentials: dict, args: dict):
    return cds_get(credentials, "/content-types/")


def fetch_form_schema(credentials: dict, args: dict):
    return cds_get(credentials, f"/form-schemas/{args['schema_id']}/", {"page_source": args.get("page_source")})


HANDLERS = {
    "resolve_url_to_content_type":  resolve_url_to_content_type,
    "fetch_ad_slots":               fetch_ad_slots,
    "fetch_content_type_definitions": fetch_content_type_definitions,
    "fetch_form_schema":            fetch_form_schema,
}
