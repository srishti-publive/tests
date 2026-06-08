# Low-Level Design: Publive MCP Server

This document covers every module's function signatures, data contracts, algorithms, and internal state. Read `docs/hld.md` first for the system context.

---

## File map

```
publive_mcp/
  settings/
    base.py          — shared settings, env var defaults
    prod.py          — production overrides (Postgres, Whitenoise, NR)
    local.py         — local overrides (SQLite, LocMemCache, DEBUG=True)
  wsgi.py            — WSGI application, New Relic WSGI wrapper
  urls.py            — root URL conf

auth_app/
  models.py          — OAuthClient, OAuthCode, OAuthToken (data models)
  services.py        — session helpers, origin check, PKCE body parser, CDS validator
  views.py           — /register, /authorize, /token, /revoke, /connect, /auth/login
  migrations/        — 0001–0012 (0011 drops orphan encrypted columns; 0012 reverts
                        `credentials` to plain JSONField after encryption was removed)

mcp_app/
  views.py           — entry points: health_check(), mcp_endpoint(), mcp_message() — thin routing only, delegates to protocol/ and transport/
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
2. WhiteNoiseMiddleware          (whitenoise.middleware)        — serves collected static files
3. RequestIDMiddleware           (mcp_app.middleware)           — attaches X-Request-ID
4. SessionMiddleware             (django.contrib.sessions.middleware)
5. CommonMiddleware              (django.middleware.common)
6. SecurityHeadersMiddleware     (mcp_app.middleware)           — CSP/X-Frame on HTML only
7. RateLimitMiddleware           (mcp_app.middleware)           — fixed-window-per-slot rate limit
```

MCP endpoints are `@csrf_exempt` so `CsrfViewMiddleware` is absent from the stack, and there's no `MessageMiddleware` — the auth pages don't use Django's messages framework.

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
credentials     JSONB         {publisherId, apiKey, apiSecret}
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
credentials    JSONB         {publisherId, apiKey, apiSecret}
created_at     TIMESTAMPTZ   auto_now_add, null allowed
```

`publisher_id` is a plain indexed column so token lookup by `(client_id, publisher_id)` works directly. Upsert pattern: `update_or_create(client_id=..., publisher_id=...)` — re-authorisation does not break in-flight sessions.

`refresh_token` rotates on every use inside an atomic DB transaction. Stolen refresh tokens are single-use.

---

## `auth_app/services.py`

### `get_session_credentials(session) → dict | None`

Returns `session["credentials"]` if it's a plain `dict`; otherwise (absent, or some other type) returns `None`.

### `set_session_credentials(session, credentials: dict)`

```python
session["credentials"] = credentials
```

Just stores the dict — it does **not** take a `remember_for_days` argument and does not touch `session_created_at`/`session_ttl_seconds`/`set_expiry()`. Those three are set directly in `auth_login()` (`auth_app/views.py`), which always writes `session_ttl_seconds = -1` (never expires) and `session.set_expiry(10 * 365 * 24 * 3600)` — a 10-year cookie ceiling. There is no per-login configurable TTL; `remember_for_days` does not exist anywhere in the code.

### `check_session_ttl(session) → bool`

```python
ttl_seconds = session.get("session_ttl_seconds", -1)
if ttl_seconds <= 0:        # -1 "always" or 0 "browser-session" → never expires here
    return False
deadline_ts = int(session["session_created_at"]) + int(ttl_seconds)
return time.time() > deadline_ts
```

Returns `True` (expired) only for sessions with a **positive** `session_ttl_seconds` whose absolute deadline has passed. Since `auth_login()` always writes `-1`, this path is effectively dead for sessions created by the current login flow — it exists to enforce a server-side deadline for any row that does carry a positive TTL (e.g. pre-existing rows), catching cases Django's cookie TTL would miss (`SESSION_SAVE_EVERY_REQUEST` disabled).

### `check_origin(request) → JsonResponse | None`

Returns `None` (allowed) when:
- No `Origin` header present — desktop MCP clients never send one.
- `Origin` (after stripping a trailing `/`) is in the allowed set.

The allowed set is `settings.OAUTH_ALLOWED_ORIGINS` if defined, **else** the hardcoded default `{"https://claude.ai", "https://api.claude.ai"}` — and `settings.BASE_URL` is *always* added to the set regardless (same-origin is always allowed).

Returns a `403 JsonResponse` (`error: "invalid_origin"`) for any other non-empty `Origin`.

### `is_loopback_redirect_uri(uri) → bool`

Returns `True` for `http://` URIs whose host is `localhost`, `127.0.0.1`, or `::1` — any port. Exists because native/desktop OAuth clients (RFC 8252 §7.3) bind an ephemeral local port at launch, so exact-string allowlisting is impossible for them.

### `is_registrable_redirect_uri(uri) → bool`

Returns `True` for `https://<host>/...` URIs, or for any loopback URI (`is_loopback_redirect_uri`). Plain `http://` to a non-loopback host is rejected — it would leak the authorization code over an insecure channel. Per RFC 7591 / OAuth 2.1, dynamic registration doesn't pre-approve specific apps by URL; the only requirement is transport security.

### `redirect_uris_match(requested, registered) → bool`

`True` on an exact string match, **or** when both URIs are loopback URIs that agree on `(scheme, hostname, path)` but differ only by port (RFC 8252 §7.3 requires the server to accept any port for loopback redirects at request time).

### `parse_oauth_token_body(request) → tuple[dict | None, JsonResponse | None]`

Returns `(body, None)` on success or `(None, error_response)` on failure — **not** a bare `dict`. Reads `Content-Type`:
- `application/json` → `json.loads(request.body)`; rejects non-dict JSON and malformed JSON with a `400 invalid_request`.
- `application/x-www-form-urlencoded` → `request.POST.dict()`, falling back to manual `parse_qs(request.body)` if `request.POST` is empty (covers bodies Django didn't auto-parse).
- Anything else → `400 invalid_request` ("Content-Type must be ...").

### `validate_cds_credentials(publisher_id, api_key, api_secret) → (bool, int)`

```python
token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
base  = settings.CDS_BASE_URL.format(publisher_id=publisher_id)
resp  = requests.get(f"{base}/posts/", params={"limit": 1},
                     headers={"Authorization": f"Basic {token}"}, timeout=10)
return 200 <= resp.status_code < 300, resp.status_code
```

Decorated with `@newrelic.agent.function_trace(name="validate_cds_auth", group="Auth")`; records `auth.cds_validation_status` / `auth.cds_validation_ms` as transaction attributes via `add_attrs()`. Returns `(True, 200)` on a 2xx, `(False, status_code)` otherwise; raises `requests.RequestException` if CDS is unreachable (caller handles it). Called once at login/authorize — not on every tool call.

---

## `mcp_app/protocol/auth.py`

### `resolve_credentials(request) → (credentials | None, None, error_code | None)`

```
Authorization: Bearer <token>  →  _resolve_oauth_token(token)
(no Bearer header)             →  _resolve_session(request)
```

Second element is always `None` (tokens have no expiry tracked here).

### `_resolve_oauth_token(token_value) → (dict | None, None, None)`

`OAuthToken.objects.get(token=token_value)` → returns `oauth_token.credentials` (plain `dict`, read directly from the `JSONField`). Returns `(None, None, None)` on `DoesNotExist`. Re-raises any other exception.

### `_resolve_session(request) → (dict | None, None, error_code | None)`

1. `get_session_credentials(request.session)` — if None, return `(None, None, None)`.
2. `check_session_ttl(request.session)` — if expired, flush session, return `(None, None, SESSION_EXPIRED)`.
3. Return `(credentials, None, None)`.

### `build_unauthorized_response(request, error_code) → JsonResponse (401)`

Response body — `error_description` is present **only** when `error_code` is a known typed code (currently just `SESSION_EXPIRED`); otherwise the body is just `{authUrl, error: "Not authenticated"}`:
```json
{
  "authUrl": "{BASE_URL}/connect",
  "error": "{error_code | 'Not authenticated'}",
  "error_description": "{present only for typed error codes, e.g. SESSION_EXPIRED}"
}
```

`WWW-Authenticate` header (RFC 6750):
```
Bearer realm="{BASE_URL}", resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource"
```

MCP clients parse this header to auto-start the OAuth flow.

### `identify_mcp_client(request) → (client_name: str, client_version: str)`

Regex: `^([^\s/]+)/([^\s]+)` on `HTTP_USER_AGENT`. On a match, the prefix (lowercased) is looked up in `_CLIENT_NAME_MAP`, falling back to the raw prefix string if unmapped, and `client_version` is the regex's second group. If the regex doesn't match but a `User-Agent` is present, it falls back to the first whitespace-separated token (still via `_CLIENT_NAME_MAP`) with `client_version = "unknown"`. No `User-Agent` at all → `("unknown", "unknown")`.

`_CLIENT_NAME_MAP`:

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

Cluster-wide fixed-window rate limit for `MCPPrompt` NR events, backed by Redis
`INCR`+`EXPIRE` (`mcp_app/redis_client.py`) — a per-process counter would
silently become `limit × process_count` once the app scales horizontally,
defeating its purpose as a NR-cost-control gate:

```python
_PROMPT_EVENT_MAX_PER_MIN = 1000
_PROMPT_EVENT_KEY_TTL     = 120   # > window length, so a bucket always self-expires

bucket = int(time.time() // 60)              # current UTC-minute bucket
key    = f"mcp:prompt_events:{bucket}"
count  = client.incr(key)                    # atomic on the Redis side
if count == 1:
    client.expire(key, _PROMPT_EVENT_KEY_TTL)
return count <= _PROMPT_EVENT_MAX_PER_MIN
```

Function name/signature unchanged from the old in-process sliding-window
implementation, so `dispatch.py`'s call site needed no edit.

---

## `mcp_app/protocol/session_store.py`

Thin compatibility shim re-exporting the Redis-backed stats API from
`mcp_app/protocol/redis_session_stats.py` under the original names
(`init_stats`, `increment`, `set_field`, `get_field`,
`get_timeline_and_set_client_name`, `append_tool_sequence`, `pop_stats`) —
`dispatch.py` and `transport/sse.py` needed only import-line changes, not
call-site rewrites. Originally existed solely to break a circular import
between `transport/sse.py` and `protocol/dispatch.py` (both need session
stats); now also the seam that lets every worker/replica share one
`session_stats` view via a Redis hash (`mcp:session_stats:{id}`, TTL 24h)
instead of a single process's in-memory dict.

Per-session counters use atomic `HINCRBY`/`HINCRBYFLOAT` (the latter for
`session_start_time`, `total_tool_duration_ms`, `last_tool_end_perf` — see
`_FLOAT_FIELDS` in `redis_session_stats.py`) in place of the old
`with session_stats_lock: stats[field] += by`. `tool_sequence` is stored as a
separate Redis list (`mcp:session_stats:{id}:tool_sequence`) rather than a
field inside the hash. `pop_stats()` atomically snapshots and deletes both via
a pipelined `HGETALL` + `LRANGE` + `DELETE`.

**`mcp:session_stats:{session_id}` Redis hash schema** (formerly the
`session_stats[session_id]` dict — same fields, same names, now persisted as
hash fields with a 24h TTL instead of dict keys in process memory; values are
read back coerced to int/float by the accessors in `redis_session_stats.py`,
mirroring the original types):

| Key | Type | Purpose |
|---|---|---|
| `tool_count` | int | Total successful tool calls this session |
| `error_count` | int | Tool calls that raised an exception |
| `degraded_count` | int | Tool calls that returned an error_type |
| `session_start_time` | float | Wall-clock `time.time()` at session open (was `perf_counter()` — switched because the value must be comparable across processes; see `redis_session_stats.py`) |
| `total_tool_duration_ms` | float | Cumulative tool execution time |
| `total_estimated_input_tokens` | int | Sum of prompt token estimates |
| `total_estimated_output_tokens` | int | Sum of output token estimates |
| `last_tool_end_perf` | float \| None | Wall-clock `time.time()` when the last tool finished (same cross-process rationale as `session_start_time`) |
| `client_name` | str \| None | From `identify_mcp_client()`, set on first tool call |
| `session_trace_id` | str | NR trace.id from the SSE open transaction |
| `tool_sequence` | list[str] | Stored as a separate Redis list `mcp:session_stats:{id}:tool_sequence`, not a hash field — Redis hashes can't hold lists |
| `create_op_count` | int | CMS create-bucket mutation count — create_*/register_*/add_*/submit_* (capped at 100 per session) |
| `update_delete_op_count` | int | CMS update/delete-bucket mutation count — update_*/delete_* (capped at 100 per session) |

`None` values are stored using the sentinel `""` (`_NONE` in
`redis_session_stats.py`) since Redis hashes cannot hold `None` directly, and
translated back to `None` on read.

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

### `dispatch_jsonrpc(body, credentials, request=None, session_id=None, token_expires_at=None) → dict | None`

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

### `_handle_tool_call(body, credentials, request, session_id, id_) → dict`

**Actual pipeline, in source order:**

```
1. params = body.get("params", {}); name = params.get("name", "")
   prompt_id, prompt_text, prompt_source, args =
       extract_prompt_for_tool_call(request, body, params)
   # args may have _prompt/prompt stripped

2. if should_emit_prompt_event():
       record_prompt_observability(...)        # emits MCPPrompt NR event
   else:
       add_attrs([mcp.prompt_id, mcp.prompt_text, mcp.prompt_source,
                  mcp.session_id, mcp.tool_name])
       record_metric("Custom/MCP/prompt_event_dropped_count", 1)
       # rate-limited: prompt observability is dropped, but the tool STILL RUNS

3. add_attrs([mcp.tool_name, mcp.tool_input])
   record_metric(f"Custom/Tool/{name}/call_count", 1)
   record_metric("Custom/MCP/tool_call_count", 1)

4. validation_error = _validate_tool_args(name, args or {})
   if validation_error:
       record_metric("Custom/MCP/tool_validation_error_count", 1)
       return jsonrpc_ok(id_, {"content": [...], "isError": True})   # tool NOT called

5. # Session timeline — only populated for SSE sessions (the session has a Redis
   # stats hash); HTTP-transport calls have no hash, so start_offset_ms/ai_think_ms
   # stay None. get_timeline_and_set_client_name() does the compound read +
   # conditional client_name write atomically server-side via a Redis pipeline,
   # guarded by an `exists` check so HTTP-stateless sessions never get a phantom
   # hash created just to hold client_name.
   timeline = get_timeline_and_set_client_name(session_id or "", user_agent)
   # → {session_start_time, last_tool_end_perf, client_name, session_trace_id}; computes
   #   start_offset_ms, ai_think_ms from the (now wall-clock, cross-process-comparable) timestamps

6. if _is_cms_write(name):                      # name not in _CMS_READ_PREFIXES = (list_, get_, validate_)
       bucket      = _cms_write_bucket(name)    # "update_delete" for update_*/delete_*, else "create"
       counter_key = "create_op_count" if bucket == "create" else "update_delete_op_count"
       bucket_op_count = increment(session_id or "", counter_key) or 0
       # HINCRBY mcp:session_stats:{id} {counter_key} 1 — atomic, correct under concurrent
       # processes (not just threads); returns None for HTTP/stateless sessions (no hash
       # exists), so bucket_op_count stays 0 and the counter never increments for that transport
       if bucket_op_count > 100:
           label = "Create" if bucket == "create" else "Update/delete"
           return jsonrpc_ok(id_, {"content": [{"type": "text", "text": json.dumps({
               "error_type": "rate_limit", "message": f"{label} operation limit (100) reached...",
               "retryable": False})}]})
   # NOTE: create and update/delete each have an INDEPENDENT 100-op cap, enforced
   # ONLY for SSE sessions (which carry a session_stats entry). Stateless
   # HTTP-transport calls always see both counters at 0 and are never rate-limited.

7. t0 = time.perf_counter()
   result = dispatch_cms_tool(credentials, name, args) if name in CMS_TOOL_NAMES \
            else dispatch_cds_tool(credentials, name, args)
   duration_ms = round((time.perf_counter() - t0) * 1000, 2)
   set_txn_name(f"MCP/{name}", group="MCP")

8. degraded_reason = result.get("error") or result.get("error_type")  (if dict)
   is_degraded = bool(degraded_reason)
   # update the session's Redis stats hash via increment()/set_field()/append_tool_sequence()
   # — duration (HINCRBYFLOAT total_tool_duration_ms), token estimates, last_tool_end_perf
   # (wall-clock time.time(), set_field), tool_sequence (RPUSH to the separate list),
   # degraded_count (HINCRBY); add_attrs + record_metric for success vs degraded; emit
   # MCPToolDegraded if degraded
   return jsonrpc_ok(id_, {"content": [{"type": "text", "text": output_text}]})
   # NOTE: degraded responses still return isError-less content — the AI sees the
   # error as text and decides what to do; isError is only set True by validation
   # failures (step 4) and the exception path below.

   # Exception path (wraps steps 7–8):
   record_event("MCPToolError", {...}); return {"content": [...], "isError": True}
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

### Session/queue state — now Redis-backed

```python
_MCP_QUEUE_MAXSIZE = int(os.environ.get("MCP_QUEUE_MAXSIZE", "100"))
```

The session registry and per-session message queue used to be an in-process
dict (`_sse_sessions: dict[session_id → (queue.Queue, credentials,
token_expires_at)]`) plus a `threading.Lock`, living in the gunicorn worker's
memory — which is what forced **exactly 1 worker** (`-w 1`; see
`docs/deployment.md`). Both are now externalized to Redis:

- **Registry** — `mcp_app/transport/redis_session_store.py`: `register_session`/
  `get_session`/`close_session`, backed by `mcp:session:{id}` (credentials stored
  as plain JSON, same shape as the Postgres-backed session/token storage) plus a
  `mcp:active_sessions` set for O(1) `SCARD` cluster-wide counts.
- **Queue** — `mcp_app/transport/redis_message_queue.py`: `push_message`/
  `pop_message`/`delete_queue`/`queue_depth`, backed by a capped Redis list
  `mcp:session_queue:{id}` consumed via `BLPOP` (chosen over Pub/Sub — drops
  messages for momentarily-disconnected subscribers — and over Streams —
  unneeded consumer-group machinery for a strict 1-producer/1-consumer pattern).

Any worker/replica sharing the same `REDIS_URL` can now look up a session or
push/pop its queue — `GET /mcp` and `POST /mcp/message` no longer need to land
on the same process. `_MCP_QUEUE_MAXSIZE` stays in `sse.py` and is passed
through to `push_message(..., maxsize=_MCP_QUEUE_MAXSIZE)`.

### `open_sse_connection(request, credentials, token_expires_at) → StreamingHttpResponse`

```
1. session_id = str(uuid4())
   set_txn_name("Transport/SSE", group="Transport"); suppress_apdex(); suppress_trace()
   add_attrs([mcp.transport="sse", mcp.session_id, mcp.thread_active_count]); _add_session_protocol_attrs(...)
   client_name, _ = identify_mcp_client(request)

2. active_on_open = register_session(session_id, credentials, token_expires_at)
   # SET mcp:session:{id} {credentials, token_expires_at} EX 24h
   # SADD mcp:active_sessions {id}; SCARD → active_on_open   (all in one pipeline)
3. session_trace_id = get_linking_metadata()["trace.id"]
   init_stats(session_id, session_trace_id)   # HSET mcp:session_stats:{id} {...}; full schema above
   record_custom_metric("Custom/MCP/active_sessions", active_on_open)
4. record_event("SSESessionOpen", {session_id, publisher_id, active_threads, active_sessions, trace_id, span_id, ...})
5. post_url = f"{BASE_URL}/mcp/message?sessionId={session_id}"
   yield f"event: endpoint\ndata: {post_url}\n\n"
6. Loop:
     popped = pop_message(session_id, timeout=25)   # BLPOP mcp:session_queue:{id} 25
     if popped is None:
         yield ": keepalive\n\n"    # keep proxy/LB from closing idle connection
         continue
     wait_ms, msg = popped
     record_custom_metric("Custom/MCP/queue_wait_ms", wait_ms)
     yield f"event: message\ndata: {json.dumps(msg)}\n\n"
7. finally (on disconnect): _close_sse_session(session_id, publisher_id, stream_t0)
     # close_session() (DEL mcp:session:{id}; SREM + SCARD mcp:active_sessions),
     # delete_queue() (DEL mcp:session_queue:{id}), pop_stats() (atomic
     # HGETALL+LRANGE+DELETE snapshot of the stats hash + tool_sequence list);
     # computes duration_ms and server_work_pct, records Custom/MCP/active_sessions
     # (post-close count), and — in this order — emits MCPSessionAbandoned (only
     # if tool_count == 0), then always MCPSessionSummary, then SSESessionClose
```

### `handle_sse_message(request) → HttpResponse`

```
1. raw_sid    = request.GET.get("sessionId", "")
   session_id = raw_sid or ("anon-" + uuid4().hex[:8])     # missing sessionId → synthetic anon ID
   add_attrs([mcp.session_id, mcp.thread_active_count, mcp.request_size_bytes])
   client_name, client_version = identify_mcp_client(request)
   add_attrs([mcp.client_name, mcp.client_version])

2. session_entry = get_session(session_id)   # GET mcp:session:{id}; parse JSON; refresh TTL
   if session_entry is None:                  # absent, expired, or undecryptable → forces reconnect
       add_attrs([("mcp.sse_session_missing", True)])
       record_metric("Custom/MCP/sse_session_missing_count", 1)
       record_event("MCPSessionMissing", {...})
       return JsonResponse({"error": "No active MCP session."}, status=400)

3. (credentials, token_expires_at) = session_entry
   session_trace_id = get_field(session_id, "session_trace_id")  # HGET mcp:session_stats:{id} session_trace_id
   # attach mcp.session_trace_id, if present

4. body = json.loads(request.body)        # malformed JSON → 400 "Invalid JSON"

5. if body["method"] == "tools/call":
       seq = increment(session_id, "tool_count")   # HINCRBY mcp:session_stats:{id} tool_count 1
       add_attrs([("mcp.session_tool_seq", seq)])

6. response_msg = dispatch_jsonrpc(body, credentials, request, session_id, token_expires_at)
   if it's a tools/call response with isError == True:
       increment(session_id, "error_count")        # HINCRBY mcp:session_stats:{id} error_count 1

7. if response_msg is not None:
       ok = push_message(session_id, response_msg, maxsize=_MCP_QUEUE_MAXSIZE, timeout=30.0)
       # RPUSH mcp:session_queue:{id} json.dumps([time.time(), response_msg]); EXPIRE
       # — wall-clock time.time(), not perf_counter(): producer (this POST handler)
       # and consumer (event_stream(), possibly a different process) must agree on
       # a comparable clock. Polls LLEN with backoff (Redis has no blocking-push
       # primitive) — a soft cap, same tolerance the in-process bounded queue had
       # under concurrent producers.
       if not ok:
           record_metric("Custom/MCP/queue_overflow_count", 1)
           add_attrs([("mcp.queue_overflow", True)]); log error
           return JsonResponse({"ok": True})   # response dropped — client never receives it
       depth = queue_depth(session_id)   # LLEN mcp:session_queue:{id}
       record mcp.session_queue_depth attr + Custom/MCP/session_queue_depth metric

8. return JsonResponse({"ok": True})
```

The `JsonResponse({"ok": True})` is an ACK only. The real response travels back
on the SSE stream — `event_stream()` calls `pop_message()`, which `BLPOP`s the
`[enqueued_at, response_dict]` JSON payload off the Redis list, computes
`queue_wait_ms` from the wall-clock timestamp, and returns
`(queue_wait_ms, response_dict)` for serialization to the `event: message` payload.

---

## `mcp_app/clients/shared.py`

### `build_base_url(template: str, credentials: dict) → str`

```python
publisher_id = credentials.get("publisherId", "")
if not publisher_id:
    raise Exception("No publisher ID in credentials — please re-authenticate")
return template.format(publisher_id=publisher_id)
```

No `.rstrip("/")` — the templates (`settings.CDS_BASE_URL` / `settings.CMS_BASE_URL`) already end without a trailing slash; both contain a `{publisher_id}` placeholder. Raises if `publisherId` is missing from the credentials dict (defensive — should never happen post-auth).

### `build_basic_auth_headers(credentials: dict) → dict`

```python
token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
return {"Authorization": f"Basic {token}", "Content-Type": "application/json"}
```

Returns **two** headers, not just `Authorization` — `Content-Type: application/json` is included for every request (CDS GETs ignore it; CMS POST/PATCH bodies need it).

### `slugify_url_path(path: str) → str`

Normalises a URL path for use as an NR transaction name segment:
```python
slug = path.strip("/").replace("/", "_")
return slug or "root"
```

There is **no numeric-ID normalisation** (no `re.sub(r"/\d+", "/{id}", ...)`) — `/post/42/comments/` slugifies to `post_42_comments`, not `post_{id}_comments`. Each distinct numeric ID therefore produces its own NR transaction-name segment.

---

## `mcp_app/clients/cds.py`

### `cds_get(credentials, path, params=None) → dict`

**Retry algorithm:**

```
_REQUEST_TIMEOUT = 5   # seconds per attempt
_RETRY_BACKOFF   = 1   # seconds (sleep before attempt 2 only)

for attempt in range(2):
    if attempt > 0:
        time.sleep(_RETRY_BACKOFF)
    try:
        resp = requests.get(url, headers={"Authorization": ...}, params=clean_params, timeout=5)
        if not resp.ok:
            exc = Exception(f"{detail_or_message_or_HTTP_status} [url={url}]"); exc.response = resp
            if resp.status_code == 408 and attempt == 0:
                last_exc = exc; continue     # retry once on explicit 408
            raise exc                        # any other non-2xx — non-retryable
        # success: add_attrs(cds.endpoint/http_status/latency_ms/response_size_bytes/
        #          retry_count/retried), record Custom/CDS/latency_ms + response_size_bytes
        #          (+ retry_count metric if retried), return resp.json()
    except requests.exceptions.Timeout as exc:
        last_exc = exc
        if attempt == 0: continue            # retry once on timeout
        break
    except Exception as exc:
        last_exc = exc; break                # any other exception — stop immediately, no retry
```

After both attempts fail: classifies the error, records `Custom/CDS/timeout_count` (if timeout) and/or `Custom/CDS/retry_count` (if retried), calls `notice_err()` with `error.layer/cds_endpoint/http_status/retry_count/category` attrs, records `Custom/CDS/error_count`, and **re-raises `last_exc`** (the caller — the tool handler — turns this into `MCPToolError`).

**What triggers a retry:** `requests.Timeout` or `HTTP 408`, and only on attempt 0 (so at most one retry, after a 1-second sleep).
**What does not retry:** Any other HTTP error (4xx, 5xx) or any other exception type.

A separate helper `is_retryable_cds_error(exc)` exists (returns `True` for `Timeout` or an HTTP-408 response) but is **not called** by `cds_get` — the retry decision is inlined in the loop above. It is only re-exported through the `mcp_app/cds_client.py` backward-compat shim.

### Error classification: `classify_cds_error(exc, http_status) → str`

| Condition | Category |
|---|---|
| `Timeout` exception or `http_status == 408` | `"timeout"` |
| `http_status == 401` | `"auth_error"` |
| `400 <= http_status < 500` (incl. 404 — there is no separate `"not_found"` category) | `"client_error"` |
| `500 <= http_status < 600` | `"upstream_error"` |
| Anything else (e.g. connection errors with no HTTP status) | `"system_error"` |

---

## `mcp_app/clients/cms.py`

### `cms_get / cms_post / cms_patch / cms_delete(credentials, path, ...) → dict`

No automatic retry (write operations are not idempotent — unlike `cds_get`'s single retry on timeout/408). `_REQUEST_TIMEOUT = 10` seconds for all four.

On a non-2xx response, all four record `cms.path/method/http_status/latency_ms`, `Custom/CMS/error_count`, and `error.category` (via `classify_cms_error`), then return `normalize_cms_error(exc, url)`. On success they record `_record_cms_attrs(...)`, `Custom/CMS/latency_ms`, and `Custom/CMS/response_size_bytes`. `requests.exceptions.Timeout` is handled by a shared helper `_record_cms_timeout_attrs(path, method)` that adds `cms.timed_out=True` and records both `Custom/CMS/timeout_count` and `Custom/CMS/error_count`, then returns the `error_type: "timeout"` dict directly (not via `normalize_cms_error`). `ConnectionError` is handled separately (see below). Each is wrapped in `@newrelic.agent.function_trace(name="cms_<verb>", group="CMS")`.

### `normalize_cms_error(exc, url) → dict`

Returns a structured error dict (never raises) — used for non-2xx HTTP responses (NOT for `Timeout`/`ConnectionError`, which are handled by their own `except` clauses before `normalize_cms_error` is ever reached):

```
HTTP 401 → {error_type:"auth_error", message:"CMS credentials rejected (HTTP 401). Please re-authenticate: visit /connect or re-run the OAuth flow.", retryable:False}
HTTP 404 → {error_type:"not_found", message:"Resource not found ({url}).", retryable:False}
HTTP 4xx → {error_type:"bad_request", message: <extracted, see below>, raw_api_response: <first 1000 chars of response body>, retryable:False}
HTTP 5xx → {error_type:"upstream_error", message:"CMS server error (HTTP {status}). Try again shortly.", retryable:True}
(via except requests.exceptions.Timeout, not normalize_cms_error) → {error_type:"timeout", message:"CMS request timed out.", retryable:True}
Other / no http_status → {error_type:"system_error", message:str(exc), retryable:False}
```

4xx message extraction priority: `detail` → `message` → `error.description` → (only if still unset) a field-error list built from any list/string-valued top-level keys, joined as `"Validation error — key: val; ..."` → falls back to `f"HTTP {status}"`. The raw response body (first 1000 chars) is always attached as `raw_api_response` for debugging, even when a friendlier message was extracted.

### `classify_cms_error(exc, http_status) → str`

**Differs from `classify_cds_error`** — CMS has its own dedicated `"not_found"` and `"bad_request"` categories that CDS's classifier doesn't:

| Condition | Category |
|---|---|
| `Timeout` exception or `http_status == 408` | `"timeout"` |
| `http_status == 401` | `"auth_error"` |
| `http_status == 404` | `"not_found"` |
| `400 <= http_status < 500` (excluding 404) | `"bad_request"` |
| `500 <= http_status < 600` | `"upstream_error"` |
| Anything else | `"system_error"` |

Note that `cms_get/post/patch/delete` call `classify_cms_error()` only to populate the `error.category` *attribute* on non-2xx responses — the actual error dict returned to the caller is built by `normalize_cms_error()`, which re-derives status-based branching independently (and happens to produce the same `error_type` strings for 401/404/4xx/5xx, but routes `Timeout` through a separate `except` clause rather than through `classify_cms_error`).

All four functions also have an `except requests.exceptions.ConnectionError` clause that **bypasses `normalize_cms_error` entirely**, returning `{"error_type": "system_error", "message": "Could not connect to CMS API.", "retryable": True}` — note `retryable: True` here, which differs from the generic/`"Other"` fallback inside `normalize_cms_error` (`retryable: False`). Any other unexpected exception is reported via `notice_err()` and **re-raised** (not normalized) — the tool-call layer turns that into `MCPToolError`.

---

## `mcp_app/cds/__init__.py`

### Tool registration

Each CDS submodule (`authors.py`, `categories.py`, `content.py`, `posts.py`, `publisher.py`, `sitemaps.py`, `static_files.py`, `tags.py`) exports:
```python
SCHEMAS: list[dict]       # list of MCP tool schema dicts
HANDLERS: dict[str, callable]  # tool_name → handler function
```

`__init__.py` aggregates at import time via explicit aliased imports (`from .posts import HANDLERS as _POSTS_HANDLERS`, etc. — not a loop or wildcard):
```python
TOOLS: list[dict] = _POSTS_SCHEMAS + _CATEGORIES_SCHEMAS + _TAGS_SCHEMAS + _AUTHORS_SCHEMAS \
    + _PUBLISHER_SCHEMAS + _CONTENT_SCHEMAS + _SITEMAPS_SCHEMAS + _STATIC_SCHEMAS

_HANDLER_REGISTRY: dict = {**_POSTS_HANDLERS, **_CATEGORIES_HANDLERS, **_TAGS_HANDLERS,
    **_AUTHORS_HANDLERS, **_PUBLISHER_HANDLERS, **_CONTENT_HANDLERS, **_SITEMAPS_HANDLERS, **_STATIC_HANDLERS}
```

### Per-tool concurrency tracking

```python
_active_calls: dict[str, int] = collections.defaultdict(int)  # tool_name → count
_active_calls_lock: threading.Lock = ...
```

`dispatch_cds_tool(credentials, name, args)` — **note the parameter order: `credentials` first, then `name`** (the doc previously had this backwards):

```python
@newrelic.agent.function_trace(name="dispatch_cds_tool", group="Tool")
def dispatch_cds_tool(credentials, name, args):
    add_attrs([("mcp.tool_name", name)])
    with _active_calls_lock:
        _active_calls[name] += 1
        concurrency = _active_calls[name]
    add_attrs([("mcp.tool_concurrency", concurrency)])
    record_custom_metric(f"Custom/Tool/{name}/active_calls", concurrency)
    try:
        handler = _HANDLER_REGISTRY.get(name)
        if handler is None:
            raise Exception(f"Unknown tool: {name}")     # NOT a returned error dict — see below
        with fn_trace(name, group="Tool"):
            return handler(credentials, args)
    except Exception as exc:
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        if http_status == 401:
            # Graceful degradation: CDS rejected the forwarded credentials mid-call
            return {"error": "auth_expired",
                    "message": "Your CDS credentials were rejected (HTTP 401). "
                               "Please re-authenticate: visit /connect or re-run the OAuth flow."}
        notice_err(exc, [("error.layer", "tool"), ("error.tool_name", name)])
        raise                                             # everything else propagates → MCPToolError
    finally:
        with _active_calls_lock:
            _active_calls[name] = max(0, _active_calls[name] - 1)
```

`concurrency` is attached as a transaction attribute (`mcp.tool_concurrency` via `add_attrs()`) and as the custom metric `Custom/Tool/<name>/active_calls`. No limit is enforced — it is an observability gauge, not a throttle.

**Two distinct error paths, not one:**
- **Unknown tool name** → raises a bare `Exception("Unknown tool: ...")`, which has no `.response` attribute, so `http_status` is `None`, the 401 branch is skipped, and the exception is re-raised → surfaces to the client as `MCPToolError` (`isError: true`), **not** a graceful `{"error_type": "not_found", ...}` degraded response.
- **CDS returned 401 mid-call** (credentials revoked/expired after auth) → caught and converted to a *degraded* result `{"error": "auth_expired", "message": "..."}`, which `_handle_tool_call` reports as `MCPToolDegraded` rather than a hard error.

Each handler call is wrapped in `fn_trace(name, group="Tool")` (a guarded `FunctionTrace` span). Handler signature: `def handler(credentials: dict, args: dict) -> dict`.

---

## `mcp_app/cms/__init__.py`

Same aggregation/concurrency-tracking shape as CDS (8 submodules: `categories`, `custom_components`, `custom_content_types`, `live_blog`, `media`, `posts`, `tags`, `validators`; `CMS_TOOLS` and `_HANDLER_REGISTRY` built the same way), **plus one addition**:

```python
CMS_TOOL_NAMES: frozenset = frozenset(_HANDLER_REGISTRY.keys())
```

Used in `dispatch.py` to route to CMS vs CDS without a linear scan.

**The exception handling in `dispatch_cms_tool` is simpler than CDS's — there is no 401-to-`auth_expired` special case.** Its `except Exception` block unconditionally calls `notice_err(exc, [("error.layer", "cms_tool"), ...])` and re-raises everything, including unknown-tool errors and CMS 401s — all surface as `MCPToolError`, not `MCPToolDegraded`. (CMS write tools route their upstream HTTP errors through `normalize_cms_error` *inside* the handler/`cms_client` layer before they ever reach `dispatch_cms_tool`, which is presumably why this dispatcher doesn't need its own 401 carve-out — but it does mean an unknown CMS tool name behaves differently from an unknown CDS tool name only in the `error.layer` attribute value, not in the client-visible result.)

### CMS write-op detection

`dispatch.py`'s `_is_cms_write(name)` (backed by `_CMS_READ_PREFIXES = ("list_", "get_", "validate_")`) treats a call as a "write op" if:
```python
name in CMS_TOOL_NAMES and not any(name.startswith(p) for p in ("list_", "get_", "validate_"))
```

`_cms_write_bucket(name)` then splits these into two independent 100-op-per-session
caps: `update_*`/`delete_*` calls count against the `update_delete_op_count`
field on the session's Redis stats hash (`mcp:session_stats:{id}`, via
`increment()`), everything else (`create_*`, `register_*`, `add_*`, `submit_*`)
counts against the `create_op_count` field.

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
       credentials=credentials,           # stored as JSON
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
  credentials    JSONB         {publisherId, apiKey, apiSecret}
  expires_at     TIMESTAMPTZ

oauth_token
  id            INTEGER  PK
  token         VARCHAR(128)  UNIQUE
  client_id     VARCHAR(64)   INDEX
  publisher_id  VARCHAR(64)   INDEX
  refresh_token VARCHAR(128)  UNIQUE  NULL ok
  credentials   JSONB         {publisherId, apiKey, apiSecret}
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
| 0008 | Adds indexed `publisher_id` column to `oauth_token`; moves `credentials` to a `TextField` and backfills `publisher_id` from existing JSON (this briefly routed through a Fernet-encrypting field — see 0012) |
| 0009 | Renames / adds `publisher_id` indexed column to `oauth_token` |
| 0010 | `SeparateDatabaseAndState(DeleteModel('AIClient'))` — state-only; no DROP (table already gone from 0007) |
| 0011 | Introspects tables, drops orphan `encrypted_api_secret`, `encrypted_api_key`, `encrypted_publisher_id` columns if present (added by now-reverted migrations) |
| 0012 | Removes credential encryption: clears `oauth_code`/`oauth_token` (sample data only) and converts `credentials` from `TextField` back to plain `JSONField` |

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
  local.py     — import base.*, override for local dev
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
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 10 * 365 * 24 * 3600   # 10-year ceiling; sessions otherwise live
                                            # until /auth/logout (session_ttl_seconds = -1)

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
import os

from django.core.wsgi import get_wsgi_application
import newrelic.agent

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "publive_mcp.settings")

application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
```

Note there's no `newrelic.agent.initialize()` call anywhere in the codebase, and `entrypoint.sh` execs `gunicorn publive_mcp.wsgi` directly — not via the `newrelic-admin run-program` wrapper that the agent's own docs recommend for auto-initialization. The only New Relic touchpoint at process start is the `import newrelic.agent` + `WSGIApplicationWrapper(...)` here. Whether the agent is actually harvesting and reporting data in production therefore hinges on something outside this repo (e.g. an env var or platform-level wrapper not visible in source) — worth confirming directly against the live New Relic dashboard rather than assuming from this file alone. The WSGI wrapper instruments every request **before** Django's middleware stack, giving accurate wall-clock timings that include middleware overhead. Gunicorn (`entrypoint.sh`) is pointed at `publive_mcp.wsgi`.

---

## `mcp_app/nr_utils.py` — guarded New Relic helpers

All New Relic calls go through these wrappers. Each checks `if _nr is None` or `if not _current_transaction()` and silently no-ops. This means the app runs cleanly with no `NEW_RELIC_LICENSE_KEY` — no agent, no crashes.

| Helper | What it wraps |
|---|---|
| `set_txn_name(name, group)` | `newrelic.agent.set_transaction_name()` |
| `add_attrs(pairs)` | `newrelic.agent.add_custom_attribute()` (looped per pair) |
| `add_span_attrs(pairs)` | `newrelic.agent.add_custom_span_attribute()` — attaches to the current span (trace waterfall) rather than the transaction |
| `notice_err(exc, attrs)` | `newrelic.agent.notice_error(exc, attributes=...)` — attrs land on the error event itself (`FROM TransactionError`), not just the transaction |
| `record_event(type, params)` | `newrelic.agent.record_custom_event()` — does not require an active transaction, only `_nr is not None` |
| `record_metric(name, val)` | `newrelic.agent.record_custom_metric()` |
| `get_linking_metadata()` | `newrelic.agent.get_linking_metadata()` — returns `{}` outside a transaction (e.g. from `event_stream()`); keys include `trace.id`, `span.id`, `entity.guid` |
| `suppress_apdex()` | `newrelic.agent.suppress_apdex_metric()` |
| `suppress_trace()` | `newrelic.agent.suppress_transaction_trace()` |
| `fn_trace(name, group)` | Context manager wrapping `newrelic.agent.FunctionTrace` — no-ops (yields plainly) outside a transaction |
| `suppress_apdex()` | `newrelic.agent.suppress_apdex_metric()` |
| `suppress_trace()` | `newrelic.agent.suppress_transaction_trace()` |
| `get_linking_metadata()` | `newrelic.agent.get_linking_metadata()` → `{}` fallback |

`SERVER_ENV` reads `RAILWAY_ENVIRONMENT` (or `"local"`). `SERVER_VERSION` reads `SERVER_VERSION` env var (or `"1.0.0"`). Both are attached to every custom event.
