"""CDS read tools for posts and live blog updates."""
import logging

import requests

from ..clients.cds import cds_get
from ..nr_utils import add_attrs

logger = logging.getLogger(__name__)

SCHEMAS = [
    {
        "name": "fetch_published_posts",
        "description": (
            "List and filter published posts. Supports filtering by type, category, tag, author, date range, title search, and pagination. "
            "Returns only published content. If the user asks for less (e.g. just titles or a quick count), return a summary and offer to fetch more details. "
            "If the user needs drafts or scheduled posts, suggest list_editorial_posts instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":                    {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit":                   {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
                "type__eq":                {"type": "string",  "description": "Filter by exact type: Article, Video, Web Story, Gallery, LiveBlog"},
                "type__neq":               {"type": "string",  "description": "Exclude a specific type"},
                "type__in":                {"type": "string",  "description": "Include multiple types comma-separated e.g. Article,Video"},
                "type__nin":               {"type": "string",  "description": "Exclude multiple types comma-separated"},
                "title__contains":         {"type": "string",  "description": "Search by title substring"},
                "categories.id__eq":       {"type": "integer", "description": "Filter by category ID"},
                "categories.id__in":       {"type": "string",  "description": "Filter by multiple category IDs comma-separated"},
                "categories.id__nin":      {"type": "string",  "description": "Exclude multiple category IDs comma-separated"},
                "tags.id__eq":             {"type": "integer", "description": "Filter by tag ID"},
                "tags.id__in":             {"type": "string",  "description": "Filter by multiple tag IDs comma-separated"},
                "tags.id__nin":            {"type": "string",  "description": "Exclude multiple tag IDs comma-separated"},
                "contributors.id__eq":     {"type": "integer", "description": "Filter by author ID"},
                "contributors.id__in":     {"type": "string",  "description": "Filter by multiple author IDs comma-separated"},
                "created_at__gte":         {"type": "string",  "description": "Posts created on or after (ISO 8601)"},
                "created_at__lte":         {"type": "string",  "description": "Posts created on or before (ISO 8601)"},
                "primary_category.id__eq": {"type": "integer", "description": "Filter by primary category ID"},
                "primary_category.id__in": {"type": "string",  "description": "Filter by multiple primary category IDs comma-separated"},
                "word_count__gt":               {"type": "integer", "description": "Word count greater than"},
                "word_count__lt":               {"type": "integer", "description": "Word count less than"},
                "primary_category__slug__eq":   {"type": "string",  "description": "Filter by primary category slug (e.g. 'technology')"},
                "contributors__slug__eq":       {"type": "string",  "description": "Filter by author slug (e.g. 'jane-doe')"},
                "tags__slug__eq":               {"type": "string",  "description": "Filter by tag slug (e.g. 'breaking-news')"},
                "sort_by":                      {"type": "string",  "description": "Sort field — e.g. created_at"},
                "sort_order":                   {"type": "string",  "description": "Sort direction: asc or desc"},
            },
        },
    },
    {
        "name": "fetch_published_post",
        "description": (
            "Get full details of a single published post by ID or slug. "
            "If the user only needs a few fields (e.g. just the title or author), return only those and offer more. "
            "If the user needs draft/scheduled post details, suggest get_editorial_post instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Post ID or slug"},
            },
        },
    },
    {
        "name": "fetch_post_by_url",
        "description": (
            "Get a post by its legacy or relative URL path. "
            "IMPORTANT: legacy_url must be a non-empty relative path starting with / "
            "(e.g. /business/article-slug-12345). Do not call with an empty string or missing path."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["legacy_url"],
            "properties": {
                "legacy_url": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Non-empty relative URL path starting with /. Example: /business/article-slug-12345",
                },
            },
        },
    },
    {
        "name": "fetch_livebupdates",
        "description": (
            "Get published live blog updates for a LiveBlog post. "
            "If the user needs to add, edit, or delete update entries, use the CMS live blog update tools instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id"],
            "properties": {
                "post_id": {"type": "integer", "description": "LiveBlog post ID"},
                "page":    {"type": "integer"},
                "limit":   {"type": "integer"},
            },
        },
    },
    {
        "name": "fetch_liveblog_with_updates",
        "description": (
            "Get a LiveBlog post and all its published update entries in a single call. "
            "Returns the full post object alongside a paginated list of updates. "
            "Only works for posts with type LiveBlog — returns an error for any other post type. "
            "If the user only needs the update entries without post metadata, use fetch_livebupdates instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["post_id"],
            "properties": {
                "post_id": {"type": "integer", "description": "LiveBlog post ID"},
                "page":    {"type": "integer", "description": "Page number for updates (default: 1, max: 1000)"},
                "limit":   {"type": "integer", "description": "Updates per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "fetch_trending_posts",
        "description": (
            "Get top-performing posts ranked by page views over a time window. "
            "Requires Publive analytics to be active. Rankings refresh every 5-10 minutes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "duration": {"type": "string",  "description": "Analytics window: 24h (default), 7d, or 30d"},
                "limit":    {"type": "integer", "description": "Items per page (default: 20, max: 50)"},
                "page":     {"type": "integer", "description": "Page number (default: 1)"},
                "type__eq": {"type": "string",  "description": "Filter by post type: Article, Video, Web Story, Gallery, LiveBlog, CustomPage, CustomEntity, or Newsletter"},
            },
        },
    },
]


def fetch_published_posts(credentials: dict, args: dict):
    page  = args.pop("page",  None)
    limit = args.pop("limit", None)
    try:
        return cds_get(credentials, "/posts/", {"page": page, "limit": limit, **args})
    except Exception as exc:
        is_timeout = (
            isinstance(exc, requests.exceptions.Timeout)
            or getattr(getattr(exc, "response", None), "status_code", None) == 408
            or "408" in str(exc)
            or "timed out" in str(exc).lower()
        )
        if is_timeout:
            logger.warning("fetch_published_posts: upstream timeout — returning structured error")
            return {
                "error": "upstream_timeout",
                "retry": True,
                "message": (
                    "The CDS /posts/ endpoint timed out. "
                    "Try narrowing your query: use a shorter date range, fewer filters, or a smaller page size."
                ),
            }
        raise


def fetch_published_post(credentials: dict, args: dict):
    return cds_get(credentials, f"/post/{args['identifier']}/")


def fetch_post_by_url(credentials: dict, args: dict):
    legacy_url = args.get("legacy_url", "").strip()
    if not legacy_url:
        logger.warning("fetch_post_by_url: called with empty legacy_url")
        return {
            "error": "invalid_input",
            "message": (
                "legacy_url is required and cannot be empty. "
                "Provide a non-empty relative URL path starting with /, "
                "e.g. /business/article-slug-12345."
            ),
        }
    return cds_get(credentials, "/post/", {"legacy_url": legacy_url})


def fetch_livebupdates(credentials: dict, args: dict):
    return cds_get(credentials, f"/post/{args['post_id']}/live-blog-updates/", {
        "page":  args.get("page"),
        "limit": args.get("limit"),
    })


def fetch_liveblog_with_updates(credentials: dict, args: dict):
    post_id = args["post_id"]
    post    = cds_get(credentials, f"/post/{post_id}/")
    if isinstance(post, dict) and "error_type" in post:
        return post
    post_data = post.get("data", post) if isinstance(post, dict) else post
    if isinstance(post_data, dict) and post_data.get("type") != "LiveBlog":
        return {
            "error": "invalid_input",
            "message": (
                f"Post {post_id} is a '{post_data.get('type')}' post, not a LiveBlog. "
                "This tool only works for LiveBlog-type posts."
            ),
        }
    updates = cds_get(credentials, f"/post/{post_id}/live-blog-updates/", {
        "page":  args.get("page"),
        "limit": args.get("limit"),
    })
    return {"post": post, "updates": updates}


def fetch_trending_posts(credentials: dict, args: dict):
    return cds_get(credentials, "/posts/trending/", {
        "duration": args.get("duration"),
        "limit":    args.get("limit"),
        "page":     args.get("page"),
        "type":     args.get("type__eq"),
    })


HANDLERS = {
    "fetch_published_posts":       fetch_published_posts,
    "fetch_published_post":        fetch_published_post,
    "fetch_post_by_url":           fetch_post_by_url,
    "fetch_livebupdates":          fetch_livebupdates,
    "fetch_liveblog_with_updates": fetch_liveblog_with_updates,
    "fetch_trending_posts":        fetch_trending_posts,
}
