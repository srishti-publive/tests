import collections
import contextlib
import logging
import threading

import newrelic.agent

from .cds_client import cds_get
from .nr_utils import add_attrs, fn_trace, notice_err

logger = logging.getLogger(__name__)

# Per-tool concurrency tracking — how many calls to each tool are in-flight.
_active_tool_calls: dict[str, int] = collections.defaultdict(int)
_active_tool_calls_lock = threading.Lock()


# ── Concurrency context manager ───────────────────────────────────────────────

@contextlib.contextmanager
def _tool_active_calls(name: str):
    """Track in-flight calls for a single tool and expose concurrency as an NR metric.

    Previously the increment and decrement were inlined at the top and in the
    finally block of call_tool, mixing concurrency bookkeeping with routing logic.
    A context manager isolates both concerns and makes the pattern reusable.
    """
    with _active_tool_calls_lock:
        _active_tool_calls[name] += 1
        concurrency = _active_tool_calls[name]
    try:
        yield concurrency
    finally:
        with _active_tool_calls_lock:
            _active_tool_calls[name] = max(0, _active_tool_calls[name] - 1)


# ── Auth error handler ────────────────────────────────────────────────────────

def _handle_cds_auth_error(name: str) -> dict:
    """Return a structured auth_expired dict for CDS 401 responses.

    Previously inlined in the except block of call_tool, mixing cross-cutting
    auth error handling with the tool routing logic.
    """
    logger.warning(
        "call_tool: CDS rejected credentials (HTTP 401): tool=%s — returning auth_expired", name
    )
    add_attrs([
        ("mcp.tool_auth_error", True),
        ("error.layer",         "tool"),
        ("error.tool_name",     name),
    ])
    return {
        "error": "auth_expired",
        "message": (
            "Your CDS credentials were rejected (HTTP 401). "
            "Please re-authenticate: visit /connect or re-run the OAuth flow."
        ),
    }


# ── Tool catalogue ────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_posts",
        "description": (
            "List and filter published posts. Supports filtering by type, category, tag, author, date range, title search, and pagination. "
            "Returns only published content. If the user asks for less (e.g. just titles or a quick count), return a summary and offer to fetch more details. "
            "If the user needs drafts or scheduled posts, suggest cms_list_posts instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":                         {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit":                        {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
                "type__eq":                     {"type": "string",  "description": "Filter by exact type: Article, Video, Web Story, Gallery, LiveBlog"},
                "type__neq":                    {"type": "string",  "description": "Exclude a specific type"},
                "type__in":                     {"type": "string",  "description": "Include multiple types comma-separated e.g. Article,Video"},
                "type__nin":                    {"type": "string",  "description": "Exclude multiple types comma-separated"},
                "title__contains":              {"type": "string",  "description": "Search by title substring"},
                "categories.id__eq":            {"type": "integer", "description": "Filter by category ID"},
                "categories.id__in":            {"type": "string",  "description": "Filter by multiple category IDs comma-separated"},
                "categories.id__nin":           {"type": "string",  "description": "Exclude multiple category IDs comma-separated"},
                "tags.id__eq":                  {"type": "integer", "description": "Filter by tag ID"},
                "tags.id__in":                  {"type": "string",  "description": "Filter by multiple tag IDs comma-separated"},
                "tags.id__nin":                 {"type": "string",  "description": "Exclude multiple tag IDs comma-separated"},
                "contributors.id__eq":          {"type": "integer", "description": "Filter by author ID"},
                "contributors.id__in":          {"type": "string",  "description": "Filter by multiple author IDs comma-separated"},
                "created_at__gte":              {"type": "string",  "description": "Posts created on or after (ISO 8601)"},
                "created_at__lte":              {"type": "string",  "description": "Posts created on or before (ISO 8601)"},
                "primary_category.id__eq":      {"type": "integer", "description": "Filter by primary category ID"},
                "primary_category.id__in":      {"type": "string",  "description": "Filter by multiple primary category IDs comma-separated"},
                "word_count__gt":               {"type": "integer", "description": "Word count greater than"},
                "word_count__lt":               {"type": "integer", "description": "Word count less than"},
            },
        },
    },
    {
        "name": "get_post",
        "description": (
            "Get full details of a single published post by ID or slug. "
            "If the user only needs a few fields (e.g. just the title or author), return only those and offer more. "
            "If the user needs draft/scheduled post details, suggest cms_get_post instead."
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
        "name": "get_post_by_url",
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
        "name": "list_categories",
        "description": (
            "List all published categories with hierarchical structure. "
            "If the user only needs a quick count or names, return a summary and offer more details. "
            "If the user needs unpublished categories or management operations, suggest cms_list_categories instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_category",
        "description": (
            "Get a single published category by ID or slug including SEO metadata and child categories. "
            "If the user only needs basic info (name, slug), return that and offer more. "
            "If the user needs management fields or plans to update, suggest cms_get_category instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Category ID or slug"},
            },
        },
    },
    {
        "name": "list_tags",
        "description": (
            "List all published tags. "
            "If the user only needs a quick count or names, return a summary and offer more. "
            "If the user needs unpublished tags or management operations, suggest cms_list_tags instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_tag",
        "description": (
            "Get a single published tag by ID or slug. "
            "If the user needs management fields or plans to update, suggest cms_get_tag instead."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Tag ID or slug"},
            },
        },
    },
    {
        "name": "list_authors",
        "description": "List all authors/contributors for this publication with pagination.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit": {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            },
        },
    },
    {
        "name": "get_author",
        "description": (
            "Get a single author by their numeric ID. "
            "identifier must be a numeric author ID. "
            "To find posts by a specific author, use list_posts with the contributors.id__eq filter. "
            "Do not guess IDs or pass non-numeric values."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {
                    "type": "string",
                    "description": "Numeric author ID (e.g. \"42\").",
                },
            },
        },
    },
    {
        "name": "get_publisher_data",
        "description": "Get publisher profile: branding, logo, accent colors, social links, app store URLs, and site metadata. Always call this first for any branding or publisher identity question — it automatically falls back to footer data if the primary endpoint is unavailable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "identify_content",
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
        "name": "get_live_blog_updates",
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
        "name": "get_trending_posts",
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
    {
        "name": "get_navbar",
        "description": "Get the navigation menu configuration including nested menu items and links.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_footer",
        "description": "Get the footer layout: menus, links, copyright text, app store URLs, social links, and logo. Use this for footer structure and navigation links. For publisher branding questions (logo, colors, identity), prefer get_publisher_data which aggregates from multiple sources.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_active_slots",
        "description": "Get configured advertisement slots with dimensions, HTML content, and slot type information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_newsletter_groups",
        "description": (
            "Get all configured newsletter groups with their metadata, logos, and descriptions. "
            "NOTE: this only works for publishers that have a newsletter email configured. "
            "If the publisher has no newsletter set up, this tool returns a not_configured error — "
            "do not retry in that case."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_content_types",
        "description": "Get all content types configured for this publication (e.g. Article, Video, Web Story) with their API and collection slugs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_form_schema",
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


# ── Tool routing ──────────────────────────────────────────────────────────────

def _route_tool(credentials, name, args):
    """Pure tool routing — no concurrency tracking or cross-cutting error handling.

    Extracted so that call_tool() owns only the lifecycle wrapper (concurrency,
    NR attrs, 401 interception) while this function owns the routing logic.
    """
    if name == "list_posts":
        with fn_trace("list_posts", group="Tool"):
            page  = args.pop("page",  None)
            limit = args.pop("limit", None)
            try:
                return cds_get(credentials, "/posts/", {"page": page, "limit": limit, **args})
            except Exception as exc:
                is_timeout = (
                    isinstance(exc, __import__("requests").exceptions.Timeout)
                    or getattr(getattr(exc, "response", None), "status_code", None) == 408
                    or "408" in str(exc)
                    or "timed out" in str(exc).lower()
                )
                if is_timeout:
                    logger.warning("list_posts: upstream timeout after retries — returning structured error")
                    return {
                        "error": "upstream_timeout",
                        "retry": True,
                        "message": (
                            "The CDS /posts/ endpoint timed out. "
                            "Try narrowing your query: use a shorter date range, "
                            "fewer filters, or a smaller page size."
                        ),
                    }
                raise

    if name == "get_post":
        with fn_trace("get_post", group="Tool"):
            return cds_get(credentials, f"/post/{args['identifier']}/")

    if name == "get_post_by_url":
        with fn_trace("get_post_by_url", group="Tool"):
            legacy_url = args.get("legacy_url", "").strip()
            if not legacy_url:
                logger.warning("get_post_by_url: called with empty legacy_url")
                return {
                    "error": "invalid_input",
                    "message": (
                        "legacy_url is required and cannot be empty. "
                        "Provide a non-empty relative URL path starting with /, "
                        "e.g. /business/article-slug-12345."
                    ),
                }
            return cds_get(credentials, "/post/", {"legacy_url": legacy_url})

    if name == "list_categories":
        with fn_trace("list_categories", group="Tool"):
            return cds_get(credentials, "/categories/", {"page": args.get("page"), "limit": args.get("limit")})

    if name == "get_category":
        with fn_trace("get_category", group="Tool"):
            return cds_get(credentials, f"/category/{args['identifier']}/")

    if name == "list_tags":
        with fn_trace("list_tags", group="Tool"):
            return cds_get(credentials, "/tags/", {"page": args.get("page"), "limit": args.get("limit")})

    if name == "get_tag":
        with fn_trace("get_tag", group="Tool"):
            return cds_get(credentials, f"/tag/{args['identifier']}/")

    if name == "list_authors":
        with fn_trace("list_authors", group="Tool"):
            return cds_get(credentials, "/authors/", {"page": args.get("page"), "limit": args.get("limit")})

    if name == "get_author":
        with fn_trace("get_author", group="Tool"):
            identifier = str(args.get("identifier", "")).strip()
            if not identifier:
                return {
                    "error": "invalid_input",
                    "message": "identifier is required. Use list_authors to find valid numeric author IDs.",
                }
            if not identifier.isdigit():
                logger.warning("get_author: non-numeric identifier=%r", identifier)
                return {
                    "error": "invalid_input",
                    "message": (
                        f"Author identifier must be a numeric ID, got {identifier!r}. "
                        "Use list_authors to discover valid author IDs."
                    ),
                }
            try:
                return cds_get(credentials, f"/author/{identifier}/")
            except Exception as exc:
                err_str     = str(exc).lower()
                http_status = getattr(getattr(exc, "response", None), "status_code", None)
                if http_status == 404 or "unknown endpoint" in err_str or "not found" in err_str:
                    return {
                        "error": "not_found",
                        "message": (
                            f"Author with ID {identifier} was not found. "
                            "Use list_authors to discover valid author IDs."
                        ),
                    }
                raise

    if name == "get_publisher_data":
        with fn_trace("get_publisher_data", group="Tool"):
            try:
                return cds_get(credentials, "/publisher-data/")
            except Exception as exc:
                err_str     = str(exc).lower()
                http_status = getattr(getattr(exc, "response", None), "status_code", None)
                is_endpoint_missing = (
                    http_status in (400, 404)
                    or "unknown endpoint" in err_str
                    or "not found" in err_str
                    or "http 404" in err_str
                    or "http 400" in err_str
                    or "no such" in err_str
                )
                if is_endpoint_missing:
                    publisher_id = (credentials or {}).get("publisherId", "unknown")
                    logger.warning(
                        "get_publisher_data: /publisher-data/ unavailable for publisher=%s — falling back to /footer/",
                        publisher_id,
                    )
                    add_attrs([
                        ("mcp.tool_fallback",        "footer"),
                        ("mcp.tool_fallback_reason", "endpoint_unavailable"),
                    ])
                    newrelic.agent.record_custom_metric("Custom/MCP/fallback_count", 1)
                    return cds_get(credentials, "/footer/")
                raise

    if name == "identify_content":
        with fn_trace("identify_content", group="Tool"):
            return cds_get(credentials, "/identify_url/", {"legacy_url": args["legacy_url"]})

    if name == "get_live_blog_updates":
        with fn_trace("get_live_blog_updates", group="Tool"):
            return cds_get(credentials, f"/post/{args['post_id']}/live-blog-updates/", {
                "page":  args.get("page"),
                "limit": args.get("limit"),
            })

    if name == "get_trending_posts":
        with fn_trace("get_trending_posts", group="Tool"):
            return cds_get(credentials, "/posts/trending/", {
                "duration": args.get("duration"),
                "limit":    args.get("limit"),
                "page":     args.get("page"),
                "type":     args.get("type__eq"),
            })

    if name == "get_navbar":
        with fn_trace("get_navbar", group="Tool"):
            return cds_get(credentials, "/navbar/")

    if name == "get_footer":
        with fn_trace("get_footer", group="Tool"):
            return cds_get(credentials, "/footer/")

    if name == "get_active_slots":
        with fn_trace("get_active_slots", group="Tool"):
            return cds_get(credentials, "/active-slots/")

    if name == "get_newsletter_groups":
        with fn_trace("get_newsletter_groups", group="Tool"):
            try:
                return cds_get(credentials, "/newsletter-groups/")
            except Exception as exc:
                err_str     = str(exc).lower()
                http_status = getattr(getattr(exc, "response", None), "status_code", None)
                is_not_configured = (
                    http_status in (400, 404)
                    or "newsletter" in err_str
                    or "email" in err_str
                    or "not configured" in err_str
                    or "unknown endpoint" in err_str
                )
                if is_not_configured:
                    publisher_id = (credentials or {}).get("publisherId", "unknown")
                    logger.warning("get_newsletter_groups: publisher=%s has no newsletter configured", publisher_id)
                    return {
                        "error": "not_configured",
                        "message": (
                            "This publisher has no newsletter email configured. "
                            "Newsletter groups are unavailable."
                        ),
                    }
                raise

    if name == "get_content_types":
        with fn_trace("get_content_types", group="Tool"):
            return cds_get(credentials, "/content-types/")

    if name == "get_form_schema":
        with fn_trace("get_form_schema", group="Tool"):
            return cds_get(credentials, f"/form-schemas/{args['schema_id']}/", {
                "page_source": args.get("page_source"),
            })

    logger.warning("call_tool: unknown tool requested: name=%s", name)
    raise Exception(f"Unknown tool: {name}")


# ── Public dispatcher ─────────────────────────────────────────────────────────

@newrelic.agent.function_trace(name="call_tool", group="Tool")
def call_tool(credentials, name, args):
    """Lifecycle wrapper: concurrency tracking → routing → 401 interception."""
    add_attrs([("mcp.tool_name", name)])
    args = args or {}
    logger.debug("call_tool: tool=%s args_count=%d", name, len(args))

    with _tool_active_calls(name) as concurrency:
        add_attrs([("mcp.tool_concurrency", concurrency)])
        newrelic.agent.record_custom_metric(f"Custom/Tool/{name}/active_calls", concurrency)
        try:
            return _route_tool(credentials, name, args)
        except Exception as exc:
            http_status = getattr(getattr(exc, "response", None), "status_code", None)
            if http_status == 401:
                return _handle_cds_auth_error(name)
            logger.error("call_tool error: tool=%s error=%s", name, exc, exc_info=True)
            notice_err(exc, [
                ("error.layer",    "tool"),
                ("error.tool_name", name),
            ])
            raise
