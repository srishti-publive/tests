"""CDS sitemap tools — return XML wrapped in the standard CDS JSON envelope."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "fetch_sitemap_index",
        "description": "Get the master sitemap XML index for all published content. Returns a sitemapindex linking to every category, tag, and content sitemap.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_sitemap_web_index",
        "description": "Get the web content sitemap XML index linking to paginated article sitemaps (sitemap_{date}.xml).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_sitemap_web_stories",
        "description": "Get the web story sitemap XML index linking to paginated web story sitemaps (webstory_sitemap_{date}.xml).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_sitemap_news",
        "description": "Get the Google News sitemap XML containing recently published articles eligible for Google News indexing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_sitemap_categories",
        "description": "Get the sitemap XML listing all published category pages.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fetch_sitemap_page",
        "description": (
            "Get a paginated date-stamped sitemap — either an article sitemap (sitemap_{date}.xml) "
            "or a web story sitemap (webstory_sitemap_{date}.xml). "
            "Discover valid date values from fetch_sitemap_web_index or fetch_sitemap_web_stories first."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["date"],
            "properties": {
                "date":  {"type": "string",  "description": "Date partition string (e.g. 2026-05-01) from the webcontent or webstory sitemap index"},
                "type":  {"type": "string",  "description": "Sitemap type: article (default) or webstory"},
            },
        },
    },
]


def fetch_sitemap_index(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/allcontent-sitemap.xml/")


def fetch_sitemap_web_index(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/webcontent-sitemap.xml/")


def fetch_sitemap_web_stories(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/webstory-sitemap.xml/")


def fetch_sitemap_news(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/news-sitemap.xml/")


def fetch_sitemap_categories(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/category-sitemap.xml/")


def fetch_sitemap_page(credentials: dict, args: dict):
    date         = args["date"]
    sitemap_type = args.get("type", "article")
    if sitemap_type == "webstory":
        path = f"/sitemap/webstory_sitemap_{date}.xml/"
    else:
        path = f"/sitemap/sitemap_{date}.xml/"
    return cds_get(credentials, path)


HANDLERS = {
    "fetch_sitemap_index":       fetch_sitemap_index,
    "fetch_sitemap_web_index":   fetch_sitemap_web_index,
    "fetch_sitemap_web_stories": fetch_sitemap_web_stories,
    "fetch_sitemap_news":        fetch_sitemap_news,
    "fetch_sitemap_categories":  fetch_sitemap_categories,
    "fetch_sitemap_page":        fetch_sitemap_page,
}
