import collections
import logging
import threading

import newrelic.agent

from .cds_client import cds_get
from .cms_client import cms_delete, cms_get, cms_patch, cms_post
from .nr_utils import add_attrs, fn_trace, notice_err, record_metric

logger = logging.getLogger(__name__)

_active_cms_tool_calls: dict[str, int] = collections.defaultdict(int)
_active_cms_tool_calls_lock = threading.Lock()

# ── Tool catalogue ────────────────────────────────────────────────────────────
# 25 CMS tools covering 5 resources × 5 operations (list, retrieve, create,
# update, delete).  All tools are prefixed with "cms_" to avoid collision with
# the existing CDS read tools.
#
# Behaviour tiers:
#   Tier 1 — list / retrieve  : no dry_run param, direct CMS call
#   Tier 2 — create           : dry_run=true (default) previews; false commits
#   Tier 3 — update           : dry_run=true fetches current state + shows diff
#   Tier 3 — delete (stricter): dry_run=true shows item; execute requires
#                               BOTH dry_run=false AND confirm_delete=true

CMS_TOOLS = [

    # ── Categories ────────────────────────────────────────────────────────────

    {
        "name": "cms_list_categories",
        "description": (
            "List all CMS categories with pagination. Returns every category including unpublished ones. "
            "NOTE: if the user only needs published categories, prefer the CDS list_categories tool — "
            "it is simpler and returns just published data. Use this tool when the user needs drafts, "
            "unpublished categories, or management operations. "
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
        "name": "cms_get_category",
        "description": (
            "Retrieve a single CMS category by ID. Returns full details including unpublished fields. "
            "NOTE: if the user only needs basic published data, prefer the CDS get_category tool. "
            "Use this when the user needs full management data or plans to update the category. "
            "Always ask the user for the category ID before calling if not already provided. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Category ID"},
            },
        },
    },
    {
        "name": "cms_create_category",
        "description": (
            "Create a new category in the CMS. "
            "BEFORE calling: confirm all details with the user — at minimum name and english_name. "
            "Workflow: dry_run=true (default) shows a full preview of what will be created — no changes made. "
            "Once the user confirms the preview, call again with dry_run=false to create. "
            "Immutable after creation: english_name, slug, parent_category, content_type."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "english_name"],
            "properties": {
                "name":                 {"type": "string",  "description": "Category name"},
                "english_name":         {"type": "string",  "description": "English name — used for permalink generation. Immutable after creation."},
                "slug":                 {"type": "string",  "description": "Custom slug (auto-generated from english_name if omitted). Immutable after creation."},
                "meta_title":           {"type": "string",  "description": "SEO title"},
                "h1_tag":               {"type": "string",  "description": "H1 heading tag"},
                "meta_description":     {"type": "string",  "description": "SEO description"},
                "parent_category":      {"type": "integer", "description": "Parent category ID. Immutable after creation."},
                "priority":             {"type": "integer", "description": "Priority level (1–1000)"},
                "content":              {"type": "string",  "description": "Category description (HTML)"},
                "category_brand_color": {"type": "string",  "description": "Brand color in hex (e.g. #EF4444)"},
                "content_type":         {"type": "string",  "description": "Content type filter e.g. Article, Web Story. Immutable after creation."},
                "dry_run":              {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "cms_update_category",
        "description": (
            "Update an existing category. "
            "BEFORE calling: confirm the category ID and all fields to change with the user. "
            "Workflow: dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply. "
            "Immutable fields that cannot be changed: english_name, slug, content_type."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":                   {"type": "integer", "description": "Category ID"},
                "name":                 {"type": "string",  "description": "New category name"},
                "meta_title":           {"type": "string",  "description": "New SEO title"},
                "meta_description":     {"type": "string",  "description": "New SEO description"},
                "content":              {"type": "string",  "description": "New category description (HTML)"},
                "category_brand_color": {"type": "string",  "description": "New brand color (hex)"},
                "priority":             {"type": "integer", "description": "New priority level"},
                "dry_run":              {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "cms_delete_category",
        "description": (
            "Permanently delete a category. This action CANNOT be undone. "
            "Posts assigned to this category will lose their category assignment. "
            "BEFORE calling: confirm the category ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full category details — no deletion. "
            "Show the preview to the user. Once they explicitly confirm deletion, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Category ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },

    # ── Tags ──────────────────────────────────────────────────────────────────

    {
        "name": "cms_list_tags",
        "description": (
            "List all CMS tags with pagination. Returns all tags including unpublished ones. "
            "NOTE: if the user only needs published tags, prefer the CDS list_tags tool — simpler and sufficient. "
            "Use this when the user needs full tag management. "
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
        "name": "cms_get_tag",
        "description": (
            "Retrieve a single CMS tag by ID. Returns full management details. "
            "NOTE: if the user only needs basic published data, prefer the CDS get_tag tool. "
            "Always ask the user for the tag ID before calling if not already provided. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Tag ID"},
            },
        },
    },
    {
        "name": "cms_create_tag",
        "description": (
            "Create a new tag in the CMS. "
            "BEFORE calling: confirm all details with the user — at minimum name and english_name. "
            "Workflow: dry_run=true (default) shows a full preview of what will be created — no changes made. "
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
        "name": "cms_update_tag",
        "description": (
            "Update an existing tag. "
            "BEFORE calling: confirm the tag ID and all fields to change with the user. "
            "Workflow: dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply. "
            "Immutable fields that cannot be changed: english_name, slug."
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
        "name": "cms_delete_tag",
        "description": (
            "Permanently delete a tag. This action CANNOT be undone. "
            "BEFORE calling: confirm the tag ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full tag details — no deletion. "
            "Show the preview to the user. Once they explicitly confirm deletion, call again with dry_run=false AND confirm_delete=true."
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

    # ── Posts ─────────────────────────────────────────────────────────────────

    {
        "name": "cms_list_posts",
        "description": (
            "List all CMS posts with pagination. Includes drafts, published, and scheduled posts. "
            "NOTE: if the user only needs published posts, prefer the CDS list_posts tool — it has richer "
            "filtering (by type, category, tag, author, date range) and is simpler. Use cms_list_posts "
            "when the user needs drafts, scheduled posts, or management operations. "
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
            "Always ask the user for the post ID before calling if not already provided. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Post ID"},
            },
        },
    },
    {
        "name": "cms_create_post",
        "description": (
            "Create a new post in the CMS. "
            "BEFORE calling: collect all required fields from the user — title, english_title, type, status, primary_category. "
            "Ask for optional fields (content, tags, banner_url, etc.) based on user intent before calling. "
            "DRAFT posts (status=Draft): created immediately — no preview step, safe because drafts are reversible. "
            "PUBLISHED/SCHEDULED/APPROVAL PENDING posts: "
            "dry_run=true (default) shows a full preview of what will be created — no changes made. "
            "Show the preview to the user and ask them to confirm, then call again with dry_run=false to create. "
            "Immutable after creation: english_title, type, slug, meta_data, custom_published_at."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["title", "english_title", "type", "status", "primary_category"],
            "properties": {
                "title":               {"type": "string",  "description": "Post headline"},
                "english_title":       {"type": "string",  "description": "English headline — used for slug generation. Immutable after creation."},
                "type":                {"type": "string",  "description": "Post type: Article, Video, Web Story, Gallery, LiveBlog, CustomPage, BlankPage. Immutable after creation."},
                "status":              {"type": "string",  "description": "Draft, Published, Scheduled, or Approval Pending"},
                "primary_category":    {"type": "integer", "description": "Primary category ID"},
                "contributors":        {"type": "string",  "description": "Comma-separated author IDs"},
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
            "BEFORE calling: confirm the post ID and all fields to change with the user. "
            "SETTING STATUS TO DRAFT: updates immediately — no dry_run step needed, saving as draft is reversible. "
            "ALL OTHER UPDATES (content, publish, schedule, etc.): "
            "dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply. "
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
                "confirm_publish":     {"type": "boolean", "description": "Must be true when setting status=Published with dry_run=false. Prevents accidental publishing."},
            },
        },
    },
    {
        "name": "cms_delete_post",
        "description": (
            "Permanently delete a post and all its associated data. This action CANNOT be undone. "
            "BEFORE calling: confirm the post ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full post details — no deletion. "
            "Show the preview to the user. Once they explicitly confirm deletion, call again with dry_run=false AND confirm_delete=true."
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

    # ── Live Blog Updates ─────────────────────────────────────────────────────

    {
        "name": "cms_list_live_blog_updates",
        "description": (
            "List all update entries for a LiveBlog post, ordered by creation time descending. Only applies to posts with type LiveBlog. "
            "NOTE: the CDS get_live_blog_updates tool also returns live blog updates for published posts — prefer it for read-only queries. "
            "Always ask the user for the post_id before calling if not already provided. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id"],
            "properties": {
                "post_id": {"type": "integer", "description": "The LiveBlog post ID"},
            },
        },
    },
    {
        "name": "cms_get_live_blog_update",
        "description": (
            "Retrieve a single live blog update entry by its ID. Only applies to posts with type LiveBlog. "
            "Always ask the user for both post_id and update id before calling if not already provided. "
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
            "BEFORE calling: confirm the post_id, title, and content with the user. "
            "Workflow: dry_run=true (default) shows a preview of the update — no changes made. "
            "Once the user confirms the preview, call again with dry_run=false to add the entry."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "title", "content"],
            "properties": {
                "post_id": {"type": "integer", "description": "The LiveBlog post ID"},
                "title":   {"type": "string",  "description": "Headline for this update entry"},
                "content": {"type": "string",  "description": "HTML body content for this update entry"},
                "dry_run": {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "cms_update_live_blog_update",
        "description": (
            "Update an existing live blog update entry. Only applies to posts with type LiveBlog. "
            "BEFORE calling: confirm the post_id, update id, and fields to change with the user. "
            "Workflow: dry_run=true (default) fetches the current entry and shows a diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id", "id"],
            "properties": {
                "post_id": {"type": "integer", "description": "The LiveBlog post ID"},
                "id":      {"type": "integer", "description": "The live blog update entry ID"},
                "title":   {"type": "string",  "description": "New headline for this update entry"},
                "content": {"type": "string",  "description": "New HTML body content"},
                "dry_run": {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "cms_delete_live_blog_update",
        "description": (
            "Permanently delete a live blog update entry. This action CANNOT be undone. "
            "BEFORE calling: confirm the post_id and update id with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full entry — no deletion. "
            "Show the preview to the user. Once they explicitly confirm, call again with dry_run=false AND confirm_delete=true."
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

    # ── Custom Components ─────────────────────────────────────────────────────

    {
        "name": "cms_list_custom_components",
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
        "name": "cms_get_custom_component",
        "description": "Retrieve a single custom component by ID. Always ask the user for the component ID before calling if not already provided. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Custom component ID"},
            },
        },
    },
    {
        "name": "cms_create_custom_component",
        "description": (
            "Create a new custom component in the CMS. "
            "BEFORE calling: confirm the component name and content with the user. "
            "Workflow: dry_run=true (default) shows a preview of what will be created — no changes made. "
            "Once the user confirms the preview, call again with dry_run=false to create."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name":    {"type": "string",  "description": "Component name"},
                "content": {"type": "string",  "description": "Component HTML/template content"},
                "dry_run": {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
    {
        "name": "cms_update_custom_component",
        "description": (
            "Update an existing custom component. "
            "BEFORE calling: confirm the component ID and all fields to change with the user. "
            "Workflow: dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":      {"type": "integer", "description": "Custom component ID"},
                "name":    {"type": "string",  "description": "New component name"},
                "content": {"type": "string",  "description": "New component HTML/template content"},
                "dry_run": {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
    {
        "name": "cms_delete_custom_component",
        "description": (
            "Permanently delete a custom component. This action CANNOT be undone. "
            "BEFORE calling: confirm the component ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full component — no deletion. "
            "Show the preview to the user. Once they explicitly confirm, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Custom component ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },

    # ── Validation (read-only pre-flight checks) ──────────────────────────────

    {
        "name": "validate_media_exists",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a media asset with the given ID exists in the CMS library. "
            "Returns {valid: true, id, filename, path} if found, "
            "{valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Media asset ID to validate"},
            },
        },
    },
    {
        "name": "validate_category_exists",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a category with the given ID exists in the CMS. "
            "Returns {valid: true, id, name} if found, "
            "{valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Category ID to validate"},
            },
        },
    },
    {
        "name": "validate_author_exists",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a contributor/author with the given ID exists via the CDS. "
            "Returns {valid: true, id, name} if found, "
            "{valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Author/contributor ID to validate"},
            },
        },
    },
    {
        "name": "validate_post_slug",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a post slug is available (not yet taken) in the CMS. "
            "Returns {valid: true, slug, available: true} if the slug is free, "
            "{valid: false, reason} if it is already taken by an existing post."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["slug"],
            "properties": {
                "slug": {"type": "string", "description": "URL slug to check for availability"},
            },
        },
    },

    # ── Media Library ─────────────────────────────────────────────────────────

    {
        "name": "cms_list_media",
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
        "name": "cms_get_media",
        "description": "Retrieve a single media asset from the CMS library by ID. Always ask the user for the media ID before calling if not already provided. Returns results directly — no confirmation step needed.",
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "integer", "description": "Media asset ID"},
            },
        },
    },
    {
        "name": "cms_create_media",
        "description": (
            "Register an existing media URL into the CMS library. "
            "Important: this does NOT upload files — it registers an external URL (e.g. from S3, Cloudinary). "
            "BEFORE calling: confirm the filename, path (URL), and optional metadata with the user. "
            "Workflow: dry_run=true (default) shows a preview of what will be registered — no changes made. "
            "Once the user confirms the preview, call again with dry_run=false to register. "
            "Immutable after creation: path, type."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["filename", "path"],
            "properties": {
                "filename":  {"type": "string",  "description": "Filename (e.g. hero-image.jpg)"},
                "path":      {"type": "string",  "description": "Direct external media URL (e.g. S3 or Cloudinary URL). Immutable after creation."},
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
        "name": "cms_update_media",
        "description": (
            "Update metadata of an existing media asset. "
            "BEFORE calling: confirm the media ID and all fields to change with the user. "
            "Workflow: dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply. "
            "Immutable fields that cannot be changed: path, type."
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
        "name": "cms_delete_media",
        "description": (
            "Permanently delete a media asset from the library. This action CANNOT be undone. "
            "Posts referencing this media will lose their associated image or file. "
            "BEFORE calling: confirm the media ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows the full asset details — no deletion. "
            "Show the preview to the user. Once they explicitly confirm, call again with dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Media asset ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },
]


# ── Preview formatters ────────────────────────────────────────────────────────

def _fmt_val(v) -> str:
    """Format a value for human-readable display in preview text."""
    if v is None:
        return "(empty)"
    s = str(v)
    if len(s) > 120:
        return s[:120] + "…"
    return s


def _preview_create(resource: str, payload: dict) -> str:
    lines = [
        f"📋  DRY RUN — Create {resource}",
        "─" * 52,
        f"Will create a new {resource.lower()} with the following details:",
        "",
    ]
    for k, v in payload.items():
        lines.append(f"  {k:<28} {_fmt_val(v)}")
    lines += [
        "",
        "⚡  No changes have been made.",
        f"To proceed, call cms_create_{resource.lower().replace(' ', '_')} again with dry_run=false.",
    ]
    return "\n".join(lines)


def _preview_update(resource: str, item_id: int, current: dict, changes: dict) -> str:
    lines = [
        f"📋  DRY RUN — Update {resource} #{item_id}",
        "─" * 52,
        "The following fields will change:",
        "",
    ]
    has_diff = False
    for field, new_val in changes.items():
        old_val = current.get(field)
        lines.append(f"  {field:<28} {_fmt_val(old_val)}  →  {_fmt_val(new_val)}")
        has_diff = True
    if not has_diff:
        lines.append("  (no fields provided — nothing will change)")
    lines += [
        "",
        "⚡  No changes have been made.",
        f"To apply, call again with dry_run=false.",
    ]
    return "\n".join(lines)


def _preview_delete(resource: str, item_id: int, item: dict, warning: str = "") -> str:
    lines = [
        f"📋  DRY RUN — Delete {resource} #{item_id}",
        "─" * 52,
        f"⚠️   WARNING: This will PERMANENTLY delete the following {resource.lower()}:",
        "",
    ]
    for k, v in item.items():
        lines.append(f"  {k:<28} {_fmt_val(v)}")
    if warning:
        lines += ["", f"⚠️   {warning}"]
    lines += [
        "",
        "⚡  No changes have been made.",
        "To permanently delete, call again with:",
        "  dry_run=false",
        "  confirm_delete=true",
    ]
    return "\n".join(lines)


# ── Confirmation guard (reused by all delete handlers) ───────────────────────

_CONFIRM_REQUIRED = {
    "error_type": "confirmation_required",
    "message": (
        "Deletion requires BOTH dry_run=false AND confirm_delete=true. "
        "Call again with both parameters set to confirm you want to permanently delete this resource."
    ),
    "retryable": False,
}


# ── Tool dispatcher ───────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="call_cms_tool", group="Tool")
def call_cms_tool(credentials, name, args):  # noqa: C901
    add_attrs([("mcp.tool_name", name)])
    args = args or {}
    logger.debug("call_cms_tool: tool=%s args_count=%d", name, len(args))

    with _active_cms_tool_calls_lock:
        _active_cms_tool_calls[name] += 1
        concurrency = _active_cms_tool_calls[name]
    add_attrs([("mcp.tool_concurrency", concurrency)])
    newrelic.agent.record_custom_metric(f"Custom/Tool/{name}/active_calls", concurrency)

    try:
        # ── Categories ────────────────────────────────────────────────────────

        if name == "cms_list_categories":
            with fn_trace("cms_list_categories", group="Tool"):
                return cms_get(credentials, "/category/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
                })

        if name == "cms_get_category":
            with fn_trace("cms_get_category", group="Tool"):
                return cms_get(credentials, f"/category/{args['id']}/")

        if name == "cms_create_category":
            with fn_trace("cms_create_category", group="Tool"):
                dry_run = args.get("dry_run", True)
                payload  = {k: v for k, v in args.items() if k != "dry_run"}
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create("Category", payload)}
                return cms_post(credentials, "/category/", payload)

        if name == "cms_update_category":
            with fn_trace("cms_update_category", group="Tool"):
                dry_run     = args.get("dry_run", True)
                category_id = args["id"]
                changes     = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
                if dry_run:
                    current = cms_get(credentials, f"/category/{category_id}/")
                    if "error_type" in current:
                        return current
                    return {"dry_run": True, "preview": _preview_update("Category", category_id, current, changes)}
                return cms_patch(credentials, f"/category/{category_id}/", changes)

        if name == "cms_delete_category":
            with fn_trace("cms_delete_category", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                category_id    = args["id"]
                if dry_run:
                    item = cms_get(credentials, f"/category/{category_id}/")
                    if "error_type" in item:
                        return item
                    return {"dry_run": True, "preview": _preview_delete(
                        "Category", category_id, item,
                        warning="Posts assigned to this category will lose their category assignment.",
                    )}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/category/{category_id}/")

        # ── Tags ──────────────────────────────────────────────────────────────

        if name == "cms_list_tags":
            with fn_trace("cms_list_tags", group="Tool"):
                return cms_get(credentials, "/tag/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
                })

        if name == "cms_get_tag":
            with fn_trace("cms_get_tag", group="Tool"):
                return cms_get(credentials, f"/tag/{args['id']}/")

        if name == "cms_create_tag":
            with fn_trace("cms_create_tag", group="Tool"):
                dry_run = args.get("dry_run", True)
                payload  = {k: v for k, v in args.items() if k != "dry_run"}
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create("Tag", payload)}
                return cms_post(credentials, "/tag/", payload)

        if name == "cms_update_tag":
            with fn_trace("cms_update_tag", group="Tool"):
                dry_run = args.get("dry_run", True)
                tag_id  = args["id"]
                changes = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
                if dry_run:
                    current = cms_get(credentials, f"/tag/{tag_id}/")
                    if "error_type" in current:
                        return current
                    return {"dry_run": True, "preview": _preview_update("Tag", tag_id, current, changes)}
                return cms_patch(credentials, f"/tag/{tag_id}/", changes)

        if name == "cms_delete_tag":
            with fn_trace("cms_delete_tag", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                tag_id         = args["id"]
                if dry_run:
                    item = cms_get(credentials, f"/tag/{tag_id}/")
                    if "error_type" in item:
                        return item
                    return {"dry_run": True, "preview": _preview_delete("Tag", tag_id, item)}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/tag/{tag_id}/")

        # ── Posts ─────────────────────────────────────────────────────────────

        if name == "cms_list_posts":
            with fn_trace("cms_list_posts", group="Tool"):
                return cms_get(credentials, "/post/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
                })

        if name == "cms_get_post":
            with fn_trace("cms_get_post", group="Tool"):
                return cms_get(credentials, f"/post/{args['id']}/")

        if name == "cms_create_post":
            with fn_trace("cms_create_post", group="Tool"):
                dry_run = args.get("dry_run", True)
                payload  = {k: v for k, v in args.items() if k != "dry_run"}
                # Draft posts are reversible — create directly, no preview step
                if payload.get("status") == "Draft":
                    return cms_post(credentials, "/post/", payload)
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create("Post", payload)}
                return cms_post(credentials, "/post/", payload)

        if name == "cms_update_post":
            with fn_trace("cms_update_post", group="Tool"):
                dry_run         = args.get("dry_run", True)
                confirm_publish = args.get("confirm_publish", False)
                post_id         = args["id"]
                changes         = {k: v for k, v in args.items() if k not in ("id", "dry_run", "confirm_publish")}
                # Saving as Draft is reversible — skip dry_run
                if changes.get("status") == "Draft":
                    return cms_patch(credentials, f"/post/{post_id}/", changes)
                if dry_run:
                    current = cms_get(credentials, f"/post/{post_id}/")
                    if "error_type" in current:
                        return current
                    return {"dry_run": True, "preview": _preview_update("Post", post_id, current, changes)}
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

        if name == "cms_delete_post":
            with fn_trace("cms_delete_post", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                post_id        = args["id"]
                if dry_run:
                    item = cms_get(credentials, f"/post/{post_id}/")
                    if "error_type" in item:
                        return item
                    return {"dry_run": True, "preview": _preview_delete(
                        "Post", post_id, item,
                        warning="This post and ALL its associated data will be permanently removed.",
                    )}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/post/{post_id}/")

        # ── Live Blog Updates ─────────────────────────────────────────────────

        if name == "cms_list_live_blog_updates":
            with fn_trace("cms_list_live_blog_updates", group="Tool"):
                post_id = args["post_id"]
                return cms_get(credentials, f"/post/{post_id}/live-blog-update/")

        if name == "cms_get_live_blog_update":
            with fn_trace("cms_get_live_blog_update", group="Tool"):
                post_id   = args["post_id"]
                update_id = args["id"]
                return cms_get(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")

        if name == "cms_create_live_blog_update":
            with fn_trace("cms_create_live_blog_update", group="Tool"):
                dry_run = args.get("dry_run", True)
                post_id = args["post_id"]
                payload = {k: v for k, v in args.items() if k not in ("dry_run", "post_id")}
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create(
                        "Live Blog Update",
                        {"post_id": post_id, **payload},
                    )}
                return cms_post(credentials, f"/post/{post_id}/live-blog-update/", payload)

        if name == "cms_update_live_blog_update":
            with fn_trace("cms_update_live_blog_update", group="Tool"):
                dry_run   = args.get("dry_run", True)
                post_id   = args["post_id"]
                update_id = args["id"]
                changes   = {k: v for k, v in args.items() if k not in ("post_id", "id", "dry_run")}
                if dry_run:
                    raw = cms_get(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")
                    if "error_type" in raw:
                        return raw
                    # The retrieve endpoint wraps the entry in a "data" key; flatten the
                    # nested content object so the diff shows title / content directly.
                    entry = raw.get("data", raw)
                    if isinstance(entry.get("content"), dict):
                        flat_current = {
                            "title":   entry["content"].get("title"),
                            "content": entry["content"].get("content"),
                        }
                    else:
                        flat_current = entry
                    return {"dry_run": True, "preview": _preview_update(
                        "Live Blog Update", update_id, flat_current, changes,
                    )}
                return cms_patch(credentials, f"/post/{post_id}/live-blog-update/{update_id}/", changes)

        if name == "cms_delete_live_blog_update":
            with fn_trace("cms_delete_live_blog_update", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                post_id        = args["post_id"]
                update_id      = args["id"]
                if dry_run:
                    raw = cms_get(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")
                    if "error_type" in raw:
                        return raw
                    entry = raw.get("data", raw)
                    return {"dry_run": True, "preview": _preview_delete("Live Blog Update", update_id, entry)}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/post/{post_id}/live-blog-update/{update_id}/")

        # ── Media Library ─────────────────────────────────────────────────────

        if name == "cms_list_media":
            with fn_trace("cms_list_media", group="Tool"):
                return cms_get(credentials, "/media-library/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
                })

        if name == "cms_get_media":
            with fn_trace("cms_get_media", group="Tool"):
                return cms_get(credentials, f"/media-library/{args['id']}/")

        if name == "cms_create_media":
            with fn_trace("cms_create_media", group="Tool"):
                dry_run = args.get("dry_run", True)
                payload  = {k: v for k, v in args.items() if k != "dry_run"}
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create("Media", payload)}
                return cms_post(credentials, "/media-library/", payload)

        if name == "cms_update_media":
            with fn_trace("cms_update_media", group="Tool"):
                dry_run  = args.get("dry_run", True)
                media_id = args["id"]
                changes  = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
                if dry_run:
                    current = cms_get(credentials, f"/media-library/{media_id}/")
                    if "error_type" in current:
                        return current
                    return {"dry_run": True, "preview": _preview_update("Media", media_id, current, changes)}
                return cms_patch(credentials, f"/media-library/{media_id}/", changes)

        if name == "cms_delete_media":
            with fn_trace("cms_delete_media", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                media_id       = args["id"]
                if dry_run:
                    item = cms_get(credentials, f"/media-library/{media_id}/")
                    if "error_type" in item:
                        return item
                    return {"dry_run": True, "preview": _preview_delete(
                        "Media", media_id, item,
                        warning="Posts referencing this media will lose their associated image or file.",
                    )}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/media-library/{media_id}/")

        # ── Custom Components ─────────────────────────────────────────────────

        if name == "cms_list_custom_components":
            with fn_trace("cms_list_custom_components", group="Tool"):
                return cms_get(credentials, "/custom-component/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
                })

        if name == "cms_get_custom_component":
            with fn_trace("cms_get_custom_component", group="Tool"):
                return cms_get(credentials, f"/custom-component/{args['id']}/")

        if name == "cms_create_custom_component":
            with fn_trace("cms_create_custom_component", group="Tool"):
                dry_run = args.get("dry_run", True)
                payload  = {k: v for k, v in args.items() if k != "dry_run"}
                if dry_run:
                    return {"dry_run": True, "preview": _preview_create("Custom Component", payload)}
                return cms_post(credentials, "/custom-component/", payload)

        if name == "cms_update_custom_component":
            with fn_trace("cms_update_custom_component", group="Tool"):
                dry_run      = args.get("dry_run", True)
                component_id = args["id"]
                changes      = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
                if dry_run:
                    current = cms_get(credentials, f"/custom-component/{component_id}/")
                    if "error_type" in current:
                        return current
                    return {"dry_run": True, "preview": _preview_update("Custom Component", component_id, current, changes)}
                return cms_patch(credentials, f"/custom-component/{component_id}/", changes)

        if name == "cms_delete_custom_component":
            with fn_trace("cms_delete_custom_component", group="Tool"):
                dry_run        = args.get("dry_run", True)
                confirm_delete = args.get("confirm_delete", False)
                component_id   = args["id"]
                if dry_run:
                    item = cms_get(credentials, f"/custom-component/{component_id}/")
                    if "error_type" in item:
                        return item
                    return {"dry_run": True, "preview": _preview_delete("Custom Component", component_id, item)}
                if not confirm_delete:
                    return _CONFIRM_REQUIRED
                return cms_delete(credentials, f"/custom-component/{component_id}/")

        # ── Validation ────────────────────────────────────────────────────────

        if name == "validate_media_exists":
            with fn_trace("validate_media_exists", group="Tool"):
                media_id = args["id"]
                result = cms_get(credentials, f"/media-library/{media_id}/")
                if "error_type" in result:
                    return {"valid": False, "reason": f"Media ID {media_id} not found."}
                return {"valid": True, "id": media_id, "filename": result.get("filename"), "path": result.get("path")}

        if name == "validate_category_exists":
            with fn_trace("validate_category_exists", group="Tool"):
                category_id = args["id"]
                result = cms_get(credentials, f"/category/{category_id}/")
                if "error_type" in result:
                    return {"valid": False, "reason": f"Category ID {category_id} not found."}
                return {"valid": True, "id": category_id, "name": result.get("name")}

        if name == "validate_author_exists":
            with fn_trace("validate_author_exists", group="Tool"):
                author_id = args["id"]
                result = cds_get(credentials, f"/contributor/{author_id}/")
                if "error_type" in result:
                    return {"valid": False, "reason": f"Author ID {author_id} not found."}
                return {"valid": True, "id": author_id, "name": result.get("name")}

        if name == "validate_post_slug":
            with fn_trace("validate_post_slug", group="Tool"):
                slug = args["slug"]
                result = cms_get(credentials, f"/post/{slug}/")
                if "error_type" in result:
                    return {"valid": True, "slug": slug, "available": True}
                return {"valid": False, "reason": f"Slug '{slug}' is already taken by post ID {result.get('id')}."}

        logger.warning("call_cms_tool: unknown tool: name=%s", name)
        raise Exception(f"Unknown CMS tool: {name}")

    except Exception as exc:
        notice_err(exc, [
            ("error.layer", "cms_tool"),
            ("error.tool_name", name),
        ])
        raise
    finally:
        with _active_cms_tool_calls_lock:
            _active_cms_tool_calls[name] = max(0, _active_cms_tool_calls[name] - 1)
