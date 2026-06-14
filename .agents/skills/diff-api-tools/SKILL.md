---
name: diff-api-tools
description: >
  Use this skill when the user asks to "check for tool changes", "update
  existing tools", "sync tool schemas with docs", "find tools that are out
  of date", "see if any tool's parameters changed", "detect tool drift",
  or wants to compare the current MCP tool implementations against the
  publive-docs API reference and apply modifications to bring them in sync.
  This skill diffs and patches — [[sync-api-tools]] handles brand-new gaps.
version: 1.0.0
---

# Diff and Update Existing MCP Tools Against the Publive Docs

Compares each already-implemented tool's `inputSchema`, handler path, and
description against the live API docs, surfaces every deviation, and — with
user approval — patches the implementation to match.

This is a **manual** workflow — run it on demand, it is not scheduled.

---

## Step 1 — Inventory implemented tools

Read the aggregated tool lists — do not grep for `"name"` strings:

```python
from mcp_app.cds import TOOLS as CDS_TOOLS
from mcp_app.cms import CMS_TOOLS
```

Or read `mcp_app/cds/__init__.py` / `mcp_app/cms/__init__.py` and each
domain file's `SCHEMAS` list directly. Build a map:

```
tool_name → { file, description, inputSchema, handler_path }
```

Where `handler_path` is the URL path string passed to `cds_get` / `cms_*`
inside the handler function.

## Step 2 — Inventory the documented endpoints

Use `publive-docs` MCP tools (`search_publive_docs` /
`query_docs_filesystem_publive_docs`) to tree `/api-reference`:

```
tree /api-reference -L 2
```

For each `.mdx` that maps to an existing tool (skip `README.mdx`, skip
`/api-reference/deprecated/`), read the full file to extract:

- **Method + path** — the HTTP verb and endpoint URL
- **Parameters** — name, type, required/optional, description, constraints
  (min/max, allowed values, default)
- **Response shape** — key fields present in the example response
- **Description / summary** — one-line purpose of the endpoint

Build a parallel map: `doc_endpoint → { method, path, params, description }`.

## Step 3 — Match docs to implemented tools

Use the same naming heuristics from [[add-tool]] to align doc pages to tool
names. If no tool matches a doc page, skip it — that's [[sync-api-tools]]'s
job. Only process endpoints that *do* have a matching tool.

## Step 4 — Diff each matched pair

For every `(tool, doc_endpoint)` pair, compute the delta across four axes:

### 4a — Parameter drift

Compare `inputSchema.properties` against the doc's parameter list:

| Case | Category |
|---|---|
| Tool has a param the doc no longer lists | **Stale param** |
| Doc has a required param the tool marks optional (or omits) | **Missing required param** |
| Doc has an optional param the tool doesn't expose | **Missing optional param** |
| Param type mismatch (e.g. tool says `string`, doc says `integer`) | **Type mismatch** |
| `required` array differs from doc | **Required-flag mismatch** |
| Description materially wrong (not just phrasing) | **Description mismatch** |
| Default or constraint (min/max/enum) differs | **Constraint mismatch** |

### 4b — Handler path drift

Extract the URL string from the handler function (read the source with the
Read tool) and compare it to the doc's `path`. Flag if they differ.
Account for path-template normalisation: `/posts/{id}/` vs `/posts/<id>/`
are the same shape — flag only if the structure differs.

### 4c — HTTP method drift

If the handler calls `cms_post` but the doc says `PATCH` (or vice versa),
flag it as a **method mismatch**.

### 4d — Description drift

Flag only when the tool description is factually wrong (wrong method,
wrong resource, missing a key constraint from the doc). Ignore style
differences — do not rewrite descriptions for phrasing alone.

## Step 5 — Present the diff report

**Do not modify any file yet.** Show the user a structured report grouped
by tool name. For each tool with at least one deviation, list the deltas
as a nested bullet list:

```
fetch_published_posts  (mcp_app/cds/posts.py)
  - Missing optional param: `tag` (string) — filter by tag slug
  - Constraint mismatch: `limit` max is 100 in docs, tool says 50
  - Stale param: `order_by` — removed from API

create_post  (mcp_app/cms/posts.py)
  - Missing required param: `canonical_url` (string, required)
  - Description drift: missing note that contributors must exist before creation
```

Then ask the user which changes (if any) to apply. For long lists, show
the full report as plain text first and use `AskUserQuestion` with
`multiSelect: true` (up to 4 options) or ask them to reply with the
tool names or numbers they want patched.

If every implemented tool matches the docs, say so — no need to edit
anything or ask.

## Step 6 — Apply the approved changes

For each tool the user selected, open the domain file with the Read tool,
then apply **only** the specific deltas the user approved (do not
opportunistically clean up anything else):

### Adding / fixing a parameter in `inputSchema`

Use Edit to add or update the property in the `properties` dict and update
the `required` array if needed. Match the doc's type, description, and
any constraints (enum, minimum, maximum, default).

### Removing a stale parameter

Remove the property from `properties` and, if present, from `required`.
Check the handler body — if the stale param was being forwarded to the
API call, remove it there too.

### Fixing a handler path

Edit only the URL string argument in the `cds_get` / `cms_*` call.
Do not touch the surrounding logic.

### Fixing an HTTP method

Change `cms_post` → `cms_patch` (or whatever the doc says). Verify the
dry_run tier is still correct for the new method (PATCH = tier 3 diff,
POST = tier 2 preview).

### Fixing a description

Replace only the `"description"` value in the SCHEMAS entry. Keep it
within the description rules from [[add-tool]].

## Step 7 — Verify

```bash
python manage.py test auth_app.tests mcp_app.tests

python -c "
from mcp_app.cds import TOOLS, _HANDLER_REGISTRY as CDS
from mcp_app.cms import CMS_TOOLS, _HANDLER_REGISTRY as CMS
all_ok = all(t['name'] in CDS for t in TOOLS) and all(t['name'] in CMS for t in CMS_TOOLS)
print('Tools:', len(TOOLS) + len(CMS_TOOLS), '| All handlers present:', all_ok)
"
```

End with a short report:

- **Updated**: tool name + what changed (param added/removed, path fixed, etc.)
- **Skipped by user**: tools they chose not to patch
- **No change needed**: tools that matched the docs exactly
- **Needs a decision**: anything ambiguous (conflicting signals between doc
  and implementation, or where patching would be a breaking change for
  existing callers) — do not touch these without explicit user instruction

---

## References

- [[add-tool]] — schema/handler patterns, description rules, tier definitions
- [[sync-api-tools]] — use this instead if the gap is a *missing* tool, not a modified one
- `mcp_app/cms/helpers.py` — `preview_create_op`, `preview_update_op`, `preview_delete_op`
- `mcp_app/clients/cds.py` / `mcp_app/clients/cms.py` — client signatures

<!-- trigger with: "check for tool changes", "update existing tools", "detect tool drift" -->
