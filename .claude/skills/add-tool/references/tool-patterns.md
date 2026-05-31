# Tool Patterns Reference

Complete examples for each tool tier with full catalogue entry + handler.

---

## CDS Read Tool (tools.py)

### List with filters

```python
# Catalogue entry in TOOLS
{
    "name": "list_widgets",
    "description": "List widgets with pagination and optional status filter.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "page":       {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
            "limit":      {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
            "status__eq": {"type": "string",  "description": "Filter by status: active, inactive"},
        },
    },
},

# Handler branch in call_tool()
if name == "list_widgets":
    with fn_trace("list_widgets", group="Tool"):
        return cds_get(credentials, "/widgets/", {
            "page":       args.get("page"),
            "limit":      args.get("limit"),
            "status__eq": args.get("status__eq"),
        })
```

### Get by ID or slug

```python
# Catalogue entry
{
    "name": "get_widget",
    "description": "Get a single widget by ID or slug.",
    "inputSchema": {
        "type": "object",
        "required": ["identifier"],
        "properties": {
            "identifier": {"type": "string", "description": "Widget ID or slug"},
        },
    },
},

# Handler
if name == "get_widget":
    with fn_trace("get_widget", group="Tool"):
        return cds_get(credentials, f"/widget/{args['identifier']}/")
```

### Get with input validation

Use when the field has a strict format constraint (numeric-only IDs, non-empty paths).

```python
if name == "get_widget":
    with fn_trace("get_widget", group="Tool"):
        identifier = str(args.get("identifier", "")).strip()
        if not identifier:
            return {"error": "invalid_input", "message": "identifier is required."}
        if not identifier.isdigit():
            return {
                "error": "invalid_input",
                "message": f"identifier must be a numeric ID, got {identifier!r}.",
            }
        return cds_get(credentials, f"/widget/{identifier}/")
```

---

## CMS Tier 1 — List / Get (cms_tools.py)

No `dry_run`. Direct call. Includes items not yet published (differs from CDS equivalents).

```python
# Catalogue entry in CMS_TOOLS
{
    "name": "cms_list_widgets",
    "description": "List all CMS widgets with pagination. Returns every widget including drafts.",
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
    "description": "Retrieve a single CMS widget by ID.",
    "inputSchema": {
        "type": "object",
        "required": ["id"],
        "properties": {
            "id": {"type": "integer", "description": "Widget ID"},
        },
    },
},

# Handlers in call_cms_tool()
if name == "cms_list_widgets":
    with fn_trace("cms_list_widgets", group="Tool"):
        return cms_get(credentials, "/widget/", {
            "page":  args.get("page"),
            "limit": args.get("limit"),
        })

if name == "cms_get_widget":
    with fn_trace("cms_get_widget", group="Tool"):
        return cms_get(credentials, f"/widget/{args['id']}/")
```

---

## CMS Tier 2 — Create (cms_tools.py)

`dry_run=True` (default) → preview with `_preview_create`. `dry_run=False` → POST and return created object.

```python
# Catalogue entry
{
    "name": "cms_create_widget",
    "description": (
        "Create a new widget in the CMS. "
        "dry_run=true (default): previews what will be created — no changes made. "
        "dry_run=false: creates the widget. "
        "Immutable after creation: slug."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name":     {"type": "string",  "description": "Widget name"},
            "slug":     {"type": "string",  "description": "URL slug (auto-generated if omitted). Immutable after creation."},
            "content":  {"type": "string",  "description": "Widget HTML content"},
            "priority": {"type": "integer", "description": "Sort priority (1–1000)"},
            "dry_run":  {"type": "boolean", "description": "true = preview only, no changes (default); false = create for real"},
        },
    },
},

# Handler
if name == "cms_create_widget":
    with fn_trace("cms_create_widget", group="Tool"):
        dry_run = args.get("dry_run", True)
        payload = {k: v for k, v in args.items() if k != "dry_run"}
        if dry_run:
            return {"dry_run": True, "preview": _preview_create("Widget", payload)}
        return cms_post(credentials, "/widget/", payload)
```

---

## CMS Tier 3 — Update (cms_tools.py)

`dry_run=True` (default) → fetch current, build diff with `_preview_update`. `dry_run=False` → PATCH.

**Important:** Always check `if "error_type" in current: return current` after the dry-run fetch — propagate the error if the resource doesn't exist.

```python
# Catalogue entry
{
    "name": "cms_update_widget",
    "description": (
        "Update an existing widget. "
        "dry_run=true (default): fetches current state and shows a field-by-field diff — no changes made. "
        "dry_run=false: applies the update. "
        "Immutable fields that cannot be changed: slug."
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

# Handler
if name == "cms_update_widget":
    with fn_trace("cms_update_widget", group="Tool"):
        dry_run   = args.get("dry_run", True)
        widget_id = args["id"]
        changes   = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
        if dry_run:
            current = cms_get(credentials, f"/widget/{widget_id}/")
            if "error_type" in current:
                return current
            return {"dry_run": True, "preview": _preview_update("Widget", widget_id, current, changes)}
        return cms_patch(credentials, f"/widget/{widget_id}/", changes)
```

---

## CMS Tier 3 — Delete (cms_tools.py)

`dry_run=True` (default) → fetch and preview with `_preview_delete`. Execute requires `dry_run=False` AND `confirm_delete=True`. Missing either → return `_CONFIRM_REQUIRED`.

```python
# Catalogue entry
{
    "name": "cms_delete_widget",
    "description": (
        "Permanently delete a widget. This action CANNOT be undone. "
        "Posts referencing this widget will lose their widget association. "
        "dry_run=true (default): fetches and shows the widget — no deletion. "
        "To delete for real: set BOTH dry_run=false AND confirm_delete=true."
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

# Handler
if name == "cms_delete_widget":
    with fn_trace("cms_delete_widget", group="Tool"):
        dry_run        = args.get("dry_run", True)
        confirm_delete = args.get("confirm_delete", False)
        widget_id      = args["id"]
        if dry_run:
            item = cms_get(credentials, f"/widget/{widget_id}/")
            if "error_type" in item:
                return item
            return {"dry_run": True, "preview": _preview_delete(
                "Widget", widget_id, item,
                warning="Posts referencing this widget will lose their widget association.",
            )}
        if not confirm_delete:
            return _CONFIRM_REQUIRED
        return cms_delete(credentials, f"/widget/{widget_id}/")
```

---

## Validation Tool (read-only pre-flight check)

No prefix. Returns `{valid: true, ...}` or `{valid: false, reason: "..."}`. Goes in `cms_tools.py` (uses CMS data) or `tools.py` (uses CDS data). Use `cms_get` when checking draft/CMS state; use `cds_get` when checking published state.

```python
# Catalogue entry in CMS_TOOLS
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
        "properties": {
            "id": {"type": "integer", "description": "Widget ID to validate"},
        },
    },
},

# Handler
if name == "validate_widget_exists":
    with fn_trace("validate_widget_exists", group="Tool"):
        result = cms_get(credentials, f"/widget/{args['id']}/")
        if "error_type" in result:
            return {"valid": False, "reason": result.get("message", "Not found")}
        return {"valid": True, "id": result.get("id"), "name": result.get("name")}
```

---

## inputSchema Quick-Reference

| Field type | JSON Schema |
|---|---|
| Integer ID | `{"type": "integer", "description": "..."}` |
| String ID / slug | `{"type": "string", "description": "..."}` |
| Boolean flag | `{"type": "boolean", "description": "..."}` |
| HTML content | `{"type": "string", "description": "... (HTML)"}` |
| Hex color | `{"type": "string", "description": "... hex e.g. #EF4444"}` |
| ISO 8601 date | `{"type": "string", "description": "... (ISO 8601)"}` |
| Comma-separated IDs | `{"type": "string", "description": "... comma-separated e.g. 1,2,3"}` |
| Pagination page | `{"type": "integer", "description": "Page number (default: 1, max: 1000)"}` |
| Pagination limit | `{"type": "integer", "description": "Items per page (default: 10, max: 50)"}` |

---

## Common Mistakes

**Forgetting `fn_trace`** — every handler branch must wrap its body in `with fn_trace("tool_name", group="Tool"):`. Without it the tool is invisible in New Relic traces.

**Not propagating fetch errors in dry-run** — after `cms_get(...)` in an update/delete dry-run, always check `if "error_type" in result: return result`. Skipping this causes a KeyError when `_preview_update` tries to read fields from an error dict.

**Stripping too many keys from changes** — for update handlers, strip only `"id"` and `"dry_run"` from `args`, not all None values. The client strips None query params for GET but for PATCH the caller should send only the fields they want to update; stripping None is optional.

**Missing `confirm_delete` guard** — for delete handlers, the `if not confirm_delete: return _CONFIRM_REQUIRED` check must come *after* the dry_run branch, not before. The dry-run preview is always safe to return even without `confirm_delete=True`.

**Wrong file** — CMS `cms_get` calls that read draft/CMS-only data go in `cms_tools.py`, not `tools.py`. The distinction: `tools.py` only imports `cds_get`; `cms_tools.py` imports all four CMS client verbs plus `cds_get` for cross-validation tools.
