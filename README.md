# Publive MCP Server

A Django server that exposes Publive's Content Delivery (CDS) and Content Management (CMS) APIs as MCP tools for AI clients.

## What is this?

The Publive MCP Server bridges AI clients (Claude Desktop, Cursor, custom agents) and Publive's publishing platform. It implements the Model Context Protocol (MCP) so that an AI can read published content, manage drafts, create posts, and manipulate media entirely through natural language. Non-technical users connect via the Claude Desktop OAuth flow; developers connect with a Bearer token and their Publive API credentials (`publisherId`, `apiKey`, `apiSecret`).

### Architecture documentation

Formal design docs and draw.io diagrams live under [`docs/`](docs/):

- [HLD.md](docs/HLD.md) — high-level design
- [LLD.md](docs/LLD.md) — low-level design
- [docs/diagrams/](docs/diagrams/) — OAuth flow, SSE lifecycle, CMS safety tiers (`.drawio`)

## Authentication

### OAuth 2.0 + PKCE (for desktop clients)

Used by Claude Desktop, Cursor, and other MCP-aware clients.

| Step | Method | Endpoint | Notes |
|---|---|---|---|
| Register client | POST | `/register` | Dynamic client registration |
| Authorize | GET/POST | `/oauth/authorize` | Validates CDS credentials, issues PKCE code |
| Exchange code | POST | `/oauth/token` | Returns Bearer token (30-day TTL) |

Use the token on every request:
```
Authorization: Bearer <token>
```

The `initialize` response includes `tokenExpiresAt` (ISO 8601) so clients can warn before expiry.

### Session Auth (for browser)

```bash
# Login
POST /auth/login
Content-Type: application/json
{"publisherId": "123", "apiKey": "key", "apiSecret": "secret"}

# Check session
GET /auth/status

# Logout
POST /auth/logout
```

Session cookie is valid for **7 days** (`SESSION_COOKIE_AGE = 604800`). Credentials are validated against the CDS API on every login attempt.

---

## Tools Reference

### CDS Tools — Read Only (19 tools)

All CDS tools are read-only GET requests against `https://cds-beta.thepublive.com/publisher/{publisherId}/`. No changes are made to any data.

#### Posts (5 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `list_posts` | List and filter published posts | — | `page`, `limit`, `type__eq`, `type__neq`, `type__in`, `type__nin`, `title__contains`, `categories.id__eq`, `categories.id__in`, `categories.id__nin`, `tags.id__eq`, `tags.id__in`, `tags.id__nin`, `contributors.id__eq`, `contributors.id__in`, `created_at__gte`, `created_at__lte`, `word_count__gt`, `word_count__lt` |
| `get_post` | Get full details of a single post by ID or slug | `identifier` | — |
| `get_post_by_url` | Get a post by its legacy or relative URL path (must start with `/`) | `legacy_url` | — |
| `get_trending_posts` | Top-performing posts ranked by page views. Requires Publive analytics active. Rankings refresh every 5–10 minutes | — | `duration` (`24h`/`7d`/`30d`, default `24h`), `limit`, `page`, `type__eq` |
| `get_live_blog_updates` | Get live blog update entries for a LiveBlog post | `post_id` | `page`, `limit` |

#### Categories (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `list_categories` | List all categories with hierarchical structure | — | `page`, `limit` |
| `get_category` | Get a single category by ID or slug including SEO metadata and child categories | `identifier` | — |

#### Tags (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `list_tags` | List all tags | — | `page`, `limit` |
| `get_tag` | Get a single tag by ID or slug | `identifier` | — |

#### Authors (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `list_authors` | List all authors/contributors for this publication | — | `page`, `limit` |
| `get_author` | Get a single author by numeric ID | `identifier` (numeric) | — |

#### Site Structure (3 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `get_publisher_data` | Publisher profile: branding, logo, accent colors, social links, app store URLs. Falls back to footer data if primary endpoint unavailable | — | — |
| `get_navbar` | Navigation menu configuration including nested items and links | — | — |
| `get_footer` | Footer layout: menus, links, copyright, app store URLs, social links, logo | — | — |

#### Content Metadata (4 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `get_content_types` | All content types configured for this publication (e.g. Article, Video, Web Story) | — | — |
| `get_active_slots` | Configured advertisement slots with dimensions and HTML content | — | — |
| `get_newsletter_groups` | All newsletter groups with metadata. Returns `not_configured` error if publisher has no newsletter — do not retry | — | — |
| `get_form_schema` | Form schema by ID including field definitions, validation rules, and captcha config | `schema_id` (24-char hex) | `page_source` |

#### Content Resolution (1 tool)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `identify_content` | Resolve a URL path to its content type: post, category, tag, author, redirect, or not_found | `legacy_url` | — |

---

### CMS Tools — Write (34 tools)

All CMS tools call `https://cms-beta.thepublive.com/publisher/{publisherId}/`. Write operations use a tiered safety model:

**Tier 1 — List / Get:** Direct call. No `dry_run`. Always executes immediately.

**Tier 2 — Create:** `dry_run=true` (default) returns a formatted preview of what will be created — **no changes made**. Set `dry_run=false` to commit.

**Tier 3 — Update:** `dry_run=true` (default) fetches the current state and returns a field-by-field diff (old → new) — **no changes made**. Set `dry_run=false` to apply.

**Tier 3+ — Delete:** `dry_run=true` (default) fetches and displays the item — **no deletion**. To permanently delete, you must set **both** `dry_run=false` **and** `confirm_delete=true`.

#### Categories (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_categories` | `page`, `limit` | Returns all categories including unpublished |
| `cms_get_category` | `id` (required) | — |
| `cms_create_category` | `name`, `english_name` (required); `slug`, `meta_title`, `h1_tag`, `meta_description`, `parent_category`, `priority`, `content`, `category_brand_color`, `content_type`, `dry_run` | Immutable after creation: `english_name`, `slug`, `parent_category`, `content_type` |
| `cms_update_category` | `id` (required); `name`, `meta_title`, `meta_description`, `content`, `category_brand_color`, `priority`, `dry_run` | Cannot change: `english_name`, `slug`, `content_type` |
| `cms_delete_category` | `id` (required); `dry_run`, `confirm_delete` | Posts lose category assignment on delete |

#### Tags (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_tags` | `page`, `limit` | — |
| `cms_get_tag` | `id` (required) | — |
| `cms_create_tag` | `name`, `english_name` (required); `slug`, `meta_title`, `meta_description`, `content`, `dry_run` | Immutable after creation: `english_name`, `slug` |
| `cms_update_tag` | `id` (required); `name`, `meta_title`, `meta_description`, `content`, `dry_run` | Cannot change: `english_name`, `slug` |
| `cms_delete_tag` | `id` (required); `dry_run`, `confirm_delete` | — |

#### Posts (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_posts` | `page`, `limit` | Includes drafts, published, and scheduled — unlike CDS `list_posts` which is published-only |
| `cms_get_post` | `id` (required) | Returns full post including draft content |
| `cms_create_post` | `title`, `english_title`, `type`, `status`, `primary_category` (required); `contributors`, `content`, `tags`, `categories`, `banner_url`, `banner_description`, `short_description`, `summary`, `seo_keyphrase`, `slug`, `scheduled_at`, `hide_banner_image`, `custom_published_at`, `dry_run` | Immutable after creation: `english_title`, `type`, `slug`, `meta_data`, `custom_published_at` |
| `cms_update_post` | `id` (required); `title`, `content`, `status`, `primary_category`, `contributors`, `tags`, `categories`, `banner_url`, `short_description`, `hide_banner_image`, `custom_published_at`, `scheduled_at`, `dry_run`, `confirm_publish` | Setting `status=Published` with `dry_run=false` also requires `confirm_publish=true`. Cannot change: `english_title`, `type`, `slug` |
| `cms_delete_post` | `id` (required); `dry_run`, `confirm_delete` | Permanently removes post and all associated data |

#### Live Blog Updates (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_live_blog_updates` | `post_id` (required) | Only applies to posts with `type=LiveBlog` |
| `cms_get_live_blog_update` | `post_id`, `id` (required) | — |
| `cms_create_live_blog_update` | `post_id`, `title`, `content` (required); `dry_run` | — |
| `cms_update_live_blog_update` | `post_id`, `id` (required); `title`, `content`, `dry_run` | — |
| `cms_delete_live_blog_update` | `post_id`, `id` (required); `dry_run`, `confirm_delete` | — |

#### Media Library (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_media` | `page`, `limit` | — |
| `cms_get_media` | `id` (required) | — |
| `cms_create_media` | `filename`, `path` (required); `alt_text`, `caption`, `source`, `type`, `meta_data`, `dry_run` | Registers an external URL (S3, Cloudinary etc.) — does **not** upload files. Immutable after creation: `path`, `type` |
| `cms_update_media` | `id` (required); `filename`, `alt_text`, `caption`, `source`, `meta_data`, `dry_run` | Cannot change: `path`, `type` |
| `cms_delete_media` | `id` (required); `dry_run`, `confirm_delete` | Posts referencing this media lose their image/file |

#### Custom Components (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `cms_list_custom_components` | `page`, `limit` | — |
| `cms_get_custom_component` | `id` (required) | — |
| `cms_create_custom_component` | `name` (required); `content`, `dry_run` | — |
| `cms_update_custom_component` | `id` (required); `name`, `content`, `dry_run` | — |
| `cms_delete_custom_component` | `id` (required); `dry_run`, `confirm_delete` | — |

#### Validation Tools — Pre-flight Checks (4 tools)

Read-only tools that check whether a resource exists before using its ID in a create/update call. Never write anything.

| Tool | Params | Returns |
|---|---|---|
| `validate_media_exists` | `id` (integer) | `{valid, id, filename, path}` or `{valid: false, reason}` |
| `validate_category_exists` | `id` (integer) | `{valid, id, name}` or `{valid: false, reason}` |
| `validate_author_exists` | `id` (integer) | `{valid, id, name}` or `{valid: false, reason}` (checks CDS) |
| `validate_post_slug` | `slug` (string) | `{valid: true, slug, available: true}` if free, or `{valid: false, reason}` if taken |

---

### Immutable Fields

Fields that cannot be changed after creation (CMS API enforces this):

| Resource | Immutable after creation |
|---|---|
| Category | `english_name`, `slug`, `parent_category`, `content_type` |
| Tag | `english_name`, `slug` |
| Post | `english_title`, `type`, `slug`, `meta_data`, `custom_published_at` |
| Media | `path`, `type` |

---

## Safety Model

1. **Input validation** — Required fields are declared in each tool's `inputSchema`. The MCP client enforces these before the server is called.

2. **Dry-run preview (default for all writes)** — Every create, update, and delete tool defaults to `dry_run=true`. The AI sees exactly what will change before committing. The server returns a formatted preview and takes no action until `dry_run=false` is explicit.

3. **Double confirmation for delete** — Deletes require *two* explicit overrides: `dry_run=false` to bypass the preview, and `confirm_delete=true` to acknowledge the action is irreversible. Either alone is insufficient.

4. **Publish gate** — Setting a post's `status` to `Published` (via `cms_update_post`) with `dry_run=false` additionally requires `confirm_publish=true`. Prevents accidental publishing during bulk updates.

5. **Per-session write rate limit** — SSE sessions are capped at 50 CMS write operations (create/update/delete). The 51st write returns a `rate_limit` error instructing the client to start a new session.

6. **Credential validation before any tool** — Both auth paths (OAuth and session) validate credentials against the live CDS API before issuing a token or cookie.

---

## Error Reference

### CDS tool errors (`"error"` key)

| `error` value | Meaning | Retryable |
|---|---|---|
| `upstream_timeout` | CDS `/posts/` timed out after retries | Yes — try narrowing filters |
| `invalid_input` | Empty or non-numeric identifier passed to a tool | No |
| `not_found` | Resource ID not found in CDS | No |
| `not_configured` | Publisher has no newsletter configured | No |
| `auth_expired` | CDS rejected credentials (HTTP 401) | No — re-authenticate |

### CMS client errors (`"error_type"` key)

| `error_type` value | Meaning | `retryable` |
|---|---|---|
| `auth_error` | HTTP 401 — CMS credentials rejected | `false` |
| `not_found` | HTTP 404 — resource does not exist | `false` |
| `bad_request` | HTTP 400–499 — invalid data sent to CMS | `false` |
| `upstream_error` | HTTP 5xx — CMS server failure | `true` |
| `timeout` | CMS request timed out (10 s) | `true` |
| `system_error` | Network or unexpected error | `false` |
| `confirmation_required` | Delete guard or publish gate triggered | `false` |
| `rate_limit` | Per-session write limit (50) reached | `false` — start new session |

---

## Observability (New Relic)

### Custom Events

| Event | When it fires | Key fields |
|---|---|---|
| `SSESessionOpen` | SSE client connects (`GET /mcp`) | `session_id`, `publisher_id`, `active_threads`, `active_sessions`, `trace_id` |
| `SSESessionClose` | SSE client disconnects | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms` |
| `MCPSessionSummary` | At SSE session close — full session rollup | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms`, `total_estimated_input_tokens`, `total_estimated_output_tokens`, `total_estimated_tokens`, `server_work_pct`, `session_client_name`, `tool_sequence` |
| `MCPSessionAbandoned` | SSE session closed with 0 tool calls | `session_id`, `publisher_id`, `duration_ms`, `session_client_name` |
| `MCPSessionMissing` | `POST /mcp/message` arrives for unknown `sessionId` | `session_id` |
| `MCPToolError` | A tool raises an unhandled exception | `tool_name`, `publisher_id`, `error_type`, `error_message`, `error_category`, `session_id`, `prompt_id`, `duration_ms`, `tool_input`, `trace_id` |
| `MCPToolDegraded` | A tool returns a structured error dict (partial failure, no exception) | `tool_name`, `publisher_id`, `degraded_reason`, `session_id`, `prompt_id`, `duration_ms`, `tool_input`, `trace_id` |
| `MCPPrompt` | Every `tools/call` (subject to 1000/min rate limit) | `prompt_id`, `prompt_text`, `prompt_source`, `session_id`, `tool_name`, `publisher_id`, `client_name`, `prompt_char_count`, `estimated_prompt_tokens`, `trace_id` |
| `MCPUnknownMethod` | Client sends a JSON-RPC method the server doesn't recognise | `method`, `session_id`, `jsonrpc_id` |

### Custom Metrics

#### Custom/MCP/\* metrics

| Metric | What it measures |
|---|---|
| `Custom/MCP/active_sessions` | Number of live SSE sessions (emitted on open and close) |
| `Custom/MCP/active_threads` | Gunicorn thread count at request time |
| `Custom/MCP/tool_call_count` | Total tool invocations (all tools, all outcomes) |
| `Custom/MCP/tool_success_count` | Tool calls that returned clean results |
| `Custom/MCP/tool_degraded_count` | Tool calls that returned a structured error dict |
| `Custom/MCP/tool_error_count` | Tool calls that raised an exception |
| `Custom/MCP/queue_wait_ms` | Time a tool response sat in the SSE queue before being sent |
| `Custom/MCP/queue_overflow_count` | SSE queue full events (client not draining stream) |
| `Custom/MCP/session_queue_depth` | Current depth of the per-session SSE message queue |
| `Custom/MCP/session_abandon_count` | Sessions that closed with 0 tool calls |
| `Custom/MCP/sse_session_missing_count` | POST /mcp/message hits for sessions not found (cross-worker routing failures) |
| `Custom/MCP/fallback_count` | Times `get_publisher_data` fell back to `/footer/` |
| `Custom/MCP/prompt_event_dropped_count` | MCPPrompt events dropped due to the 1000/min rate cap |

#### Custom/Tool/\* metrics

| Metric | What it measures |
|---|---|
| `Custom/Tool/{name}/call_count` | Invocations of a specific tool |
| `Custom/Tool/{name}/active_calls` | In-flight concurrent calls for a specific tool |
| `Custom/Tool/{name}/duration_ms` | Execution time for successful calls |
| `Custom/Tool/{name}/error_count` | Exceptions thrown by a specific tool |
| `Custom/Tool/{name}/error_duration_ms` | Execution time for failed calls |
| `Custom/Tool/{name}/degraded_count` | Structured-error (degraded) responses from a specific tool |

#### Custom/CDS/\* metrics

| Metric | What it measures |
|---|---|
| `Custom/CDS/latency_ms` | CDS HTTP request round-trip time |
| `Custom/CDS/response_size_bytes` | CDS response payload size |
| `Custom/CDS/retry_count` | Requests that needed a retry (408 / timeout) |
| `Custom/CDS/timeout_count` | Requests that timed out after all attempts |
| `Custom/CDS/error_count` | All CDS errors (any category) |

#### Custom/CMS/\* metrics

| Metric | What it measures |
|---|---|
| `Custom/CMS/latency_ms` | CMS HTTP request round-trip time |
| `Custom/CMS/response_size_bytes` | CMS response payload size |
| `Custom/CMS/timeout_count` | CMS requests that timed out |
| `Custom/CMS/error_count` | All CMS errors (any category) |

#### Custom/Auth/\* metrics

| Metric | What it measures |
|---|---|
| `Custom/Auth/client_registered_count` | Successful OAuth client registrations |
| `Custom/Auth/token_issued_count` | Bearer tokens issued |
| `Custom/Auth/auth_failure_count` | Failed login / token exchange attempts |
| `Custom/Auth/session_login_count` | Successful session logins |
| `Custom/Auth/session_logout_count` | Session logouts |

### Transaction Attributes

Key attributes set on every transaction (queryable via `FROM Transaction WHERE ...`):

| Attribute | Set on | Description |
|---|---|---|
| `mcp.tool_name` | Every `tools/call` | Name of the tool being called |
| `mcp.tool_input` | Every `tools/call` | JSON-serialised tool arguments (first 500 chars) |
| `mcp.tool_result_status` | After execution | `"success"`, `"degraded"`, or `"error"` |
| `mcp.tool_is_error` | After execution | `true` if the tool raised an exception |
| `mcp.tool_is_degraded` | After execution | `true` if the tool returned a structured error dict |
| `mcp.tool_duration_ms` | After execution | Wall-clock execution time |
| `mcp.tool_response_size` | After execution | Response payload size in bytes |
| `mcp.tool_concurrency` | Every `tools/call` | In-flight calls to this tool at dispatch time |
| `mcp.session_id` | Every request | Session identifier |
| `mcp.session_trace_id` | SSE requests | Stable trace ID for correlating all requests in one session |
| `mcp.session_tool_seq` | SSE `tools/call` | Ordinal position of this call within the session |
| `mcp.transport` | Every request | `"sse"` or `"http"` |
| `mcp.client_name` | Every request | Normalised client name (Claude Desktop, Cursor, etc.) |
| `mcp.client_version` | Every request | Version extracted from User-Agent |
| `mcp.prompt_id` | Every `tools/call` | UUID for the associated MCPPrompt event |
| `mcp.prompt_source` | Every `tools/call` | Where the prompt was found (`header`, `meta.*`, `tool_args`, etc.) |
| `mcp.tool_start_offset_ms` | SSE `tools/call` | Milliseconds since session started |
| `mcp.ai_think_time_ms` | SSE `tools/call` | Gap between previous tool response and this call (AI latency proxy) |
| `mcp.estimated_output_tokens` | After execution | Output token count proxy (chars ÷ 4) |
| `auth.flow` | Auth requests | `"oauth"` or `"session"` |
| `auth.result` | Auth requests | `"success"` or `"failure"` |
| `auth.publisher_id` | Auth requests | Publisher ID being authenticated |
| `cds.latency_ms` | CDS tool calls | CDS HTTP round-trip time |
| `cds.http_status` | CDS tool calls | CDS HTTP response status |
| `cds.retried` | CDS tool calls | `true` if the request was retried |
| `cms.latency_ms` | CMS tool calls | CMS HTTP round-trip time |
| `cms.http_status` | CMS tool calls | CMS HTTP response status |
| `cms.method` | CMS tool calls | `GET`, `POST`, `PATCH`, or `DELETE` |
| `error.category` | Error paths | `timeout`, `auth_error`, `not_found`, `bad_request`, `upstream_error`, `system_error` |

### Useful NRQL Queries

```sql
-- All tool calls in the last hour
SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL
SINCE 1 hour ago FACET mcp.tool_name

-- Tool error rate
SELECT count(*) FROM Transaction WHERE mcp.tool_is_error = true
SINCE 1 hour ago FACET mcp.tool_name

-- CMS tool latency (p95)
SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction
WHERE mcp.tool_name LIKE 'cms_%' SINCE 1 hour ago FACET mcp.tool_name

-- Session summary: tool sequences
SELECT session_id, tool_sequence, tool_call_count, duration_ms
FROM MCPSessionSummary SINCE 1 day ago

-- Failed auth attempts
SELECT count(*) FROM Transaction WHERE auth.result = 'failure'
SINCE 1 hour ago FACET auth.failure_reason

-- CDS upstream timeouts
SELECT count(*) FROM Transaction WHERE cds.timed_out = true
SINCE 1 hour ago

-- All transactions in a specific session
SELECT * FROM Transaction WHERE mcp.session_trace_id = '<paste_session_trace_id_here>'

-- Publisher activity
SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL
SINCE 1 day ago FACET mcp.client_name
```

---

## Deployment

### Environment Variables

| Variable | Required | Description | Default |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | Yes | Django secret key | `dev-insecure-key-change-me` |
| `BASE_URL` | Yes | Public URL of this server (used in OAuth redirect URIs) | `http://localhost:8000` |
| `DATABASE_URL` | Yes (prod) | PostgreSQL connection string. Set automatically by Railway | SQLite (`db.sqlite3`) |
| `DEBUG` | No | Enable Django debug mode | `False` |
| `REDIS_URL` | No | Redis connection string for session caching. Without it, DB sessions are used | — |
| `SESSION_COOKIE_AGE` | No | Session lifetime in seconds | `604800` (7 days) |
| `NEW_RELIC_LICENSE_KEY` | No | New Relic ingest key | — |
| `NEW_RELIC_APP_NAME` | No | App name shown in NR UI | `Publive MCP` |
| `NEW_RELIC_USER_KEY` | No | NR user key for deploy markers | — |
| `SERVER_VERSION` | No | Version tag stamped on all NR events | `1.0.0` |

### Running Locally

```bash
# Copy and fill env
cp .env.example .env

# Apply migrations
python manage.py migrate

# Start dev server
python manage.py runserver
```

### Production (Railway)

```
web:     gunicorn publive_mcp.wsgi -w 1 --threads 50 -b 0.0.0.0:$PORT --timeout 60
release: python manage.py migrate && python manage.py collectstatic --noinput
```

Single worker with 50 threads — required because SSE sessions hold a thread for the life of the connection. Multiple workers would break SSE session routing (`mcp_message` would land on a worker that doesn't have the session).

### MCP Endpoints

| Endpoint | Method | Transport |
|---|---|---|
| `GET /mcp` | SSE | Opens a long-lived SSE stream. Returns a `endpoint` event with the message URL |
| `POST /mcp/message?sessionId=X` | HTTP | Send JSON-RPC messages to an active SSE session |
| `POST /mcp` | HTTP | Stateless Streamable HTTP transport (MCP 2025-03-26). Supports batch requests |
| `GET /` | HTTP | Health check |
| `GET /auth/status` | HTTP | Session liveness check (used as Railway health check) |
