# MCP Protocol

## What this server implements

Model Context Protocol (MCP) version `2024-11-05`. The server exposes 61 tools (22 CDS read + 39 CMS write) to AI clients over two transports. It speaks JSON-RPC 2.0 over both.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/mcp` | Open an SSE session (long-lived streaming transport) |
| `POST` | `/mcp` | Stateless HTTP request (single or batch JSON-RPC) |
| `POST` | `/mcp/message?sessionId=<id>` | Send a message into an open SSE session |
| `GET` | `/.well-known/oauth-protected-resource` | RFC 9728 resource metadata (MCP auth discovery) |
| `GET` | `/.well-known/oauth-authorization-server` | OAuth 2.0 server metadata (MCP auth discovery) |

All MCP endpoints are `@csrf_exempt` — MCP clients are machine clients that do not send cookies with a CSRF token. CSRF protection only applies to human browser forms.

---

## Transport selection

`POST /mcp` with `Content-Type: application/json` enters the **Streamable HTTP** transport.  
`GET /mcp` enters the **SSE** transport.

The choice is made in `mcp_app/views.py:mcp_endpoint()` after authentication passes:

```
request.method == "GET"  →  open_sse_connection()   (SSE)
request.method == "POST" →  handle_http_request()   (HTTP)
```

**Why two transports:** Claude Desktop and the MCP Python SDK use SSE (the original 2024-11-05 protocol). Newer clients and programmatic API callers use stateless HTTP POST, which is simpler to proxy and doesn't require persistent connections. Both transports speak the same JSON-RPC — only the delivery mechanism differs.

---

## Transport 1: SSE (Server-Sent Events)

### How it works

```
Client                          Server
  │                               │
  │── GET /mcp ──────────────────>│  (auth: Bearer or session cookie)
  │<─ event: endpoint             │  data: https://.../mcp/message?sessionId=<uuid>
  │<─ : keepalive (every 25s)     │
  │                               │
  │── POST /mcp/message ─────────>│  ?sessionId=<uuid>  body: JSON-RPC
  │<─ HTTP 200 {"ok": true}       │  (ACK only — response comes on the SSE stream)
  │<─ event: message              │  data: JSON-RPC response
  │                               │
  │   ... more tool calls ...     │
  │                               │
  │── disconnect ────────────────>│  stream finally block fires → SSESessionClose
```

### Session lifecycle in detail

**Open (`GET /mcp`):**
1. Auth resolved (Bearer or session cookie).
2. UUID session ID generated.
3. Session registered in Redis via `register_session()` (`mcp_app/transport/redis_session_store.py`) — encrypted credentials + a capped message queue (`mcp:session_queue:{id}`, see `redis_message_queue.py`), both keyed by session ID and shared across every worker/replica.
4. Session stats hash initialised in Redis via `init_stats()` (`mcp_app/protocol/redis_session_stats.py`) — tool count, timings, token estimates.
5. First SSE event sent: `event: endpoint` with the `POST /mcp/message` URL including the session ID. The client reads this URL and uses it for all subsequent messages.
6. `StreamingHttpResponse` holds the connection open; a gunicorn thread blocks on `pop_message()` (Redis `BLPOP`) for the session lifetime.
7. Every 25 seconds without a message: `": keepalive\n\n"` is yielded to prevent proxy/load-balancer idle timeouts.

**Message (`POST /mcp/message?sessionId=<id>`):**
1. `sessionId` looked up via `get_session()` — a Redis read, reachable from any process. If missing → `MCPSessionMissing` event, HTTP 400.
2. JSON-RPC body dispatched synchronously via `dispatch_jsonrpc()`.
3. Response pushed onto the Redis-backed queue via `push_message()` with a 30-second timeout (polls `LLEN` with backoff — Redis has no blocking-push primitive). If still full after 30s → response dropped, `Custom/MCP/queue_overflow_count` incremented.
4. HTTP 200 `{"ok": true}` returned immediately (ACK). The response travels back on the SSE stream — possibly served by a *different* process than the one that handled this POST — not in this HTTP response.

**Close (client disconnect):**
1. `event_stream()` generator's `finally` block fires.
2. Session and queue entries removed from Redis (`close_session()`, `delete_queue()`); stats hash atomically snapshotted and removed (`pop_stats()`).
3. `MCPSessionSummary` and `SSESessionClose` events emitted with full session roll-up.
4. If `tool_count == 0`: `MCPSessionAbandoned` also emitted.

### Why the queue has a 100-message cap (`MCP_QUEUE_MAXSIZE`)

The queue is bounded so a stalled or slow SSE consumer can't grow Redis memory unboundedly. 100 messages is far more than any single AI session would queue — a full queue means the client has stopped reading and the session is effectively dead. Override via `MCP_QUEUE_MAXSIZE` env var (passed through to `push_message()`'s `maxsize`).

### Cross-process routing (formerly: why SSE required exactly 1 gunicorn worker)

SSE session state — the session registry, message queues, and stats — used to
live in `_sse_sessions` / `session_stats`, in-process dicts in a single
worker's memory. `POST /mcp/message` had to reach the **exact same process**
that held the queue for that `sessionId`; with multiple workers the
ALB/Railway router could send the POST to a different worker than the one
serving the `GET /mcp` stream → `MCPSessionMissing`. Exactly 1 worker was the
only way to guarantee both requests shared one dict.

That state now lives in Redis (`mcp_app/transport/redis_session_store.py`,
`redis_message_queue.py`, `mcp_app/protocol/redis_session_stats.py`) — a
session opened on one process is fully visible to, and routable from, every
other process or replica that shares the same `REDIS_URL`. `GET /mcp` and
`POST /mcp/message` for the same session can now land on different
workers/replicas without breaking. The codebase still runs `-w 1` for now (see
`docs/deployment.md`) as a staged rollout — the worker count is a capacity knob
to raise once the Redis-backed routing is verified in production, not an
architectural pin anymore.

---

## Transport 2: Streamable HTTP (stateless POST)

Every `POST /mcp` is a self-contained request. No session state is held between calls.

**Single request:**
```json
POST /mcp
{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_posts","arguments":{}}}
```
Returns a single JSON-RPC response object.

**Batch request** (array body):
```json
POST /mcp
[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}},
 {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}]
```
Returns a JSON array of responses (notifications with no `id` are omitted — returns HTTP 202 if all are notifications).

**Session ID for HTTP transport:** Derived from the Bearer token (SHA-256 prefix) or Django session key — not a UUID. Same token = same session ID across requests, enabling per-session metrics even without a persistent connection.

---

## Authentication on MCP endpoints

`mcp_app/protocol/auth.py:resolve_credentials()` runs before every transport handler:

```
Authorization: Bearer <token>  →  OAuthToken DB lookup  (PKCE flow)
No Bearer header               →  Django session cookie  (browser flow)
Neither                        →  401 Unauthorized
```

The 401 response always includes:
- `WWW-Authenticate: Bearer realm="...", resource_metadata=".../.well-known/oauth-protected-resource"`
- `{"authUrl": ".../connect", "error": "..."}` body

MCP clients read `WWW-Authenticate` to start the OAuth flow automatically. `authUrl` is for human browser users.

Credentials are **not re-validated** against the CDS API on each request — that would add ~500ms per tool call. They are validated once at login/authorize and stored encrypted in the DB or session.

---

## JSON-RPC dispatch

`mcp_app/protocol/dispatch.py:dispatch_jsonrpc()` routes every request:

| `method` | Handler | Notes |
|---|---|---|
| `initialize` | Inline | Returns `protocolVersion`, `capabilities`, `serverInfo`. Stores protocol version in session for NR attribution. |
| `tools/list` | Inline | Returns all 40 tools (TOOLS + CMS_TOOLS combined). |
| `tools/call` | `_handle_tool_call()` | See below. |
| `ping` | Inline | Returns `{}`. Used by clients to keep the connection alive. |
| Known-unimplemented | Inline | `sampling/createMessage`, `roots/list`, `resources/*`, `prompts/*`, `completion/complete`, `logging/setLevel` — returns `-32601 Method not found` without logging a warning (these are expected from MCP clients that probe capabilities). |
| Unknown | Inline | Returns `-32601`, logs a warning, emits `MCPUnknownMethod` NR event. |
| Notification (no `id`) | — | Returns `None`; no response sent. HTTP transport returns 202. |

---

## Tool call pipeline

Every `tools/call` goes through this sequence in `_handle_tool_call()`:

```
1. extract_prompt_for_tool_call()   — extract user prompt from headers/meta/args
2. record_prompt_observability()    — emit MCPPrompt NR event (rate-limited to 1000/min)
3. _validate_tool_args()            — check required fields + type constraints vs inputSchema
4. CMS write-op rate limit check    — max 100 creates + 100 update/deletes per SSE session (independent buckets)
5. dispatch_cds_tool() or           — call the actual tool handler
   dispatch_cms_tool()
6. Degraded check                   — result dict with error_type key → MCPToolDegraded
7. Success path                     — update session stats, emit metrics
8. Exception path                   — MCPToolError, return isError:true to client
```

### Step 3: Input validation

Every tool's `inputSchema` is pre-compiled into `_SCHEMA_REGISTRY` at module load (not per request). Validation checks:
- Required fields present and non-empty.
- Field types match (`string`, `integer`, `boolean`, `object`, `array`, `number`).
- `bool` is rejected for `integer` fields (Python `bool` is a subclass of `int` — without this check, `true` would pass an integer field).
- `minLength` for string fields.

A validation failure returns `isError: true` to the client **without calling the tool**. This saves an API round-trip and gives the AI a clear retry signal.

### Step 4: CMS write-op rate limit

CMS write tools (anything that isn't `list_*`, `get_*`, `validate_*`) are split into two independent per-SSE-session buckets by `_cms_write_bucket()`:
- **create** — `create_*`, `register_*`, `add_*`, `submit_*` → `create_op_count` field on the session's Redis stats hash
- **update_delete** — `update_*`, `delete_*` → `update_delete_op_count` field on the same hash

Each bucket is capped at 100; crossing either cap returns a rate-limit error for *that* bucket only — e.g. hitting 100 creates doesn't block updates/deletes, and vice versa. The limits are per-session, not per-publisher or per-minute, so a new session resets both counters. The counters are atomic Redis `HINCRBY`s (`increment()` in `mcp_app/protocol/redis_session_stats.py`) — correct under concurrent processes, not just concurrent threads.

**Stateless HTTP transport is not covered by this check.** The counters live on the per-session Redis stats hash, which is only created when an SSE session opens (`GET /mcp`'s `init_stats()`). `POST /mcp` calls have no such hash, so `increment()` returns `None` (mirroring the old `if stats is not None` guard), both counters are treated as `0` on every call, and the limits never trigger for that transport.

**Why split + 100:** Prevents runaway AI agents from bulk-modifying content in a single session, while keeping create flows (e.g. bulk-importing posts) from starving update/delete flows (e.g. bulk-editing existing ones) and vice versa. 100 per bucket is generous for normal editorial workflows; anything larger is likely an agent loop.

### Step 6: Degraded vs error

| State | What happened | `isError` | NR event |
|---|---|---|---|
| **Success** | Tool returned a result dict with no `error`/`error_type` key | `false` | — |
| **Degraded** | Tool returned normally but result contains `error_type` (upstream API rejected the call) | `false` | `MCPToolDegraded` |
| **Error** | Tool raised a Python exception | `true` | `MCPToolError` |

Degraded is not an exception. The tool ran, the CDS/CMS API responded, but the response was an error (e.g. 404 not found, 401 re-auth required). The AI client receives the error message as text content and can decide what to do. A Python exception means the tool itself crashed.

---

## Credential resolution for tools

After auth, `credentials` is a dict `{publisherId, apiKey, apiSecret}`. It is passed directly to every tool handler. Tools pass it to `cds_get()` or `cms_*()`, which build the publisher-scoped base URL:

```
https://cds-beta.thepublive.com/publisher/{publisherId}/posts/
```

No credential lookup happens during tool dispatch — the dict travels as an opaque blob from auth resolution through to the HTTP client.

---

## Session ID across transports

| Client type | Session ID source | Stability |
|---|---|---|
| SSE | UUID generated at `GET /mcp` | Unique per connection |
| HTTP + Bearer | SHA-256 prefix of the token | Same across all requests with the same token |
| HTTP + session cookie | Django session key | Same across all requests in the same browser session |
| HTTP + no auth | `anon-<uuid8>` | Transient per request |

This means NR queries on `session_id` correctly group all tool calls from one AI conversation even over the stateless HTTP transport.

---

## Client identification

`identify_mcp_client()` parses `User-Agent` and maps known prefixes to human-readable names:

| User-Agent prefix | Identified as |
|---|---|
| `claude/` | Claude Desktop |
| `cursor/` | Cursor |
| `python-httpx/` | Python HTTPX Client |
| `python-requests/` | Python Requests Client |
| `mcp/` | MCP Python SDK |
| `anthropic/` | Anthropic SDK |

The client name is attached to every NR transaction and session event so dashboards can break down usage by client type.

---

## Adding a new JSON-RPC method

1. Add a branch in `dispatch_jsonrpc()` in `mcp_app/protocol/dispatch.py`.
2. Call `set_txn_name("MCP/<name>", group="MCP")` for NR transaction naming.
3. Return `jsonrpc_ok(id_, {...})` for success or `jsonrpc_error(id_, code, message)` for failure.
4. If it's a known-but-unimplemented MCP method that clients probe for, add it to `_UNIMPLEMENTED_METHODS` instead (returns -32601 silently).

## Adding a new tool

See `CLAUDE.md` — the dispatch is data-driven from `TOOLS` / `CMS_TOOLS`. No changes to this dispatch layer needed.
