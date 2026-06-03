import logging

from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

logger = logging.getLogger(__name__)

_BASE = "/entities/content-type/custom-component/"

SCHEMAS = [
    {
        "name": "list_component_schemas",
        "description": "List all custom components with pagination. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit": {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "get_component_schema",
        "description": "Retrieve a single custom component by ID. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string", "description": "Custom component ID (MongoDB ObjectID, e.g. '6a153d41653f8ae9df571c7e')"}},
        },
    },
    {
        "name": "create_component_schema",
        "description": (
            "Create a new custom component schema in the CMS. "
            "Custom components are reusable typed-field schemas (like a form builder), NOT HTML templates. "
            "Workflow: dry_run=true (default) shows a preview — no changes made. "
            "Once the user confirms, call again with dry_run=false to create."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name":        {"type": "string",  "description": "Display name for the component"},
                "meta_data":   {"type": "object",  "description": "Additional metadata. Supports: {\"description\": \"...\"}"},
                "field_types": {"type": "array",   "description": "Array of field definitions. Each item: {name (string, required), type (string, required — short_text|long_text|integer|decimal|boolean|email|url|phone_number|json|media|embed|component|dynamic_zone), meta_data (object — label/tooltip/type/placeholder/default each as {value:...}), validations (object — required/max_length as {value:...}), group_id (string)}"},
                "settings":    {"type": "object",  "description": "Component-level settings object"},
                "dry_run":     {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "update_component_schema",
        "description": (
            "Update an existing custom component schema. "
            "Workflow: dry_run=true (default) fetches current state and shows a diff — no changes made. "
            "NOTE: sending field_types replaces the entire field definition array."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":          {"type": "string",  "description": "Custom component ID (MongoDB ObjectID)"},
                "name":        {"type": "string",  "description": "New display name for the component"},
                "meta_data":   {"type": "object",  "description": "Updated metadata"},
                "field_types": {"type": "array",   "description": "Replacement field definitions (replaces the whole array). Same schema as create: short_text|long_text|integer|decimal|boolean|email|url|phone_number|json|media|embed|component|dynamic_zone"},
                "settings":    {"type": "object",  "description": "Updated component-level settings"},
                "dry_run":     {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "delete_component_schema",
        "description": (
            "Permanently delete a custom component. This action CANNOT be undone. "
            "Workflow: dry_run=true (default) shows the full component — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "string", "description": "Custom component ID (MongoDB ObjectID)"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false"},
            },
        },
    },
]


def list_component_schemas(credentials: dict, args: dict):
    return cms_get(credentials, _BASE, {"page": args.get("page"), "limit": args.get("limit")})


def get_component_schema(credentials: dict, args: dict):
    return cms_get(credentials, f"{_BASE}{args['id']}/")


def create_component_schema(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Custom Component", payload)}
    result = cms_post(credentials, _BASE, payload)
    if isinstance(result, dict) and result.get("error_type") == "upstream_error":
        # Check if the server committed before returning 5xx (prevent duplicate)
        listing = cms_get(credentials, _BASE, {"limit": 50})
        items   = []
        if isinstance(listing, dict) and not listing.get("error_type"):
            items = listing.get("results") or listing.get("data") or []
        for item in items:
            if isinstance(item, dict) and item.get("name") == payload.get("name"):
                return item
        result = cms_post(credentials, _BASE, payload)
    return result


def update_component_schema(credentials: dict, args: dict):
    dry_run      = args.get("dry_run", True)
    component_id = args["id"]
    changes      = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"{_BASE}{component_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Custom Component", component_id, current, changes)}
    return cms_patch(credentials, f"{_BASE}{component_id}/", changes)


def delete_component_schema(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    component_id   = args["id"]
    if dry_run:
        item = cms_get(credentials, f"{_BASE}{component_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op("Custom Component", component_id, item)}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"{_BASE}{component_id}/")


HANDLERS = {
    "list_component_schemas":   list_component_schemas,
    "get_component_schema":     get_component_schema,
    "create_component_schema":  create_component_schema,
    "update_component_schema":  update_component_schema,
    "delete_component_schema":  delete_component_schema,
}
