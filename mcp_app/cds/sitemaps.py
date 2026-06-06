"""CDS sitemap tools — return XML wrapped in the standard CDS JSON envelope."""
from mcp_app.clients.cds import cds_get

_SITEMAP_PATHS = {
    "index":       "/sitemap/allcontent-sitemap.xml/",
    "web_index":   "/sitemap/webcontent-sitemap.xml/",
    "web_stories": "/sitemap/webstory-sitemap.xml/",
    "news":        "/sitemap/news-sitemap.xml/",
    "categories":  "/sitemap/category-sitemap.xml/",
}

SCHEMAS = [
    {
        "name": "fetch_sitemap",
        "description": (
            "Get a sitemap XML file by type. "
            "Use 'index' for the master sitemapindex linking all content. "
            "Use 'web_index' for the paginated article sitemap index (lists dates for fetch_sitemap_page). "
            "Use 'web_stories' for the web story sitemap index. "
            "Use 'news' for the Google News sitemap. "
            "Use 'categories' for the categories sitemap."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["index", "web_index", "web_stories", "news", "categories"],
                    "description": "Which sitemap to fetch",
                },
            },
        },
    },
    {
        "name": "fetch_sitemap_page",
        "description": (
            "Get a paginated date-stamped sitemap — either an article sitemap (sitemap_{date}.xml) "
            "or a web story sitemap (webstory_sitemap_{date}.xml). "
            "Discover valid date values from fetch_sitemap(type='web_index') or fetch_sitemap(type='web_stories') first."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["date"],
            "properties": {
                "date": {"type": "string", "description": "Date partition string (e.g. 2026-05-01) from the web_index or web_stories sitemap"},
                "type": {"type": "string", "description": "Sitemap type: article (default) or webstory"},
            },
        },
    },
]


def fetch_sitemap(credentials: dict, args: dict):
    sitemap_type = args["type"]
    path = _SITEMAP_PATHS[sitemap_type]
    return cds_get(credentials, path)


def fetch_sitemap_page(credentials: dict, args: dict):
    date         = args["date"]
    sitemap_type = args.get("type", "article")
    if sitemap_type == "webstory":
        path = f"/sitemap/webstory_sitemap_{date}.xml/"
    else:
        path = f"/sitemap/sitemap_{date}.xml/"
    return cds_get(credentials, path)


HANDLERS = {
    "fetch_sitemap":      fetch_sitemap,
    "fetch_sitemap_page": fetch_sitemap_page,
}
