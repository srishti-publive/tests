# Tool Patterns Reference

Complete SCHEMAS + HANDLERS examples for every tier, matching the actual codebase style.
Copy–paste and substitute your resource name and endpoint path.

---

## inputSchema Quick-Reference

| Field type | JSON Schema |
|---|---|
| Integer ID | `{"type": "integer", "description": "Resource ID"}` |
| String ID or slug | `{"type": "string", "description": "Resource ID or slug"}` |
| Boolean flag | `{"type": "boolean", "description": "..."}` |
| HTML content | `{"type": "string", "description": "HTML body content"}` |
| Hex colour | `{"type": "string", "description": "Brand colour in hex (e.g. #EF4444)"}` |
| ISO 8601 timestamp | `{"type": "string", "description": "... (ISO 8601)"}` |
| Comma-separated IDs | `{"type": "string", "description": "Comma-separated IDs (e.g. '1,2,3')"}` |
| Pagination page | `{"type": "integer", "description": "Page number (default: 1, max: 1000)"}` |
| Pagination limit | `{"type": "integer", "description": "Items per page (default: 10, max: 50)"}` |
| dry_run | `{"type": "boolean", "description": "true = preview only, no changes (default); false = execute"}` |
| confirm_delete | `{"type": "boolean", "description": "Must be true — together with dry_run=false — to permanently delete"}` |

---

## Tier 0 — CDS Read: list with filters

```python
# mcp_app/cds/widgets.py
from ..clients.cds import cds_get

SCHEMAS = [
    {
        "name": "list_widgets",
        "description": (
            "List all published widgets with pagination and optional status filter. "
            "If the user needs unpublished widgets, use cms_list_widgets instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page":       {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit":      {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
                "status__eq": {"type": "string",  "description": "Filter by status: active or inactive"},
                "sort_by":    {"type": "string",  "description": "Sort field e.g. created_at"},
                "sort_order": {"type": "string",  "description": "asc or desc"},
            },
        },
    },
]


def list_widgets(credentials: dict, args: dict):
    return cds_get(credentials, "/widgets/", {
        "page":       args.get("page"),
        "limit":      args.get("limit"),
        "status__eq": args.get("status__eq"),
        "sort_by":    args.get("sort_by"),
        "sort_order": args.get("sort_order"),
    })


HANDLERS = {
    "list_widgets": list_widgets,
}
```

---

## Tier 0 — CDS Read: get by ID/slug with input validation

```python
SCHEMAS = [
    {
        "name": "get_widget",
        "description": (
            "Get a single published widget by ID or slug. "
            "identifier must be a numeric ID or a URL slug. "
            "Use list_widgets to discover valid identifiers."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["identifier"],
            "properties": {
                "identifier": {"type": "string", "description": "Widget ID or slug"},
            },
        },
    },
]


def get_widget(credentials: dict, args: dict):
    identifier = str(args.get("identifier", "")).strip()
    if not identifier:
        return {
            "error": "invalid_input",
            "message": "identifier is required. Use list_widgets to discover valid IDs.",
        }
    return cds_get(credentials, f"/widget/{identifier}/")


HANDLERS = {
    "get_widget": get_widget,
}
```

---

## Tier 0 — CDS Read: get with strict numeric ID

Use when the API only accepts integer IDs (not slugs).

```python
def get_author_by_id(credentials: dict, args: dict):
    identifier = str(args.get("identifier", "")).strip()
    if not identifier:
        return {"error": "invalid_input", "message": "identifier is required."}
    if not identifier.isdigit():
        return {
            "error": "invalid_input",
            "message": (
                f"identifier must be a numeric ID, got {identifier!r}. "
                "Use list_authors to discover valid IDs."
            ),
        }
    return cds_get(credentials, f"/author/{identifier}/")
```

---

## Tier 0 — CDS Read: fallback on endpoint missing

Use when the primary endpoint is not available for all publishers.

```python
import newrelic.agent
from ..nr_utils import add_attrs

def get_publisher_data(credentials: dict, args: dict):
    try:
        return cds_get(credentials, "/publisher-data/")
    except Exception as exc:
        err_str     = str(exc).lower()
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        is_missing  = (
            http_status in (400, 404)
            or "unknown endpoint" in err_str
            or "not found"        in err_str
        )
        if is_missing:
            add_attrs([("mcp.tool_fallback", "footer"), ("mcp.tool_fallback_reason", "endpoint_unavailable")])
            newrelic.agent.record_custom_metric("Custom/MCP/fallback_count", 1)
            return cds_get(credentials, "/footer/")
        raise
```

---

## Tier 1 — CMS Read: list + get (no dry_run)

```python
# mcp_app/cms/widgets.py
from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import DELETION_REQUIRES_CONFIRMATION, preview_create_op, preview_delete_op, preview_update_op

SCHEMAS = [
    {
        "name": "cms_list_widgets",
        "description": (
            "List all CMS widgets with pagination. Returns every widget including unpublished ones. "
            "If the user only needs published widgets, prefer the CDS list_widgets tool. "
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
        "name": "cms_get_widget",
        "description": (
            "Retrieve a single CMS widget by ID. Returns full management details including unpublished fields. "
            "If the user only needs basic published data, prefer the CDS get_widget tool. "
            "Returns results directly — no confirmation step needed."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Widget ID"}},
        },
    },
]


def list_widgets(credentials: dict, args: dict):
    return cms_get(credentials, "/widget/", {
        "page":  args.get("page"),
        "limit": args.get("limit"),
    })


def get_widget(credentials: dict, args: dict):
    return cms_get(credentials, f"/widget/{args['id']}/")
```

---

## Tier 2 — CMS Create (dry_run preview → POST)

```python
SCHEMAS = [
    {
        "name": "cms_create_widget",
        "description": (
            "Create a new widget in the CMS. "
            "BEFORE calling: confirm all details with the user — at minimum name and english_name. "
            "Workflow: dry_run=true (default) shows a full preview — no changes made. "
            "Once the user confirms, call again with dry_run=false to create. "
            "Immutable after creation: english_name, slug."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["name", "english_name"],
            "properties": {
                "name":         {"type": "string",  "description": "Widget name"},
                "english_name": {"type": "string",  "description": "English name for slug generation. Immutable after creation."},
                "slug":         {"type": "string",  "description": "Custom slug (auto-generated if omitted). Immutable after creation."},
                "content":      {"type": "string",  "description": "Widget HTML content"},
                "priority":     {"type": "integer", "description": "Sort priority (1–1000)"},
                "dry_run":      {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
            },
        },
    },
]


def create_widget(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Widget", payload)}
    return cms_post(credentials, "/widget/", payload)
```

---

## Tier 3 — CMS Update (dry_run diff → PATCH)

```python
SCHEMAS = [
    {
        "name": "cms_update_widget",
        "description": (
            "Update an existing widget. "
            "BEFORE calling: confirm the widget ID and all fields to change with the user. "
            "Workflow: dry_run=true (default) fetches current state and shows a field-by-field diff — no changes made. "
            "Show the diff to the user. Once they confirm, call again with dry_run=false to apply. "
            "Immutable fields that cannot be changed: english_name, slug."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":       {"type": "integer", "description": "Widget ID"},
                "name":     {"type": "string",  "description": "New widget name"},
                "content":  {"type": "string",  "description": "New HTML content"},
                "priority": {"type": "integer", "description": "New sort priority"},
                "dry_run":  {"type": "boolean", "description": "true = show diff only, no changes (default); false = apply update"},
            },
        },
    },
]


def update_widget(credentials: dict, args: dict):
    dry_run   = args.get("dry_run", True)
    widget_id = args["id"]
    changes   = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"/widget/{widget_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Widget", widget_id, current, changes)}
    return cms_patch(credentials, f"/widget/{widget_id}/", changes)
```

---

## Tier 3 — CMS Delete (dry_run preview → DELETE + double confirm)

```python
SCHEMAS = [
    {
        "name": "cms_delete_widget",
        "description": (
            "Permanently delete a widget. This action CANNOT be undone. "
            "Posts referencing this widget will lose their widget association. "
            "BEFORE calling: confirm the widget ID with the user. "
            "Workflow: dry_run=true (default) fetches and shows full widget details — no deletion. "
            "Show the preview to the user. Once they explicitly confirm, call again with "
            "dry_run=false AND confirm_delete=true."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {
                "id":             {"type": "integer", "description": "Widget ID"},
                "dry_run":        {"type": "boolean", "description": "true = preview only (default); false = delete (also requires confirm_delete=true)"},
                "confirm_delete": {"type": "boolean", "description": "Must be explicitly set to true — together with dry_run=false — to execute the deletion"},
            },
        },
    },
]


def delete_widget(credentials: dict, args: dict):
    dry_run        = args.get("dry_run", True)
    confirm_delete = args.get("confirm_delete", False)
    widget_id      = args["id"]
    if dry_run:
        item = cms_get(credentials, f"/widget/{widget_id}/")
        if "error_type" in item:
            return item
        return {"dry_run": True, "preview": preview_delete_op(
            "Widget", widget_id, item,
            warning="Posts referencing this widget will lose their widget association.",
        )}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"/widget/{widget_id}/")
```

---

## HANDLERS dict (assemble at bottom of every domain file)

```python
HANDLERS = {
    "list_widgets":           list_widgets,
    "get_widget":             get_widget,
    "cms_list_widgets":       list_widgets,        # if CMS version differs
    "cms_get_widget":         get_widget,
    "cms_create_widget":      create_widget,
    "cms_update_widget":      update_widget,
    "cms_delete_widget":      delete_widget,
    "validate_widget_exists": validate_widget_exists,
}
```

---

## Validation tool

```python
SCHEMAS = [
    {
        "name": "validate_widget_exists",
        "description": (
            "Validation check — no changes made. "
            "Checks whether a widget with the given ID exists in the CMS. "
            "Returns {valid: true, id, name} if found, "
            "{valid: false, reason} if not."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer", "description": "Widget ID to validate"}},
        },
    },
]


def validate_widget_exists(credentials: dict, args: dict):
    widget_id = args["id"]
    result    = cms_get(credentials, f"/widget/{widget_id}/")
    if "error_type" in result:
        return {"valid": False, "reason": f"Widget ID {widget_id} not found."}
    return {"valid": True, "id": widget_id, "name": result.get("name")}
```

---

## Special pattern — publish gate (cms_update_post style)

When setting `status=Published` requires an extra explicit confirmation:

```python
def update_widget(credentials: dict, args: dict):
    dry_run          = args.get("dry_run", True)
    confirm_publish  = args.get("confirm_publish", False)
    widget_id        = args["id"]
    changes          = {k: v for k, v in args.items() if k not in ("id", "dry_run", "confirm_publish")}

    # Draft is always safe — apply immediately without preview
    if changes.get("status") == "Draft":
        return cms_patch(credentials, f"/widget/{widget_id}/", changes)

    if dry_run:
        current = cms_get(credentials, f"/widget/{widget_id}/")
        if "error_type" in current:
            return current
        return {"dry_run": True, "preview": preview_update_op("Widget", widget_id, current, changes)}

    if changes.get("status") == "Published" and not confirm_publish:
        return {
            "error_type": "confirmation_required",
            "message": (
                "Publishing a widget requires confirm_publish=true. "
                "Call again with dry_run=false AND confirm_publish=true."
            ),
            "retryable": False,
        }
    return cms_patch(credentials, f"/widget/{widget_id}/", changes)
```

Add `"confirm_publish"` to the inputSchema too:
```python
"confirm_publish": {"type": "boolean", "description": "Must be true when setting status=Published with dry_run=false."},
```

---

## Special pattern — integer field coercion (cms_create_post style)

When AI clients may send integer fields as strings (e.g. `"156228"` instead of `156228`):

```python
def create_widget(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run" and v is not None and v != ""}

    # Coerce integer fields AI clients sometimes send as strings
    for field in ("primary_category", "banner_url"):
        if field in payload:
            try:
                payload[field] = int(payload[field])
            except (ValueError, TypeError):
                pass

    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Widget", payload)}
    return cms_post(credentials, "/widget/", payload)
```

---

## Special pattern — upstream 500 idempotency check (cms_create_custom_component style)

When the API sometimes 500s after actually committing the write, check before retrying:

```python
def create_widget(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Widget", payload)}
    result = cms_post(credentials, "/widget/", payload)
    if isinstance(result, dict) and result.get("error_type") == "upstream_error":
        # Check if server committed before returning 5xx (prevents duplicates on retry)
        listing = cms_get(credentials, "/widget/", {"limit": 50})
        items   = (listing.get("results") or listing.get("data") or []) if isinstance(listing, dict) and not listing.get("error_type") else []
        for item in items:
            if isinstance(item, dict) and item.get("name") == payload.get("name"):
                return item
        result = cms_post(credentials, "/widget/", payload)
    return result
```

---

## Common Mistakes

| Mistake | Fix |
|---|---|
| Adding `fn_trace` inside a handler | Remove it — the dispatcher in `__init__.py` already wraps every handler call in `fn_trace(name, group="Tool")` |
| Forgetting `if "error_type" in current: return current` in dry-run branches | Always propagate client errors before passing `current` to `preview_update_op` |
| New CMS tool with non-`cms_`/`validate_` name routes to CDS | `CMS_TOOL_NAMES` is derived from `_HANDLER_REGISTRY` — as long as the tool is in `_HANDLER_REGISTRY`, routing is correct regardless of name |
| Creating a new domain file but forgetting to import it | Always add the import + registry entries in the corresponding `__init__.py` |
| Adding `required: ["dry_run"]` to a CMS write tool | `dry_run` is always optional with a `True` default — never required |
| Stripping `None` values in update payload | For PATCH, only strip `"id"` and `"dry_run"` — let the caller decide which fields to update; `None` should not appear for optional fields the caller didn't provide because the schema omits `None` by default |
