import logging

import newrelic.agent

from .cds_client import cds_get
from .nr_utils import add_attrs, fn_trace, notice_err

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "list_posts",
        "description": "List and filter published posts. Supports filtering by type, category, tag, author, date range, title search, and pagination.",
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
                "primary_category.id__eq":      {"type": "integer", "description": "Filter by primary category ID"},
                "primary_category.id__in":      {"type": "string",  "description": "Filter by multiple primary category IDs comma-separated"},
                "created_at__gte":              {"type": "string",  "description": "Posts created on or after (ISO 8601)"},
                "created_at__lte":              {"type": "string",  "description": "Posts created on or before (ISO 8601)"},
                "word_count__gt":               {"type": "integer", "description": "Word count greater than"},
                "word_count__lt":               {"type": "integer", "description": "Word count less than"},
            },
        },
    },
    {
        "name": "get_post",
        "description": "Get full details of a single post by ID or slug.",
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
        "description": "Get a post by its legacy or relative URL path.",
        "inputSchema": {
            "type": "object",
            "required": ["legacy_url"],
            "properties": {
                "legacy_url": {"type": "string", "description": "Relative URL e.g. /business/article-slug-12345"},
            },
        },
    },
    {
        "name": "list_categories",
        "description": "List all categories with hierarchical structure.",
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
        "description": "Get a single category by ID or slug including SEO metadata and child categories.",
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
        "description": "List all tags.",
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
        "description": "Get a single tag by ID or slug.",
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
        "description": "List all authors with profile info and social links.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":  {"type": "integer"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_author",
        "description": "Get a single author by ID or slug.",
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Author ID or slug"},
            },
        },
    },
    {
        "name": "get_publisher_data",
        "description": "Get publisher profile: branding, logo, colors, social links, site metadata.",
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
        "description": "Get live blog updates for a LiveBlog post.",
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
        "name": "get_navbar",
        "description": "Get the navigation menu configuration including nested menu items and links.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_footer",
        "description": "Get the footer configuration including logo, social links, app store URLs, menus, and copyright text.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_active_slots",
        "description": "Get configured advertisement slots with dimensions, HTML content, and slot type information.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_newsletter_groups",
        "description": "Get all configured newsletter groups with their metadata, logos, and descriptions.",
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


@newrelic.agent.function_trace(name="call_tool", group="Tool")
def call_tool(credentials, name, args):
    add_attrs([("mcp.tool_name", name)])
    args = args or {}
    logger.debug("call_tool: tool=%s args_count=%d", name, len(args))

    try:
        if name == "list_posts":
            with fn_trace("list_posts", group="Tool"):
                page  = args.pop("page",  None)
                limit = args.pop("limit", None)
                return cds_get(credentials, "/posts/", {"page": page, "limit": limit, **args})

        if name == "get_post":
            with fn_trace("get_post", group="Tool"):
                return cds_get(credentials, f"/post/{args['identifier']}/")

        if name == "get_post_by_url":
            with fn_trace("get_post_by_url", group="Tool"):
                return cds_get(credentials, "/post/", {"legacy_url": args["legacy_url"]})

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
                return cds_get(credentials, "/contributors/", {"page": args.get("page"), "limit": args.get("limit")})

        if name == "get_author":
            with fn_trace("get_author", group="Tool"):
                return cds_get(credentials, f"/contributor/{args['identifier']}/")

        if name == "get_publisher_data":
            with fn_trace("get_publisher_data", group="Tool"):
                return cds_get(credentials, "/publisher-data/")

        if name == "identify_content":
            with fn_trace("identify_content", group="Tool"):
                return cds_get(credentials, "/identify_url/", {"legacy_url": args["legacy_url"]})

        if name == "get_live_blog_updates":
            with fn_trace("get_live_blog_updates", group="Tool"):
                return cds_get(credentials, f"/live-blog/{args['post_id']}/updates/", {
                    "page":  args.get("page"),
                    "limit": args.get("limit"),
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
                return cds_get(credentials, "/newsletter-groups/")

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
    except Exception as exc:
        logger.error("call_tool error: tool=%s error=%s", name, exc, exc_info=True)
        notice_err(exc, [
            ("error.layer", "tool"),
            ("error.tool_name", name),
        ])
        raise
