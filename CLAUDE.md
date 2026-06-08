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

**Environment:** Copy `.env.example` to `.env` and fill in values before running locally. Requires `DJANGO_SECRET_KEY` and optionally `DATABASE_URL` (defaults to SQLite).

**Deployment:** Docker image (`Dockerfile`, `python:3.12-slim`) deployed to Railway, run with gunicorn (`-w 1 --threads 50`, see `entrypoint.sh`). There is no `Procfile` / release phase — `collectstatic` runs at image build time, and `entrypoint.sh` runs `migrate --noinput` then execs gunicorn on every container start. See `docs/deployment.md` for details.

## Architecture

### MCP Protocol Layer (`mcp_app/views.py` + `protocol/` + `transport/`)

`views.py` is a thin routing layer only (`health_check`, `mcp_endpoint`, `mcp_message`) — it authenticates via `protocol/auth.py` and immediately delegates to the right transport/protocol module. No business logic lives there.

- **Transport** (`mcp_app/transport/`): `sse.py` handles `GET /mcp` (long-lived SSE stream, 25s keepalive pings, session UUID, messages arrive via `POST /mcp/message?sessionId=X`); `http.py` handles stateless `POST /mcp` (single or batch JSON-RPC).
- **Protocol** (`mcp_app/protocol/`): `dispatch.py`'s `dispatch_jsonrpc()` is the JSON-RPC router — handles `initialize`, `tools/list`, and `tools/call`, validates arguments against each tool's `inputSchema`, then routes to `dispatch_cds_tool()` or `dispatch_cms_tool()` by tool name. `auth.py` resolves credentials from either a Bearer token (DB lookup via `OAuthToken`) or a Django session cookie. `session.py`/`session_store.py` track per-session state and enforce write-rate limits.

### Tool Layers

**`mcp_app/cds/`** — 22 read-only CDS tools, split across `authors.py`, `categories.py`, `content.py`, `posts.py`, `publisher.py`, `sitemaps.py`, `static_files.py`, `tags.py`. Each module exports `SCHEMAS` + `HANDLERS`; the package `__init__.py` aggregates them into `TOOLS` and dispatches via `dispatch_cds_tool()`. Each tool entry is a dict with `name`, `description`, `inputSchema`, and a `handler` callable that takes `(credentials, arguments)` and returns an MCP content list.

**`mcp_app/cms/`** — 39 CMS write tools, split across `categories.py`, `custom_components.py`, `custom_content_types.py`, `live_blog.py`, `media.py`, `posts.py`, `tags.py`, `validators.py` (plus `helpers.py` for shared dry-run/confirm preview formatters). Same `SCHEMAS`/`HANDLERS` pattern, aggregated into `CMS_TOOLS` and dispatched via `dispatch_cms_tool()`. Write operations follow a tiered safety model:
- **Tier 2 (create):** `dry_run=True` by default — returns a preview without writing.
- **Tier 3 (update):** `dry_run=True` shows a human-readable diff of old vs new fields.
- **Tier 3 (delete):** Requires both `dry_run=false` AND `confirm_delete=true` to execute.

`mcp_app/tools.py` and `mcp_app/cms_tools.py` are now thin re-export shims (`call_tool`/`call_cms_tool` are backward-compat aliases for `dispatch_cds_tool`/`dispatch_cms_tool`) — import from `mcp_app.cds` / `mcp_app.cms` directly in new code.

### HTTP Clients (`mcp_app/clients/`)

**`clients/cds.py`** — `cds_get(credentials, path, params)`. Basic Auth, 5s timeout, 1 automatic retry on HTTP 408 or `requests.Timeout`.

**`clients/cms.py`** — `cms_get/post/patch/delete(credentials, path, ...)`. Basic Auth, 10s timeout, no retry. All functions return either the parsed JSON response or a normalized error dict with `error_type`, `message`, `retryable`.

**`clients/shared.py`** — shared `build_base_url()` and Basic Auth header helpers. Both clients derive the base URL as `https://{cds|cms}-beta.thepublive.com/publisher/{publisher_id}` (overridable via the `CDS_BASE_URL`/`CMS_BASE_URL` env vars) where `publisher_id` comes from credentials.

`mcp_app/cds_client.py` and `mcp_app/cms_client.py` are thin re-export shims over `clients/cds.py` / `clients/cms.py` — import from `mcp_app.clients` directly in new code.

### Auth Layer (`auth_app/`)

Two auth paths:
1. **OAuth 2.0 + PKCE** (`/register`, `/oauth/authorize`, `/oauth/token`): For API clients (Claude Desktop, Cursor). Issues `OAuthToken` records (no expiry — permanent until revoked or upserted) that are stored in the database and resolved by `views.py` on each tool call.
2. **Session auth** (`/connect`, `/auth/login`): Browser-based login that stores credentials in Django sessions (no self-expiry — `session_ttl_seconds = -1` and a 10-year cookie ceiling; ends only via explicit `/auth/logout`).

Both paths validate credentials against the CDS API before issuing tokens/sessions.

### Observability (`mcp_app/nr_utils.py`, `mcp_app/prompt_capture.py`)

All New Relic calls are wrapped in `nr_utils.py` as no-ops when the agent is absent, so the app runs cleanly without New Relic configured.

`prompt_capture.py` extracts the user's natural language prompt from multiple sources (HTTP headers, JSON-RPC `_meta`, tool arguments) and emits `MCPPrompt` custom events. The `_prompt` key in tool arguments is stripped before the tool runs.

Key custom events: `MCPPrompt`, `MCPToolError`, `MCPToolDegraded`, `MCPUnknownMethod`, `SSESessionOpen`, `SSESessionClose`, `MCPSessionAbandoned`, `MCPSessionMissing`, `MCPSessionSummary`.

### Adding a New Tool

1. Add a handler function in `tools.py` (CDS read) or `cms_tools.py` (CMS write).
2. Append an entry to `TOOLS` or `CMS_TOOLS` with `name`, `description`, `inputSchema`.
3. No changes needed in `views.py` — dispatch is data-driven from those lists.

For CMS write tools, follow the dry_run/confirm pattern matching the tier of the operation.
