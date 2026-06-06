# Low-Level Design: Publive MCP Server

This document covers every module's function signatures, data contracts, algorithms, and internal state. Read `docs/hld.md` first for the system context.

---

## File map

```
publive_mcp/
  settings/
    base.py          — shared settings, env var defaults
    prod.py          — production overrides (Postgres, Whitenoise, NR)
    dev.py           — local overrides (SQLite, LocMemCache, DEBUG=True)
  wsgi.py            — WSGI application, New Relic WSGI wrapper
  urls.py            — root URL conf

auth_app/
  models.py          — OAuthClient, OAuthCode, OAuthToken (data models)
  fields.py          — EncryptedJSONField (transparent Fernet encryption)
  crypto.py          — get_fernet(), encrypt_json(), decrypt_json()
  services.py        — session helpers, origin check, PKCE body parser, CDS validator
  views.py           — /register, /authorize, /token, /revoke, /connect, /auth/login
  migrations/        — 0001–0011 (0011 drops orphan encrypted columns)

mcp_app/
  views.py           — entry points: mcp_endpoint(), message_endpoint(), healthcheck()
  middleware.py      — RateLimitMiddleware, RequestIDMiddleware, SecurityHeadersMiddleware
  nr_utils.py        — guarded New Relic helpers (no-ops when agent absent)
  prompt_capture.py  — extract_prompt_for_tool_call(), record_prompt_observability()

  protocol/
    auth.py          — resolve_credentials(), build_unauthorized_response(), identify_mcp_client()
    dispatch.py      — dispatch_jsonrpc(), _handle_tool_call(), _validate_tool_args()
    session.py       — derive_session_id(), should_emit_prompt_event()
    session_store.py — session_stats dict + lock (shared state, exists to break circular import)

  transport/
    sse.py           — open_sse_connection(), handle_sse_message()
    http.py          — handle_http_request()

  clients/
    shared.py        — build_base_url(), build_basic_auth_headers(), slugify_url_path()
    cds.py           — cds_get()
    cms.py           — cms_get(), cms_post(), cms_patch(), cms_delete()

  cds/
    __init__.py      — TOOLS list, _HANDLER_REGISTRY, dispatch_cds_tool()
    posts.py         — SCHEMAS + HANDLERS for post tools
    categories.py    — SCHEMAS + HANDLERS for category tools
    (other modules)

  cms/
    __init__.py      — CMS_TOOLS, CMS_TOOL_NAMES frozenset, dispatch_cms_tool()
    posts.py         — SCHEMAS + HANDLERS for CMS post write tools
    categories.py    — SCHEMAS + HANDLERS for CMS category write tools
    helpers.py       — preview_create_op(), preview_update_op(), preview_delete_op(),
                       validate_live_blog_post_type(), DELETION_REQUIRES_CONFIRMATION
    (other modules)
```

---

## Middleware pipeline

Every request passes through this chain in `settings.MIDDLEWARE` order:

```
1. SecurityMiddleware            (django.middleware.security)
2. SessionMiddleware             (django.contrib.sessions.middleware)
3. RequestIDMiddleware           (mcp_app.middleware)   — attaches X-Request-ID
4. RateLimitMiddleware           (mcp_app.middleware)   — sliding-window rate limit
5. CommonMiddleware              (django.middleware.common)
6. MessageMiddleware             (django.contrib.messages.middleware)
7. SecurityHeadersMiddleware     (mcp_app.middleware)   — CSP/X-Frame on HTML only
```

MCP endpoints are `@csrf_exempt` so `CsrfViewMiddleware` is absent from the production stack.

---

## `mcp_app/middleware.py`

### `RateLimitMiddleware`

**Algorithm:** fixed-window slot (not true sliding window).

```python
slot      = int(time.time()) // window           # e.g. current 60s bucket
cache_key = f"rl:{prefix}:{ident}:{slot}"       # e.g. "rl:/auth/login:ip:127.0.0.1:28012345"
count     = cache.get(cache_key, 0)
if count >= limit:
    return 429
cache.set(cache_key, count + 1, timeout=window * 2)
```

`timeout = window * 2` — the key outlives the slot so the counter isn't deleted while the window is still active.

**Rules table:**

| Prefix | Method | Limit | Window | Key strategy |
|---|---|---|---|---|
| `/auth/login` | POST | 10 | 60s | IP |
| `/register` | POST | 20 | 60s | IP |
| `/authorize` | any | 20 | 60s | IP |
| `/token` | POST | 20 | 60s | IP |
| `/mcp` | any | 300 | 60s | Bearer prefix (first 12 chars) |

Only the **first matching rule** applies (inner `break`). IP is extracted from `X-Forwarded-For` (first entry) or `REMOTE_ADDR`. Cache failures **fail open** — a cache outage never blocks traffic.

Disable for testing: `RATE_LIMIT_ENABLED=False` in settings.

### `RequestIDMiddleware`

```python
request_id = request.META.get("HTTP_X_REQUEST_ID") or str(uuid.uuid4())
request.request_id = request_id          # available to views
response["X-Request-ID"] = request_id   # echoed back for log correlation
```

### `SecurityHeadersMiddleware`

Applied only when `Content-Type` contains `text/html`. Sets:
- `Content-Security-Policy` — `default-src 'self'`, no inline JS, no external fonts
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy` — disables geolocation, camera, microphone

---

## `auth_app/crypto.py`

### Module state

```python
_fernet: Optional[Fernet] = None  # singleton, initialised once
```

### `get_fernet() → Fernet`

1. If `_fernet` is not None, return it.
2. Read `CREDENTIALS_ENCRYPTION_KEY` from env.
3. If absent: `Fernet.generate_key()` → log WARNING → ephemeral key (unreadable after restart).
4. If present: `Fernet(key.encode())`.
5. Store in `_fernet`, return.

### `encrypt_json(data: dict) → str`

```
data → json.dumps(separators=(",",":")) → bytes → Fernet.encrypt() → .decode() → str
```

Output is a URL-safe base64 Fernet token.

### `decrypt_json(token: str) → dict`

```
token → .encode() → Fernet.decrypt() → bytes → json.loads() → dict
```

Raises `cryptography.fernet.InvalidToken` if the key is wrong or the token is malformed.

---

## `auth_app/fields.py` — `EncryptedJSONField`

Extends `models.TextField`. Three overrides:

| Hook | Direction | Logic |
|---|---|---|
| `from_db_value(value, ...)` | DB → Python | `decrypt_json(value)` |
| `to_python(value)` | string → Python | `decrypt_json(value)`; falls back to `json.loads()` for legacy plaintext (migration window) |
| `get_prep_value(value)` | Python → DB | `encrypt_json(value)` if dict; otherwise `super()` |

Python callers always assign/read plain `dict`s. Encryption is transparent.

---

## `auth_app/models.py`

### `OAuthClient`

```
Table:  oauth_client
Column      Type          Constraints
client_id   VARCHAR(64)   UNIQUE, INDEX
redirect_uri VARCHAR(512) blank allowed
created_at  TIMESTAMPTZ   auto_now_add
```

One row per AI client install. Created by `POST /register`. Never deleted automatically.

### `OAuthCode`

```
Table:  oauth_code
Column          Type          Constraints
code            VARCHAR(128)  UNIQUE
client_id       VARCHAR(64)   INDEX
redirect_uri    TEXT
code_challenge  VARCHAR(256)
credentials     TEXT          Fernet-encrypted JSON
expires_at      TIMESTAMPTZ
```

Single-use. 10-minute TTL. Deleted atomically on redemption at `POST /token`.

### `OAuthToken`

```
Table:  oauth_token
Column         Type          Constraints
token          VARCHAR(128)  UNIQUE
client_id      VARCHAR(64)   INDEX, blank allowed
publisher_id   VARCHAR(64)   INDEX, blank allowed
refresh_token  VARCHAR(128)  UNIQUE, null allowed
credentials    TEXT          Fernet-encrypted JSON
created_at     TIMESTAMPTZ   auto_now_add, null allowed
```

`publisher_id` is a plain indexed column so token lookup by `(client_id, publisher_id)` works without decrypting every row. Upsert pattern: `update_or_create(client_id=..., publisher_id=...)` — re-authorisation does not break in-flight sessions.

`refresh_token` rotates on every use inside an atomic DB transaction. Stolen refresh tokens are single-use.

---

## `auth_app/services.py`

### `get_session_credentials(session) → dict | None`

```python
return session.get("credentials")  # set by set_session_credentials at login
```

### `set_session_credentials(session, credentials: dict, remember_for_days: int)`

```python
session["credentials"] = credentials        # stored as plain dict (session backend encrypts at rest)
session["session_created_at"] = int(time.time())
if remember_for_days > 0:
    session["session_ttl_seconds"] = remember_for_days * 86400
    session.set_expiry(remember_for_days * 86400)
elif remember_for_days == -1:               # browser-session only
    session.set_expiry(0)
# remember_for_days == 0 → Django controls (SESSION_COOKIE_AGE = 90d)
```

### `check_session_ttl(session) → bool`

Returns `True` (expired) when:
1. `session_ttl_seconds > 0` (absolute TTL was set), AND
2. `time.time() > session["session_created_at"] + session_ttl_seconds`

Returns `False` (not expired) for `ttl_seconds <= 0` (browser-session or Django-controlled).

### `check_origin(request) → JsonResponse | None`

Returns `None` (allowed) when:
- No `Origin` header present — desktop MCP clients never send one.
- `Origin` is in `settings.OAUTH_ALLOWED_ORIGINS`.

Returns `403 JsonResponse` otherwise.

`OAUTH_ALLOWED_ORIGINS` defaults to `[settings.BASE_URL]`. Extend via env var.

### `parse_oauth_token_body(request) → dict`

Reads `Content-Type`:
- `application/json` → `json.loads(request.body)`
- `application/x-www-form-urlencoded` → `request.POST`
- Other → `request.POST` fallback

Returns a plain dict safe for key access.

### `validate_cds_credentials(publisher_id, api_key, api_secret) → (bool, int)`

```python
token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
base  = settings.CDS_BASE_URL.format(publisher_id=publisher_id)
resp  = requests.get(f"{base}/posts/", params={"limit": 1},
                     headers={"Authorization": f"Basic {token}"}, timeout=10)
return 200 <= resp.status_code < 300, resp.status_code
```

Returns `(True, 200)` on success. Returns `(False, status_code)` on any non-2xx.  
Called once at login/authorize — not on every tool call.

---

## `mcp_app/protocol/auth.py`

### `resolve_credentials(request) → (credentials | None, None, error_code | None)`

```
Authorization: Bearer <token>  →  _resolve_oauth_token(token)
(no Bearer header)             →  _resolve_session(request)
```

Second element is always `None` (tokens have no expiry tracked here).

### `_resolve_oauth_token(token_value) → (dict | None, None, None)`

`OAuthToken.objects.get(token=token_value)` → returns `oauth_token.credentials` (dict, auto-decrypted by `EncryptedJSONField`). Returns `(None, None, None)` on `DoesNotExist`. Re-raises any other exception.

### `_resolve_session(request) → (dict | None, None, error_code | None)`

1. `get_session_credentials(request.session)` — if None, return `(None, None, None)`.
2. `check_session_ttl(request.session)` — if expired, flush session, return `(None, None, SESSION_EXPIRED)`.
3. Return `(credentials, None, None)`.

### `build_unauthorized_response(request, error_code) → JsonResponse (401)`

Response body:
```json
{
  "authUrl": "{BASE_URL}/connect",
  "error": "{error_code | 'Not authenticated'}",
  "error_description": "{human-readable description}"
}
```

`WWW-Authenticate` header (RFC 6750):
```
Bearer realm="{BASE_URL}", resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"
```

MCP clients parse this header to auto-start the OAuth flow.

### `identify_mcp_client(request) → (client_name: str, client_version: str)`

Regex: `^([^\s/]+)/([^\s]+)` on `HTTP_USER_AGENT`. Prefix → name lookup via `_CLIENT_NAME_MAP`:

| Prefix | Name |
|---|---|
| `claude` | Claude Desktop |
| `cursor` | Cursor |
| `anthropic` | Anthropic SDK |
| `python-requests` | Python Requests Client |
| `python-httpx` | Python HTTPX Client |
| `mcp` | MCP Python SDK |
| `node` | Node.js MCP Client |
| `go-http-client` | Go MCP Client |
| `axios` | Axios (JS) |
| `openai` | OpenAI SDK |

---

## `mcp_app/protocol/session.py`

### `derive_session_id(request) → str`

Priority:
1. `request.session.session_key` — browser/session-cookie clients.
2. SHA-256 prefix of Bearer token: `"oauth-" + sha256(token)[:16]` — stable across requests.
3. `"anon-" + uuid4().hex[:8]` — unauthenticated probes.

### `should_emit_prompt_event() → bool`

Sliding-window rate limit for `MCPPrompt` NR events:

```python
# module-level state
_PROMPT_EVENT_MAX_PER_MIN  = 1000
_prompt_event_count        = 0
_prompt_event_window_start = time.monotonic()
```

On each call (thread-safe via `_prompt_event_lock`):
1. If `now - window_start >= 60.0`: reset count to 0, reset window_start.
2. If `count < 1000`: increment count, return `True`.
3. Else return `False`.

---

## `mcp_app/protocol/session_store.py`

Exists solely to break a circular import between `transport/sse.py` and `protocol/dispatch.py` — both need `session_stats`.

```python
session_stats: dict = {}                   # session_id → stats dict (schema below)
session_stats_lock: threading.Lock = ...
```

**`session_stats[session_id]` schema:**

| Key | Type | Purpose |
|---|---|---|
| `tool_count` | int | Total successful tool calls this session |
| `error_count` | int | Tool calls that raised an exception |
| `degraded_count` | int | Tool calls that returned an error_type |
| `session_start_time` | float | `time.perf_counter()` at session open |
| `total_tool_duration_ms` | float | Cumulative tool execution time |
| `total_estimated_input_tokens` | int | Sum of prompt token estimates |
| `total_estimated_output_tokens` | int | Sum of output token estimates |
| `last_tool_end_perf` | float \| None | `perf_counter()` when the last tool finished |
| `client_name` | str \| None | From `identify_mcp_client()`, set on first tool call |
| `session_trace_id` | str | NR trace.id from the SSE open transaction |
| `tool_sequence` | list[str] | Ordered list of tool names called |
| `write_op_count` | int | CMS mutation count (capped at 50 per session) |

---

## `mcp_app/protocol/dispatch.py`

### Module-level precomputation

```python
# Built at import time — O(1) per-tool schema lookup, not O(n) scan per request
_SCHEMA_REGISTRY: dict = {
    tool["name"]: tool.get("inputSchema", {})
    for tool in (TOOLS + CMS_TOOLS)
}
```

### `dispatch_jsonrpc(request, body, credentials, session_id, token_expires_at) → dict | None`

Routes on `body["method"]`:

| Method | Handler | Returns |
|---|---|---|
| `initialize` | Inline | `{protocolVersion, capabilities, serverInfo}` |
| `tools/list` | Inline | `{tools: TOOLS + CMS_TOOLS}` |
| `tools/call` | `_handle_tool_call()` | MCP content list |
| `ping` | Inline | `{}` |
| Known-unimplemented† | Inline | `-32601 Method not found` (no log warning) |
| Unknown | Inline | `-32601`, logs WARNING, emits `MCPUnknownMethod` |
| Notification (no `id`) | — | `None` |

† Known-unimplemented: `sampling/createMessage`, `roots/list`, `resources/*`, `prompts/*`, `completion/complete`, `logging/setLevel`.

### `_handle_tool_call(request, params, credentials, session_id, body) → dict`

**Full pipeline (8 steps):**

```
1. name  = params["name"]
   args  = params.get("arguments", {})
   route = "cms" if name in CMS_TOOL_NAMES else "cds"

2. prompt_id, prompt_text, prompt_source, args =
       extract_prompt_for_tool_call(request, body, params)
   # args may have _prompt/prompt stripped

3. if should_emit_prompt_event():
       record_prompt_observability(...)   # MCPPrompt NR event

4. validation_error = _validate_tool_args(name, args)
   if validation_error:
       record_metric("Custom/MCP/tool_validation_error_count", 1)
       return {"content": [{"type":"text","text": error_msg}], "isError": True}

5. if route == "cms" and is_write_op(name):
       with session_stats_lock:
           if session_stats[session_id]["write_op_count"] >= 50:
               return rate_limit_error_response

6. t0 = time.perf_counter()
   result = dispatch_cds_tool(name, credentials, args)
            or dispatch_cms_tool(name, credentials, args)
   duration_ms = (time.perf_counter() - t0) * 1000

7. if "error_type" in result or "error" in result:
       # Degraded — tool ran, upstream rejected it
       record_metric("Custom/MCP/tool_degraded_count", 1)
       record_event("MCPToolDegraded", {...})
       return {"content": [{"type":"text","text": format_error(result)}], "isError": False}

8. # Success
   update session_stats (tool_count, duration, sequence, tokens)
   record metrics (call_count, success_count, duration_ms)
   return {"content": [{"type":"text","text": json.dumps(result)}], "isError": False}

   # Exception path (wraps steps 6–8):
   record_event("MCPToolError", {...})
   return {"content": [...], "isError": True}
```

### `_validate_tool_args(name: str, args: dict) → dict | None`

1. Fetch `schema = _SCHEMA_REGISTRY.get(name)`. If absent, return `None` (unknown tool — error surfaces elsewhere).
2. **Required field check:** For each field in `schema["required"]`: if missing, None, or `""` → return `{error_type: "invalid_params", ...}`.
3. **Type check:** For each provided field:
   - Skip `None` values.
   - Skip fields not in `schema["properties"]` (extra fields tolerated).
   - `expected_type == "integer"` and `isinstance(value, bool)` → reject (Python `bool` is `int` subclass).
   - `not isinstance(value, _JSON_TYPE_MAP[expected_type])` → reject.
4. **minLength check:** For `string` fields with `minLength` in schema.
5. Return `None` if all checks pass.

---

## `mcp_app/transport/sse.py`

### In-process state

```python
_sse_sessions: dict[str, tuple[queue.Queue, dict, object]] = {}
# session_id → (msg_queue, credentials, token_expires_at)
_sse_sessions_lock: threading.Lock = ...
_MCP_QUEUE_MAXSIZE = int(os.environ.get("MCP_QUEUE_MAXSIZE", "100"))
```

This dict lives in the gunicorn worker's memory. **Requires exactly 1 worker** (`-w 1`) — see `docs/deployment.md`.

### `open_sse_connection(request, credentials, token_expires_at) → StreamingHttpResponse`

```
1. session_id = uuid4()
2. msg_queue  = queue.Queue(maxsize=100)
3. _sse_sessions[session_id] = (msg_queue, credentials, token_expires_at)
4. session_stats[session_id] = {tool_count:0, ...}   # full schema above
5. record_event("SSESessionOpen", {...})
6. yield f"event: endpoint\ndata: {BASE_URL}/mcp/message?sessionId={session_id}\n\n"
7. Loop:
     try:
         msg = msg_queue.get(timeout=25)
         yield f"event: message\ndata: {msg}\n\n"
     except queue.Empty:
         yield ": keepalive\n\n"    # keep proxy/LB from closing idle connection
8. finally (on disconnect):
     del _sse_sessions[session_id]
     del session_stats[session_id]
     record_event("SSESessionClose", {...})
     record_event("MCPSessionSummary", {...})
     if tool_count == 0: record_event("MCPSessionAbandoned", {...})
```

### `handle_sse_message(request) → JsonResponse`

```
1. session_id = request.GET.get("sessionId")
2. session    = _sse_sessions.get(session_id)
   if not session: record_event("MCPSessionMissing"); return 400

3. (msg_queue, credentials, token_expires_at) = session
4. response = dispatch_jsonrpc(request, body, credentials, session_id, token_expires_at)
5. msg_queue.put(json.dumps(response), timeout=30)
   # If full after 30s: drop response, record_metric("Custom/MCP/queue_overflow_count", 1)
6. return JsonResponse({"ok": True})
```

The `JsonResponse({"ok": True})` is an ACK only. The real response travels back on the SSE stream.

---

## `mcp_app/clients/shared.py`

### `build_base_url(base_template: str, credentials: dict) → str`

```python
return base_template.format(publisher_id=credentials["publisherId"]).rstrip("/")
```

`base_template` is `settings.CDS_BASE_URL` or `settings.CMS_BASE_URL` — both contain `{publisher_id}` placeholder.

### `build_basic_auth_headers(credentials: dict) → dict`

```python
token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
return {"Authorization": f"Basic {token}"}
```

### `slugify_url_path(path: str) → str`

Normalises a URL path for use as a NR transaction name:
```python
re.sub(r"/\d+", "/{id}", path).strip("/").replace("/", "_") or "root"
```

e.g. `/post/42/comments/` → `post_{id}_comments`

---

## `mcp_app/clients/cds.py`

### `cds_get(credentials, path, params=None) → dict`

**Retry algorithm:**

```
_REQUEST_TIMEOUT = 5   # seconds
_RETRY_BACKOFF   = 0.5 # seconds (waits before attempt 2)

for attempt in range(2):
    if attempt > 0:
        time.sleep(_RETRY_BACKOFF)
    try:
        resp = requests.get(url, headers=..., params=clean_params, timeout=5)
        if resp.status_code == 408 and attempt == 0:
            last_exc = exc; continue     # retry on explicit 408
        if not resp.ok:
            raise exc                    # non-retryable HTTP error
        return resp.json()
    except requests.exceptions.Timeout:
        last_exc = exc
        if attempt == 0: continue        # retry on timeout
        break
    except Exception:
        last_exc = exc; break            # non-retryable — stop immediately
```

After both attempts fail: emits `MCPToolError`-related NR attrs, records `Custom/CDS/error_count`, raises `last_exc`.

**What triggers a retry:** `requests.Timeout` or `HTTP 408` on attempt 0 only.  
**What does not retry:** Any other HTTP error (4xx, 5xx), network error other than timeout.

### Error classification: `classify_cds_error(exc, http_status) → str`

| Condition | Category |
|---|---|
| Timeout or HTTP 408 | `"timeout"` |
| HTTP 401 | `"auth_error"` |
| HTTP 404 | `"not_found"` |
| 400–499 | `"bad_request"` |
| 500–599 | `"upstream_error"` |
| Other | `"system_error"` |

---

## `mcp_app/clients/cms.py`

### `cms_get / cms_post / cms_patch / cms_delete(credentials, path, ...) → dict`

No automatic retry (write operations are not idempotent). `_REQUEST_TIMEOUT = 10` seconds.

All four functions share the same error path: `normalize_cms_error(exc, url)`.

### `normalize_cms_error(exc, url) → dict`

Returns a structured error dict (never raises):

```
HTTP 401 → {error_type:"auth_error", message:"CDS credentials rejected...", retryable:False}
HTTP 404 → {error_type:"not_found", message:"Resource not found ({url}).", retryable:False}
HTTP 4xx → {error_type:"bad_request", message: extracted from JSON detail/message/field errors, retryable:False}
HTTP 5xx → {error_type:"upstream_error", message:"CMS server error HTTP {status}", retryable:True}
Timeout  → {error_type:"timeout", message:"CMS request timed out", retryable:True}
Other    → {error_type:"system_error", message:str(exc), retryable:False}
```

4xx message extraction priority: `detail` → `message` → `error.description` → field-error list → `f"HTTP {status}"`.

### `classify_cms_error(exc, http_status) → str`

Same table as CDS classification (above).

---

## `mcp_app/cds/__init__.py`

### Tool registration

Each CDS submodule (`posts.py`, `categories.py`, …) exports:
```python
SCHEMAS: list[dict]       # list of MCP tool schema dicts
HANDLERS: dict[str, callable]  # tool_name → handler function
```

`__init__.py` aggregates at import time:
```python
from mcp_app.cds import posts, categories, ...

TOOLS: list[dict] = posts.SCHEMAS + categories.SCHEMAS + ...

_HANDLER_REGISTRY: dict[str, callable] = {
    **posts.HANDLERS,
    **categories.HANDLERS,
    ...
}
```

### Per-tool concurrency tracking

```python
_active_calls: dict[str, int] = collections.defaultdict(int)  # tool_name → count
_active_calls_lock: threading.Lock = ...
```

On each `dispatch_cds_tool` call:
```python
with _active_calls_lock:
    _active_calls[name] += 1
    concurrent = _active_calls[name]

try:
    result = _HANDLER_REGISTRY[name](credentials, args)
finally:
    with _active_calls_lock:
        _active_calls[name] -= 1
```

`concurrent` is attached as a NR span attribute (`cds.concurrent_calls`). No limit is enforced — it is an observability metric, not a throttle.

### `dispatch_cds_tool(name, credentials, args) → dict`

```python
if name not in _HANDLER_REGISTRY:
    return {"error_type": "not_found", "message": f"Unknown tool: {name}"}
```

Calls the handler and returns its result. The handler signature is:
```python
def handler(credentials: dict, args: dict) -> dict
```

---

## `mcp_app/cms/__init__.py`

Identical pattern to CDS with two additions:

```python
CMS_TOOL_NAMES: frozenset = frozenset(tool["name"] for tool in CMS_TOOLS)
```

Used in `dispatch.py` to route to CMS vs CDS without a linear scan.

### CMS write-op detection

In `dispatch.py`, a tool call is a "write op" if:
```python
name in CMS_TOOL_NAMES and not name.startswith(("list_", "get_", "validate_"))
```

These are counted against the 50-write-per-session cap in `session_stats["write_op_count"]`.

---

## `mcp_app/cms/helpers.py`

### `DELETION_REQUIRES_CONFIRMATION: dict`

A constant returned by every delete handler that hasn't received both `dry_run=false` AND `confirm_delete=true`:

```python
{
    "error_type": "confirmation_required",
    "message": "Deletion requires BOTH dry_run=false AND confirm_delete=true. ...",
    "retryable": False,
}
```

### `format_field_value(v) → str`

Truncates to 120 characters with `…` for diff output. Returns `"(empty)"` for `None`.

### `preview_create_op(resource: str, payload: dict) → str`

Returns a multi-line human-readable dry-run preview:
```
📋  DRY RUN — Create {resource}
────────────────────────────────────────────────────
Will create a new {resource} with the following details:

  {field1:<28} {value1}
  {field2:<28} {value2}
  ...

⚡  No changes have been made.
To proceed, call this tool again with dry_run=false.
```

### `preview_update_op(resource: str, item_id, current: dict, changes: dict) → str`

Fetches `current` from the CMS (caller must pass it), diffs each changed field:
```
📋  DRY RUN — Update {resource} #{item_id}
────────────────────────────────────────────────────
The following fields will change:

  {field:<28} {old_val}  →  {new_val}
  ...

⚡  No changes have been made.
To apply, call again with dry_run=false.
```

If `changes` is empty: `(no fields provided — nothing will change)`.

### `preview_delete_op(resource: str, item_id, item: dict, warning: str = "") → str`

```
📋  DRY RUN — Delete {resource} #{item_id}
────────────────────────────────────────────────────
⚠️   WARNING: This will PERMANENTLY delete the following {resource}:

  {field:<28} {value}
  ...

[optional warning line]

⚡  No changes have been made.
To permanently delete, call again with:
  dry_run=false
  confirm_delete=true
```

### `validate_live_blog_post_type(credentials, post_id) → dict | None`

Validates that `post_id` exists and is a `"LiveBlog"` post. Returns an error dict on failure, `None` on success. Used by live-blog write tools before writing.

---

## CMS tool tier model

| Tier | Operation | Default `dry_run` | Execute condition |
|---|---|---|---|
| 1 | List / Get / Validate (read) | N/A | Always executes |
| 2 | Create | `True` | `dry_run=False` |
| 3 | Update | `True` | `dry_run=False` |
| 3 | Delete | `True` | `dry_run=False` AND `confirm_delete=True` |

**Why `dry_run=True` by default on writes:** AI agents can call tools in fast loops. Dry-run by default forces a human-visible preview step before any data changes. The agent must make a second explicit call to execute.

**Why double-confirm for delete:** Deletion is irreversible. Requiring both parameters simultaneously prevents accidentally passing `confirm_delete=true` in a retry without also intending `dry_run=false`.

---

## `mcp_app/prompt_capture.py`

### `extract_prompt_for_tool_call(request, body, params) → (prompt_id, prompt_text, prompt_source, args)`

**Priority chain (first match wins):**

```
1. HTTP headers (in order):
   X-MCP-Prompt, X-User-Prompt, X-Prompt-Text, X-LLM-Prompt

2. body["_meta"] or params["_meta"] dict:
   keys: "prompt", "userMessage", "user_message", "message"

3. params["prompt"]

4. args["_prompt"] or args["prompt"]    ← stripped from args before returning

5. Fallback: json.dumps(args)           source = "tool_args"
```

`prompt_id` is a new `uuid4()` per call — used to join `MCPPrompt` with `MCPToolError`/`MCPToolDegraded` in NR.

`_prompt` and `prompt` keys are removed from `args` before returning so tool handlers never see them.

Prompt text is truncated to 2000 characters.

---

## `auth_app/views.py` — OAuth PKCE flow

### `POST /register`

```
1. check_origin(request) — 403 if browser origin not in allowlist
2. Parse body (JSON or form)
3. Validate redirect_uri against OAUTH_ALLOWED_REDIRECT_URIS (+ EXTRA)
4. client_id = secrets.token_urlsafe(32)
5. OAuthClient.objects.create(client_id=client_id, redirect_uri=redirect_uri)
6. Return {"client_id": client_id}
```

### `POST /authorize` (form submission)

```
1. check_origin(request) — 403 if browser origin not in allowlist
2. Extract: publisher_id, api_key, api_secret, client_id, redirect_uri,
            code_challenge, code_challenge_method, state
3. Validate client_id against OAuthClient
4. validate_cds_credentials(publisher_id, api_key, api_secret)
   → (False, status) → render error on login form
5. code = secrets.token_urlsafe(32)
6. credentials = {"publisherId": publisher_id, "apiKey": api_key, "apiSecret": api_secret}
7. OAuthCode.objects.create(code=code, client_id=client_id,
       redirect_uri=redirect_uri, code_challenge=code_challenge,
       credentials=credentials,           # encrypted by EncryptedJSONField
       expires_at=now + timedelta(minutes=10))
8. Redirect to redirect_uri?code={code}&state={state}
```

### `POST /token`

```
1. check_origin(request) — 403
2. parse_oauth_token_body(request)
3. grant_type == "authorization_code":
     a. OAuthCode.objects.get(code=code) — 400 "invalid_grant" if missing/expired
     b. PKCE S256 check:
        expected  = base64url(SHA-256(code_verifier.encode()))
        if expected != oauth_code.code_challenge: 400 "invalid_grant"
     c. atomic:
           OAuthCode.objects.filter(pk=...).delete()   # single-use
           token, refresh_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
           OAuthToken.objects.update_or_create(
               client_id=client_id, publisher_id=publisher_id,
               defaults={token, refresh_token, credentials})
     d. Return {access_token, token_type:"bearer", refresh_token}

4. grant_type == "refresh_token":
     a. OAuthToken.objects.get(refresh_token=refresh_token) — 400 if missing
     b. atomic: new_token = token_urlsafe(32); update token + refresh_token
     c. Return new {access_token, refresh_token}
```

**PKCE S256 check detail:**

```python
digest   = hashlib.sha256(code_verifier.encode("ascii")).digest()
expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
# compare to oauth_code.code_challenge stored at /authorize time
```

---

## Database schema summary

```
oauth_client
  id           INTEGER  PK
  client_id    VARCHAR(64)   UNIQUE INDEX
  redirect_uri VARCHAR(512)  blank ok
  created_at   TIMESTAMPTZ   auto

oauth_code
  id             INTEGER  PK
  code           VARCHAR(128)  UNIQUE
  client_id      VARCHAR(64)   INDEX
  redirect_uri   TEXT
  code_challenge VARCHAR(256)
  credentials    TEXT          Fernet-encrypted JSON
  expires_at     TIMESTAMPTZ

oauth_token
  id            INTEGER  PK
  token         VARCHAR(128)  UNIQUE
  client_id     VARCHAR(64)   INDEX
  publisher_id  VARCHAR(64)   INDEX
  refresh_token VARCHAR(128)  UNIQUE  NULL ok
  credentials   TEXT          Fernet-encrypted JSON
  created_at    TIMESTAMPTZ   NULL ok  auto

django_session  (Django built-in)
  session_key  VARCHAR(40)  PK
  session_data TEXT         base64(pickle({credentials, session_created_at, session_ttl_seconds, ...}))
  expire_date  TIMESTAMPTZ  INDEX
```

---

## Migration history (`auth_app/migrations/`)

| Migration | What it does |
|---|---|
| 0001 | Creates `oauth_client`, `oauth_code`, `oauth_token` with plaintext credential columns |
| 0002–0006 | Iterative schema changes (indexes, field additions) |
| 0007 | Drops `ai_client` table (DB only — state not updated) |
| 0008 | Adds `EncryptedJSONField` (`credentials` column) to `oauth_token` and `oauth_code` |
| 0009 | Renames / adds `publisher_id` indexed column to `oauth_token` |
| 0010 | `SeparateDatabaseAndState(DeleteModel('AIClient'))` — state-only; no DROP (table already gone from 0007) |
| 0011 | Introspects tables, drops orphan `encrypted_api_secret`, `encrypted_api_key`, `encrypted_publisher_id` columns if present (added by now-reverted migrations) |

**Migration 0011 algorithm:**

```python
def _drop_orphans(apps, schema_editor):
    connection = schema_editor.connection
    quote = connection.ops.quote_name
    with connection.cursor() as cursor:
        existing_tables = set(connection.introspection.table_names(cursor))
        for table in _TABLES:
            if table not in existing_tables:
                continue
            present = {col.name for col in connection.introspection.get_table_description(cursor, table)}
            for column in _ORPHAN_COLUMNS:
                if column in present:
                    cursor.execute(f"ALTER TABLE {quote(table)} DROP COLUMN {quote(column)};")
```

No `IF EXISTS` — SQLite doesn't support it. Introspect first, then drop only if present.

---

## Settings structure

```
publive_mcp/settings/
  base.py      — loaded by all environments
  prod.py      — import base.*, override for production
  dev.py       — import base.*, override for local dev
```

### Key settings in `base.py`

```python
# Publive API base URL templates — configurable via env var
CDS_BASE_URL = os.environ.get("CDS_BASE_URL", "https://cds-beta.thepublive.com/publisher/{publisher_id}")
CMS_BASE_URL = os.environ.get("CMS_BASE_URL", "https://cms-beta.thepublive.com/publisher/{publisher_id}")

# Database
DATABASES = {"default": dj_database_url.parse(os.environ.get("DATABASE_URL", "sqlite:///db.sqlite3"),
    conn_max_age=600)}   # connection pooling — reuse per-thread for up to 10 minutes

# Sessions (DB-backed for persistence across deploys)
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90    # 90 days default TTL

# Security
BASE_URL        = os.environ.get("BASE_URL", "http://localhost:8000")
RATE_LIMIT_ENABLED = True
OAUTH_ALLOWED_ORIGINS = [BASE_URL]
OAUTH_ALLOWED_REDIRECT_URIS = ["https://claude.ai/...", "cursor://...", ...]
```

### `prod.py` additions

- `ALLOWED_HOSTS` derived from `BASE_URL`
- `STATIC_ROOT` set for Whitenoise
- `STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"`
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")`
- `SESSION_COOKIE_SECURE = True`

---

## `publive_mcp/wsgi.py`

```python
from django.core.wsgi import get_wsgi_application
import newrelic.agent

newrelic.agent.initialize("newrelic.ini")
application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
```

The WSGI wrapper instruments every request **before** Django's middleware stack, giving accurate wall-clock timings that include middleware overhead. Gunicorn is pointed at `publive_mcp.wsgi:application`.

---

## `mcp_app/nr_utils.py` — guarded New Relic helpers

All New Relic calls go through these wrappers. Each checks `if _nr is None` or `if not _current_transaction()` and silently no-ops. This means the app runs cleanly with no `NEW_RELIC_LICENSE_KEY` — no agent, no crashes.

| Helper | What it wraps |
|---|---|
| `add_attrs(pairs)` | `newrelic.agent.add_custom_attributes()` |
| `record_event(type, attrs)` | `newrelic.agent.record_custom_event()` |
| `record_metric(name, val)` | `newrelic.agent.record_custom_metric()` |
| `notice_err(exc, pairs)` | `newrelic.agent.notice_error()` |
| `set_txn_name(name, group)` | `newrelic.agent.set_transaction_name()` |
| `suppress_apdex()` | `newrelic.agent.suppress_apdex_metric()` |
| `suppress_trace()` | `newrelic.agent.suppress_transaction_trace()` |
| `get_linking_metadata()` | `newrelic.agent.get_linking_metadata()` → `{}` fallback |

`SERVER_ENV` reads `RAILWAY_ENVIRONMENT` (or `"local"`). `SERVER_VERSION` reads `SERVER_VERSION` env var (or `"1.0.0"`). Both are attached to every custom event.
