from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import (
    DELETION_REQUIRES_CONFIRMATION,
    preview_create_op,
    preview_delete_op,
    preview_update_op,
    validate_live_blog_post_type,
)

SCHEMAS = [
    {
        "name": "cms_list_live_blog_updates",
        "description": (
            "List all update entries for a LiveBlog post, ordered by creation time descending. "
            "Only applies to posts with type LiveBlog. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id"],
            "properties": {"post_id": {"type": "integer", "description": "The LiveBlog post ID"}},
        },
    },
    {
        "name": "cms_get_live_blog_update",
        "description": (
            "Retrieve a single live blog update entry by its ID. Only applies to posts with type LiveBlog. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "id"],
            "properties": {
                "post_id": {"type": "integer", "description": "The LiveBlog post ID"},
                "id":      {"type": "integer", "description": "The live blog update entry ID"},
            },
        },
    },
    {
        "name": "cms_create_live_blog_update",
        "description": (
            "Add a new update entry to a LiveBlog post. Only applies to posts with type LiveBlog. "
            "Workflow: dry_run=true (default) shows a preview — no changes made. "
            "Once the user confirms, call again with dry_run=false to add the entry."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "title", "content"],
            "properties": {
                "post_id":    {"type": "integer", "description": "The LiveBlog post ID"},
                "title":      {"type": "string",  "description": "Headline for this update entry"},
                "content":    {"type": "string",  "description": "HTML body content for this update entry"},
                "is_pinned":  {"type": "boolean", "description": "Pin this entry to the top of the live blog (default: false)"},
                "dry_run":    {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "cms_update_live_blog_update",
        "description": (
            "Update an existing live blog update entry. Only applies to posts with type LiveBlog. "
            "Workflow: dry_run=true (default) fetches the current entry and shows a diff — no changes made. "
            "Once confirmed, call again with dry_run=false to apply."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "id"],
            "properties": {
                "post_id":   {"type": "integer", "description": "The LiveBlog post ID"},
                "id":        {"type": "integer", "description": "The live blog update entry ID"},
                "title":     {"type": "string",  "description": "New headline for this update entry"},
                "content":   {"type": "string",  "description": "New HTML body content"},
                "is_pinned": {"type": "boolean", "description": "Pin or unpin this entry"},
                "dry_run":   {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "cms_delete_live_blog_update",
        "description": (
            "Permanently delete a live blog update entry. This action CANNOT be undone. "
            "Workflow: dry_run=true (default) shows the full entry — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "id"],
            "properties": {
                "post_id":        {"type": "integer", "description": "The LiveBlog post ID"},
                "id":             {"type": "integer", "description": "The live blog update entry ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },
]


def list_live_blog_updates(credentials: dict, args: dict):
    post_id = args["post_id"]
    return cms_get(credentials, f"/post/{post_id}/live-blog-update/")


def get_live_blog_update(credentials: dict, args: dict):
    return cms_get(credentials, f"/post/{args['post_id']}/live-blog-update/{args['id']}/")


def create_live_blog_update(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    post_id = args["post_id"]
    payload = {k: v for k, v in args.items() if k not in ("dry_run", "post_id")}
    err = validate_live_blog_post_type(credentials, post_id)
    if err:
        return err
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Live Blog Update", {"post_id": post_id, **payload})}
    return cms_post(credentials, f"/post/{post_id}/live-blog-update/", payload)


def update_live_blog_update(credentials: dict, args: dict):
    dry_run   = args.get("dry_run", True)
    post_id   = args["post_id"]
    update_id = args["id"]
    changes   = {k: v for k, v in args.items() if k not in ("post_id", "id", "dry_run")}
    err = validate_live_blog_post_type(credentials, post_id)
    if err:
        return err
    if dry_run:
        raw = cms_get(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")
        if "error_type" in raw:
            return raw
        entry = raw.get("data", raw)
        flat_current = (
            {"title": entry["content"].get("title"), "content": entry["content"].get("content")}
            if isinstance(entry.get("content"), dict)
            else entry
        )
        return {"dry_run": True, "preview": preview_update_op("Live Blog Update", update_id, flat_current, changes)}
    return cms_patch(credentials, f"/post/{post_id}/live-blog-update/{update_id}/", changes)


def delete_live_blog_update(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    post_id        = args["post_id"]
    update_id      = args["id"]
    err = validate_live_blog_post_type(credentials, post_id)
    if err:
        return err
    if dry_run:
        raw = cms_get(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")
        if "error_type" in raw:
            return raw
        entry = raw.get("data", raw)
        return {"dry_run": True, "preview": preview_delete_op("Live Blog Update", update_id, entry)}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")


HANDLERS = {
    "cms_list_live_blog_updates":   list_live_blog_updates,
    "cms_get_live_blog_update":     get_live_blog_update,
    "cms_create_live_blog_update":  create_live_blog_update,
    "cms_update_live_blog_update":  update_live_blog_update,
    "cms_delete_live_blog_update":  delete_live_blog_update,
}
