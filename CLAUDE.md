# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run dev server
python manage.py runserver

# Apply migrations
python manage.py migrate

# Create new migration after model changes
python manage.py makemigrations

# Collect static files
python manage.py collectstatic --noinput

# Run tests
python manage.py test

# Run a single test
python manage.py test mcp_app.tests.TestClassName.test_method_name
```

**Environment:** Copy `.env.example` to `.env` and fill in values before running locally. Requires `DJANGO_SECRET_KEY` and optionally `DATABASE_URL` (defaults to SQLite), `REDIS_URL` (defaults to LocMemCache).

**Deployment:** Railway with gunicorn (`-w 1 --threads 50`). The `release` phase in `Procfile` runs migrations and `collectstatic` automatically.

## Architecture

### MCP Protocol Layer (`mcp_app/views.py`)

The core MCP server. Handles two transports:
- **SSE** (`GET /mcp`): Long-lived streaming connection with 25s keepalive pings. Each connection gets a session UUID. Messages arrive via `POST /mcp/message?sessionId=X`.
- **HTTP POST** (`POST /mcp`): Stateless batch or single JSON-RPC.

`_dispatch()` is the JSON-RPC router. It handles `initialize`, `tools/list`, and `tools/call`. On `tools/call`, it resolves credentials from either a Bearer token (DB lookup via `OAuthToken`) or a Django session cookie, then routes to `call_tool()` (CDS) or `call_cms_tool()` (CMS) based on the tool name prefix.

### Tool Layers

**`mcp_app/tools.py`** — 15 read-only CDS tools. Each tool entry in `TOOLS` is a dict with `name`, `description`, `inputSchema`, and a `handler` callable. `call_tool()` looks up the handler, calls it with `(credentials, arguments)`, and returns a MCP content list.

**`mcp_app/cms_tools.py`** — 25 CMS write tools. Same pattern with `CMS_TOOLS` list and `call_cms_tool()`. Write operations follow a tiered safety model:
- **Tier 2 (create):** `dry_run=True` by default — returns a preview without writing.
- **Tier 3 (update):** `dry_run=True` shows a human-readable diff of old vs new fields.
- **Tier 3 (delete):** Requires both `dry_run=false` AND `confirm_delete=true` to execute.

### HTTP Clients

**`mcp_app/cds_client.py`** — `cds_get(credentials, path, params)`. Basic Auth, 5s timeout, 1 automatic retry on HTTP 408 or `requests.Timeout`.

**`mcp_app/cms_client.py`** — `cms_get/post/patch/delete(credentials, path, ...)`. Basic Auth, 10s timeout, no retry. All functions return either the parsed JSON response or a normalized error dict with `error_type`, `message`, `retryable`.

Both clients derive the base URL as `https://{cds|cms}.thepublive.com/publisher/{publisher_id}` where `publisher_id` comes from credentials.

### Auth Layer (`auth_app/`)

Two auth paths:
1. **OAuth 2.0 + PKCE** (`/register`, `/oauth/authorize`, `/oauth/token`): For API clients (Claude Desktop, Cursor). Issues `OAuthToken` records (30-day TTL) that are stored in the database and resolved by `views.py` on each tool call.
2. **Session auth** (`/connect`, `/auth/login`): Browser-based login that stores credentials in Django sessions (7-day TTL via cached DB sessions).

Both paths validate credentials against the CDS API before issuing tokens/sessions.

### Observability (`mcp_app/nr_utils.py`, `mcp_app/prompt_capture.py`)

All New Relic calls are wrapped in `nr_utils.py` as no-ops when the agent is absent, so the app runs cleanly without New Relic configured.

`prompt_capture.py` extracts the user's natural language prompt from multiple sources (HTTP headers, JSON-RPC `_meta`, tool arguments) and emits `MCPPrompt` custom events. The `_prompt` key in tool arguments is stripped before the tool runs.

Key custom events: `MCPPrompt`, `MCPToolError`, `MCPToolDegraded`, `SSESessionOpen`, `SSESessionClose`, `MCPSessionAbandoned`, `MCPSessionSummary`.

### Adding a New Tool

1. Add a handler function in `tools.py` (CDS read) or `cms_tools.py` (CMS write).
2. Append an entry to `TOOLS` or `CMS_TOOLS` with `name`, `description`, `inputSchema`.
3. No changes needed in `views.py` — dispatch is data-driven from those lists.

For CMS write tools, follow the dry_run/confirm pattern matching the tier of the operation.
