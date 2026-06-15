# Publive MCP Server - End-to-End Project Reference

This document explains the project from start to finish: how the server boots,
which endpoints exist, which function each endpoint enters, how authentication
works, how MCP JSON-RPC requests are dispatched, how tools call Publive APIs,
and where state is stored.

The project is a Django application that exposes Publive CDS and CMS APIs as MCP
tools for AI clients such as Claude Desktop, Cursor, ChatGPT-style MCP clients,
and other SDKs.

Current code inventory:

- CDS tools: 22 read-only tools
- CMS tools: 47 editorial/write/validation/account tools
- Total tool schemas in code: 69
- Main MCP protocol version returned by the server: `2024-11-05`

Note: older docs in the repository may mention 61 tools or in-memory SSE state.
The current code uses Redis for SSE sessions, SSE queues, session stats, prompt
rate limits, and Django cache-backed rate limits.

---

## 1. Project Structure

Top-level Django project:

```text
publive_mcp/
  urls.py                 Root URL include
  wsgi.py                 WSGI entrypoint wrapped with New Relic
  settings/
    base.py               Shared settings
    local.py              Local development overrides
    prod.py               Railway/production overrides

auth_app/
  urls.py                 OAuth and browser-session auth routes
  views.py                Auth endpoint handlers
  services.py             Auth helper logic
  models.py               OAuthClient, OAuthCode, OAuthToken
  templates/              Login, authorize, success pages
  static/auth/            Browser JS for auth pages

mcp_app/
  urls.py                 Health and MCP routes
  views/                  Thin HTTP routing layer
  protocol/               JSON-RPC dispatch, auth resolution, session helpers
  transport/              Streamable HTTP and SSE transport handlers
  cds/                    CDS read-only tools
  cms/                    CMS tools
  clients/                Publive CDS/CMS HTTP clients
  middleware.py           Rate limiting, request id, security headers
  redis_client.py         Shared raw redis-py client
  nr_utils.py             New Relic wrappers
  prompt_capture.py       Prompt extraction and observability
```

The server has two large responsibilities:

1. Authenticate a caller and obtain Publive credentials:
   `publisherId`, `apiKey`, `apiSecret`.
2. Accept MCP JSON-RPC requests and run the correct Publive tool using those
   credentials.

---

## 2. Boot Flow

### Local development (Redis runs as a separate process)

Locally you start Redis yourself, then run the dev server against it. The local
settings module does **not** enforce the production REDIS_URL guard, but the app
still needs a reachable Redis for the cache, SSE sessions, queues, and stats.

Commands:

```bash
redis-server                 # separate terminal, or: brew services start redis
python manage.py migrate
python manage.py runserver
```

Flow:

```text
redis-server
  -> listens on redis://127.0.0.1:6379/0 (the REDIS_URL default)

manage.py
  -> sets DJANGO_SETTINGS_MODULE to publive_mcp.settings
  -> publive_mcp/settings/__init__.py selects local or prod settings
     from RAILWAY_ENVIRONMENT or DJANGO_ENV
  -> django.core.management.execute_from_command_line()
  -> Django loads settings, URLs, middleware, apps
  -> development server starts and connects to the local Redis
```

Local notes:

- `DJANGO_ENV=local` (or unset) selects `publive_mcp/settings/local.py`, which
  sets `DEBUG = True` and `RATE_LIMIT_ENABLED = False`.
- `REDIS_URL` defaults to `redis://127.0.0.1:6379/0`, so a plain `redis-server`
  needs no extra configuration.
- With no `DATABASE_URL`, the database falls back to local SQLite.
- `docker compose up` is the alternative: it starts Postgres + Redis + the web
  server together so you do not run Redis by hand.

Important local commands:

```bash
python manage.py migrate
python manage.py makemigrations
python manage.py test
python manage.py collectstatic --noinput
```

### Production/Railway (Redis bundled inside the image)

In production there is no separate Redis service. The Docker image installs and
runs its own `redis-server`, started by `entrypoint.sh` before gunicorn, so the
deployed container is self-contained.

Docker build:

```text
Dockerfile
  -> python:3.12-slim
  -> apt-get install libpq-dev, gcc, redis-server
  -> install requirements
  -> copy source
  -> set DJANGO_SETTINGS_MODULE=publive_mcp.settings.prod
  -> collectstatic (with a build-only REDIS_URL placeholder so the prod guard
     does not trip; it is not an ENV, so it never reaches the running container)
  -> set CMD to /app/entrypoint.sh
```

Container start:

```text
entrypoint.sh
  -> redis-server --daemonize yes --save "" --appendonly no   (in-container Redis)
  -> export REDIS_URL=${REDIS_URL:-redis://127.0.0.1:6379/0}   (external value wins)
  -> python manage.py migrate --noinput
  -> python manage.py showmigrations auth_app
  -> gunicorn publive_mcp.wsgi -w 1 --threads 4 -b 0.0.0.0:${PORT:-8000}
```

WSGI:

```text
publive_mcp/wsgi.py
  -> get_wsgi_application()
  -> newrelic.agent.WSGIApplicationWrapper(...)
```

Production setting guard:

```text
publive_mcp/settings/prod.py
  -> DEBUG = False
  -> SESSION_COOKIE_SECURE = True
  -> refuses to boot if REDIS_URL is missing
```

The guard is satisfied at runtime because `entrypoint.sh` exports `REDIS_URL`
(pointing at the bundled Redis) before any Django command runs. You no longer
have to provision a Railway Redis instance for the container to boot.

Redis still backs the same state, now served from inside the container:

- SSE session registry
- Per-session SSE message queues
- Per-session MCP stats
- Prompt event rate-limit counters
- Django cache rate limits

Two consequences of in-container Redis:

- It is **ephemeral** (`--save "" --appendonly no`): SSE state and rate-limit
  counters reset on redeploy. Durable login sessions and OAuth tokens are in
  Postgres, so this is safe.
- It is **per-container**, not shared across replicas — so stay single-container
  / `-w 1`. To scale out, point `REDIS_URL` at an external shared Redis (that
  value wins and the bundled one is ignored).

---

## 3. URL Routing

Root router:

```text
publive_mcp/urls.py
  path("", include("auth_app.urls"))
  path("", include("mcp_app.urls"))
```

This means both `auth_app` and `mcp_app` mount at the site root.

### MCP routes

File: `mcp_app/urls.py`

```text
GET  /              -> health_check()
GET  /mcp           -> mcp_endpoint()
POST /mcp           -> mcp_endpoint()
POST /mcp/message   -> sse_message()
```

### Auth routes

File: `auth_app/urls.py`

```text
GET  /.well-known/oauth-protected-resource
GET  /.well-known/oauth-protected-resource/<path>
GET  /.well-known/oauth-authorization-server
GET  /.well-known/openid-configuration

POST /register
GET  /authorize
POST /authorize
GET  /oauth/authorize
POST /oauth/authorize
POST /token
POST /oauth/token
POST /revoke
POST /oauth/revoke
GET  /userinfo

GET  /connect
POST /auth/login
GET  /auth/success
GET  /auth/status
POST /auth/logout
```

---

## 4. Request Middleware

Every request passes through the middleware configured in
`publive_mcp/settings/base.py`.

Relevant custom middleware:

```text
mcp_app.middleware.RequestIDMiddleware
  -> reads X-Request-ID or creates a UUID
  -> stores request.request_id
  -> echoes X-Request-ID response header

mcp_app.middleware.SecurityHeadersMiddleware
  -> applies HTML-only security headers:
     Content-Security-Policy
     X-Frame-Options
     X-Content-Type-Options
     Referrer-Policy
     Permissions-Policy

mcp_app.middleware.RateLimitMiddleware
  -> applies sliding-window request limits using Django cache/Redis
```

`django.middleware.csrf.CsrfViewMiddleware` is not configured in
`publive_mcp/settings/base.py`, so CSRF token validation is not part of the
current request pipeline. The OAuth machine endpoints that call
`check_origin()` are `/register` and `/token`; other protection comes from
endpoint authentication, SameSite session cookies, and rate limits.

Rate-limit rules:

```text
POST /auth/login  -> 10/min per IP
POST /register    -> 20/min per IP
ANY  /authorize   -> 20/min per IP, path-prefix match on the root alias
POST /token       -> 20/min per IP, path-prefix match on the root alias
ANY  /mcp         -> 120/min per bearer token prefix, else IP
```

The rate limiter is prefix-based. The OAuth discovery metadata advertises the
root `/authorize`, `/token`, and `/revoke` endpoints; `/oauth/authorize`,
`/oauth/token`, and `/oauth/revoke` are compatibility aliases that enter the
same views. The current middleware rules match the root paths listed above; the
`/oauth/*` aliases do not match the `/authorize` or `/token` prefix rules unless
a future rule is added for those alias paths.

Local development disables rate limiting in `publive_mcp/settings/local.py`:

```text
RATE_LIMIT_ENABLED = False
```

---

## 5. Authentication Architecture

There are two authentication paths, but both end with the same credential shape:

```json
{
  "publisherId": "...",
  "apiKey": "...",
  "apiSecret": "..."
}
```

Those credentials are later used by CDS/CMS clients to call Publive with Basic
Auth.

### Auth path A: OAuth 2.0 + PKCE

Used by desktop/API clients.

Important functions:

```text
oauth_register()       auth_app/views.py
oauth_authorize()      auth_app/views.py
oauth_token()          auth_app/views.py
oauth_revoke()         auth_app/views.py
oauth_userinfo()       auth_app/views.py
validate_cds_credentials() auth_app/services.py
```

Database models:

```text
OAuthClient            auth_app/models.py
OAuthCode              auth_app/models.py
OAuthToken             auth_app/models.py
```

### Auth path B: browser session login

Used by users visiting `/connect`.

Important functions:

```text
connect()              auth_app/views.py
auth_login()           auth_app/views.py
auth_success()         auth_app/views.py
auth_status()          auth_app/views.py
auth_logout()          auth_app/views.py
set_session_credentials() auth_app/services.py
get_session_credentials() auth_app/services.py
check_session_ttl()    auth_app/services.py
```

Session credentials are stored in the Django session.

---

## 6. OAuth PKCE Flow

This is the flow used by MCP clients that want a bearer token.

### Step 1: OAuth metadata discovery

Request:

```text
GET /.well-known/oauth-authorization-server
```

Function:

```text
oauth_server_metadata()
```

Returns URLs for:

```text
issuer
authorization_endpoint
token_endpoint
revocation_endpoint
registration_endpoint
userinfo_endpoint
response_types_supported = ["code"]
grant_types_supported = ["authorization_code", "refresh_token"]
code_challenge_methods_supported = ["S256"]
token_endpoint_auth_methods_supported = ["none"]
revocation_endpoint_auth_methods_supported = ["none"]
scopes_supported = ["read", "write"]
```

Protected resource metadata:

```text
GET /.well-known/oauth-protected-resource
  -> oauth_protected_resource()
  -> returns resource: <BASE_URL>/mcp
```

### Step 2: Dynamic client registration

Request:

```text
POST /register
```

Function chain:

```text
oauth_register()
  -> check_origin()
  -> parse JSON body
  -> is_registrable_redirect_uri()
  -> OAuthClient.objects.create()
  -> return client_id
```

What it stores:

```text
oauth_client.client_id
oauth_client.redirect_uri
oauth_client.created_at
```

Redirect URI rules:

- `https://...` is allowed.
- `http://localhost:<port>/...` is allowed.
- `http://127.0.0.1:<port>/...` is allowed.
- `http://[::1]:<port>/...` is allowed.
- Plain HTTP to non-loopback hosts is rejected.

At authorization time, the requested `redirect_uri` must match the registered
URI exactly. Loopback redirect URIs are the exception: they may differ by port
as long as scheme, host, and path match, which is required for native apps that
bind an ephemeral local port.

### Step 3: Open authorize page

Request:

```text
GET /authorize?response_type=code&client_id=...&redirect_uri=...&code_challenge=...&code_challenge_method=S256&state=...
```

Function chain:

```text
oauth_authorize()
  -> _validate_authorize_request()
  -> OAuthClient.objects.get()
  -> redirect_uris_match()
  -> render authorize.html
```

If the client id is unknown, the server may auto-register it when the redirect
URI is acceptable. This helps desktop clients recover after DB wipes or cached
client IDs.

Only `response_type=code` is accepted. Implicit-style response types such as
`token` or `id_token token` are rejected.

### Step 4: User submits Publive credentials

Request:

```text
POST /authorize
```

Submitted form fields:

```text
publisherId
apiKey
apiSecret
client_id
redirect_uri
state
code_challenge
code_challenge_method
```

Function chain:

```text
oauth_authorize()
  -> _validate_authorize_request()
  -> validate_cds_credentials()
  -> OAuthCode.objects.create()
  -> redirect to redirect_uri?code=...&state=...
```

Credential validation:

```text
validate_cds_credentials()
  -> builds Basic Auth header from apiKey:apiSecret
  -> GET <CDS_BASE_URL>/posts/?limit=1
  -> returns true only for 2xx
```

The authorization code:

- Is random.
- Is single-use.
- Expires in 10 minutes.
- Stores the PKCE `code_challenge`.
- Temporarily stores Publive credentials until token exchange.

### Step 5: Exchange code for token

Request:

```text
POST /token
Content-Type: application/json
or application/x-www-form-urlencoded
```

Body:

```json
{
  "grant_type": "authorization_code",
  "code": "...",
  "code_verifier": "...",
  "client_id": "...",
  "redirect_uri": "..."
}
```

Function chain:

```text
oauth_token()
  -> parse_oauth_token_body()
  -> check_origin()
  -> reject implicit or unsupported grant types
  -> OAuthCode.objects.get()
  -> verify code expiry
  -> verify PKCE:
       expected = BASE64URL(SHA256(code_verifier))
       expected must equal stored code_challenge
  -> verify redirect_uri
  -> auth_code.delete()
  -> OAuthToken.objects.filter(client_id, publisher_id).first()
  -> reuse existing token or create new OAuthToken
  -> return access_token and refresh_token
```

The access token is long-lived until revoked or replaced. The code stores
`publisher_id` as a flat column and stores the remaining credentials JSON
without `publisherId`; `resolve_credentials()` merges it back later.

Token responses intentionally do not include `expires_in`, because OAuth access
tokens currently do not expire. `token_expires_at` is therefore always `None`
in MCP transport/session code today.

### Step 6: Refresh token

Request:

```text
POST /token
```

Body:

```json
{
  "grant_type": "refresh_token",
  "refresh_token": "..."
}
```

Function chain:

```text
oauth_token()
  -> transaction.atomic()
  -> OAuthToken.objects.select_for_update().get(refresh_token=...)
  -> keep existing access_token
  -> rotate refresh_token
  -> return same access_token and new refresh_token
```

Refresh token rotation uses a row lock so two simultaneous refresh requests do
not both succeed with the same old refresh token.

### Step 7: Revoke token

Request:

```text
POST /revoke
```

Function chain:

```text
oauth_revoke()
  -> parse_oauth_token_body()
  -> delete OAuthToken by access token or refresh token
  -> always return {}
```

It returns HTTP 200 even if the token did not exist, following OAuth revocation
behavior.

### Step 8: UserInfo

Request:

```text
GET /userinfo
Authorization: Bearer <token>
```

Function chain:

```text
oauth_userinfo()
  -> resolve_credentials()
  -> if unauthenticated, build_unauthorized_response()
  -> return sub and publisher_id
```

`/userinfo` also works with a valid browser session cookie. The `sub` claim is
the Publive `publisherId`, because this server delegates publisher-level
Publive API credentials rather than individual user identities.

---

## 7. Browser Session Login Flow

This flow is for users using the web auth UI.

### Step 1: Open connect page

Request:

```text
GET /connect
```

Function:

```text
connect()
  -> render connect.html
```

### Step 2: Submit login

Request:

```text
POST /auth/login
```

Body:

```json
{
  "publisherId": "...",
  "apiKey": "...",
  "apiSecret": "..."
}
```

Function chain:

```text
auth_login()
  -> parse JSON
  -> validate required fields
  -> validate_cds_credentials()
  -> set_session_credentials()
  -> set authenticatedAt
  -> set session_created_at
  -> set session_ttl_seconds = -1
  -> request.session.set_expiry(10 years)
  -> return { "success": true, "redirectTo": "/auth/success" }
```

`session_ttl_seconds = -1` means the session does not self-expire. It ends by
explicit logout or by server/session storage deletion.

### Step 3: Success page

Request:

```text
GET /auth/success
```

Function:

```text
auth_success()
  -> if no credentials, redirect /connect
  -> otherwise render success.html
```

### Step 4: Status check

Request:

```text
GET /auth/status
```

Function chain:

```text
auth_status()
  -> get_session_credentials()
  -> check_session_ttl()
  -> return authenticated state and publisherId
```

Authenticated response:

```json
{
  "authenticated": true,
  "publisherId": "...",
  "authenticatedAt": "...",
  "session_expires_in_seconds": null
}
```

`session_expires_in_seconds` is `null` for the current never-self-expiring
session policy. If a finite TTL session is ever configured and has expired,
`auth_status()` flushes the Django session and returns:

```json
{
  "authenticated": false,
  "error": "SESSION_EXPIRED"
}
```

### Step 5: Logout

Request:

```text
POST /auth/logout
```

Function:

```text
auth_logout()
  -> request.session.flush()
  -> return { "success": true }
```

---

## 8. Credential Resolution for MCP Requests

Every main MCP request enters:

```text
mcp_endpoint()
```

File:

```text
mcp_app/views/__init__.py
```

Function chain:

```text
mcp_endpoint()
  -> identify_mcp_client()
  -> resolve_credentials()
  -> if missing credentials:
       build_unauthorized_response()
  -> if GET:
       sse_open()
  -> if POST:
       http_mcp()
```

Credential resolver:

```text
resolve_credentials()
  -> if Authorization header starts with "Bearer ":
       _resolve_oauth_token()
     else:
       _resolve_session()
```

Bearer auth takes precedence over a Django session cookie. If a Bearer header is
present but invalid or unknown, credential resolution returns unauthenticated
instead of falling back to the session.

Bearer token path:

```text
_resolve_oauth_token()
  -> OAuthToken.objects.get(token=...)
  -> credentials = { **oauth_token.credentials, "publisherId": oauth_token.publisher_id }
  -> return credentials
```

Session cookie path:

```text
_resolve_session()
  -> get_session_credentials(request.session)
  -> check_session_ttl(request.session)
  -> if expired, flush session and return SESSION_EXPIRED
  -> return credentials
```

Unauthorized response:

```text
build_unauthorized_response()
  -> HTTP 401
  -> body includes authUrl: <BASE_URL>/connect
  -> body includes error "Not authenticated" or typed SESSION_EXPIRED details
  -> WWW-Authenticate header includes OAuth protected resource metadata URL
```

---

## 9. MCP Transport: Stateless HTTP

This is the simpler MCP transport.

Request:

```text
POST /mcp
Authorization: Bearer <token>
Content-Type: application/json
```

End-to-end chain:

```text
Django URL router
  -> mcp_endpoint()
  -> resolve_credentials()
  -> http_mcp()
  -> handle_http_request()
  -> json.loads(request.body)
  -> dispatch_jsonrpc()
  -> JsonResponse(response)
```

Functions:

```text
http_mcp()
  -> checks Content-Type contains application/json
  -> if not JSON, return HTTP 415 unsupported_media_type
  -> calls handle_http_request()

handle_http_request()
  -> derives session id:
       Django session key, or oauth-<sha256 token prefix>, or anon-<uuid>
  -> records request metrics
  -> parses JSON
  -> if invalid JSON, return HTTP 400
  -> if body is a list:
       dispatch each JSON-RPC message as a batch
     else:
       dispatch one JSON-RPC message
  -> notification with no id returns HTTP 202
  -> normal response returns JSON-RPC result
```

The HTTP transport resolves credentials on every request. It does not store
credentials in a transport session.

---

## 10. MCP Transport: SSE

SSE is the stateful/long-lived transport.

### Step 1: Open stream

Request:

```text
GET /mcp
Authorization: Bearer <token>
```

End-to-end chain:

```text
Django URL router
  -> mcp_endpoint()
  -> resolve_credentials()
  -> sse_open()
  -> open_sse_connection()
```

Inside `open_sse_connection()`:

```text
open_sse_connection()
  -> create session_id = uuid4()
  -> identify publisherId
  -> register_session(session_id, credentials, token_expires_at)
  -> enforce MCP_MAX_SSE_SESSIONS admission gate
  -> init_stats(session_id, session_trace_id)
  -> return StreamingHttpResponse(event_stream())
```

If the admission gate is full, the server rolls back the just-registered
session and returns HTTP 503 with `Retry-After: 30` and
`error=server_at_capacity`.

First SSE event sent to the client:

```text
event: endpoint
data: <BASE_URL>/mcp/message?sessionId=<session_id>
```

Then the stream waits for queued messages:

```text
event_stream()
  -> pop_message(session_id, timeout=25)
  -> if no message, yield keepalive
  -> if message exists, yield event: message
```

### Step 2: Client sends JSON-RPC message

Request:

```text
POST /mcp/message?sessionId=<session_id>
Content-Type: application/json
```

End-to-end chain:

```text
Django URL router
  -> sse_message()
  -> handle_sse_message()
  -> get_session(session_id)
  -> json.loads(request.body)
  -> dispatch_jsonrpc()
  -> push_message(session_id, response_msg)
  -> return { "ok": true }
```

The actual JSON-RPC response is not returned directly from `/mcp/message`.
Instead it is pushed into the Redis queue and delivered over the still-open SSE
stream.

`POST /mcp/message` does not run `resolve_credentials()` again. The opaque
`sessionId` from the initial authenticated `GET /mcp` is the lookup key for the
Redis session entry that contains credentials. Because this POST usually has no
Bearer token, the `/mcp` rate-limit rule falls back to the client IP for SSE
message posts.

### Step 3: Close stream

When the client disconnects:

```text
event_stream() finally block
  -> _close_sse_session()
  -> close_session(session_id)
  -> delete_queue(session_id)
  -> pop_stats(session_id)
  -> emit New Relic session summary events
```

### Redis objects used by SSE

```text
mcp:session:<session_id>
  -> credentials and token_expires_at

mcp:active_sessions
  -> Redis set of active session IDs

mcp:session_queue:<session_id>
  -> Redis list of pending JSON-RPC responses

mcp:session_stats:<session_id>
  -> Redis hash for counters and timings

mcp:session_stats:<session_id>:tool_sequence
  -> Redis list of tool names executed in the session
```

The Redis session, queue, and stats keys have a 24 hour safety-net TTL. Session
and stats TTLs refresh while the session is active; close cleanup still deletes
the keys immediately on normal disconnect.

---

## 11. JSON-RPC Dispatch

All MCP messages eventually enter:

```text
dispatch_jsonrpc()
```

File:

```text
mcp_app/protocol/dispatch.py
```

Input:

```python
dispatch_jsonrpc(body, credentials, request, session_id, token_expires_at)
```

Supported methods:

```text
initialize
tools/list
tools/call
ping
```

Known but intentionally unimplemented methods return JSON-RPC `-32601`:

```text
sampling/createMessage
roots/list
resources/list
resources/read
resources/subscribe
resources/unsubscribe
prompts/list
prompts/get
completion/complete
logging/setLevel
```

### initialize

Request:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize"
}
```

Function path:

```text
dispatch_jsonrpc()
  -> method == "initialize"
  -> save mcp_protocol_version in Django session when request exists
  -> return protocolVersion, capabilities, serverInfo
```

Response includes:

```json
{
  "protocolVersion": "2024-11-05",
  "capabilities": { "tools": {} },
  "serverInfo": { "name": "publive-cds", "version": "1.0.0" }
}
```

### tools/list

Function path:

```text
dispatch_jsonrpc()
  -> method == "tools/list"
  -> all_tools = TOOLS + CMS_TOOLS
  -> return schemas
```

`TOOLS` comes from `mcp_app.cds`.
`CMS_TOOLS` comes from `mcp_app.cms`.

### tools/call

Function path:

```text
dispatch_jsonrpc()
  -> method == "tools/call"
  -> _handle_tool_call()
```

Inside `_handle_tool_call()`:

```text
_handle_tool_call()
  -> read params.name
  -> extract prompt metadata with extract_prompt_for_tool_call()
  -> validate args against inputSchema
  -> record prompt observability or drop if rate-limited
  -> if CMS write, increment per-session write bucket
  -> dispatch to CMS or CDS:
       if name in CMS_TOOL_NAMES:
         dispatch_cms_tool(credentials, name, args)
       else:
         dispatch_cds_tool(credentials, name, args)
  -> record success/degraded/error metrics
  -> return JSON-RPC result content
```

Validation uses the tool's `inputSchema`:

```text
_validate_tool_args()
  -> checks required fields
  -> checks basic JSON types
  -> checks minLength for strings
  -> tolerates extra fields
```

CMS write rate limits:

```text
create/register/add/submit tools:
  -> create_op_count bucket
  -> 100 per SSE session

update/delete tools:
  -> update_delete_op_count bucket
  -> 100 per SSE session
```

For stateless HTTP, no Redis stats entry exists, so the per-session SSE write
counter no-ops.

---

## 12. Tool Registry and Dispatch

### CDS tools

File:

```text
mcp_app/cds/__init__.py
```

Aggregation:

```text
TOOLS =
  posts.SCHEMAS
  + categories.SCHEMAS
  + tags.SCHEMAS
  + authors.SCHEMAS
  + publisher.SCHEMAS
  + content.SCHEMAS
  + sitemaps.SCHEMAS
  + static_files.SCHEMAS

_HANDLER_REGISTRY =
  posts.HANDLERS
  + categories.HANDLERS
  + tags.HANDLERS
  + authors.HANDLERS
  + publisher.HANDLERS
  + content.HANDLERS
  + sitemaps.HANDLERS
  + static_files.HANDLERS
```

Dispatch:

```text
dispatch_cds_tool(credentials, name, args)
  -> find handler in _HANDLER_REGISTRY
  -> track active call concurrency
  -> execute handler(credentials, args)
  -> if CDS returns HTTP 401, return auth_expired structured error
```

CDS modules:

```text
mcp_app/cds/posts.py
mcp_app/cds/categories.py
mcp_app/cds/tags.py
mcp_app/cds/authors.py
mcp_app/cds/publisher.py
mcp_app/cds/content.py
mcp_app/cds/sitemaps.py
mcp_app/cds/static_files.py
```

Compatibility shim:

```text
mcp_app/tools.py
  -> re-exports TOOLS and dispatch_cds_tool
  -> call_tool is a backward-compatible alias
```

Example CDS tool:

```text
tools/call name: fetch_published_posts
  -> _handle_tool_call()
  -> dispatch_cds_tool()
  -> fetch_published_posts()
  -> cds_get(credentials, "/posts/", params)
  -> Publive CDS API
  -> return JSON to MCP client
```

### CMS tools

File:

```text
mcp_app/cms/__init__.py
```

Aggregation:

```text
CMS_TOOLS =
  categories.SCHEMAS
  + tags.SCHEMAS
  + posts.SCHEMAS
  + live_blog.SCHEMAS
  + custom_components.SCHEMAS
  + custom_content_types.SCHEMAS
  + validators.SCHEMAS
  + media.SCHEMAS
  + newsletter.SCHEMAS
  + reader.SCHEMAS

CMS_TOOL_NAMES = frozenset(_HANDLER_REGISTRY.keys())
```

Dispatch:

```text
dispatch_cms_tool(credentials, name, args)
  -> find handler in _HANDLER_REGISTRY
  -> track active call concurrency
  -> execute handler(credentials, args)
```

CMS modules:

```text
mcp_app/cms/categories.py
mcp_app/cms/tags.py
mcp_app/cms/posts.py
mcp_app/cms/live_blog.py
mcp_app/cms/custom_components.py
mcp_app/cms/custom_content_types.py
mcp_app/cms/validators.py
mcp_app/cms/media.py
mcp_app/cms/newsletter.py
mcp_app/cms/reader.py
```

Compatibility shim:

```text
mcp_app/cms_tools.py
  -> re-exports CMS_TOOLS and dispatch_cms_tool
  -> call_cms_tool is a backward-compatible alias
```

Example CMS read tool:

```text
tools/call name: list_editorial_posts
  -> _handle_tool_call()
  -> dispatch_cms_tool()
  -> list_editorial_posts()
  -> cms_get(credentials, "/post/", params)
  -> Publive CMS API
```

Example CMS write tool:

```text
tools/call name: create_post
  -> _handle_tool_call()
  -> validate required args
  -> dispatch_cms_tool()
  -> create_post()
  -> if dry_run=true:
       return preview only
     else:
       cms_post(credentials, "/post/", payload)
```

---

## 13. CMS Write Safety Model

CMS tools follow a safety model so AI clients do not accidentally mutate or
delete content.

General pattern:

```text
List/Get/Validate:
  -> execute immediately

Create:
  -> dry_run=true by default for most create tools
  -> returns preview
  -> dry_run=false commits

Update:
  -> dry_run=true by default
  -> fetches current object
  -> returns field diff
  -> dry_run=false applies patch

Delete:
  -> dry_run=true by default
  -> returns item preview and warning
  -> requires dry_run=false and confirm_delete=true to execute
```

Shared helper file:

```text
mcp_app/cms/helpers.py
```

Important helpers:

```text
preview_create_op()
preview_update_op()
preview_delete_op()
DELETION_REQUIRES_CONFIRMATION
validate_live_blog_post_type()
```

Post-specific behavior:

- Draft posts may be created or updated immediately.
- Publishing requires `confirm_publish=true` with `dry_run=false`.
- Video post creation is blocked because of an upstream CMS validator issue.
- Web Story and Gallery creation include extra validation guidance.
- Some fields are treated as immutable after creation.

---

## 14. Publive API Clients

### Shared client helpers

File:

```text
mcp_app/clients/shared.py
```

Important functions:

```text
build_pooled_session()
  -> creates requests.Session with connection pooling

build_base_url(template, credentials)
  -> requires credentials["publisherId"]
  -> formats CDS_BASE_URL or CMS_BASE_URL

build_basic_auth_headers(credentials)
  -> base64(apiKey:apiSecret)
  -> returns Authorization: Basic ...
```

Base URLs from settings:

```text
CDS_BASE_URL = https://cds-beta.thepublive.com/publisher/{publisher_id}
CMS_BASE_URL = https://cms-beta.thepublive.com/publisher/{publisher_id}
```

Both are environment-overridable.

Compatibility shims:

```text
mcp_app/cds_client.py
  -> re-exports mcp_app.clients.cds helpers

mcp_app/cms_client.py
  -> re-exports mcp_app.clients.cms helpers
```

### CDS client

File:

```text
mcp_app/clients/cds.py
```

Function:

```text
cds_get(credentials, path, params=None)
```

Flow:

```text
cds_get()
  -> validate publisherId exists
  -> build Basic Auth header
  -> build URL from CDS_BASE_URL + path
  -> clean empty params
  -> GET request with 5s timeout
  -> retry once on timeout or HTTP 408
  -> return resp.json()
```

CDS errors are raised except for tool-level structured fallbacks such as
`fetch_published_posts()` returning an `upstream_timeout` object.

### CMS client

File:

```text
mcp_app/clients/cms.py
```

Functions:

```text
cms_get()
cms_post()
cms_patch()
cms_delete()
```

Flow:

```text
cms_*()
  -> build URL from CMS_BASE_URL + path
  -> build Basic Auth headers
  -> send request with 10s timeout
  -> on success, return resp.json()
  -> on expected HTTP failure, return normalized error dict
  -> on timeout, return retryable timeout dict
```

CMS writes are not retried automatically because duplicate writes could create
or mutate content more than once.

Normalized CMS error example:

```json
{
  "error_type": "bad_request",
  "message": "...",
  "retryable": false
}
```

---

## 15. Database Model Deep Dive

File:

```text
auth_app/models.py
```

### OAuthClient

Table:

```text
oauth_client
```

Fields:

```text
client_id       unique client identifier
redirect_uri    registered callback URI
created_at      creation timestamp
```

Created by:

```text
oauth_register()
```

Also auto-created by:

```text
_validate_authorize_request()
```

when an unknown client id provides an acceptable redirect URI.

### OAuthCode

Table:

```text
oauth_code
```

Fields:

```text
code             single-use authorization code
client_id        client requesting auth
redirect_uri     callback URI
code_challenge   PKCE challenge
credentials      temporary Publive credentials
expires_at       10 minute expiry
```

Created by:

```text
oauth_authorize()
```

Deleted by:

```text
oauth_token()
```

after successful token exchange or expiry detection.

### OAuthToken

Table:

```text
oauth_token
```

Fields:

```text
token            bearer access token
client_id        OAuth client id
publisher_id     Publive publisher id
refresh_token    rotating refresh token
credentials      JSON with apiKey/apiSecret
created_at       creation timestamp
```

Created/reused by:

```text
oauth_token()
```

Read by:

```text
_resolve_oauth_token()
```

Deleted by:

```text
oauth_revoke()
```

### Django sessions

Used by browser login.

Configured in:

```text
publive_mcp/settings/base.py
```

```text
SESSION_ENGINE = django.contrib.sessions.backends.db
SESSION_COOKIE_AGE = 10 * 356 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = Lax
SESSION_SAVE_EVERY_REQUEST = True
```

Stores:

```text
credentials
authenticatedAt
session_created_at
session_ttl_seconds
```

---

## 16. Redis State Deep Dive

Raw Redis client:

```text
mcp_app/redis_client.py
  -> get_redis_client()
  -> redis.Redis.from_url(REDIS_URL, decode_responses=True)
```

### SSE session store

File:

```text
mcp_app/transport/redis_session_store.py
```

Functions:

```text
register_session()
get_session()
close_session()
```

Purpose:

```text
Allows GET /mcp and POST /mcp/message for the same session to land on different
gunicorn threads, workers, or replicas and still find the session credentials.
```

### SSE message queue

File:

```text
mcp_app/transport/redis_message_queue.py
```

Functions:

```text
push_message()
pop_message()
delete_queue()
queue_depth()
```

Purpose:

```text
POST /mcp/message pushes a JSON-RPC response into Redis.
The open GET /mcp SSE stream blocks on BLPOP and sends that response to the client.
```

### Session stats

File:

```text
mcp_app/protocol/redis_session_stats.py
```

Functions:

```text
init_stats()
increment()
set_field()
append_tool_sequence()
get_timeline_and_set_client_name()
get_field()
pop_stats()
```

Purpose:

```text
Tracks tool counts, error counts, degraded counts, duration, estimated tokens,
client name, trace id, and per-session write buckets.
```

### Prompt event rate limit

File:

```text
mcp_app/protocol/session.py
```

Function:

```text
should_emit_prompt_event()
  -> Redis key mcp:prompt_events:<minute_bucket>
  -> max 1000 prompt events per minute cluster-wide
```

---

## 17. Observability

New Relic calls are wrapped so the app still runs when New Relic is absent.

File:

```text
mcp_app/nr_utils.py
```

Common wrappers:

```text
set_txn_name()
add_attrs()
notice_err()
record_event()
record_metric()
get_linking_metadata()
suppress_apdex()
suppress_trace()
fn_trace()
```

Custom events include:

```text
MCPPrompt
MCPToolError
MCPToolDegraded
MCPUnknownMethod
SSESessionOpen
SSESessionClose
MCPSessionAbandoned
MCPSessionMissing
MCPSessionSummary
```

Prompt capture:

```text
mcp_app/prompt_capture.py
  -> extract_prompt_for_tool_call()
  -> record_prompt_observability()
```

Prompt text may come from:

- HTTP headers
- JSON-RPC `_meta`
- Tool arguments

The `_prompt` key is stripped before the tool handler runs.

---

## 18. Full End-to-End Traces

### Trace A: AI client registers and calls a read tool over HTTP

```text
Client
  -> POST /register
  -> oauth_register()
  -> check_origin()
  -> is_registrable_redirect_uri()
  -> OAuthClient.objects.create()
  <- client_id

Client
  -> GET /authorize?...code_challenge...
  -> oauth_authorize()
  -> _validate_authorize_request()
  -> render authorize.html

User submits Publive credentials
  -> POST /authorize
  -> oauth_authorize()
  -> validate_cds_credentials()
  -> GET CDS /posts/?limit=1
  -> OAuthCode.objects.create()
  <- 302 redirect_uri?code=...

Client
  -> POST /token
  -> oauth_token()
  -> parse_oauth_token_body()
  -> verify PKCE
  -> delete OAuthCode
  -> create/reuse OAuthToken
  <- access_token

Client
  -> POST /mcp Authorization: Bearer <token>
  -> mcp_endpoint()
  -> resolve_credentials()
  -> _resolve_oauth_token()
  -> http_mcp()
  -> handle_http_request()
  -> dispatch_jsonrpc()
  -> method tools/call
  -> _handle_tool_call()
  -> dispatch_cds_tool()
  -> fetch_published_posts()
  -> cds_get()
  -> GET Publive CDS /posts/
  <- JSON-RPC response
```

### Trace B: Browser session calls a CMS write tool over HTTP

```text
Browser
  -> GET /connect
  -> connect()
  <- connect.html

Browser
  -> POST /auth/login
  -> auth_login()
  -> validate_cds_credentials()
  -> set_session_credentials()
  <- success

Browser or MCP client with session cookie
  -> POST /mcp
  -> mcp_endpoint()
  -> resolve_credentials()
  -> _resolve_session()
  -> http_mcp()
  -> handle_http_request()
  -> dispatch_jsonrpc()
  -> _handle_tool_call()
  -> dispatch_cms_tool()
  -> create_post()
  -> dry_run=true, return preview
```

If the user confirms:

```text
Client
  -> POST /mcp tools/call create_post dry_run=false
  -> create_post()
  -> cms_post()
  -> POST Publive CMS /post/
  <- created post response
```

### Trace C: SSE client opens session and calls a tool

```text
Client
  -> GET /mcp Authorization: Bearer <token>
  -> mcp_endpoint()
  -> resolve_credentials()
  -> sse_open()
  -> open_sse_connection()
  -> register_session() in Redis
  -> init_stats() in Redis
  <- SSE event: endpoint /mcp/message?sessionId=<uuid>

Client
  -> POST /mcp/message?sessionId=<uuid>
  -> sse_message()
  -> handle_sse_message()
  -> get_session() from Redis
  -> dispatch_jsonrpc()
  -> _handle_tool_call()
  -> dispatch_cds_tool() or dispatch_cms_tool()
  -> push_message() into Redis queue
  <- { "ok": true }

Open SSE stream
  -> pop_message() from Redis queue
  <- event: message with JSON-RPC response

Client disconnects
  -> _close_sse_session()
  -> close_session()
  -> delete_queue()
  -> pop_stats()
  -> emit session summary events
```

### Trace D: Unknown method

```text
Client
  -> POST /mcp { "method": "resources/list", "id": 1 }
  -> mcp_endpoint()
  -> handle_http_request()
  -> dispatch_jsonrpc()
  -> method in _UNIMPLEMENTED_METHODS
  <- JSON-RPC error -32601
```

### Trace E: Invalid tool arguments

```text
Client
  -> tools/call fetch_published_post with no identifier
  -> dispatch_jsonrpc()
  -> _handle_tool_call()
  -> _validate_tool_args()
  -> required field missing
  <- JSON-RPC result with isError=true and invalid_params payload
```

---

## 19. Important Settings and Environment Variables

Required:

```text
DJANGO_SECRET_KEY
```

Production:

```text
REDIS_URL   prod.py refuses to boot without it, but entrypoint.sh sets it to the
            bundled in-container Redis by default. Only set it yourself to point
            at an external/shared Redis (e.g. when running multiple replicas).
```

Common:

```text
BASE_URL
DATABASE_URL
CDS_BASE_URL
CMS_BASE_URL
MCP_QUEUE_MAXSIZE
MCP_MAX_SSE_SESSIONS
NEW_RELIC_LICENSE_KEY
NEW_RELIC_APP_NAME
NEW_RELIC_USER_KEY
SERVER_VERSION
```

Defaults:

```text
BASE_URL=http://localhost:8000
CDS_BASE_URL=https://cds-beta.thepublive.com/publisher/{publisher_id}
CMS_BASE_URL=https://cms-beta.thepublive.com/publisher/{publisher_id}
REDIS_URL=redis://127.0.0.1:6379/0
MCP_QUEUE_MAXSIZE=100
MCP_MAX_SSE_SESSIONS=2
```

---

## 20. Security Summary

Security controls in this project:

```text
PKCE S256
  -> protects OAuth code exchange for native clients

CDS credential validation before token/session issue
  -> validates publisherId/apiKey/apiSecret before storing them

Redirect URI validation
  -> only HTTPS or loopback HTTP

Origin allowlist
  -> check_origin() currently runs on /register and /token

Authorization code TTL
  -> 10 minute single-use OAuth codes

Refresh token rotation
  -> row-locked transaction with select_for_update()

Rate limiting
  -> auth endpoints per IP, MCP per token prefix or IP

Security headers
  -> applied to HTML responses

Redis-backed SSE session admission gate
  -> avoids too many long-lived streams occupying gunicorn threads

SSE sessionId lookup
  -> /mcp/message uses the Redis session created by authenticated GET /mcp

CMS write dry-run and confirmation rules
  -> protects create/update/delete workflows
```

Credential storage notes:

- OAuth tokens are stored in the database.
- OAuth token credentials are stored as JSON.
- Browser session credentials are stored in Django sessions.
- SSE credentials are stored in Redis for the lifetime of the SSE session.
- Treat SSE `sessionId` values as bearer-like secrets for that stream lifetime.
- Redis and database access must be network-protected in production.

---

## 21. Quick Function Map

### Entry points

```text
health_check()             GET /
mcp_endpoint()             GET/POST /mcp
sse_message()              POST /mcp/message
oauth_register()           POST /register
oauth_authorize()          GET/POST /authorize, /oauth/authorize
oauth_token()              POST /token, /oauth/token
oauth_revoke()             POST /revoke, /oauth/revoke
oauth_userinfo()           GET /userinfo
connect()                  GET /connect
auth_login()               POST /auth/login
auth_status()              GET /auth/status
auth_logout()              POST /auth/logout
```

### MCP core

```text
resolve_credentials()
identify_mcp_client()
build_unauthorized_response()
handle_http_request()
open_sse_connection()
handle_sse_message()
dispatch_jsonrpc()
_handle_tool_call()
_validate_tool_args()
dispatch_cds_tool()
dispatch_cms_tool()
```

### Publive API clients

```text
cds_get()
cms_get()
cms_post()
cms_patch()
cms_delete()
build_base_url()
build_basic_auth_headers()
```

### Redis helpers

```text
get_redis_client()
register_session()
get_session()
close_session()
push_message()
pop_message()
delete_queue()
queue_depth()
init_stats()
increment()
set_field()
append_tool_sequence()
pop_stats()
```

---

## 22. Mental Model

Think of the project as a pipeline:

```text
HTTP request
  -> Django middleware
  -> URL router
  -> auth or MCP view
  -> credential resolution
  -> transport handler
  -> JSON-RPC dispatcher
  -> tool registry
  -> tool handler
  -> CDS/CMS HTTP client
  -> Publive API
  -> normalized response
  -> JSON-RPC response
  -> HTTP response or SSE message
```

The most important code path for MCP is:

```text
/mcp
  -> mcp_endpoint()
  -> resolve_credentials()
  -> http_mcp() or sse_open()
  -> handle_http_request() or open_sse_connection()/handle_sse_message()
  -> dispatch_jsonrpc()
  -> _handle_tool_call()
  -> dispatch_cds_tool() or dispatch_cms_tool()
  -> actual handler in mcp_app/cds/* or mcp_app/cms/*
  -> cds_get() or cms_get/post/patch/delete()
  -> Publive API
```

That is the end-to-end flow from endpoint to function to downstream API.
