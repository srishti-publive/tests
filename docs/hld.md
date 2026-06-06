# High-Level Design: Publive MCP Server

## Purpose

A Model Context Protocol (MCP) server that bridges AI clients (Claude Desktop, Cursor, Anthropic SDK) to the Publive content platform. It exposes 61 tools — read-only CDS tools for fetching published content and write CMS tools for managing editorial content — over a standard JSON-RPC interface that any MCP-compatible AI client can call without knowing anything about the Publive API.

---

## System context

```
┌─────────────────────────────────────────────────────────────────┐
│                        AI Clients                               │
│  Claude Desktop    Cursor    Anthropic SDK    Python HTTPX     │
└────────────┬──────────┬───────────┬─────────────┬──────────────┘
             │ SSE      │ HTTP POST │ HTTP POST   │ HTTP POST
             │ (long-   │ (stateless│             │
             │  lived)  │  batch)   │             │
             ▼          ▼           ▼             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Publive MCP Server (Railway)                  │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────────┐ │
│  │  Auth Layer  │  │  MCP Layer   │  │   Observability Layer  │ │
│  │  (auth_app)  │  │  (mcp_app)   │  │   (New Relic)          │ │
│  └──────┬───────┘  └──────┬───────┘  └────────────────────────┘ │
│         │                 │                                      │
└─────────┼─────────────────┼──────────────────────────────────────┘
          │                 │
          ▼                 ▼
┌──────────────────┐  ┌────────────────────────────────────────┐
│  PostgreSQL DB   │  │         Publive APIs                   │
│  (Railway)       │  │                                        │
│  · OAuthClient   │  │  CDS (read)      CMS (write)           │
│  · OAuthCode     │  │  cds-beta.       cms-beta.             │
│  · OAuthToken    │  │  thepublive.com  thepublive.com        │
│  · Sessions      │  └────────────────────────────────────────┘
└──────────────────┘
```

---

## Component overview

### `auth_app` — Authentication & authorization

Handles both entry paths for users:

**OAuth 2.0 + PKCE** (AI clients — Claude Desktop, Cursor):
- Dynamic client registration (`POST /register`) — no pre-registration needed.
- Authorization form (`GET /authorize` → `POST /authorize`) — user enters Publive credentials, server validates against CDS, issues a short-lived auth code.
- Token exchange (`POST /token`) — PKCE verifier checked, long-lived bearer token issued.
- Token refresh (`POST /token` with `grant_type=refresh_token`) — refresh token rotates atomically on each use.
- Revocation (`POST /revoke`).

**Session auth** (browser users):
- Login form at `/connect` → `POST /auth/login` → credentials stored encrypted in Django session.
- Server-side absolute TTL enforced on every request — independent of the cookie expiry.

Both paths validate credentials against `CDS /posts/?limit=1` before issuing a token or session.

**Key components:**
- `models.py` — `OAuthClient`, `OAuthCode`, `OAuthToken` (all with `EncryptedJSONField` for credentials).
- `crypto.py` — Fernet symmetric encryption. Key from `CREDENTIALS_ENCRYPTION_KEY` env var.
- `services.py` — `validate_cds_credentials()`, `get/set_session_credentials()`, `check_session_ttl()`.
- `fields.py` — `EncryptedJSONField`: transparent encrypt/decrypt on DB read/write.

---

### `mcp_app` — MCP protocol, tool dispatch, observability

The core of the server. Layered into four sub-concerns:

**Transport layer** (`mcp_app/transport/`):
- `sse.py` — Long-lived SSE sessions. One gunicorn thread per session. In-process message queue. Keepalive every 25s.
- `http.py` — Stateless POST. Supports single and batch JSON-RPC. Session ID derived from token hash (stable across requests).

**Protocol layer** (`mcp_app/protocol/`):
- `auth.py` — `resolve_credentials()`: Bearer token → DB lookup, or session cookie → session store.
- `dispatch.py` — JSON-RPC router. Handles `initialize`, `tools/list`, `tools/call`, `ping`. Validates tool args against `inputSchema`. Enforces 50 CMS write-op limit per SSE session.
- `session.py` — Session ID derivation, `MCPPrompt` rate limit (1000 events/min).
- `session_store.py` — Shared in-process `session_stats` dict, updated by both transport and dispatch layers.

**Tool layer** (`mcp_app/cds/`, `mcp_app/cms/`):
- Data-driven: each tool is a dict `{name, description, inputSchema, handler}` in `TOOLS` or `CMS_TOOLS`.
- Dispatch calls `handler(credentials, arguments)`.
- No changes to the protocol layer when adding tools.

**HTTP clients** (`mcp_app/clients/`):
- `cds.py` — `cds_get()`. 5s timeout, 1 retry on timeout/408.
- `cms.py` — `cms_get/post/patch/delete()`. 10s timeout, no retry (write operations are not idempotent).
- Both build URLs as `{CDS_BASE_URL|CMS_BASE_URL}.format(publisher_id=...)` + path. Hosts configurable via env vars.

**Observability** (`mcp_app/nr_utils.py`, `mcp_app/prompt_capture.py`, `mcp_app/middleware.py`):
- All NR calls guarded — app runs cleanly without the agent.
- Prompt extraction from 6 sources (header → `_meta` → `params` → `arguments`).
- Rate limiting and security headers in middleware.

---

### `publive_mcp` — Django project wrapper

- `wsgi.py` — WSGI entry point, wrapped in `newrelic.agent.WSGIApplicationWrapper`.
- `settings/base.py` — All shared config: DB, cache, sessions, OAuth allowlists, Publive API hosts.
- `settings/prod.py` — `DEBUG=False`, `SESSION_COOKIE_SECURE=True`.
- `settings/local.py` — Local dev overrides.
- `urls.py` — Root router: includes `auth_app.urls` and `mcp_app.urls`.

---

## Request flows

### Flow 1: AI client connecting for the first time (OAuth PKCE)

```
1. Client → POST /mcp                          → 401 + WWW-Authenticate header
2. Client reads resource_metadata URL          → GET /.well-known/oauth-protected-resource
3. Client reads auth server metadata           → GET /.well-known/oauth-authorization-server
4. Client → POST /register                     → {client_id}  (saved to oauth_client table)
5. Client opens browser to GET /authorize      → login form rendered
6. User submits credentials
7. POST /authorize
   → validate against CDS /posts/?limit=1
   → OAuthCode created (encrypted credentials, 10-min TTL)
   → redirect to redirect_uri?code=...&state=...
8. Client → POST /token (code + PKCE verifier)
   → PKCE SHA-256 check
   → OAuthCode deleted
   → OAuthToken upserted (same client_id+publisher_id → stable token)
   → {access_token, refresh_token}
9. Client → POST /mcp (Authorization: Bearer <token>) → MCP tool calls work
```

### Flow 2: MCP tool call (SSE transport)

```
1. Client → GET /mcp (Bearer token)
   → resolve_credentials: OAuthToken lookup
   → open_sse_connection(): UUID session, queue created, SSESessionOpen emitted
   ← event: endpoint  data: .../mcp/message?sessionId=<uuid>

2. Client → POST /mcp/message?sessionId=<uuid>
   body: {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"fetch_published_posts","arguments":{...}}}
   → dispatch_jsonrpc()
     → extract_prompt_for_tool_call()     prompt extracted, MCPPrompt event
     → _validate_tool_args()              inputSchema check
     → dispatch_cds_tool()               → cds_get(credentials, "/posts/", params)
                                         → CDS API → JSON response
     → session_stats updated (timing, token estimate)
   → response put on msg_queue
   ← HTTP 200 {"ok":true}

   ← (SSE stream) event: message  data: {"jsonrpc":"2.0","id":1,"result":{"content":[...]}}

3. Client disconnects
   → _close_sse_session()
   → MCPSessionSummary + SSESessionClose emitted
```

### Flow 3: MCP tool call (HTTP transport)

```
Client → POST /mcp  (Authorization: Bearer <token>)
  body: {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{...}}
  → handle_http_request()
  → derive_session_id() from token hash (stable)
  → dispatch_jsonrpc() [same as SSE, no session_stats]
  ← JsonResponse with full JSON-RPC response
```

---

## Tool inventory

### CDS tools — read-only (22 tools)

| Group | Tools |
|---|---|
| Posts | `fetch_published_posts`, `fetch_published_post`, `fetch_post_by_url`, `fetch_liveblog_with_updates`, `fetch_trending_posts` |
| Categories | `fetch_published_categories`, `fetch_published_category` |
| Tags | `fetch_published_tags`, `fetch_published_tag` |
| Authors | `fetch_authors`, `fetch_author` |
| Site config | `fetch_publisher_profile`, `fetch_site_navigation`, `fetch_site_footer`, `fetch_newsletter_groups`, `fetch_ad_slots` |
| Content types | `fetch_content_type_definitions`, `fetch_form_schema`, `resolve_url_to_content_type` |
| Sitemaps | `fetch_sitemap`, `fetch_sitemap_page` |
| Static files | `fetch_static_file` |

### CMS tools — read + write (39 tools)

Write tools follow a tiered safety model. See `docs/tools.md` for full detail.

| Group | Read tools | Write tools |
|---|---|---|
| Posts | `list_editorial_posts`, `get_editorial_post` | `create_post`¹, `update_post`², `delete_post`³ |
| Categories | `list_editorial_categories`, `get_editorial_category` | `create_category`¹, `update_category`², `delete_category`³ |
| Tags | `list_editorial_tags`, `get_editorial_tag` | `create_tag`¹, `update_tag`², `delete_tag`³ |
| Live blog | `list_editorial_liveblog_updates`, `get_liveblog_update` | `add_liveblog_update`, `update_liveblog_update`², `delete_liveblog_update`³ |
| Component schemas | `list_component_schemas`, `get_component_schema` | `create_component_schema`¹, `update_component_schema`², `delete_component_schema`³ |
| Content type schemas | `list_content_type_schemas`, `get_content_type_schema` | `create_content_type_schema`¹, `update_content_type_schema`², `delete_content_type_schema`³ |
| Media | `list_media_assets`, `get_media_asset` | `register_media_asset`¹, `update_media_asset`², `delete_media_asset`³ |
| Validation | `validate_media_asset`, `validate_category`, `validate_author`, `validate_post_slug` | — |

¹ Tier 2 (create): `dry_run=True` by default — returns preview without writing.  
² Tier 3 (update): `dry_run=True` shows human-readable diff of old vs new fields.  
³ Tier 3 (delete): requires both `dry_run=false` AND `confirm_delete=true` to execute.

---

## Data model

```
┌─────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│  OAuthClient    │       │   OAuthCode      │       │   OAuthToken     │
│─────────────────│       │──────────────────│       │──────────────────│
│ client_id (PK)  │       │ code (PK)        │       │ token (PK)       │
│ redirect_uri    │       │ client_id        │       │ client_id        │
│ created_at      │       │ redirect_uri     │       │ publisher_id ◄── index
└─────────────────┘       │ code_challenge   │       │ refresh_token    │
                          │ credentials ◄── Fernet   │ credentials ◄── Fernet
                          │ expires_at       │       │ created_at       │
                          └──────────────────┘       └──────────────────┘

                          ┌──────────────────┐
                          │  django_session  │
                          │──────────────────│
                          │ session_key (PK) │
                          │ session_data ◄── encrypted credentials inside
                          │ expire_date      │
                          └──────────────────┘
```

`OAuthCode` — single-use, 10-minute TTL, deleted on redemption.  
`OAuthToken` — permanent, upserted on re-auth (same `client_id + publisher_id` → same token).  
`OAuthClient` — permanent, one per AI client install.  
`credentials` in all models — Fernet-encrypted JSON `{publisherId, apiKey, apiSecret}`.

---

## Middleware stack

Executed in order on every request:

| Middleware | Responsibility |
|---|---|
| `SecurityMiddleware` | HTTPS redirect, HSTS, `X-Content-Type-Options` |
| `WhiteNoiseMiddleware` | Serve static files directly from the process (no nginx needed) |
| `RequestIDMiddleware` | Attach `X-Request-ID` to every request/response for log correlation |
| `SessionMiddleware` | Load/save Django session for cookie-auth clients |
| `CommonMiddleware` | URL normalisation (trailing slashes) |
| `SecurityHeadersMiddleware` | CSP, `X-Frame-Options`, `Referrer-Policy` on HTML responses only |
| `RateLimitMiddleware` | Sliding-window rate limits on auth and MCP endpoints (fail-open) |

---

## Security model

| Concern | Mechanism |
|---|---|
| Credentials at rest | Fernet symmetric encryption (`EncryptedJSONField`) on every DB write |
| Transport security | HTTPS enforced by `SecurityMiddleware` in production |
| PKCE | Code verifier verified via SHA-256 at token exchange — no client secret needed |
| Refresh token rotation | Atomic DB transaction; stolen token becomes single-use |
| Origin check | `POST /register`, `/token`, `/authorize` reject browser `Origin` not in allowlist |
| Redirect URI allowlist | Only `OAUTH_ALLOWED_REDIRECT_URIS` accepted at registration |
| Implicit grant | Disabled — only `response_type=code` accepted |
| Rate limiting | Sliding window: 10 req/min on login, 300 req/min on MCP (by token prefix) |
| CMS write cap | 50 mutations per SSE session — prevents runaway agent loops |
| Session TTL | Server-side absolute deadline enforced independently of cookie expiry |
| CSRF | Exempt on MCP endpoints (machine clients); enforced on browser auth forms |

---

## Observability model

All observability is in `mcp_app/nr_utils.py` — guarded no-ops when New Relic is absent.

| Signal | What | Where |
|---|---|---|
| **Custom events** | `MCPPrompt`, `MCPToolError`, `MCPToolDegraded`, `SSESessionOpen/Close`, `MCPSessionAbandoned`, `MCPSessionSummary`, `MCPSessionMissing`, `MCPUnknownMethod` | New Relic Insights |
| **Custom metrics** | Per-tool call/error/latency, active sessions, queue depth, auth counts | New Relic Metrics |
| **Transaction attrs** | `mcp.tool_name`, `mcp.tool_duration_ms`, `auth.publisher_id`, `cds.http_status`, etc. | Every NR APM transaction |
| **Structured logs** | JSON via `python-json-logger`; every line has `asctime`, `levelname`, `name`, `message` | Railway log stream / NR Logs |
| **Error reporting** | `notice_err()` with attrs on `error_type`, `error_layer`, `error_category` | NR Error Inbox |

---

## Deployment topology

```
GitHub (main branch)
    │
    │ push → Railway auto-deploy
    ▼
Docker image build
    → pip install
    → collectstatic (baked into image)
    │
    ▼
Container start (entrypoint.sh)
    → python manage.py migrate   (visible in logs)
    → showmigrations auth_app    (visible in logs)
    → exec gunicorn -w 1 --threads 50

Railway healthcheck: GET / → 200 within 300s
    │
    ▼
Running container
    · 1 worker, 50 threads
    · PORT from Railway env
    · DATABASE_URL → Railway PostgreSQL
    · CREDENTIALS_ENCRYPTION_KEY → Fernet key (must be set manually)
```

**Scaling constraint:** 1 worker is an architectural requirement for SSE session affinity, not a tuning parameter. Horizontal scaling requires externalising `_sse_sessions` to Redis. See `docs/deployment.md`.

---

## Configuration reference

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | Yes | — | Django cryptographic signing |
| `DATABASE_URL` | Yes (prod) | SQLite | Postgres connection string |
| `CREDENTIALS_ENCRYPTION_KEY` | Yes (prod) | Ephemeral (unsafe) | Fernet key for credential encryption |
| `BASE_URL` | Yes (prod) | `http://localhost:8000` | OAuth metadata discovery, session redirect |
| `CDS_BASE_URL` | No | `https://cds-beta.thepublive.com/publisher/{publisher_id}` | CDS host template |
| `CMS_BASE_URL` | No | `https://cms-beta.thepublive.com/publisher/{publisher_id}` | CMS host template |
| `NEW_RELIC_LICENSE_KEY` | No | — | NR activation; all NR calls are no-ops without it |
| `NEW_RELIC_APP_NAME` | No | `Publive MCP` | NR app grouping (useful to separate staging/prod) |
| `SERVER_VERSION` | No | `1.0.0` | Attached to every NR event |
| `MCP_QUEUE_MAXSIZE` | No | `100` | SSE per-session message queue cap |
| `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` | No | — | Comma-separated extra redirect URIs for non-standard clients |
| `RAILWAY_ENVIRONMENT` | Auto | — | Set by Railway; read as `SERVER_ENV` on every NR event |

---

## Dependency map

```
Django 4.2          — web framework, ORM, sessions, migrations
gunicorn 21         — WSGI server (gthread worker for SSE)
psycopg2-binary     — PostgreSQL adapter
dj-database-url     — parse DATABASE_URL into Django DATABASES dict
whitenoise          — static file serving without nginx
cryptography        — Fernet encryption for credentials at rest
python-dotenv       — load .env in local dev
requests            — outbound HTTP to CDS/CMS APIs
newrelic 13         — APM, custom events, metrics
python-json-logger  — structured JSON log output
django-redis        — optional Redis cache backend (not yet wired; locmem used)
redis               — Redis client (required by django-redis; installed but inactive)
```

`django-redis` and `redis` are installed but the cache backend is currently `LocMemCache`. They are ready to be wired when SSE session state is externalised for multi-instance deployments.

---

## Further reading

| Topic | Document |
|---|---|
| Auth flows, OAuth PKCE, session TTL, encryption | `docs/auth.md` |
| MCP transports, session lifecycle, dispatch pipeline | `docs/mcp-protocol.md` |
| New Relic events, metrics, transaction naming | `docs/newrelic.md` |
| Docker, Railway, Fargate migration | `docs/deployment.md` |
