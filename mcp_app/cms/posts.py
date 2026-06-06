import contextlib
import logging

from mcp_app.clients.cms import cms_delete, cms_get, cms_patch, cms_post

from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

logger = logging.getLogger(__name__)

SCHEMAS = [
    {
        "name": "list_editorial_posts",
        "description": (
            "List all CMS posts with pagination. Includes drafts, published, and scheduled posts. "
            "NOTE: if the user only needs published posts, prefer the CDS fetch_published_posts tool. "
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
        "name": "get_editorial_post",
        "description": (
            "Retrieve a single CMS post by ID. Returns full details including draft and scheduled content. "
            "NOTE: if the user only needs basic published data, prefer the CDS fetch_published_post tool. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Post ID"}},
        },
    },
    {
        "name": "create_post",
        "description": (
            "Create a new post in the CMS. "
            "BEFORE calling: you MUST have all six required fields — title, english_title, type, status, "
            "primary_category, AND contributors (at least one author ID). "
            "contributors is REQUIRED by the API — omitting it causes a hard validation failure. "
            "If the user has not provided an author ID, call fetch_authors first to get one, then ask the user to confirm. "
            "english_title must be plain English text matching the title, NOT a pre-slugified string. "
            "TYPE-SPECIFIC REQUIREMENTS — do NOT attempt to create these without the noted fields: "
            "Video: the CMS API rejects meta_video_embed regardless of the value passed (known upstream bug). "
            "Create an empty Video draft via the Publive dashboard first, then use update_post to set title, content, tags, and other mutable fields. "
            "Web Story: requires AMP story slide markup in the content field AND meta_landscape_thumbnail (numeric media ID integer from the Publive media library, e.g. 295255 — use the 'id' field from list_media_assets or get_media_asset). "
            "Gallery: requires gallery image data in content or custom_entity, and after_para (integer, default 0). "
            "Article, LiveBlog, CustomPage: no extra required fields beyond the six standard ones. "
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
                "type":                {"type": "string",  "description": "Post type: Article, Video, Web Story, Gallery, LiveBlog, CustomPage. Immutable after creation."},
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
                "custom_published_at":      {"type": "string",  "description": "Backdated publish timestamp ISO 8601. Immutable after creation."},
                "meta_video_url":           {"type": "string",  "description": "Video post only — URL of the video page (e.g. YouTube/Vimeo URL). Merged into meta_data. Immutable after creation."},
                "meta_video_embed":         {"type": "string",  "description": "Video post only — raw iframe embed HTML. NOTE: the CMS API currently rejects this field during creation (known upstream validator bug — rejects both iframe strings and media IDs). Create Video posts via the Publive dashboard instead, then use update_post for mutable fields."},
                "meta_landscape_thumbnail": {"type": "integer", "description": "Web Story only — numeric media ID of the landscape thumbnail image (e.g. 295255). Retrieve the ID from get_media_asset or list_media_assets (use the 'id' field, NOT the path). Merged into meta_data. Immutable after creation."},
                "after_para":              {"type": "integer", "description": "Gallery/Article — paragraph position for injecting content. Defaults to 0 automatically for both Gallery and Article posts if not provided (the CMS requires it but has no default of its own)."},
                "meta_data":               {"type": "object",  "description": "Arbitrary key-value metadata (e.g. access_type). Merged with any type-specific meta fields above. Immutable after creation."},
                "dry_run":                 {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "update_post",
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
        "name": "delete_post",
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
            with contextlib.suppress(ValueError, TypeError):
                payload[field] = int(payload[field])


def _strip_list_brackets(payload: dict) -> None:
    for field in ("tags", "categories"):
        if field in payload and isinstance(payload[field], str):
            payload[field] = payload[field].strip("[]")


def _remap_post_type_error(result: dict, post_type: str) -> dict:
    """Translate the CMS's opaque type-validation bad_request into an actionable message.

    The CMS returns 'Invalid value for key : type' when a post type is not enabled
    for the publisher (e.g. CustomPage is a publisher-gated feature). The raw message
    gives no context on which type failed or how to fix it.
    """
    if not (
        isinstance(result, dict)
        and result.get("error_type") == "bad_request"
        and "invalid value" in result.get("message", "").lower()
        and "type" in result.get("message", "").lower()
    ):
        return result
    return {
        "error_type": "bad_request",
        "message": (
            f"Post type '{post_type}' is not enabled for this publisher. "
            "Contact Publive support to have it activated, or use one of the "
            "standard types: Article, Video, Web Story, Gallery, LiveBlog, CustomPage."
        ),
        "retryable": False,
    }


def list_editorial_posts(credentials: dict, args: dict):
    return cms_get(credentials, "/post/", {"page": args.get("page"), "limit": args.get("limit")})


def get_editorial_post(credentials: dict, args: dict):
    return cms_get(credentials, f"/post/{args['id']}/")


def create_post(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run" and v is not None and v != ""}

    if not payload.get("contributors"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "contributors is required to create a post. "
                "Call fetch_authors to find valid author IDs, then include "
                "contributors as a comma-separated string (e.g. '12' or '12,15')."
            ),
            "retryable": False,
        }

    # Merge type-specific helper fields into meta_data before any validation.
    _META_HELPER_FIELDS = ("meta_video_url", "meta_video_embed", "meta_landscape_thumbnail")
    meta_extras = {f: payload.pop(f) for f in _META_HELPER_FIELDS if f in payload}
    if meta_extras:
        existing_meta = payload.get("meta_data") or {}
        payload["meta_data"] = {**existing_meta, **meta_extras}

    post_type = payload.get("type", "")
    if post_type == "Video":
        # The CMS API validator rejects meta_video_embed regardless of the value passed
        # (both iframe HTML strings and valid media IDs fail with "must be a valid publive media ID").
        # Existing Video posts were created via the dashboard which bypasses this validator.
        # Block early and guide the user to the dashboard-first workaround.
        return {
            "error_type": "unsupported_operation",
            "message": (
                "Video posts cannot be created directly via the CMS API. "
                "The CMS backend rejects the meta_video_embed field regardless of the value passed "
                "(known upstream validator bug — affects both iframe strings and media IDs). "
                "Workaround: create an empty Video draft via the Publive dashboard, "
                "then use update_post to set the title, content, tags, contributors, and other mutable fields."
            ),
            "retryable": False,
        }

    if post_type == "Web Story" and not payload.get("content") and not payload.get("custom_entity"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "Web Story posts require AMP story slide markup in the 'content' field "
                "and a numeric media ID in 'meta_landscape_thumbnail' "
                "(e.g. 295255 — use the 'id' field from list_media_assets or get_media_asset, NOT the file path)."
            ),
            "retryable": False,
        }
    if post_type == "Web Story" and not (payload.get("meta_data") or {}).get("meta_landscape_thumbnail"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "Web Story posts require meta_landscape_thumbnail — the numeric media ID integer "
                "of the landscape thumbnail image (e.g. 295255). "
                "Call list_media_assets or get_media_asset to find the 'id' field of an image asset, "
                "then pass that integer as meta_landscape_thumbnail."
            ),
            "retryable": False,
        }
    if post_type == "Gallery" and not payload.get("content") and not payload.get("custom_entity"):
        return {
            "error_type": "missing_required_field",
            "message": (
                "Gallery posts require gallery image data in the 'content' or 'custom_entity' field. "
                "Create an empty Gallery draft via the Publive dashboard first, "
                "then use update_post to update other fields programmatically."
            ),
            "retryable": False,
        }

    if post_type in ("Article", "Gallery"):
        payload.setdefault("after_para", 0)

    _coerce_post_int_fields(payload)
    _strip_list_brackets(payload)

    if payload.get("status") == "Draft":
        return _remap_post_type_error(cms_post(credentials, "/post/", payload), post_type)

    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Post", payload)}

    # CMS API does not support creating a post directly in non-Draft status (returns HTTP 500).
    # Two-step: POST as Draft, then PATCH to the intended status.
    intended_status = payload["status"]
    draft_payload = {**payload, "status": "Draft"}

    result = _remap_post_type_error(cms_post(credentials, "/post/", draft_payload), post_type)

    if (
        isinstance(result, dict)
        and result.get("error_type") == "bad_request"
        and "no data provided" in result.get("message", "").lower()
    ):
        type_hints = {
            "Web Story": (
                "Web Story posts require valid AMP story slide markup in the 'content' field. "
                "Create the post via the Publive dashboard first, then update other fields via update_post."
            ),
            "Gallery": (
                "Gallery posts require gallery image data in the 'content' or 'custom_entity' field. "
                "Create the post via the Publive dashboard first, then update other fields via update_post."
            ),
        }
        hint = type_hints.get(post_type)
        if hint:
            return {"error_type": "bad_request", "message": hint, "retryable": False}

    if isinstance(result, dict) and "error_type" in result:
        return result

    data = result.get("data", result) if isinstance(result, dict) else result
    post_id = data.get("id") if isinstance(data, dict) else None
    if not post_id:
        return result

    patch = {"status": intended_status}
    if intended_status == "Scheduled" and payload.get("scheduled_at"):
        patch["scheduled_at"] = payload["scheduled_at"]

    patch_result = cms_patch(credentials, f"/post/{post_id}/", patch)
    if isinstance(patch_result, dict) and "error_type" in patch_result:
        return {
            "error_type": "partial_success",
            "message": (
                f"Post was created as Draft (ID: {post_id}) but setting status to "
                f"{intended_status} failed: {patch_result.get('message', 'unknown error')}. "
                "Use update_post to retry the status change."
            ),
            "post_id": post_id,
            "retryable": False,
        }

    return patch_result


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
    "list_editorial_posts": list_editorial_posts,
    "get_editorial_post":   get_editorial_post,
    "create_post":          create_post,
    "update_post":          update_post,
    "delete_post":          delete_post,
}
