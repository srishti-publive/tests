---
name: add-tool
description: >
  Use this skill when the user asks to "add a tool", "create a new tool",
  "add a CDS tool", "add a CMS tool", "add a read tool", "add a write tool",
  "implement a new MCP tool", "register a tool", or wants to expose a new
  Publive API endpoint as an MCP tool.
version: 2.0.0
---

# Adding MCP Tools to the Publive MCP Server

Tools are **fully data-driven**. No changes to `views.py`, `protocol/`, or `transport/` are ever needed. Adding a tool means three things: adding the schema entry, adding a handler function, and registering both in the domain module.

---

## Architecture Snapshot

```
mcp_app/
  cds/                     ← read-only CDS tools (public content)
    posts.py               ← list_posts, get_post, get_post_by_url …
    categories.py          ← list_categories, get_category
    tags.py                ← list_tags, get_tag
    authors.py             ← list_authors, get_author
    publisher.py           ← get_publisher_data, get_navbar, get_footer …
    content.py             ← identify_content, get_active_slots …
    sitemaps.py            ← get_sitemap_* (6 tools)
    static_files.py        ← get_static_* (4 tools)
    __init__.py            ← assembles TOOLS list + dispatch_cds_tool()
  cms/
    helpers.py             ← preview_create_op, preview_update_op,
                              preview_delete_op, DELETION_REQUIRES_CONFIRMATION
    categories.py          ← cms_list/get/create/update/delete_category
    tags.py                ← cms_list/get/create/update/delete_tag
    posts.py               ← cms_list/get/create/update/delete_post
    live_blog.py           ← cms_*_live_blog_update
    media.py               ← cms_*_media
    custom_components.py   ← cms_*_custom_component
    custom_content_types.py← cms_*_custom_content_type
    validators.py          ← validate_media_exists, validate_category_exists …
    forms.py               ← submit_form
    __init__.py            ← assembles CMS_TOOLS + CMS_TOOL_NAMES + dispatch_cms_tool()
  clients/
    cds.py                 ← cds_get(credentials, path, params)
    cms.py                 ← cms_get / cms_post / cms_patch / cms_delete
    shared.py              ← build_base_url, build_basic_auth_headers, slugify_url_path
```

---

## Decision Tree

```
New tool request
│
├── Is it read-only (list, get, search, identify)?
│   ├── Uses public CDS API → mcp_app/cds/{domain}.py
│   └── Uses private CMS API (drafts, management) → mcp_app/cms/{domain}.py
│       with cms_get (no dry_run, no confirm_delete)
│
└── Is it a write operation (create, update, delete, submit)?
    └── Always → mcp_app/cms/{domain}.py
        ├── create  → dry_run=True preview, dry_run=False POST
        ├── update  → dry_run=True diff,    dry_run=False PATCH
        └── delete  → dry_run=True preview, dry_run=False + confirm_delete=True DELETE

Domain file?
├── Tool belongs to an existing domain (posts, categories, tags…) → add to that file
└── Entirely new resource → create mcp_app/cds/newdomain.py (or cms/newdomain.py)
    and import it in __init__.py
```

---

## Naming Rules

| Tool type | Convention | Examples |
|---|---|---|
| CDS read | `verb_noun` | `list_posts`, `get_author`, `get_trending_posts` |
| CMS list/get | `cms_verb_noun` | `cms_list_categories`, `cms_get_post` |
| CMS create/update/delete | `cms_verb_noun` | `cms_create_category`, `cms_delete_tag` |
| Validation (pre-flight, no side effects) | `validate_noun_verb` | `validate_media_exists`, `validate_post_slug` |
| Form submission | `submit_noun` | `submit_form` |

---

## Step 1 — Write the schema entry

Every tool needs a dict with `name`, `description`, and `inputSchema`.
Add it to the `SCHEMAS` list in the domain file.

```python
# In mcp_app/cds/categories.py  (or wherever the domain lives)
SCHEMAS = [
    # ... existing entries ...
    {
        "name": "tool_name",
        "description": "...",       # see description rules below
        "inputSchema": {
            "type": "object",
            "required": ["id"],     # omit the key entirely if nothing is required
            "properties": {
                "id":      {"type": "integer", "description": "Resource ID"},
                "page":    {"type": "integer", "description": "Page number (default: 1, max: 1000)"},
                "limit":   {"type": "integer", "description": "Items per page (default: 10, max: 50)"},
                "dry_run": {"type": "boolean", "description": "true = preview only, no changes (default); false = execute"},
            },
        },
    },
]
```

### Description rules

| Tool tier | What to include in description |
|---|---|
| CDS list | What is returned, key filter capabilities, when to prefer this over a CMS equivalent |
| CDS get | What is returned, when to prefer CMS version instead |
| CMS list/get | Same as CDS + note it includes drafts/unpublished |
| CMS create | List all immutable fields; state dry_run preview behaviour; "BEFORE calling" confirmation requirement |
| CMS update | List immutable fields; state dry_run diff behaviour; any special status gates (publish, draft bypass) |
| CMS delete | "CANNOT be undone"; downstream impact (orphaned references); dry_run=true + confirm_delete=true requirement |
| Validation | "Validation check — no changes made." + what {valid:true} and {valid:false} look like |

---

## Step 2 — Write the handler function

Add a named function to the same domain file. The dispatcher wraps it in
`fn_trace` automatically — **do not add fn_trace inside handlers**.

### CDS read — list with filters

```python
def list_widgets(credentials: dict, args: dict):
    return cds_get(credentials, "/widgets/", {
        "page":       args.get("page"),
        "limit":      args.get("limit"),
        "status__eq": args.get("status__eq"),
    })
```

### CDS read — get by ID or slug

```python
def get_widget(credentials: dict, args: dict):
    return cds_get(credentials, f"/widget/{args['identifier']}/")
```

### CDS read — get with input validation

Use when the field has a strict format constraint (e.g. numeric-only IDs).

```python
def get_widget(credentials: dict, args: dict):
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

### CMS tier 1 — list / get (direct, no dry_run)

```python
def list_widgets(credentials: dict, args: dict):
    return cms_get(credentials, "/widget/", {
        "page":  args.get("page"),
        "limit": args.get("limit"),
    })

def get_widget(credentials: dict, args: dict):
    return cms_get(credentials, f"/widget/{args['id']}/")
```

### CMS tier 2 — create (dry_run preview → POST)

```python
def create_widget(credentials: dict, args: dict):
    dry_run = args.get("dry_run", True)
    payload = {k: v for k, v in args.items() if k != "dry_run"}
    if dry_run:
        return {"dry_run": True, "preview": preview_create_op("Widget", payload)}
    return cms_post(credentials, "/widget/", payload)
```

### CMS tier 3 — update (dry_run diff → PATCH)

```python
def update_widget(credentials: dict, args: dict):
    dry_run   = args.get("dry_run", True)
    widget_id = args["id"]
    changes   = {k: v for k, v in args.items() if k not in ("id", "dry_run")}
    if dry_run:
        current = cms_get(credentials, f"/widget/{widget_id}/")
        if "error_type" in current:
            return current                  # propagate 404 / auth error
        return {"dry_run": True, "preview": preview_update_op("Widget", widget_id, current, changes)}
    return cms_patch(credentials, f"/widget/{widget_id}/", changes)
```

### CMS tier 3 — delete (dry_run preview → DELETE + double confirm)

```python
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
            warning="Posts referencing this widget will lose their association.",
        )}
    if not confirm_delete:
        return DELETION_REQUIRES_CONFIRMATION
    return cms_delete(credentials, f"/widget/{widget_id}/")
```

### Validation tool (read-only pre-flight check)

```python
def validate_widget_exists(credentials: dict, args: dict):
    widget_id = args["id"]
    result    = cms_get(credentials, f"/widget/{widget_id}/")
    if "error_type" in result:
        return {"valid": False, "reason": f"Widget ID {widget_id} not found."}
    return {"valid": True, "id": widget_id, "name": result.get("name")}
```

---

## Step 3 — Register in HANDLERS

At the bottom of the domain file, add the function to the `HANDLERS` dict:

```python
HANDLERS = {
    # ... existing entries ...
    "cms_list_widgets":       list_widgets,
    "cms_get_widget":         get_widget,
    "cms_create_widget":      create_widget,
    "cms_update_widget":      update_widget,
    "cms_delete_widget":      delete_widget,
    "validate_widget_exists": validate_widget_exists,
}
```

---

## Step 4 — Register the domain module in `__init__.py`

**If adding to an existing domain file** — nothing to do; it's already imported.

**If creating a new domain file** — add one import line and one entry each to
`TOOLS`/`CMS_TOOLS` and `_HANDLER_REGISTRY` in the appropriate `__init__.py`.

### For a new CDS domain (`mcp_app/cds/__init__.py`)

```python
# 1. Add import (keep alphabetical with existing imports)
from .widgets import HANDLERS as _WIDGETS_HANDLERS, SCHEMAS as _WIDGETS_SCHEMAS

# 2. Add to TOOLS list
TOOLS: list[dict] = (
    _POSTS_SCHEMAS
    + ...
    + _WIDGETS_SCHEMAS          # ← append
)

# 3. Add to _HANDLER_REGISTRY
_HANDLER_REGISTRY: dict = {
    **_POSTS_HANDLERS,
    ...
    **_WIDGETS_HANDLERS,        # ← append
}
```

### For a new CMS domain (`mcp_app/cms/__init__.py`)

```python
# 1. Add import
from .widgets import HANDLERS as _WIDGETS_H, SCHEMAS as _WIDGETS_S

# 2. Add to CMS_TOOLS list
CMS_TOOLS: list[dict] = (
    _CAT_S + ... + _WIDGETS_S          # ← append
)

# 3. Add to _HANDLER_REGISTRY
_HANDLER_REGISTRY: dict = {
    **_CAT_H, ..., **_WIDGETS_H,       # ← append
}

# CMS_TOOL_NAMES is rebuilt from _HANDLER_REGISTRY automatically — no edit needed.
```

---

## Step 5 — Verify

```bash
# 1. Run tests — should still be 75/75 (or more if you added test cases)
python manage.py test auth_app.tests mcp_app.tests

# 2. Confirm tool count and that every tool has a handler
python -c "
from mcp_app.cds import TOOLS, _HANDLER_REGISTRY as CDS
from mcp_app.cms import CMS_TOOLS, _HANDLER_REGISTRY as CMS
all_ok = all(t['name'] in CDS for t in TOOLS) and all(t['name'] in CMS for t in CMS_TOOLS)
print('Tools:', len(TOOLS) + len(CMS_TOOLS), '| All handlers present:', all_ok)
"

# 3. Confirm routing — no CMS tool misrouted to CDS dispatcher
python -c "
from mcp_app.cms import CMS_TOOL_NAMES
# Any tool in CMS registry routes to dispatch_cms_tool — no prefix assumptions
print('CMS tools correctly registered:', len(CMS_TOOL_NAMES))
"
```

No migration, no server restart needed. The tool list is built at import time.

---

## Imports each domain file needs

```python
# CDS domain file
from ..clients.cds import cds_get

# CMS domain file
from ..clients.cms import cms_delete, cms_get, cms_patch, cms_post
from .helpers import (
    DELETION_REQUIRES_CONFIRMATION,
    preview_create_op,
    preview_delete_op,
    preview_update_op,
)

# CMS domain that also needs CDS (e.g. cross-validation)
from ..clients.cds import cds_get
from ..clients.cms import cms_get
```

---

## Error Handling Rules

1. **Never add try/except around client calls.** The clients handle retries, timeouts, and error normalisation. They return either a parsed JSON dict or `{"error_type": "...", "message": "...", "retryable": true/false}`.

2. **Always propagate client errors in dry-run branches:**
   ```python
   current = cms_get(credentials, f"/widget/{widget_id}/")
   if "error_type" in current:
       return current      # 404, auth error, timeout — surface it, don't crash
   ```

3. **For input validation errors**, return without calling the client:
   ```python
   return {"error": "invalid_input", "message": "widget_id must be a positive integer."}
   ```

4. **For create tools with special API constraints** (e.g. `cms_create_post` requires contributors), validate before the dry_run branch and return a `missing_required_field` error.

---

## References

- `references/tool-patterns.md` — Complete SCHEMAS + HANDLERS examples for each tier, inputSchema quick-reference, and common mistakes
- `mcp_app/cms/helpers.py` — `preview_create_op`, `preview_update_op`, `preview_delete_op`, `DELETION_REQUIRES_CONFIRMATION`, `validate_live_blog_post_type`
- `mcp_app/clients/cds.py` — `cds_get` signature and retry behaviour
- `mcp_app/clients/cms.py` — `cms_get/post/patch/delete` signatures and error normalisation
