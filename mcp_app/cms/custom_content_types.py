from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

_BASE = "/entities/content-type/"

SCHEMAS = [
    {
        "name": "list_content_type_schemas",
        "description": (
            "List all custom content type schemas defined for this publisher. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit": {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "get_content_type_schema",
        "description": (
            "Retrieve a single custom content type schema by ID. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string", "description": "Custom content type schema ID (MongoDB ObjectID)"}},
        },
    },
    {
        "name": "create_content_type_schema",
        "description": (
            "Create a new custom content type schema. "
            "Immutable after creation: api_slug, api_collections_slug. "
            "Workflow: dry_run=true (default) shows a preview — no changes made. "
            "Once confirmed, call again with dry_run=false."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "api_slug", "api_collections_slug"],
            "properties": {
                "name":                   {"type": "string",  "description": "Display name (e.g. 'Movies')"},
                "type":                   {"type": "string",  "description": "Schema type: Collection (default)"},
                "api_slug":               {"type": "string",  "description": "Singular API slug. Immutable after creation."},
                "api_collections_slug":   {"type": "string",  "description": "Plural API slug. Immutable after creation."},
                "response_type":          {"type": "string",  "description": "Response format — json (default)"},
                "field_types":            {"type": "array",   "description": "Array of field definitions. Each item: {name (string, required), type (string, required — short_text|long_text|integer|decimal|boolean|email|url|phone_number|json|media|embed|component|dynamic_zone|dynamic_list), meta_data (object), validations (object — required/group_editable), group_id (string)}"},
                "groups":                 {"type": "array",   "description": "Field group definitions"},
                "components":             {"type": "array",   "description": "Associated custom component schema IDs"},
                "settings":               {"type": "object",  "description": "Type-level settings"},
                "global_system_default":  {"type": "boolean", "description": "Mark as system-level default type (default: false)"},
                "dry_run":                {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "update_content_type_schema",
        "description": (
            "Update an existing custom content type schema. "
            "Immutable fields: api_slug, api_collections_slug. "
            "Workflow: dry_run=true (default) shows a diff — no changes made."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":          {"type": "string",  "description": "Custom content type schema ID (MongoDB ObjectID)"},
                "name":        {"type": "string",  "description": "New display name"},
                "field_types": {"type": "array",   "description": "Replacement field definitions"},
                "groups":      {"type": "array",   "description": "Updated field group definitions"},
                "components":  {"type": "array",   "description": "Updated associated component schema IDs"},
                "settings":    {"type": "object",  "description": "Updated type-level settings"},
                "dry_run":     {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "delete_content_type_schema",
        "description": (
            "Permanently delete a custom content type schema. This action CANNOT be undone. "
            "All content entries based on this schema will lose their schema reference. "
            "Workflow: dry_run=true (default) shows the full schema — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "string",  "description": "Custom content type schema ID (MongoDB ObjectID)"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false"},
            },
        },
    },
]


def list_content_type_schemas(credentials: dict, args: dict):
    return cms_get(credentials, _BASE, {"page": args.get("page"), "limit": args.get("limit")})


def get_content_type_schema(credentials: dict, args: dict):
    return cms_get(credentials, f"{_BASE}{args['id']}/")


def create_content_type_schema(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Custom Content Type", payload)}
    return cms_post(credentials, _BASE, payload)


def update_content_type_schema(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    type_id = args["id"]
    changes = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"{_BASE}{type_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Custom Content Type", type_id, current, changes)}
    return cms_patch(credentials, f"{_BASE}{type_id}/", changes)


def delete_content_type_schema(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    type_id        = args["id"]
    if dry_run:
        item = cms_get(credentials, f"{_BASE}{type_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op(
            "Custom Content Type", type_id, item,
            warning="All content entries based on this schema will lose their schema reference.",
        )}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"{_BASE}{type_id}/")


HANDLERS = {
    "list_content_type_schemas":   list_content_type_schemas,
    "get_content_type_schema":     get_content_type_schema,
    "create_content_type_schema":  create_content_type_schema,
    "update_content_type_schema":  update_content_type_schema,
    "delete_content_type_schema":  delete_content_type_schema,
}
