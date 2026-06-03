import logging

from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

logger = logging.getLogger(__name__)

SCHEMAS = [
    {
        "name": "cms_list_posts",
        "description": (
            "List all CMS posts with pagination. Includes drafts, published, and scheduled posts. "
            "NOTE: if the user only needs published posts, prefer the CDS list_posts tool. "
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
        "name": "cms_get_post",
        "description": (
            "Retrieve a single CMS post by ID. Returns full details including draft and scheduled content. "
            "NOTE: if the user only needs basic published data, prefer the CDS get_post tool. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Post ID"}},
        },
    },
    {
        "name": "cms_create_post",
        "description": (
            "Create a new post in the CMS. "
            "BEFORE calling: you MUST have all six required fields — title, english_title, type, status, "
            "primary_category, AND contributors (at least one author ID). "
            "contributors is REQUIRED by the API — omitting it causes a hard validation failure. "
            "If the user has not provided an author ID, call list_authors first to get one, then ask the user to confirm. "
            "english_title must be plain English text matching the title, NOT a pre-slugified string. "
            "TYPE-SPECIFIC REQUIREMENTS — do NOT attempt to create these without the noted fields: "
            "Video: requires meta_data with meta_video_url and meta_video_embed. "
            "Web Story: requires AMP story slide markup in the content field. "
            "Gallery: requires gallery image data in content or custom_entity. "
            "Article, LiveBlog, CustomPage, BlankPage: no extra required fields beyond the six standard ones. "
            "DRAFT posts (status=Draft): created immediately — no preview step. "
            "PUBLISHED/SCHEDULED/APPROVAL PENDING posts: dry_run=true (default) shows a full preview. "
            "Immutable after creation: english_title, type, slug, meta_data, custom_published_at."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "english_title", "type", "status", "primary_category", "contributors"],
            "properties": {
                "title":               {"type": "string",  "description": "Post headline"},
                "english_title":       {"type": "string",  "description": "Plain English headline for slug generation. Immutable after creation."},
                "type":                {"type": "string",  "description": "Post type: Article, Video, Web Story, Gallery, LiveBlog, CustomPage, BlankPage. Immutable after creation."},
                "status":              {"type": "string",  "description": "Draft, Published, Scheduled, or Approval Pending"},
                "primary_category":    {"type": "integer", "description": "Primary category ID"},
                "contributors":        {"type": "string",  "description": "REQUIRED — comma-separated author IDs (e.g. '12' or '12,15')."},
                "content":             {"type": "string",  "description": "HTML body content"},
                "tags":                {"type": "string",  "description": "Comma-separated tag IDs"},
                "categories":          {"type": "string",  "description": "Comma-separated additional category IDs"},
                "banner_url":          {"type": "integer", "description": "Media ID for the featured image"},
                "banner_description":  {"type": "string",  "description": "Featured image caption"},
                "short_description":   {"type": "string",  "description": "SEO meta description"},
                "summary":             {"type": "string",  "description": "Post summary"},
                "seo_keyphrase":       {"type": "string",  "description": "Focus keyword for SEO"},
                "slug":                {"type": "string",  "description": "Custom URL slug (auto-generated from english_title if omitted). Immutable after creation."},
                "scheduled_at":        {"type": "string",  "description": "Future publish date ISO 8601 — status must be Scheduled"},
                "hide_banner_image":   {"type": "boolean", "description": "Hide the featured image on the post"},
                "custom_published_at": {"type": "string",  "description": "Backdated publish timestamp ISO 8601. Immutable after creation."},
                "dry_run":             {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "cms_update_post",
        "description": (
            "Update an existing post. "
            "SETTING STATUS TO DRAFT: updates immediately — no dry_run step needed. "
            "ALL OTHER UPDATES: dry_run=true (default) shows a field-by-field diff — no changes made. "
            "PUBLISHING: also requires confirm_publish=true together with dry_run=false. "
            "Cannot be changed after creation: english_title, type, slug."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":                  {"type": "integer", "description": "Post ID"},
                "title":               {"type": "string",  "description": "New post headline"},
                "content":             {"type": "string",  "description": "New HTML body content"},
                "status":              {"type": "string",  "description": "Draft, Published, Scheduled, or Approval Pending"},
                "primary_category":    {"type": "integer", "description": "New primary category ID"},
                "contributors":        {"type": "string",  "description": "Comma-separated author IDs"},
                "tags":                {"type": "string",  "description": "Comma-separated tag IDs"},
                "categories":          {"type": "string",  "description": "Comma-separated category IDs"},
                "banner_url":          {"type": "integer", "description": "New media ID for featured image"},
                "short_description":   {"type": "string",  "description": "New SEO meta description"},
                "hide_banner_image":   {"type": "boolean", "description": "Hide the featured image"},
                "custom_published_at": {"type": "string",  "description": "Backdated publish timestamp ISO 8601"},
                "scheduled_at":        {"type": "string",  "description": "Future publish date ISO 8601"},
                "dry_run":             {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
                "confirm_publish":     {"type": "boolean", "description": "Must be true when setting status=Published with dry_run=false."},
            },
        },
    },
    {
        "name": "cms_delete_post",
        "description": (
            "Permanently delete a post and all its associated data. This action CANNOT be undone. "
            "Workflow: dry_run=true (default) shows full post details — no deletion. "
            "Once confirmed, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Post ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },
]


def _coerce_post_int_fields(payload: dict) -> None:
    for field in ("primary_category", "banner_url", "after_para"):
        if field in payload:
            try:
                payload[field] = int(payload[field])
            except (ValueError, TypeError):
                pass


def _strip_list_brackets(payload: dict) -> None:
    for field in ("tags", "categories"):
        if field in payload and isinstance(payload[field], str):
            payload[field] = payload[field].strip("[]")


def list_posts(credentials: dict, args: dict):
    return cms_get(credentials, "/post/", {"page": args.get("page"), "limit": args.get("limit")})


def get_post(credentials: dict, args: dict):
    return cms_get(credentials, f"/post/{args['id']}/")


def create_post(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run" and v is not None and v != ""}

    if not payload.get("contributors"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "contributors is required to create a post. "
                "Call list_authors to find valid author IDs, then include "
                "contributors as a comma-separated string (e.g. '12' or '12,15')."
            ),
            "retryable": False,
        }

    post_type = payload.get("type", "")
    if post_type == "Web Story" and not payload.get("content") and not payload.get("custom_entity"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "Web Story posts require AMP story slide content in the 'content' field. "
                "Create an empty Web Story draft via the Publive dashboard first, "
                "then use cms_update_post to update other fields programmatically."
            ),
            "retryable": False,
        }
    if post_type == "Gallery" and not payload.get("content") and not payload.get("custom_entity"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "Gallery posts require gallery image data in the 'content' or 'custom_entity' field. "
                "Create an empty Gallery draft via the Publive dashboard first, "
                "then use cms_update_post to update other fields programmatically."
            ),
            "retryable": False,
        }

    if post_type == "Article":
        payload.setdefault("after_para", 0)

    _coerce_post_int_fields(payload)
    _strip_list_brackets(payload)

    if payload.get("status") == "Draft":
        return cms_post(credentials, "/post/", payload)

    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Post", payload)}

    result = cms_post(credentials, "/post/", payload)
    if (
        isinstance(result, dict)
        and result.get("error_type") == "bad_request"
        and "no data provided" in result.get("message", "").lower()
    ):
        type_hints = {
            "Web Story": (
                "Web Story posts require valid AMP story slide markup in the 'content' field. "
                "Create the post via the Publive dashboard first, then update other fields via cms_update_post."
            ),
            "Gallery": (
                "Gallery posts require gallery image data in the 'content' or 'custom_entity' field. "
                "Create the post via the Publive dashboard first, then update other fields via cms_update_post."
            ),
            "Article": (
                "Article post creation is not currently supported via the API (server-side limitation). "
                "Use LiveBlog, CustomPage, or BlankPage instead, or create the post via the Publive dashboard."
            ),
        }
        hint = type_hints.get(post_type)
        if hint:
            return {"error_type": "bad_request", "message": hint, "retryable": False}
    return result


def update_post(credentials: dict, args: dict):
    dry_run         = args.get("dry_run", True)
    confirm_publish = args.get("confirm_publish", False)
    post_id         = args["id"]
    changes         = {k: v for k, v in args.items() if k not in ("id", "dry_run", "confirm_publish") and v is not None and v != ""}

    _coerce_post_int_fields(changes)
    _strip_list_brackets(changes)

    if changes.get("status") == "Draft":
        return cms_patch(credentials, f"/post/{post_id}/", changes)

    if dry_run:
        current = cms_get(credentials, f"/post/{post_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Post", post_id, current, changes)}

    if changes.get("status") == "Published" and not confirm_publish:
        return {
            "error_type": "confirmation_required",
            "message": (
                "Publishing a post requires confirm_publish=true. "
                "Call again with dry_run=false AND confirm_publish=true to publish."
            ),
            "retryable": False,
        }
    return cms_patch(credentials, f"/post/{post_id}/", changes)


def delete_post(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    post_id        = args["id"]
    if dry_run:
        item = cms_get(credentials, f"/post/{post_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op(
            "Post", post_id, item,
            warning="This post and ALL its associated data will be permanently removed.",
        )}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"/post/{post_id}/")


HANDLERS = {
    "cms_list_posts":   list_posts,
    "cms_get_post":     get_post,
    "cms_create_post":  create_post,
    "cms_update_post":  update_post,
    "cms_delete_post":  delete_post,
}
