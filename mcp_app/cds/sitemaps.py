"""CDS sitemap tools — return XML wrapped in the standard CDS JSON envelope."""
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "get_sitemap_allcontent",
        "description": "Get the master sitemap XML index for all published content. Returns a sitemapindex linking to every category, tag, and content sitemap.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sitemap_webcontent",
        "description": "Get the web content sitemap XML index linking to paginated article sitemaps (sitemap_{date}.xml).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sitemap_webstory",
        "description": "Get the web story sitemap XML index linking to paginated web story sitemaps (webstory_sitemap_{date}.xml).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sitemap_news",
        "description": "Get the Google News sitemap XML containing recently published articles eligible for Google News indexing.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sitemap_category",
        "description": "Get the sitemap XML listing all published category pages.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sitemap_paginated",
        "description": (
            "Get a paginated date-stamped sitemap — either an article sitemap (sitemap_{date}.xml) "
            "or a web story sitemap (webstory_sitemap_{date}.xml). "
            "Discover valid date values from get_sitemap_webcontent or get_sitemap_webstory first."
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


def get_sitemap_allcontent(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/allcontent-sitemap.xml/")


def get_sitemap_webcontent(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/webcontent-sitemap.xml/")


def get_sitemap_webstory(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/webstory-sitemap.xml/")


def get_sitemap_news(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/news-sitemap.xml/")


def get_sitemap_category(credentials: dict, args: dict):
    return cds_get(credentials, "/sitemap/category-sitemap.xml/")


def get_sitemap_paginated(credentials: dict, args: dict):
    date      = args["date"]
    sitemap_type = args.get("type", "article")
    if sitemap_type == "webstory":
        path = f"/sitemap/webstory_sitemap_{date}.xml/"
    else:
        path = f"/sitemap/sitemap_{date}.xml/"
    return cds_get(credentials, path)


HANDLERS = {
    "get_sitemap_allcontent": get_sitemap_allcontent,
    "get_sitemap_webcontent": get_sitemap_webcontent,
    "get_sitemap_webstory":   get_sitemap_webstory,
    "get_sitemap_news":       get_sitemap_news,
    "get_sitemap_category":   get_sitemap_category,
    "get_sitemap_paginated":  get_sitemap_paginated,
}
