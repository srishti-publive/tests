from mcp_app.clients.cms import cms_delete, cms_get, cms_patch, cms_post

from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

_BASE = "/media-library/"

SCHEMAS = [
    {
        "name": "list_media_assets",
        "description": "List all media assets in the CMS library with pagination. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit": {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "get_media_asset",
        "description": "Retrieve a single media asset from the CMS library by ID. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Media asset ID"}},
        },
    },
    {
        "name": "register_media_asset",
        "description": (
            "Register an existing media URL into the CMS library. "
            "Important: this does NOT upload files — it registers an external URL (e.g. from S3, Cloudinary). "
            "Immutable after creation: path, type. "
            "Workflow: dry_run=true (default) shows a preview — no changes made. "
            "Once confirmed, call again with dry_run=false."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["filename", "path"],
            "properties": {
                "filename":  {"type": "string",  "description": "Filename (e.g. hero-image.jpg)"},
                "path":      {"type": "string",  "description": "Direct external media URL. Immutable after creation."},
                "alt_text":  {"type": "string",  "description": "Alt text for accessibility"},
                "caption":   {"type": "string",  "description": "Caption or description"},
                "source":    {"type": "string",  "description": "Source or credit line (e.g. Reuters, PTI, Staff)"},
                "type":      {"type": "string",  "description": "Image, Video, or File. Immutable after creation."},
                "meta_data": {"type": "object",  "description": "Metadata object e.g. {\"width\": 1200, \"height\": 630}"},
                "dry_run":   {"type": "boolean", "description": "true = preview only, no changes (default); false = register for real"},
            },
        },
    },
    {
        "name": "update_media_asset",
        "description": (
            "Update metadata of an existing media asset. "
            "Immutable fields: path, type. "
            "Workflow: dry_run=true (default) shows a diff — no changes made. Once confirmed, call again with dry_run=false."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":        {"type": "integer", "description": "Media asset ID"},
                "filename":  {"type": "string",  "description": "New filename"},
                "alt_text":  {"type": "string",  "description": "New alt text"},
                "caption":   {"type": "string",  "description": "New caption"},
                "source":    {"type": "string",  "description": "New source or credit line"},
                "meta_data": {"type": "object",  "description": "New metadata object"},
                "dry_run":   {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "delete_media_asset",
        "description": (
            "Permanently delete a media asset from the library. This action CANNOT be undone. "
            "Posts referencing this media will lose their associated image or file. "
            "Workflow: dry_run=true (default) shows full asset details — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Media asset ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false"},
            },
        },
    },
]


def list_media_assets(credentials: dict, args: dict):
    return cms_get(credentials, _BASE, {"page": args.get("page"), "limit": args.get("limit")})


def get_media_asset(credentials: dict, args: dict):
    return cms_get(credentials, f"{_BASE}{args['id']}/")


def register_media_asset(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Media", payload)}
    return cms_post(credentials, _BASE, payload)


def update_media_asset(credentials: dict, args: dict):
    dry_run  = args.get("dry_run", True)
    media_id = args["id"]
    changes  = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"{_BASE}{media_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Media", media_id, current, changes)}
    return cms_patch(credentials, f"{_BASE}{media_id}/", changes)


def delete_media_asset(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    media_id       = args["id"]
    if dry_run:
        item = cms_get(credentials, f"{_BASE}{media_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op(
            "Media", media_id, item,
            warning="Posts referencing this media will lose their associated image or file.",
        )}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"{_BASE}{media_id}/")


HANDLERS = {
    "list_media_assets":   list_media_assets,
    "get_media_asset":     get_media_asset,
    "register_media_asset": register_media_asset,
    "update_media_asset":  update_media_asset,
    "delete_media_asset":  delete_media_asset,
}
