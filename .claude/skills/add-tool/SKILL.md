---
name: add-tool
description: This skill should be used when the user asks to "add a tool", "create a new tool", "add a CDS tool", "add a CMS tool", "add a read tool", "add a write tool", "implement a new MCP tool", "register a tool", or wants to expose a new API endpoint as an MCP tool in this project.
version: 0.1.0
---

# Adding MCP Tools to the Publive MCP Server

This project exposes CDS (read) and CMS (write) API endpoints as MCP tools. All tool registration is **data-driven** — no changes to `views.py` are ever needed. Adding a tool means two things: appending an entry to the tool catalogue, and adding a handler branch to the dispatcher.

## File Map

| File | Purpose |
|---|---|
| `mcp_app/tools.py` | 15 CDS read tools — `TOOLS` list + `call_tool()` dispatcher |
| `mcp_app/cms_tools.py` | 25+ CMS write tools — `CMS_TOOLS` list + `call_cms_tool()` dispatcher |
| `mcp_app/cds_client.py` | `cds_get(credentials, path, params)` |
| `mcp_app/cms_client.py` | `cms_get/post/patch/delete(credentials, path, ...)` |

The correct file to edit is determined by the operation type:
- **Read-only** (list, get, search, validate) → `tools.py` using `cds_get`, **unless** the data only exists in CMS (e.g. draft/unpublished content), in which case use `cms_tools.py` with `cms_get`.
- **Write** (create, update, delete) → `cms_tools.py` using `cms_post`, `cms_patch`, or `cms_delete`.

## Tool Naming Conventions

- CDS tools: plain verb-noun — `list_posts`, `get_category`, `get_trending_posts`
- CMS tools: always prefixed with `cms_` — `cms_list_categories`, `cms_create_post`, `cms_delete_tag`
- Validation helpers (read-only pre-flight): no prefix — `validate_media_exists`, `validate_post_slug`

## Step 1 — Add the Catalogue Entry

Append a dict to `TOOLS` (for CDS) or `CMS_TOOLS` (for CMS). Every entry has three required keys:

```python
{
    "name": "tool_name",
    "description": "...",
    "inputSchema": {
        "type": "object",
        "required": [...],   # omit if no required fields
        "properties": {...},
    },
}
```

**Description rules:**
- One sentence for list/get tools: what it returns and its key filter/sort abilities.
- For create: state immutable fields and what `dry_run=true` vs `false` does.
- For update: state immutable fields and what `dry_run` shows (diff of old vs new).
- For delete: always include "CANNOT be undone" and the two-param requirement.

**inputSchema rules:**
- Every parameter needs a `"description"` string — this is what the AI client sends to users.
- Always declare `"required": [...]` for fields that have no sensible default.
- Use `"type": "integer"` for IDs; `"type": "string"` for slugs, text, enums.
- Pagination fields (`page`, `limit`) are always optional integers.
- CMS write tools always include `"dry_run": {"type": "boolean", "description": "..."}`.
- CMS delete tools also include `"confirm_delete": {"type": "boolean", "description": "..."}`.

See `references/tool-patterns.md` for complete catalogue entry examples per tier.

## Step 2 — Add the Handler Branch

Inside `call_tool()` or `call_cms_tool()`, add an `if name == "tool_name":` block. Wrap the body in `with fn_trace("tool_name", group="Tool"):`.

### CDS read handler (tools.py)

```python
if name == "list_widgets":
    with fn_trace("list_widgets", group="Tool"):
        return cds_get(credentials, "/widgets/", {
            "page":  args.get("page"),
            "limit": args.get("limit"),
        })

if name == "get_widget":
    with fn_trace("get_widget", group="Tool"):
        return cds_get(credentials, f"/widget/{args['identifier']}/")
```

### CMS tier 1 — list / get (cms_tools.py)

No `dry_run`. Direct call, return immediately.

```python
if name == "cms_list_widgets":
    with fn_trace("cms_list_widgets", group="Tool"):
        return cms_get(credentials, "/widget/", {
            "page":  args.get("page"),
            "limit": args.get("limit"),
        })
```

### CMS tier 2 — create (cms_tools.py)

`dry_run=True` by default → return a preview. `dry_run=False` → POST.

```python
if name == "cms_create_widget":
    with fn_trace("cms_create_widget", group="Tool"):
        dry_run = args.get("dry_run", True)
        payload = {k: v for k, v in args.items() if k != "dry_run"}
        if dry_run:
            return {"dry_run": True, "preview": _preview_create("Widget", payload)}
        return cms_post(credentials, "/widget/", payload)
```

### CMS tier 3 — update (cms_tools.py)

`dry_run=True` → fetch current, return diff. `dry_run=False` → PATCH.

```python
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

### CMS tier 3 — delete (cms_tools.py)

`dry_run=True` → fetch and preview. Execute requires `dry_run=False` **AND** `confirm_delete=True`.

```python
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

## Step 3 — Verify

```bash
python manage.py runserver
```

No migration needed — tools are not models. Confirm the new tool appears in the MCP tools list by checking that `tools/list` returns it (the list is built dynamically from `TOOLS` + `CMS_TOOLS` on every request).

## Preview Helpers (CMS only)

Three helpers live in `cms_tools.py` — use them, don't duplicate logic:

| Helper | When to call |
|---|---|
| `_preview_create(resource, payload)` | Tier 2 create dry run |
| `_preview_update(resource, id, current, changes)` | Tier 3 update dry run |
| `_preview_delete(resource, id, item, warning="")` | Tier 3 delete dry run |

Pass the resource name as a title-case string (`"Widget"`, `"Live Blog"`) — it appears in the formatted dry-run output.

## Error Handling

- `cds_get` and all `cms_*` functions return either a parsed JSON dict or an error dict with `error_type`, `message`, `retryable`.
- Always check `if "error_type" in result: return result` before using a fetch result in update/delete dry-run branches.
- Do not add try/except around individual client calls — the clients handle retries and error normalization.
- For input validation errors (bad ID type, empty required field), return `{"error": "invalid_input", "message": "..."}` directly without calling the client.

## Additional Resources

- **`references/tool-patterns.md`** — Complete catalogue entry examples for every tier, inputSchema patterns, and handler edge cases
