# Publive MCP Server

A Django server that exposes Publive's Content Delivery (CDS) and Content Management (CMS) APIs as MCP tools for AI clients.

## What is this?

The Publive MCP Server bridges AI clients (Claude Desktop, Cursor, custom agents) and Publive's publishing platform. It implements the Model Context Protocol (MCP) so that an AI can read published content, manage drafts, create posts, and manipulate media entirely through natural language. Non-technical users connect via the Claude Desktop OAuth flow; developers connect with a Bearer token and their Publive API credentials (`publisherId`, `apiKey`, `apiSecret`).

### Architecture documentation

Formal design docs live under [`docs/`](docs/):

- [architecture.md](docs/architecture.md) — layered architecture of `mcp_app` (transport → protocol → tools → HTTP clients)
- [hld.md](docs/hld.md) — high-level design: system context, components, request flows, tool inventory, deployment topology
- [lld.md](docs/lld.md) — low-level design: function-by-function reference for every module
- [auth.md](docs/auth.md) — OAuth 2.0 + PKCE and session-auth implementation details
- [mcp-protocol.md](docs/mcp-protocol.md) — transports, session lifecycle, JSON-RPC dispatch, tool call pipeline
- [deployment.md](docs/deployment.md) — Docker, Railway, and Fargate migration guide
- [newrelic.md](docs/newrelic.md) — custom events, metrics, transaction naming, design decisions

## Authentication

### OAuth 2.0 + PKCE (for desktop & CLI clients)

Used by Claude Desktop, Cursor, Claude Code CLI, and other MCP-aware clients — including native/CLI tools that spin up a temporary local callback server (`http://localhost:<port>/...`) for the OAuth redirect. Loopback redirect URIs are accepted with any port (RFC 8252 §7.3): the server matches them on scheme + host + path and ignores the port, both at registration and at authorize time.

| Step | Method | Endpoint | Notes |
|---|---|---|---|
| Register client | POST | `/register` | Dynamic client registration. Accepts the static allowlisted redirect URIs (claude.ai, claude.com, chatgpt.com) plus any `http://localhost:*` / `http://127.0.0.1:*` loopback URI |
| Authorize | GET/POST | `/oauth/authorize` | Validates CDS credentials, issues PKCE code |
| Exchange code | POST | `/oauth/token` | Returns a long-lived Bearer token (no expiry — `OAuthToken` has no TTL field; same `client_id`+`publisherId` always upserts to the same token, with a rotating `refresh_token`) |

Use the token on every request:
```
Authorization: Bearer <token>
```

> `dispatch_jsonrpc` will surface a `tokenExpiresAt` (ISO 8601) field on the `initialize` response when a token expiry is known, but Bearer tokens issued by `/oauth/token` currently never expire — `resolve_credentials()` always resolves them with `token_expires_at=None`, so this field does not appear in practice for the OAuth flow.

#### Connecting a CLI client

Most MCP-aware CLIs handle the OAuth dance for you — just point them at `/mcp`:

```bash
claude mcp add --transport http publive https://<your-server>/mcp
```

For headless setups that can't open a browser, run the OAuth flow once to obtain a Bearer token and pass it as a static header instead:

```bash
claude mcp add --transport http publive https://<your-server>/mcp \
  --header "Authorization: Bearer <token>"
```

The server doesn't gate access by client identity — `resolve_credentials()` accepts any Bearer token issued via `/oauth/token` or any valid session cookie, regardless of which MCP client presents it. `client_name`/`client_version` are recorded for observability only (see `mcp.client_name` below).

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

Sessions don't expire on their own — `auth_login` sets `session_ttl_seconds = -1` and `set_expiry(10 years)`, so a session lives until the user explicitly hits `POST /auth/logout` (or `check_session_ttl` rejects a legacy finite-TTL session created before this behavior). Credentials are encrypted at rest in the session (Fernet, see `CREDENTIALS_ENCRYPTION_KEY`) and validated against the CDS API on every login attempt.

---

## Tools Reference

The server exposes **61 tools** total — 22 read-only CDS tools and 39 CMS read/write tools. The full data-driven tool registries live in `mcp_app/cds/` (`TOOLS`) and `mcp_app/cms/` (`CMS_TOOLS`); this is a summary.

### CDS Tools — Read Only (22 tools)

All CDS tools are read-only GET requests against `https://cds-beta.thepublive.com/publisher/{publisherId}/`. No changes are made to any data.

#### Posts (5 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_published_posts` | List and filter published posts | — | `page`, `limit`, `type__eq`, `type__neq`, `type__in`, `type__nin`, `title__contains`, `categories.id__eq`, `categories.id__in`, `categories.id__nin`, `tags.id__eq`, `tags.id__in`, `tags.id__nin`, `tags__slug__eq`, `contributors.id__eq`, `contributors.id__in`, `contributors__slug__eq`, `primary_category.id__eq`, `primary_category.id__in`, `primary_category__slug__eq`, `created_at__gte`, `created_at__lte`, `word_count__gt`, `word_count__lt`, `sort_by`, `sort_order` |
| `fetch_published_post` | Get full details of a single published post by ID or slug | `identifier` | — |
| `fetch_post_by_url` | Get a post by its legacy or relative URL path (must start with `/`) | `legacy_url` | — |
| `fetch_liveblog_with_updates` | Get a LiveBlog post and all its published update entries in a single call (only works for `type=LiveBlog`) | `post_id` | `page`, `limit` |
| `fetch_trending_posts` | Top-performing posts ranked by page views. Requires Publive analytics active. Rankings refresh every 5–10 minutes | — | `duration` (`24h`/`7d`/`30d`, default `24h`), `limit`, `page`, `type__eq` |

#### Categories (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_published_categories` | List all published categories with hierarchical structure | — | `page`, `limit` |
| `fetch_published_category` | Get a single published category by ID or slug including SEO metadata and child categories | `identifier` | — |

#### Tags (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_published_tags` | List all published tags | — | `page`, `limit` |
| `fetch_published_tag` | Get a single published tag by ID or slug | `identifier` | — |

#### Authors (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_authors` | List all authors/contributors for this publication | — | `page`, `limit` |
| `fetch_author` | Get a single author by numeric ID | `identifier` (numeric) | — |

#### Site Structure (4 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_publisher_profile` | Publisher profile: branding, logo, accent colors, social links, app store URLs. Falls back to footer data if primary endpoint unavailable | — | — |
| `fetch_site_navigation` | Navigation menu configuration including nested items and links | — | — |
| `fetch_site_footer` | Footer layout: menus, links, copyright, app store URLs, social links, logo | — | — |
| `fetch_newsletter_groups` | All newsletter groups with metadata. Returns `not_configured` error if publisher has no newsletter — do not retry | — | — |

#### Content Metadata (3 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_content_type_definitions` | All content types configured for this publication (e.g. Article, Video, Web Story) with their API and collection slugs | — | — |
| `fetch_ad_slots` | Configured advertisement slots with dimensions, HTML content, and slot type | — | — |
| `fetch_form_schema` | Form schema by ID including field definitions, validation rules, field groups, and captcha config | `schema_id` | `page_source` |

#### Content Resolution (1 tool)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `resolve_url_to_content_type` | Resolve a URL path to its content type: post, category, tag, author, redirect, or not_found | `legacy_url` | — |

#### Sitemaps (2 tools)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_sitemap` | Get a sitemap XML by type — `index` (master), `web_index` (article), `web_stories`, `news`, or `categories` | `type` (enum: `index`/`web_index`/`web_stories`/`news`/`categories`) | — |
| `fetch_sitemap_page` | Paginated date-stamped sitemap (article `sitemap_{date}.xml` or web story `webstory_sitemap_{date}.xml`). Discover valid dates from `fetch_sitemap(type='web_index')` or `fetch_sitemap(type='web_stories')` first | `date` | `type` (`article`/`webstory`, default `article`) |

#### Static Files (1 tool)

| Tool | Description | Required | Optional |
|---|---|---|---|
| `fetch_static_file` | Get a publisher-specific static file. `ads.txt`/`robots.txt` always exist; `service-worker.js` and the push-notification HTML files (`izooto.html`, `helper-iframe.html`, `permission-dialog.html`) return `not_configured` if push notifications aren't set up | `filename` (enum: `ads.txt`/`robots.txt`/`service-worker.js`/`izooto.html`/`helper-iframe.html`/`permission-dialog.html`) | — |

---

### CMS Tools — Read + Write (39 tools)

All CMS tools call `https://cms-beta.thepublive.com/publisher/{publisherId}/`. Write operations use a tiered safety model:

**Tier 1 — List / Get / Validate:** Direct call. No `dry_run`. Always executes immediately.

**Tier 2 — Create:** `dry_run=true` (default) returns a formatted preview of what will be created — **no changes made**. Set `dry_run=false` to commit. (Draft posts are an exception — see Posts below.)

**Tier 3 — Update:** `dry_run=true` (default) fetches the current state and returns a field-by-field diff (old → new) — **no changes made**. Set `dry_run=false` to apply.

**Tier 3+ — Delete:** `dry_run=true` (default) fetches and displays the item — **no deletion**. To permanently delete, you must set **both** `dry_run=false` **and** `confirm_delete=true`.

> `submit_form` was removed — it required a browser reCAPTCHA token that can't be obtained in an MCP context (`mcp_app/cms/forms.py`).

#### Categories (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_editorial_categories` | `page`, `limit` | Returns all categories including unpublished |
| `get_editorial_category` | `id` (required) | — |
| `create_category` | `name`, `english_name` (required); `slug`, `meta_title`, `h1_tag`, `meta_description`, `parent_category`, `priority`, `content`, `category_brand_color`, `content_type`, `dry_run` | Immutable after creation: `english_name`, `slug`, `parent_category`, `content_type` |
| `update_category` | `id` (required); `name`, `meta_title`, `meta_description`, `content`, `category_brand_color`, `priority`, `dry_run` | Cannot change: `english_name`, `slug`, `content_type` |
| `delete_category` | `id` (required); `dry_run`, `confirm_delete` | Posts lose category assignment on delete |

#### Tags (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_editorial_tags` | `page`, `limit` | — |
| `get_editorial_tag` | `id` (required) | — |
| `create_tag` | `name`, `english_name` (required); `slug`, `meta_title`, `meta_description`, `content`, `dry_run` | Immutable after creation: `english_name`, `slug` |
| `update_tag` | `id` (required); `name`, `meta_title`, `meta_description`, `content`, `dry_run` | Cannot change: `english_name`, `slug` |
| `delete_tag` | `id` (required); `dry_run`, `confirm_delete` | — |

#### Posts (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_editorial_posts` | `page`, `limit` | Includes drafts, published, and scheduled — unlike `fetch_published_posts` which is published-only |
| `get_editorial_post` | `id` (required) | Returns full post including draft content |
| `create_post` | `title`, `english_title`, `type`, `status`, `primary_category`, `contributors` (all six required); `content`, `tags`, `categories`, `banner_url`, `banner_description`, `short_description`, `summary`, `seo_keyphrase`, `slug`, `scheduled_at`, `hide_banner_image`, `custom_published_at`, `meta_video_url`, `meta_video_embed`, `meta_landscape_thumbnail`, `after_para`, `meta_data`, `dry_run` | `contributors` (≥1 author ID) is required by the upstream API. **Draft posts are created immediately — no `dry_run` step.** Published/Scheduled/Approval-Pending posts go through the Tier 2 preview. Type-specific requirements: Web Story needs `meta_landscape_thumbnail` (numeric media ID); Gallery needs `after_para`; Video creation via this tool is blocked by an upstream API bug — create an empty draft in the dashboard, then `update_post`. Immutable after creation: `english_title`, `type`, `slug`, `meta_data`, `custom_published_at` |
| `update_post` | `id` (required); `title`, `content`, `status`, `primary_category`, `contributors`, `tags`, `categories`, `banner_url`, `short_description`, `hide_banner_image`, `custom_published_at`, `scheduled_at`, `dry_run`, `confirm_publish` | Setting `status=Draft` applies immediately (no `dry_run` step); all other field updates go through the Tier 3 diff preview. Setting `status=Published` with `dry_run=false` additionally requires `confirm_publish=true`. Cannot change: `english_title`, `type`, `slug` |
| `delete_post` | `id` (required); `dry_run`, `confirm_delete` | Permanently removes post and all associated data |

#### Live Blog Updates (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_editorial_liveblog_updates` | `post_id` (required) | Only applies to posts with `type=LiveBlog` |
| `get_liveblog_update` | `post_id`, `id` (required) | — |
| `add_liveblog_update` | `post_id`, `title`, `content` (required); `is_pinned`, `dry_run` | — |
| `update_liveblog_update` | `post_id`, `id` (required); `title`, `content`, `is_pinned`, `dry_run` | — |
| `delete_liveblog_update` | `post_id`, `id` (required); `dry_run`, `confirm_delete` | — |

#### Media Library (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_media_assets` | `page`, `limit` | — |
| `get_media_asset` | `id` (required) | — |
| `register_media_asset` | `filename`, `path` (required); `alt_text`, `caption`, `source`, `type`, `meta_data`, `dry_run` | Registers an external URL (S3, Cloudinary etc.) — does **not** upload files. Immutable after creation: `path`, `type` |
| `update_media_asset` | `id` (required); `filename`, `alt_text`, `caption`, `source`, `meta_data`, `dry_run` | Cannot change: `path`, `type` |
| `delete_media_asset` | `id` (required); `dry_run`, `confirm_delete` | Posts referencing this media lose their image/file |

#### Component Schemas (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_component_schemas` | `page`, `limit` | — |
| `get_component_schema` | `id` (required) | — |
| `create_component_schema` | `name` (required); `meta_data`, `field_types`, `settings`, `dry_run` | Reusable typed-field schemas (form-builder style), not HTML templates |
| `update_component_schema` | `id` (required); `name`, `meta_data`, `field_types`, `settings`, `dry_run` | `field_types` replaces the whole array |
| `delete_component_schema` | `id` (required); `dry_run`, `confirm_delete` | — |

#### Content Type Schemas (5 tools)

| Tool | Key Params | Notes |
|---|---|---|
| `list_content_type_schemas` | `page`, `limit` | — |
| `get_content_type_schema` | `id` (required) | — |
| `create_content_type_schema` | `name`, `api_slug`, `api_collections_slug` (required); `type`, `response_type`, `field_types`, `groups`, `components`, `settings`, `global_system_default`, `dry_run` | Immutable after creation: `api_slug`, `api_collections_slug` |
| `update_content_type_schema` | `id` (required); `name`, `field_types`, `groups`, `components`, `settings`, `dry_run` | Cannot change: `api_slug`, `api_collections_slug` |
| `delete_content_type_schema` | `id` (required); `dry_run`, `confirm_delete` | All content entries lose their schema reference |

#### Validation Tools — Pre-flight Checks (4 tools)

Tier 1 read-only tools that check whether a resource exists before using its ID in a create/update call. Never write anything.

| Tool | Params | Returns |
|---|---|---|
| `validate_media_asset` | `id` (required) | `{valid, id, filename, path}` or `{valid: false, reason}` |
| `validate_category` | `id` (required) | `{valid, id, name}` or `{valid: false, reason}` |
| `validate_author` | `id` (required) | `{valid, id, name}` or `{valid: false, reason}` (checks CDS) |
| `validate_post_slug` | `slug` (required) | `{valid: true, slug, available: true}` if free, or `{valid: false, reason}` if taken |

---

### Immutable Fields

Fields that cannot be changed after creation (CMS API enforces this):

| Resource | Immutable after creation |
|---|---|
| Category | `english_name`, `slug`, `parent_category`, `content_type` |
| Tag | `english_name`, `slug` |
| Post | `english_title`, `type`, `slug`, `meta_data`, `custom_published_at` |
| Media | `path`, `type` |
| Content Type Schema | `api_slug`, `api_collections_slug` |

---

## Safety Model

1. **Input validation** — Required fields are declared in each tool's `inputSchema`. The MCP client enforces these before the server is called.

2. **Dry-run preview (default for create/update/delete)** — These tools default to `dry_run=true`. The AI sees exactly what will change before committing, and the server takes no action until `dry_run=false` is explicit. **Exception:** Draft posts (`create_post`/`update_post` with `status=Draft`) apply immediately — drafts are private, low-risk, and easily edited or deleted afterward, so the preview step is skipped.

3. **Double confirmation for delete** — Deletes require *two* explicit overrides: `dry_run=false` to bypass the preview, and `confirm_delete=true` to acknowledge the action is irreversible. Either alone is insufficient.

4. **Publish gate** — Setting a post's `status` to `Published` (via `update_post`) with `dry_run=false` additionally requires `confirm_publish=true`. Prevents accidental publishing during bulk updates.

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
| `Custom/MCP/fallback_count` | Times `fetch_publisher_profile` fell back to `/footer/` |
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

-- Editorial (CMS) tool latency (p95)
SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction
WHERE mcp.tool_name IN (
  'list_editorial_posts', 'get_editorial_post', 'create_post', 'update_post', 'delete_post',
  'list_editorial_categories', 'get_editorial_category', 'create_category', 'update_category', 'delete_category',
  'list_editorial_tags', 'get_editorial_tag', 'create_tag', 'update_tag', 'delete_tag',
  'list_media_assets', 'get_media_asset', 'register_media_asset', 'update_media_asset', 'delete_media_asset',
  'list_editorial_liveblog_updates', 'get_liveblog_update', 'add_liveblog_update', 'update_liveblog_update', 'delete_liveblog_update',
  'list_component_schemas', 'get_component_schema', 'create_component_schema', 'update_component_schema', 'delete_component_schema',
  'list_content_type_schemas', 'get_content_type_schema', 'create_content_type_schema', 'update_content_type_schema', 'delete_content_type_schema',
  'validate_author', 'validate_category', 'validate_media_asset', 'validate_post_slug'
) SINCE 1 hour ago FACET mcp.tool_name

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
| `DJANGO_SECRET_KEY` | Yes (prod) | Django secret key | `dev-insecure-key-change-me` |
| `BASE_URL` | Yes (prod) | Public URL of this server (used in OAuth redirect URIs, origin checks, and metadata) | `http://localhost:8000` |
| `DATABASE_URL` | Yes (prod) | PostgreSQL connection string. Set automatically by Railway; sessions and OAuth tokens live here so they survive redeploys | SQLite (`db.sqlite3`) |
| `CREDENTIALS_ENCRYPTION_KEY` | Yes (prod) | Fernet key (URL-safe base64, 32 bytes) encrypting stored CDS/CMS credentials (sessions, `OAuthCode`). Without it, an ephemeral key is generated at boot and credentials become unreadable after every restart | Ephemeral (unsafe for prod) |
| `RAILWAY_ENVIRONMENT` / `DJANGO_ENV` | Yes (prod) | Selects the settings module (`settings.prod` when set to `production`/`prod`, otherwise `settings.local`), which controls `DEBUG`, secure cookies, and rate limiting. Also stamped on New Relic events as `server.environment` | unset → local settings, `DEBUG=True` |
| `CDS_BASE_URL` | No | Base URL template for the read-only Delivery API (`{publisher_id}` is substituted per request) | `https://cds-beta.thepublive.com/publisher/{publisher_id}` |
| `CMS_BASE_URL` | No | Base URL template for the write CMS API (`{publisher_id}` is substituted per request) | `https://cms-beta.thepublive.com/publisher/{publisher_id}` |
| `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` | No | Comma-separated extra redirect URIs allowed at `/register`, beyond the built-in claude.ai/claude.com list and loopback (`localhost`/`127.0.0.1`) URIs | — |
| `MCP_QUEUE_MAXSIZE` | No | Per-SSE-session outbound message queue cap before the session is dropped as `queue_overflow` | `100` |
| `NEW_RELIC_LICENSE_KEY` | No | New Relic ingest key — all NR calls are no-ops without it | — |
| `NEW_RELIC_APP_NAME` | No | App name shown in NR UI | `Publive MCP` |
| `NEW_RELIC_USER_KEY` | No | NR user key for deploy markers | — |
| `SERVER_VERSION` | No | Version tag stamped on all NR events | `1.0.0` |

> **Note:** `DEBUG`, `ALLOWED_HOSTS`, and `SESSION_COOKIE_AGE` are **not** environment-configurable — they're hardcoded per settings module (`publive_mcp/settings/{base,local,prod}.py`): `ALLOWED_HOSTS = ["*"]`, `SESSION_COOKIE_AGE` is a fixed 90-day ceiling, and `DEBUG` follows local (`True`) vs. prod (`False`) based on `RAILWAY_ENVIRONMENT`/`DJANGO_ENV` above.

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

Deployed as a Docker image (`railway.toml` sets `builder = "dockerfile"`) — there is no Procfile. `Dockerfile` builds the image and runs `collectstatic` at build time; `entrypoint.sh` is the container's `CMD`:

```sh
python manage.py migrate --noinput   # logged, but doesn't block startup on failure
exec gunicorn publive_mcp.wsgi -w 1 --threads 50 -b 0.0.0.0:${PORT:-8000} --timeout 60
```

Migrations run in the entrypoint rather than Railway's release phase so their output always lands in the same container log stream and a failed migration can't silently block the deploy. `DJANGO_SETTINGS_MODULE=publive_mcp.settings.prod` is pinned at image build time. Single worker with 50 threads is required because SSE sessions are pinned to one process in memory — multiple workers would break SSE session routing (`mcp_message` could land on a worker that doesn't hold the session). See [docs/deployment.md](docs/deployment.md) for the full Docker/Railway/Fargate breakdown.

### MCP Endpoints

| Endpoint | Method | Transport |
|---|---|---|
| `GET /mcp` | SSE | Opens a long-lived SSE stream. Returns a `endpoint` event with the message URL |
| `POST /mcp/message?sessionId=X` | HTTP | Send JSON-RPC messages to an active SSE session |
| `POST /mcp` | HTTP | Stateless Streamable HTTP transport (MCP 2025-03-26). Supports batch requests |
| `GET /` | HTTP | Health check — dependency-free (no DB, no session), used as Railway's `healthcheckPath` so it reflects pure process liveness |
| `GET /auth/status` | HTTP | Session liveness check (whether the current browser session is authenticated) |
