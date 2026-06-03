from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

SCHEMAS = [
    {
        "name": "list_editorial_tags",
        "description": (
            "List all CMS tags with pagination. Returns all tags including unpublished ones. "
            "NOTE: if the user only needs published tags, prefer the CDS fetch_published_tags tool. "
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
        "name": "get_editorial_tag",
        "description": (
            "Retrieve a single CMS tag by ID. Returns full management details. "
            "NOTE: if the user only needs basic published data, prefer the CDS fetch_published_tag tool. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Tag ID"}},
        },
    },
    {
        "name": "create_tag",
        "description": (
            "Create a new tag in the CMS. "
            "BEFORE calling: confirm all details with the user — at minimum name and english_name. "
            "Workflow: dry_run=true (default) shows a full preview — no changes made. "
            "Once the user confirms the preview, call again with dry_run=false to create. "
            "Immutable after creation: english_name, slug."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "english_name"],
            "properties": {
                "name":             {"type": "string", "description": "Tag name"},
                "english_name":     {"type": "string", "description": "English name — used for slug generation. Immutable after creation."},
                "slug":             {"type": "string", "description": "Custom slug (auto-generated if omitted). Immutable after creation."},
                "meta_title":       {"type": "string", "description": "SEO title"},
                "meta_description": {"type": "string", "description": "SEO description"},
                "content":          {"type": "string", "description": "Tag description (HTML)"},
                "dry_run":          {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "update_tag",
        "description": (
            "Update an existing tag. "
            "Workflow: dry_run=true (default) fetches current state and shows a diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false. "
            "Immutable fields: english_name, slug."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":               {"type": "integer", "description": "Tag ID"},
                "name":             {"type": "string",  "description": "New tag name"},
                "meta_title":       {"type": "string",  "description": "New SEO title"},
                "meta_description": {"type": "string",  "description": "New SEO description"},
                "content":          {"type": "string",  "description": "New tag description (HTML)"},
                "dry_run":          {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "delete_tag",
        "description": (
            "Permanently delete a tag. This action CANNOT be undone. "
            "Workflow: dry_run=true (default) shows full tag details — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Tag ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },
]


def list_editorial_tags(credentials: dict, args: dict):
    return cms_get(credentials, "/tag/", {"page": args.get("page"), "limit": args.get("limit")})


def get_editorial_tag(credentials: dict, args: dict):
    return cms_get(credentials, f"/tag/{args['id']}/")


def create_tag(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Tag", payload)}
    return cms_post(credentials, "/tag/", payload)


def update_tag(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    tag_id  = args["id"]
    changes = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"/tag/{tag_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Tag", tag_id, current, changes)}
    return cms_patch(credentials, f"/tag/{tag_id}/", changes)


def delete_tag(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    tag_id         = args["id"]
    if dry_run:
        item = cms_get(credentials, f"/tag/{tag_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op("Tag", tag_id, item)}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"/tag/{tag_id}/")


HANDLERS = {
    "list_editorial_tags": list_editorial_tags,
    "get_editorial_tag":   get_editorial_tag,
    "create_tag":          create_tag,
    "update_tag":          update_tag,
    "delete_tag":          delete_tag,
}
