# Publive MCP Server — End-to-End Architecture & Execution Guide

> A complete trace of how an MCP request enters the system, gets authenticated,
> dispatched, executed against the Publive APIs, and returned to the client.
>
> Source of truth: the code in this repo as of branch `CMS-complete`. Where the
> code contradicts older docs (`CLAUDE.md`, memory notes), this document follows
> the **code**.

---

## 0. One-paragraph summary

This is a **Django WSGI app** that exposes the Publive CDS (read) and CMS (write)
REST APIs as **Model Context Protocol (MCP) tools** for AI clients (Claude Desktop,
Cursor, ChatGPT, etc.). A request arrives at `/mcp`, is authenticated via either an
OAuth2 Bearer token or a Django session cookie, and is routed to one of two
transports — **SSE** (`GET /mcp`, long-lived stream) or **Streamable HTTP**
(`POST /mcp`, stateless). The body is JSON-RPC 2.0; `dispatch_jsonrpc()` handles
`initialize` / `tools/list` / `tools/call` / `ping`. A tool call is validated against
its `inputSchema`, dispatched to a CDS or CMS handler, which calls the upstream
Publive API over Basic Auth and returns an MCP content list. Every step is wrapped
in New Relic observability. There is **no Redis** — it was removed; state lives in
the database (sessions, OAuth) and in process memory (SSE sessions, telemetry).

---

# Part 1 — Entry Points and Routing

### Routing chain
```
publive_mcp/urls.py        ← ROOT_URLCONF (settings.base)
 ├── include("auth_app.urls")   → OAuth + session auth endpoints
 └── include("mcp_app.urls")    → MCP endpoints + health
```

### `mcp_app/urls.py`
| URL | Method | Handler | Purpose | Response |
|-----|--------|---------|---------|----------|
| `/` | GET | `views.health.health_check` | Liveness probe | JSON `{status, service, version, protocol}` |
| `/mcp` | GET | `views.mcp_endpoint` → `sse_open` | Open SSE stream | `text/event-stream` (streaming) |
| `/mcp` | POST | `views.mcp_endpoint` → `http_mcp` | Stateless JSON-RPC | `application/json` |
| `/mcp/message` | POST | `views.sse.sse_message` | Deliver a message into an open SSE session | JSON `{ok:true}` |

### `auth_app/urls.py`
| URL | Method | Handler | Purpose | Response |
|-----|--------|---------|---------|----------|
| `/.well-known/oauth-protected-resource[/...]` | GET | `oauth_protected_resource` | RFC 9728 resource metadata | JSON |
| `/.well-known/oauth-authorization-server` | GET | `oauth_server_metadata` | AS metadata | JSON |
| `/.well-known/openid-configuration` | GET | `oauth_server_metadata` | OIDC discovery (same doc) | JSON |
| `/register` | POST | `oauth_register` | Dynamic client registration (RFC 7591) | JSON 201 |
| `/authorize`, `/oauth/authorize` | GET/POST | `oauth_authorize` | Show login form / issue auth code | HTML or 302 redirect |
| `/token`, `/oauth/token` | POST | `oauth_token` | Code→token + refresh grant | JSON |
| `/revoke`, `/oauth/revoke` | POST | `oauth_revoke` | RFC 7009 revocation | JSON 200 (always) |
| `/userinfo` | GET | `oauth_userinfo` | OIDC identity (`sub`=publisherId) | JSON |
| `/connect` | GET | `connect` | Browser login page | HTML |
| `/auth/login` | POST | `auth_login` | Session login (validates via CDS) | JSON |
| `/auth/success` | GET | `auth_success` | Post-login page | HTML |
| `/auth/status` | GET | `auth_status` | Session introspection | JSON |
| `/auth/logout` | POST | `auth_logout` | Flush session | JSON |

**First code executed after Django routing for an MCP request:**
`mcp_app/views/__init__.py :: mcp_endpoint` (after the middleware stack).

---

# Part 2 — Request Lifecycle

### Middleware stack (settings/base.py, top→bottom on the way in)
1. `django.middleware.security.SecurityMiddleware`
2. `django.contrib.sessions.middleware.SessionMiddleware` — loads `request.session` from the DB session backend
3. `django.middleware.common.CommonMiddleware`
4. `whitenoise.middleware.WhiteNoiseMiddleware` — static files
5. `mcp_app.middleware.RequestIDMiddleware` — sets `request.request_id`, echoes `X-Request-ID`
6. `mcp_app.middleware.SecurityHeadersMiddleware` — CSP/X-Frame/nosniff on `text/html` responses only
7. `mcp_app.middleware.RateLimitMiddleware` — sliding-window limits (see table below)

### Rate limit rules (`middleware.py :: _RULES`)
| Prefix | Method | Limit | Window | Bucket key |
|--------|--------|-------|--------|-----------|
| `/auth/login` | POST | 10 | 60s | client IP |
| `/register` | POST | 20 | 60s | client IP |
| `/authorize` | any | 20 | 60s | client IP |
| `/token` | POST | 20 | 60s | client IP |
| `/mcp` | any | 300 | 60s | Bearer-token prefix (`tok:` first 12) or IP |

Counters live in Django's cache (`LocMemCache`). The middleware **fails open** — a cache
error never blocks traffic. Exceeding a limit → HTTP 429 with `Retry-After`.

### `mcp_endpoint` step-by-step
| Step | File · Function | Input | Processing | Output / Next |
|------|-----------------|-------|------------|---------------|
| 1 | `views/__init__.py :: mcp_endpoint` | `HttpRequest` | `identify_mcp_client()` parses User-Agent; logs method/bearer | calls `resolve_credentials` |
| 2 | `protocol/auth.py :: resolve_credentials` | request | Bearer→`OAuthToken` lookup; else session creds + TTL check | `(credentials, None, error_code)` |
| 3a | (auth fail) `build_unauthorized_response` | error_code | 401 + `WWW-Authenticate` + `authUrl` | returns to client |
| 3b | GET | credentials | `sse_open` | SSE transport |
| 3c | POST | credentials | `http_mcp` (asserts `Content-Type: application/json`, else 415) | HTTP transport |
| 4 | transport | request+creds | `json.loads(body)`; dispatch | JSON-RPC response |

Validation happens at **two** layers: transport (valid JSON, content-type) and
protocol (`_validate_tool_args` against each tool's `inputSchema`).

---

# Part 3 — Session Management

There are **two distinct notions of "session"** — don't conflate them.

### (A) Django auth session (browser login)
- **Generated/stored:** `SessionMiddleware` + `SESSION_ENGINE = django...backends.db`. Session row in the DB; session key in an HttpOnly cookie (`SESSION_COOKIE_AGE` ≈ 10 years).
- **Credentials:** stored in the session dict under `"credentials"` (`auth_app/services.py :: set_session_credentials`). Stored as **plain JSON** (Fernet encryption was intentionally removed — see memory `credentials-encryption-removed`).
- **TTL:** `session_ttl_seconds = -1` (never self-expires); `check_session_ttl()` enforces an absolute deadline only if a positive TTL is ever set. Ends via `/auth/logout` → `session.flush()`.

### (B) MCP transport session (per-connection telemetry/queue)
- **Session ID generation** — `protocol/session.py :: derive_session_id()` (HTTP transport) and `uuid.uuid4()` (SSE `open_sse_connection`). Priority for `derive_session_id`:
  1. Django `session.session_key` (cookie clients)
  2. `oauth-<sha256(token)[:16]>` (Bearer clients — stable per token)
  3. `anon-<uuid[:8]>` (sessionless probes)
- **SSE session registry** — `transport/sse.py :: _sse_sessions` (in-process dict guarded by `_sessions_lock`): `session_id → {credentials, token_expires_at, queue}`. The `queue` is a `queue.Queue(maxsize=MCP_QUEUE_MAXSIZE=100)`.
- **Telemetry store** — `protocol/session_store.py :: _stats` (in-process dict + `threading.Lock`): per-session counters (`tool_count`, `error_count`, token estimates, `tool_sequence`, write-op buckets, timing anchors).
- **Persistence:** both (B) stores are **process-memory only** — they vanish on restart and are **not shared across workers**. This is why the deployment runs a **single gunicorn worker** (Part 9).

> There is **no Redis**. The previous Redis-backed session/queue/stats were replaced
> by Django models (auth/OAuth) + in-process dicts (SSE/telemetry). See Part 8.

---

# Part 4 — Authentication & OAuth

Two auth paths resolve to the same thing: a `credentials = {publisherId, apiKey, apiSecret}` dict.

### Path 1 — OAuth 2.0 + PKCE (AI API clients)
```
register → authorize → callback(code) → token exchange → OAuthToken → Bearer on every /mcp call
```
1. **`POST /register`** (`oauth_register`): `check_origin()` gates web origins; desktop clients (no Origin) pass. `redirect_uri` must be HTTPS or loopback (`is_registrable_redirect_uri`). Creates `OAuthClient(client_id=token_urlsafe(24))`. → JSON `{client_id, ...}`.
2. **`GET /authorize`** (`oauth_authorize`): validates `response_type=code` + client + redirect URI; renders `authorize.html`.
3. **`POST /authorize`**: reads `publisherId/apiKey/apiSecret` + PKCE `code_challenge`. Calls **`validate_cds_credentials()`** (live `GET {CDS}/posts/?limit=1` with Basic Auth — only a 2xx is valid). On success creates a single-use **`OAuthCode`** (`code_challenge` stored, `expires_at = now+10min`) and 302-redirects to `redirect_uri?code=...&state=...`.
4. **`POST /token`** (`oauth_token`):
   - `grant_type=authorization_code`: looks up `OAuthCode`, checks expiry, verifies PKCE (`base64url(sha256(code_verifier)) == code_challenge`), checks redirect URI, deletes the code. **Upsert:** if an `OAuthToken` already exists for `(client_id, publisher_id)` it is reused (stable token identity); else a new `token` + `refresh_token` are created. Note: `publisherId` is stripped from the stored `credentials` JSON and kept in the flat `publisher_id` column.
   - `grant_type=refresh_token`: `select_for_update()` the row, **rotate** the refresh token atomically, return the same access token.
5. **Usage:** client sends `Authorization: Bearer <token>` on every `/mcp` call → `_resolve_oauth_token()` does `OAuthToken.objects.get(token=...)` and merges `publisherId` back in.
6. **Revoke:** `POST /revoke` deletes by access or refresh token; always 200 (RFC 7009).
7. **Refresh of credentials:** there is none — tokens are permanent until revoked/re-upserted. If the upstream CDS later rejects the stored apiKey/apiSecret (HTTP 401), the tool dispatcher returns an `auth_expired` message telling the user to re-auth.

### Path 2 — Session auth (browser)
`POST /auth/login` → `validate_cds_credentials()` → on success store creds in the Django session, set far-future expiry, return `{redirectTo:/auth/success}`. Resolved on `/mcp` via `_resolve_session()`.

### Models (`auth_app/models.py`)
- `OAuthClient(client_id, redirect_uri)` — one per client install.
- `OAuthCode(code, client_id, redirect_uri, code_challenge, credentials, expires_at)` — single-use, 10-min PKCE code.
- `OAuthToken(token, client_id, publisher_id, refresh_token, credentials)` — long-lived bearer; `credentials` is JSON, `publisher_id` is a flat column.

---

# Part 5 — MCP Protocol Flow

Both transports funnel into `protocol/dispatch.py :: dispatch_jsonrpc(body, credentials, request, session_id, token_expires_at)`.

```
dispatch_jsonrpc
 ├─ id is None  → notification, return None (no response)
 ├─ "initialize" → {protocolVersion 2024-11-05, capabilities{tools}, serverInfo, tokenExpiresAt?}
 ├─ "tools/list" → {tools: TOOLS + CMS_TOOLS}   (all tools, schemas inline)
 ├─ "tools/call" → _handle_tool_call(...)
 ├─ "ping"       → {}
 ├─ in _UNIMPLEMENTED_METHODS → JSON-RPC error -32601 (expected, quiet)
 └─ else         → -32601 + MCPUnknownMethod event
```

### `_handle_tool_call`
1. **Prompt capture** — `extract_prompt_for_tool_call()` pulls the user prompt from headers / `_meta` / `params.prompt` / `arguments._prompt` (stripped before the tool runs) / falls back to a JSON snapshot of args. Emits `MCPPrompt` (rate-limited to 1000/min by `should_emit_prompt_event`).
2. **Schema validation** — `_validate_tool_args()` checks required fields, JSON types (rejecting `bool` for `integer`), and `minLength`. Failure → `isError:true` content (still a JSON-RPC *success* envelope, MCP-style).
3. **Per-session write rate limit** — for CMS mutations, two independent 100-op buckets (`create` vs `update_delete`) tracked in the telemetry store; over-limit → `rate_limit` error content.
4. **Dispatch** — `dispatch_cms_tool(...)` if `name in CMS_TOOL_NAMES` else `dispatch_cds_tool(...)`.
5. **Result classification** — success / **degraded** (handler returned a dict carrying `error`/`error_type`) / **error** (exception). Each path records NR attrs/metrics/events and updates the session timeline.
6. **Return** — `{"content":[{"type":"text","text": json.dumps(result)}], "isError"?}` wrapped in `jsonrpc_ok`.

`PROTOCOL_VERSION = "2024-11-05"`. Schema registry `_SCHEMA_REGISTRY` is prebuilt from `TOOLS + CMS_TOOLS` at import time.

---

# Part 6 — "Skill" (Tool) Execution Flow

In this codebase a "skill" = an **MCP tool**. There is no plugin discovery at runtime;
tools are **statically registered** via Python imports.

```
Request → dispatch_jsonrpc → _handle_tool_call → dispatch_cds_tool/dispatch_cms_tool
        → _HANDLER_REGISTRY[name](credentials, args) → cds_get / cms_post|patch|delete
        → upstream Publive API → result dict → MCP content list → response
```

- **Discovery/registration:** each module under `cds/` and `cms/` exports `SCHEMAS` (list of `{name, description, inputSchema}`) and `HANDLERS` (`{name: callable}`). The package `__init__.py` concatenates them into `TOOLS` / `CMS_TOOLS` and merges handler dicts into `_HANDLER_REGISTRY`. `CMS_TOOL_NAMES` (frozenset) is how the dispatcher decides CDS vs CMS.
- **Loading:** import-time; nothing dynamic.
- **Execution:** `dispatch_cds_tool` / `dispatch_cms_tool` add concurrency tracking (`_active_calls` per tool name under a lock), wrap the handler in an NR `fn_trace`, and centralize error handling (CDS maps upstream 401 → `auth_expired` dict; CMS re-raises after `notice_err`).
- **Handler example** (`cds/posts.py :: fetch_published_posts`): pops `page`/`limit`, calls `cds_get(credentials, "/posts/", {...})`, and on upstream timeout returns a structured `{error:"upstream_timeout", retry:true}` dict (→ counts as "degraded").
- **Counts:** 22 CDS read tools + 39 CMS tools (see CLAUDE.md), aggregated and exposed via `tools/list`.

### CMS write-safety tiers (`cms/helpers.py` previews)
- **Create (Tier 2):** `dry_run=true` default → returns a preview, no write. (Draft posts are created immediately — no preview; see memory `cms-workflow`.)
- **Update (Tier 3):** `dry_run=true` default → human-readable old/new diff.
- **Delete (Tier 3):** requires `dry_run=false` **and** `confirm_delete=true` to actually delete.

---

# Part 7 — Transport Layer

## SSE (`transport/sse.py`)
- **Init:** `GET /mcp` → `open_sse_connection()` mints `session_id=uuid4`, `register_session()` (stores creds+queue), `init_stats()`, emits `SSESessionOpen`.
- **Streaming:** returns a `StreamingHttpResponse(event_stream())`. First frame is `event: endpoint\ndata: {BASE_URL}/mcp/message?sessionId=<id>` so the client knows where to POST. Then loops: `pop_message(timeout=25)` → on `None` send `: keepalive`, else `event: message\ndata: <json>`.
- **Inbound messages:** client POSTs JSON-RPC to `/mcp/message?sessionId=X` → `handle_sse_message()` looks up the session, runs `dispatch_jsonrpc`, then `push_message()` enqueues the response onto that session's queue (the streaming loop drains it). Missing session → 400 + `MCPSessionMissing`.
- **Teardown:** `finally` in `event_stream` → `_close_sse_session()` emits `MCPSessionSummary` + `SSESessionClose` (and `MCPSessionAbandoned` if 0 tool calls).
- **Backpressure:** `push_message` blocks up to 30s on a full queue; on overflow it drops the response, records `queue_overflow_count`.

## Streamable HTTP (`transport/http.py`)
- **Handling:** `POST /mcp` → `handle_http_request()` parses JSON (single object or batch list), calls `dispatch_jsonrpc` per message, returns `JsonResponse`. Notifications (id=None) yield no response → HTTP 202.
- **State:** stateless — no registry entry; `session_id` derived from `derive_session_id()` purely for telemetry tagging. (Because there is no `_stats` entry, the per-session timeline/rate-limit features are effectively SSE-only.)
- **Response:** single JSON object, or a JSON array for batches, or 202 if nothing to return.

## Comparison
| Feature | SSE | Streamable HTTP |
|---------|-----|-----------------|
| Connection | Long-lived `text/event-stream` (GET) + side-channel POST | Single request/response (POST) |
| State | In-process registry `_sse_sessions` + `_stats` | Stateless (telemetry tag only) |
| Response style | Async — pushed onto a per-session queue, streamed | Synchronous JSON in the POST response |
| Session handling | `uuid4` per connection; full lifecycle events | `derive_session_id` (stable per token), no lifecycle |
| Use cases | Legacy MCP 2024-11-05 clients needing server push | Modern stateless clients, batch JSON-RPC |
| Worker constraint | **Requires single worker** (in-proc registry) | Works with any worker count |

---

# Part 8 — Redis Integration

**There is no Redis.** It was removed (memory `redis-scaling`, 2026-06-15). What replaced it:

| Former Redis use | Now |
|------------------|-----|
| Sessions | Django DB session backend (`SESSION_ENGINE=...backends.db`) |
| OAuth codes/tokens | `OAuthCode` / `OAuthToken` DB models |
| Rate-limit counters | Django cache — **`LocMemCache`** in `settings/base.py` (per-process, in-memory) |
| SSE session registry + message queue | In-process `_sse_sessions` dict + `queue.Queue` |
| Per-session stats | In-process `_stats` dict (`session_store.py`) |

> Note: memory referenced `DatabaseCache` for rate limits; the **current code uses
> `LocMemCache`**. With a single worker this is process-wide and adequate; it would
> not be shared if the app were scaled to multiple workers/instances.

---

# Part 9 — Workers and Threads

- **Server:** gunicorn, WSGI app `publive_mcp.wsgi`.
- **Process/worker model:** `entrypoint.sh` runs `gunicorn -w 1 --threads 4 --timeout 60`.
  - **1 worker process**, **4 threads** (gunicorn `gthread`).
  > `CLAUDE.md` mentions `--threads 50`; the **actual entrypoint uses `--threads 4`**. Follow the entrypoint.
- **Shared vs isolated state:**
  - *Shared across threads in the one process:* `_sse_sessions`, `_stats`, `_active_calls`, the prompt rate-limit bucket, `LocMemCache`. All guarded by `threading.Lock`s.
  - *Isolated per request:* `request`, `credentials`, local vars.
  - *Shared across everything (durable):* the database (sessions, OAuth).
- **Why single worker matters:** SSE registry and telemetry are in-process. A second worker would not see sessions created in the first → SSE would break and rate-limit/telemetry would fragment. Scaling out requires moving that state to a shared store (DB/Redis) first.

**Examples**
- *Single worker, idle:* 1 process, base threads + up to 4 request threads.
- *3 concurrent HTTP tool calls:* handled on 3 of the 4 threads; `_active_calls[name]` reflects concurrency; each holds its own DB connection (`CONN_MAX_AGE=600`).
- *Long SSE stream + tool calls:* the SSE GET ties up one thread for the connection's life (apdex/trace suppressed); each `/mcp/message` POST uses another thread briefly.

---

# Part 10 — Database Layer

- **Engine:** `dj_database_url` — SQLite locally (`db.sqlite3`), Postgres on Railway via `DATABASE_URL`. `conn_max_age=600`.
- **Models in the request path:**
  | Table | Read on | Written on |
  |-------|---------|-----------|
  | `django_session` | every cookie-auth `/mcp`, `/auth/status` | login, every request (`SESSION_SAVE_EVERY_REQUEST=True`) |
  | `oauth_client` | `/authorize` validation | `/register` |
  | `oauth_code` | `/token` exchange | `/authorize` (POST) |
  | `oauth_token` | every Bearer `/mcp` call (`resolve_credentials`) | `/token`, `/revoke`, refresh rotation |
- **Trace (Bearer tool call):** request → `OAuthToken.objects.get(token=...)` (1 indexed SELECT) → credentials dict → tool runs (no further DB; upstream is HTTP) → response. Cookie clients add session SELECT + UPDATE.
- The upstream Publive CDS/CMS are **external HTTP APIs**, not this database.

---

# Part 11 — Error Handling

| Layer | Where | Behavior |
|-------|-------|----------|
| Transport | `http.py` / `sse.py` | Invalid JSON → 400; bad content-type → 415; missing SSE session → 400 + `MCPSessionMissing`; unexpected → `notice_err` + re-raise |
| Protocol | `dispatch.py` | Unknown method → -32601 (+`MCPUnknownMethod`); validation fail → `isError` content; tool exception caught in `_handle_tool_call` → `MCPToolError` + `{isError:true}` (never 500s the JSON-RPC) |
| Tool dispatch | `cds/__init__.py` | Upstream 401 → friendly `auth_expired` dict; else `notice_err` + raise |
| CDS client | `clients/cds.py` | 1 retry on timeout/408; on exhaustion classifies error, records metrics, raises |
| CMS client | `clients/cms.py` | **No retry**; returns a normalized `{error_type, message, retryable}` dict for 4xx/5xx/timeout/conn errors (→ surfaces as "degraded") |
| Auth | `auth_app/views.py` | CDS unreachable → 500; bad creds → 401; PKCE/grant errors → RFC-style `invalid_grant` 400 |
| Rate limit | `middleware.py` | 429 + `Retry-After`; **fails open** on cache errors |

- **Expected errors:** unimplemented MCP methods, validation failures, upstream 4xx, timeouts — all returned as structured content, not crashes.
- **Unexpected errors:** logged with `exc_info`, reported to NR via `notice_err`, re-raised at transport (→ Django 500) only when not catchable as tool content.
- **Recovery:** CDS single retry; CMS `retryable` flag; refresh-token rotation; SSE queue backpressure with overflow drop.

---

# Part 12 — Logging and Monitoring

- **Logging:** `settings/base.py LOGGING` — structured **JSON** via `pythonjsonlogger` to stdout. `mcp_app`/`auth_app` at INFO, `django` at WARNING. NR injects `trace.id`/`span.id` for APM↔Logs linking. `RequestIDMiddleware` adds correlation IDs.
- **New Relic:** all calls go through `nr_utils.py`, which **no-ops if the agent isn't installed** (`import newrelic.agent` guarded). Helpers: `set_txn_name`, `add_attrs`, `add_span_attrs`, `notice_err`, `record_event`, `record_metric`, `get_linking_metadata`, `suppress_apdex`/`suppress_trace`, `fn_trace`. Configured by `newrelic.ini`; started via the agent at boot.
- **Custom events:** `MCPPrompt`, `MCPToolError`, `MCPToolDegraded`, `MCPUnknownMethod`, `SSESessionOpen/Close`, `MCPSessionAbandoned`, `MCPSessionMissing`, `MCPSessionSummary`.
- **Key metrics:** `Custom/Tool/<name>/{call_count,duration_ms,error_count,degraded_count,active_calls}`, `Custom/MCP/{tool_call_count,active_sessions,active_threads,queue_wait_ms,queue_overflow_count}`, `Custom/CDS/*`, `Custom/CMS/*`, `Custom/Auth/*`.
- **Prompt/token proxy:** `prompt_capture.py` estimates tokens as `chars//4` (no tokenizer dependency).

---

# Part 13 — Deployment Architecture

- **Image:** `Dockerfile` — `python:3.12-slim`, installs `libpq-dev`/`gcc`, `pip install -r requirements.txt`, `collectstatic` at **build time**, `DJANGO_SETTINGS_MODULE=publive_mcp.settings.prod`.
- **Start:** `entrypoint.sh` on every container start → `migrate --noinput` → `showmigrations auth_app` → `exec gunicorn publive_mcp.wsgi -w 1 --threads 4 -b 0.0.0.0:$PORT --timeout 60`.
- **Platform:** Railway (`railway.toml`). No `Procfile`/release phase — migrations run in the entrypoint.
- **Required env:** `DJANGO_SECRET_KEY`; optional `DATABASE_URL` (else SQLite), `BASE_URL`, `CDS_BASE_URL`, `CMS_BASE_URL`, `MCP_QUEUE_MAXSIZE`, NR vars, `RATE_LIMIT_ENABLED`.
- **Settings split:** `base.py` (shared) → `prod.py` (`DEBUG=False`, `SESSION_COOKIE_SECURE=True`) / `local.py`.
- **Startup sequence:** container launch → entrypoint → migrate → gunicorn boots WSGI app → NR agent attaches → first request hits middleware → `mcp_endpoint`.

---

# Part 14 — Complete Request Walkthrough (Bearer `tools/call` over HTTP)

`POST /mcp` body `{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"fetch_published_posts","arguments":{"limit":5}}}`, header `Authorization: Bearer abc...`.

| # | File · Function | Input | Output | Next |
|---|-----------------|-------|--------|------|
| 1 | middleware chain | request | session loaded, request-id set, rate-limit checked (`/mcp` token bucket) | `mcp_endpoint` |
| 2 | `views/__init__.py :: mcp_endpoint` | request | identifies client, logs | `resolve_credentials` |
| 3 | `protocol/auth.py :: _resolve_oauth_token` | bearer | `OAuthToken.objects.get` → `{publisherId,apiKey,apiSecret}` | `http_mcp` |
| 4 | `views/http.py :: http_mcp` | creds | asserts `application/json` | `handle_http_request` |
| 5 | `transport/http.py :: handle_http_request` | request | `json.loads`, NR attrs, `derive_session_id` → `oauth-<hash>` | `dispatch_jsonrpc` |
| 6 | `protocol/dispatch.py :: dispatch_jsonrpc` | body | method=`tools/call` | `_handle_tool_call` |
| 7 | `_handle_tool_call` | params | prompt capture (`tool_args`), `_validate_tool_args` ok, not a CMS write | `dispatch_cds_tool` |
| 8 | `cds/__init__.py :: dispatch_cds_tool` | name, args | concurrency++, `fn_trace` | `fetch_published_posts` |
| 9 | `cds/posts.py :: fetch_published_posts` | creds, args | pops page/limit | `cds_get(/posts/)` |
| 10 | `clients/cds.py :: cds_get` | creds, path | Basic Auth `GET {CDS}/publisher/<id>/posts/?limit=5`, 5s timeout, ≤1 retry | parsed JSON |
| 11 | back in `_handle_tool_call` | result | classify success, record metrics/timeline | `jsonrpc_ok` content list |
| 12 | `handle_http_request` | response dict | `JsonResponse` | client gets `{result:{content:[{type:text,text:...}]}}` |

---

# Part 15 — Diagrams

### 15.1 High-level architecture
```
            ┌────────────────────────── Railway container ──────────────────────────┐
AI client → │ gunicorn (1 worker, 4 threads) → Django WSGI                            │
(Claude,    │   middleware: security│session│common│whitenoise│reqID│secHdr│rateLimit │
 Cursor,    │   ├─ auth_app  (OAuth PKCE + session login)  ── DB: oauth_*, sessions   │
 ChatGPT)   │   └─ mcp_app                                                            │
            │       views → transport(sse│http) → protocol.dispatch → cds/cms tools   │
            │                                          │                              │
            │                              clients(cds│cms) ──HTTP Basic Auth──┐      │
            └──────────────────────────────────────────────────────────────────┼─────┘
   New Relic (events/metrics/traces, no-op if absent)          Publive CDS/CMS APIs
```

### 15.2 Request flow
```
HTTP request → middleware → mcp_endpoint → resolve_credentials
   ├ GET  → open_sse_connection → StreamingHttpResponse (queue-drained)
   └ POST → handle_http_request → dispatch_jsonrpc
              ├ initialize / tools/list / ping
              └ tools/call → validate → dispatch_(cds|cms)_tool → handler → client(cds|cms) → API
```

### 15.3 OAuth flow
```
/register → client_id
/authorize(GET form) →(POST creds+PKCE)→ validate_cds_credentials → OAuthCode(code,challenge,10min) → redirect ?code
/token(code+verifier) → verify PKCE → upsert OAuthToken → {access_token, refresh_token}
Bearer access_token on /mcp → OAuthToken lookup → credentials
/token(refresh) → rotate refresh, same access   |   /revoke → delete
```

### 15.4 Session flow (browser)
```
/connect (form) → /auth/login(creds) → validate_cds_credentials → session["credentials"] + far-future expiry
/mcp (cookie) → _resolve_session → check_session_ttl → credentials   |   /auth/logout → session.flush()
```

### 15.5 "Redis" interaction — N/A
```
(removed)  rate limits → LocMemCache | sessions/oauth → DB | sse+stats → in-process dicts
```

### 15.6 SSE flow
```
GET /mcp → register_session(creds,queue) + init_stats → emit endpoint event → loop pop(25s){keepalive|message}
POST /mcp/message?sessionId → get_session → dispatch_jsonrpc → push_message(queue) → (stream drains it)
disconnect → _close_sse_session → MCPSessionSummary/Close
```

### 15.7 Streamable HTTP flow
```
POST /mcp (json|batch) → dispatch_jsonrpc per msg → JsonResponse(obj|array) | 202 if only notifications
```

---

# Part 16 — Call Stack Quick Reference

```
HTTP request
 → Django middleware (security, session, common, whitenoise, RequestID, SecurityHeaders, RateLimit)
 → mcp_app/views/__init__.py :: mcp_endpoint
   → protocol/auth.py :: resolve_credentials
        Bearer → _resolve_oauth_token → OAuthToken (DB)
        Cookie → _resolve_session → Django session (DB) + check_session_ttl
   → GET  → views/sse.py::sse_open → transport/sse.py::open_sse_connection
            → register_session / init_stats → StreamingHttpResponse(event_stream)
            (inbound) views/sse.py::sse_message → transport/sse.py::handle_sse_message
   → POST → views/http.py::http_mcp → transport/http.py::handle_http_request
   → protocol/dispatch.py :: dispatch_jsonrpc
        initialize | tools/list | ping | tools/call(_handle_tool_call) | -32601
   → _handle_tool_call
        extract_prompt_for_tool_call → _validate_tool_args → (CMS write-rate-limit)
        → cds/__init__.py::dispatch_cds_tool | cms/__init__.py::dispatch_cms_tool
        → _HANDLER_REGISTRY[name](credentials, args)
        → clients/cds.py::cds_get | clients/cms.py::cms_get/post/patch/delete  (Basic Auth → Publive API)
   → jsonrpc_ok({content:[{type:text,text:...}]})  (+ NR events/metrics throughout)
 → JsonResponse | StreamingHttpResponse → client
```

---

## Appendix — Notable discrepancies between docs and code (follow the code)
1. **Threads:** `CLAUDE.md` says `--threads 50`; `entrypoint.sh` uses `--threads 4`.
2. **Rate-limit cache:** memory note says `DatabaseCache`; `settings/base.py` uses `LocMemCache`.
3. **Redis:** older parts of the task brief assume Redis; it has been fully removed.
4. **Credential encryption:** removed intentionally — credentials are plain JSON.
