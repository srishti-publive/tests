"""Read-only pre-flight validation tools — no CMS writes."""
from ..clients.cds import cds_get
from ..clients.cms import cms_get

SCHEMAS = [
    {
        "name": "validate_media_asset",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a media asset with the given ID exists in the CMS library. "
            "Returns {valid: true, id, filename, path} if found, {valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Media asset ID to validate"}},
        },
    },
    {
        "name": "validate_category",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a category with the given ID exists in the CMS. "
            "Returns {valid: true, id, name} if found, {valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Category ID to validate"}},
        },
    },
    {
        "name": "validate_author",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a contributor/author with the given ID exists via the CDS. "
            "Returns {valid: true, id, name} if found, {valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Author/contributor ID to validate"}},
        },
    },
    {
        "name": "validate_post_slug",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a post slug is available (not yet taken) in the CMS. "
            "Returns {valid: true, slug, available: true} if the slug is free, "
            "{valid: false, reason} if it is already taken."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["slug"],
            "properties": {"slug": {"type": "string", "description": "URL slug to check for availability"}},
        },
    },
]


def validate_media_asset(credentials: dict, args: dict):
    media_id = args["id"]
    result   = cms_get(credentials, f"/media-library/{media_id}/")
    if "error_type" in result:
        return {"valid": False, "reason": f"Media ID {media_id} not found."}
    return {"valid": True, "id": media_id, "filename": result.get("filename"), "path": result.get("path")}


def validate_category(credentials: dict, args: dict):
    category_id = args["id"]
    result      = cms_get(credentials, f"/category/{category_id}/")
    if "error_type" in result:
        return {"valid": False, "reason": f"Category ID {category_id} not found."}
    return {"valid": True, "id": category_id, "name": result.get("name")}


def validate_author(credentials: dict, args: dict):
    author_id = args["id"]
    result    = cds_get(credentials, f"/author/{author_id}/")
    if "error_type" in result:
        return {"valid": False, "reason": f"Author ID {author_id} not found."}
    data = result.get("data", result)
    return {"valid": True, "id": author_id, "name": data.get("name") if isinstance(data, dict) else result.get("name")}


def validate_post_slug(credentials: dict, args: dict):
    slug   = args["slug"]
    result = cms_get(credentials, f"/post/{slug}/")
    if "error_type" in result:
        return {"valid": True, "slug": slug, "available": True}
    return {"valid": False, "reason": f"Slug '{slug}' is already taken by post ID {result.get('id')}."}


HANDLERS = {
    "validate_media_asset":    validate_media_asset,
    "validate_category":       validate_category,
    "validate_author":         validate_author,
    "validate_post_slug":      validate_post_slug,
}
