# New Relic — Publive MCP Server
## Production Observability Reference

What New Relic does for this project, where each piece lives in the code, and how to query it.

---

## Changelog — latest iteration (doc audit pass)

| What changed | File | Why |
|---|---|---|
| Fixed broken NRQL: `FROM Metric, Transaction` → two separate widget queries | `NewRelic.md` | NRQL does not allow multiple event types in one FROM clause; the combined query was a parse error in the NR query builder |
| Fixed broken NRQL: `FROM SSESessionOpen, MCPSessionAbandoned` → metric-based single-source query | `NewRelic.md` | Same multi-source issue; replaced with `sum(Custom/MCP/session_abandon_count) / count(*) FROM SSESessionOpen` |
| Added 6 missing alert YAML specs (alerts #20–25) | `NewRelic.md` | Coverage audit listed queue overflow, session abandonment, OAuth stopped, probe wave, prompt drop, cross-worker routing as "add alert" with no formal spec |
| Updated audit row 18: "15 dashboards" → "17 dashboards, ~175 widgets, 17 dimensions" | `NewRelic.md` | Dashboards 16 and 17 were added in the previous iteration but the audit count was not updated |
| Updated audit row 19: alert count and dimension count corrected | `NewRelic.md` | 6 new alert specs added; dimension count was 13, now 18 |
| Updated audit row 25: "7 event types" → "10 event types" | `NewRelic.md` | MCPRateLimit, MCPSessionAbandoned, MCPSessionMissing were added but the count was not updated |
| Added audit rows 67–76 | `NewRelic.md` | New dashboards 16–17, auth metrics, queue overflow, thread profiler, Django DB, SQL tracing, deployment markers, SIGTERM handler, and 6 new alert specs now tracked |
| Added `active_threads` partial-coverage note in §5 Custom Metrics | `NewRelic.md` | Metric is emitted from HTTP POST path only; SSE path uses transaction attribute instead |
| Added §Thread Profiler section | `NewRelic.md` | `thread_profiler.enabled = true` in newrelic.ini was undocumented |
| Added §Django Auto-Instrumentation section | `NewRelic.md` | ORM, middleware, URL routing timing auto-captured but not documented |
| Added §SQL Query Tracing section | `NewRelic.md` | `record_sql = obfuscated` and `explain_enabled = true` were undocumented |

## Changelog — previous iteration

| What changed | File | Why |
|---|---|---|
| `notice_err()` now passes `attributes=` to `notice_error()` | `nr_utils.py` | Error attributes now appear in `FROM TransactionError`, not only `FROM Transaction` |
| Added `record_metric()` guarded wrapper | `nr_utils.py` | Prevents crashes when NR agent absent; use instead of direct `record_custom_metric()` |
| Added `SERVER_ENV` / `SERVER_VERSION` constants | `nr_utils.py` | All custom events now carry `env` and `server_version` tags |
| Added `suppress_apdex()` / `suppress_trace()` helpers | `nr_utils.py` | SSE sessions no longer pollute the Apdex score or slow-transaction list |
| SSE `GET /mcp` now suppresses Apdex + transaction trace | `views.py` | Long-lived SSE sessions were marking every session as "Frustrated" in Apdex |
| Added `_CLIENT_NAME_MAP` for User-Agent normalisation | `views.py` | `mcp.client_name` now shows "Claude Desktop" instead of "claude" |
| `_get_credentials()` now has a function trace | `views.py` | OAuthToken DB lookup is visible in the APM waterfall |
| `extract_prompt_for_tool_call()` now has a function trace | `prompt_capture.py` | Prompt-parsing time is visible in the APM waterfall |
| Added bounded SSE queue (`maxsize=MCP_QUEUE_MAXSIZE`, default 100) | `views.py` | Prevents unbounded memory growth from a slow SSE consumer |
| Added `MCPRateLimit` event + `Custom/MCP/rate_limited_count` metric | `views.py` | Rate-limited requests are now queryable, alertable, and visible in dashboards |
| Added `Custom/MCP/prompt_event_dropped_count` metric | `views.py` | Observability degradation from the MCPPrompt rate limiter is now measurable |
| Added `MCPSessionAbandoned` event + `Custom/MCP/session_abandon_count` metric | `views.py` | Sessions with 0 tool calls are now a distinct, alertable signal |
| Added `MCPSessionMissing` event + `Custom/MCP/sse_session_missing_count` metric | `views.py` | Cross-worker routing failures (session on wrong gunicorn worker) are quantifiable |
| Added `Custom/MCP/queue_overflow_count` metric | `views.py` | Queue overflow (slow SSE consumer) is now alertable |
| `MCPToolError` + `MCPToolDegraded` now include `session_trace_id` | `views.py` | Error events can be filtered by session without joining via `session_id` |
| All custom events now include `env` + `server_version` fields | All | Multi-environment NRQL filtering; deployment correlation |
| `MCPSessionSummary` now includes `tool_sequence` | `views.py` | Session replay without querying N separate Transaction records |
| `_session_stats` initialised with `tool_sequence: []` | `views.py` | Ordered tool name list accumulated on every tool call (success + error) |
| Added `Custom/Auth/*` metrics to auth flows | `auth_app/views.py` | Auth layer now has alertable metrics for token issuance and failure rates |
| Added SIGTERM handler in `wsgi.py` | `wsgi.py` | NR harvest is flushed before Railway kills the process — no more lost session events |
| Added `error_collector.ignore_classes` to `newrelic.ini` | `newrelic.ini` | `BrokenPipeError` / `ConnectionResetError` from SSE client disconnects no longer inflate error rate |

---

## How the agent starts

`publive_mcp/wsgi.py` initialises the agent and wraps the Django app before the first request arrives. Every inbound HTTP request automatically becomes a New Relic transaction from this point on — no per-view wiring needed.

```python
newrelic.agent.initialize("newrelic.ini")
application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
```

---

## 1. Transactions — know what your server is doing

Every request gets a meaningful name instead of the raw URL, so the APM summary screen shows clean groupings.

| What you see in NR | Triggered by |
|--------------------|--------------|
| `MCP/list_posts`, `MCP/get_post`, … | Any tool call |
| `MCP/initialize`, `MCP/tools_list`, `MCP/ping` | MCP handshake methods |
| `Transport/SSE` | SSE stream open |
| `CDS/posts_`, `CDS/post_{id}_`, … | Every outbound CDS API call — named by slugified path |
| `Auth/pkce_authorize`, `Auth/pkce_token` | OAuth PKCE flow |
| `Auth/oauth_register` | Client self-registration |
| `Auth/session_login`, `Auth/session_verify` | Session auth |

**Where:** `set_txn_name()` calls in `mcp_app/views.py`, `mcp_app/cds_client.py`, and `auth_app/views.py`.

---

## 2. Function Traces — see where time is spent inside a request

Breaks a single transaction into named segments so the APM waterfall shows exactly which layer was slow.

```
mcp_endpoint [Transport]
  └── call_tool [Tool]
        └── list_posts [Tool]
              └── cds_get [CDS]   ← where most latency lives
```

Every one of the 18 tools gets its own segment. Auth credential validation (`_validate_cds`) is also traced so a slow CDS call during login is visible.

**Where:** `@newrelic.agent.function_trace(...)` decorators and `fn_trace()` context managers in `views.py`, `tools.py`, `cds_client.py`, `auth_app/views.py`.

---

## 3. Custom Attributes — filter and slice everything in NRQL

Key-value pairs attached to each transaction. Let you answer questions like "which publisher is slowest?" or "which AI client causes the most errors?"

### MCP Layer (`mcp.*`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `mcp.session_id` | string | Stable ID for the session (session cookie, oauth hash, or anon-UUID) |
| `mcp.transport` | string | `sse` or `http` |
| `mcp.tool_name` | string | Tool being invoked |
| `mcp.tool_input` | string | JSON of tool args (truncated to 500 chars) |
| `mcp.tool_result_status` | string | `success`, `degraded`, or `error` |
| `mcp.tool_is_error` | boolean | True when tool raised an exception |
| `mcp.tool_is_degraded` | boolean | True when tool returned a structured `{"error":…}` dict without raising |
| `mcp.degraded_reason` | string | Degraded error key: `upstream_timeout`, `invalid_input`, `not_found`, `not_configured`, `auth_expired` |
| `mcp.tool_concurrency` | integer | Number of in-flight calls to this specific tool at the moment it was invoked |
| `mcp.tool_args_count` | integer | Number of arguments passed |
| `mcp.tool_response_size` | integer | Response JSON size in bytes |
| `mcp.tool_output_char_count` | integer | Character count of tool output |
| `mcp.tool_duration_ms` | float | Tool execution time in ms |
| `mcp.tool_output_preview` | string | First 500 chars of output (or error message) |
| `mcp.error_category` | string | `timeout`, `auth_error`, `client_error`, `upstream_error`, `system_error` |
| `mcp.tool_fallback` | string | Set when a tool falls back (e.g. `"footer"`) |
| `mcp.tool_fallback_reason` | string | Why fallback fired (e.g. `"endpoint_unavailable"`) |
| `mcp.tool_auth_error` | boolean | True when CDS returned 401 for an authenticated session |
| `mcp.client_name` | string | AI client name parsed from User-Agent |
| `mcp.client_version` | string | AI client version parsed from User-Agent |
| `mcp.protocol_version` | string | MCP protocol version from initialize |
| `mcp.thread_active_count` | integer | Active threads at request time (per gunicorn worker) |
| `mcp.request_size_bytes` | integer | Inbound request body size |
| `mcp.session_queue_depth` | integer | SSE message queue depth after each message put |
| `mcp.active_sessions` | integer | Active SSE sessions at time of SSE open |
| `mcp.session_tool_seq` | integer | Tool call number within this SSE session (1-based) |
| `mcp.rate_limited` | boolean | True when IP was rate-limited for unauthenticated requests |
| `mcp.client_ip` | string | Client IP (honours X-Forwarded-For) |
| `mcp.prompt_id` | string | UUID for this prompt observation |
| `mcp.prompt_text` | string | User/LLM prompt text (truncated to 2000 chars) |
| `mcp.prompt_source` | string | Where prompt was found: `header`, `meta.*`, `params.prompt`, `arguments._prompt`, `tool_args`, `client_not_provided` |
| `mcp.prompt_char_count` | integer | Character length of the prompt |
| `mcp.estimated_prompt_tokens` | integer | `prompt_char_count ÷ 4` — input token cost proxy |
| `mcp.estimated_output_tokens` | integer | `tool_output_char_count ÷ 4` — output token cost proxy (success path only) |
| `mcp.tool_start_offset_ms` | float | Milliseconds from SSE session open to this tool call — timeline anchor |
| `mcp.ai_think_time_ms` | float | Gap between previous tool response enqueue and this call start — AI processing time proxy |
| `mcp.session_trace_id` | string | `trace.id` of the SSE-open transaction — stable key for cross-transaction NRQL joins |
| `mcp.jsonrpc_error_code` | integer | JSON-RPC error code for unimplemented or unknown methods (always `-32601`) |
| `mcp.unknown_method` | string | Method name for truly unknown JSON-RPC methods — not set for the known-unimplemented set (`sampling/createMessage`, etc.) |
| `mcp.sse_session_missing` | boolean | `true` when `mcp_message` receives a `sessionId` with no active SSE queue (stale or invalid client) |
| `mcp.jsonrpc_id` | string | JSON-RPC request ID for the tool call — set when the request ID is non-null (from `prompt_capture.py`) |
| `mcp.queue_overflow` | boolean | `true` when the bounded SSE queue (`maxsize=MCP_QUEUE_MAXSIZE`) is full after a 30 s wait — response dropped |

### CDS Layer (`cds.*`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `cds.endpoint` | string | CDS API path (e.g. `/posts/`) |
| `cds.publisher_id` | string | Publisher making the request |
| `cds.http_status` | integer | CDS HTTP response code |
| `cds.latency_ms` | float | CDS round-trip time in ms |
| `cds.response_size_bytes` | integer | CDS response size |
| `cds.retry_count` | integer | Number of retries performed (0 = first-try success) |
| `cds.retried` | boolean | True if at least one retry occurred |
| `cds.timed_out` | boolean | True if the request timed out (all attempts) |
| `cds.url` | string *(span-only)* | Full CDS request URL — set via `add_custom_span_attribute()`; visible in the NR trace waterfall but not in `FROM Transaction` queries |
| `cds.path` | string *(span-only)* | URL path component of the CDS request — set via `add_custom_span_attribute()` |

### Auth Layer (`auth.*`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `auth.flow` | string | `oauth_pkce`, `oauth_register`, `session` |
| `auth.result` | string | `success` or `failure` |
| `auth.failure_reason` | string | `missing_params`, `cds_auth_failed`, `invalid_session`, `expired_token`, `invalid_pkce` |
| `auth.publisher_id` | string | Publisher being authenticated |
| `auth.client_id` | string | OAuth client ID |
| `auth.cds_validation_status` | integer | HTTP status from CDS during auth |
| `auth.cds_validation_ms` | float | CDS auth validation latency |

### Error Layer (`error.*`)

| Attribute | Type | Description |
|-----------|------|-------------|
| `error.layer` | string | `transport`, `tool`, `cds`, `auth` |
| `error.tool_name` | string | Tool that failed |
| `error.cds_endpoint` | string | CDS path that failed |
| `error.http_status` | integer | HTTP status from the failure |
| `error.retry_count` | integer | Retries before giving up |
| `error.category` | string | `timeout`, `auth_error`, `client_error`, `upstream_error`, `system_error` — consistent with `mcp.error_category` |

**Where:** `add_attrs()` calls throughout `views.py`, `tools.py`, `cds_client.py`, `auth_app/views.py`.

---

## 4. Custom Events — dedicated tables for specific things

Separate event types you query with `FROM <EventType>`. Useful for things that don't map neatly to a single transaction.

> **All events** now carry `env` (Railway environment or `DJANGO_ENV`, e.g. `"production"`) and `server_version` (e.g. `"1.0.0"`) so you can filter events by environment and correlate behaviour changes with deployments.

### Core events

| Event | When it fires | Key fields |
|-------|--------------|------------|
| `MCPPrompt` | Every tool call (up to 1 000/min rate limit) | `prompt_id`, `prompt_text`, `tool_name`, `session_id`, `publisher_id`, `jsonrpc_id`, `client_name`, `prompt_source`, `prompt_char_count`, `estimated_prompt_tokens`, `trace_id`, `span_id`, `env`, `server_version` |
| `MCPToolError` | Tool call raises an exception | `tool_name`, `publisher_id`, `error_type`, `error_message`, `error_category`, `session_id`, **`session_trace_id`**, `prompt_id`, `prompt_text`, `duration_ms`, `tool_input`, `tool_start_offset_ms`, `ai_think_time_ms`, `trace_id`, `span_id`, `env`, `server_version` |
| `MCPToolDegraded` | Tool returns structured `{"error":…}` dict (no exception raised) | `tool_name`, `publisher_id`, `degraded_reason`, `session_id`, **`session_trace_id`**, `prompt_id`, `duration_ms`, `tool_input`, `tool_start_offset_ms`, `ai_think_time_ms`, `trace_id`, `span_id`, `env`, `server_version` |
| `MCPUnknownMethod` | Client sends truly unknown JSON-RPC method (not in known-unimplemented set) | `method`, `session_id`, `jsonrpc_id`, `env`, `server_version` |
| `MCPSessionSummary` | SSE session closes | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms`, `total_estimated_input_tokens`, `total_estimated_output_tokens`, `total_estimated_tokens`, `server_work_pct`, `session_client_name`, `session_trace_id`, `active_sessions_remaining`, **`tool_sequence`**, `env`, `server_version` |
| `SSESessionOpen` | SSE client connects | `session_id`, `publisher_id`, `active_threads`, `active_sessions`, `trace_id`, `span_id`, `env`, `server_version` |
| `SSESessionClose` | SSE stream ends | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms`, `session_trace_id`, `env`, `server_version` |

### New events

| Event | When it fires | Key fields | Why it matters |
|-------|--------------|------------|----------------|
| `MCPRateLimit` | An unauthenticated IP is rate-limited (HTTP 429) | `client_ip`, `retry_after_seconds`, `env`, `server_version` | Probe waves and misconfigured clients are now queryable, not just logged |
| `MCPSessionAbandoned` | SSE session closes with 0 tool calls | `session_id`, `publisher_id`, `duration_ms`, `session_client_name`, `session_trace_id`, `env`, `server_version` | Session funnel drop-off is now a distinct, alertable signal |
| `MCPSessionMissing` | `mcp_message` POST arrives for a `sessionId` with no registered SSE queue | `session_id`, `env`, `server_version` | Cross-worker routing failures (session on worker A, request arriving at worker B) are now quantifiable |

> **`MCPToolError` / `MCPToolDegraded` — `session_trace_id` added:** previously these events had `trace_id` and `span_id` (their own tool-execution trace) but not the session anchor. Adding `session_trace_id` allows `FROM MCPToolError WHERE session_trace_id = 'X'` without joining via the noisier `session_id` string.

> **`MCPSessionSummary` — `tool_sequence` added:** compact ordered string of tool names called this session (e.g. `"list_posts,get_post,get_category"`, truncated to 500 chars). Enables session replay and "what did this AI workflow do?" debugging without joining N Transaction records.

> **Note:** Known-but-unimplemented MCP methods (`sampling/createMessage`, `roots/list`, `resources/*`, `prompts/*`, `completion/complete`, `logging/setLevel`) are logged at DEBUG and return `-32601` without firing `MCPUnknownMethod` — they're expected from compliant clients.

**Where:** `record_event()` calls in `views.py` and `prompt_capture.py`.

---

## 5. Custom Metrics — long-retention numbers for alerting

Unlike events (30-day retention), custom metrics keep data for 13 months and can drive alert policies directly.

### MCP-level

| Metric | Measures |
|--------|----------|
| `Custom/MCP/tool_call_count` | Clean successful tool calls (no exception, no degraded result) |
| `Custom/MCP/tool_error_count` | Total tool errors (exceptions) |
| `Custom/MCP/tool_degraded_count` | Total degraded tool results (structured `{"error":…}` dict returns) |
| `Custom/MCP/active_threads` | Thread count per worker at request time *(emitted from HTTP POST path only — SSE sessions set `mcp.thread_active_count` as a transaction attribute at session open but do not emit this metric; use `max(mcp.thread_active_count) FROM Transaction` for the widest coverage)* |
| `Custom/MCP/active_sessions` | Active SSE sessions (emitted on open and close) |
| `Custom/MCP/session_queue_depth` | SSE message queue depth after each enqueue |
| `Custom/MCP/queue_wait_ms` | Time a message sat in the SSE queue before the generator consumed it |
| `Custom/MCP/fallback_count` | Times a tool fell back to an alternate endpoint |
| `Custom/MCP/unauth_tracker_size` | Unique IPs in the rate-limiter dict (leak proxy) |
| `Custom/MCP/rate_limited_count` | *(new)* Unauthenticated IP rate-limit hits (429 responses) — alert when non-zero during business hours |
| `Custom/MCP/prompt_event_dropped_count` | *(new)* MCPPrompt events dropped by the 1 000/min rate limiter — alert when drop rate > 5 % of tool calls |
| `Custom/MCP/session_abandon_count` | *(new)* SSE sessions closed with 0 tool calls — alert when abandon rate > 20 % of opens |
| `Custom/MCP/sse_session_missing_count` | *(new)* `mcp_message` POST with no matching SSE session — non-zero in single-worker deploys; rising count signals multi-worker routing failures |
| `Custom/MCP/queue_overflow_count` | *(new)* SSE queue full after 30 s — the SSE consumer (AI client) was too slow; response was dropped |

### Per-tool

| Metric | Measures |
|--------|----------|
| `Custom/Tool/{name}/call_count` | Invocations per tool (success + degraded + error) |
| `Custom/Tool/{name}/duration_ms` | Per-tool execution latency (clean success and degraded paths) |
| `Custom/Tool/{name}/error_duration_ms` | Execution latency on the exception path (timeout/error latency distribution) |
| `Custom/Tool/{name}/error_count` | Per-tool exception count |
| `Custom/Tool/{name}/degraded_count` | Per-tool degraded result count |
| `Custom/Tool/{name}/active_calls` | Current in-flight concurrency for this tool (gauge) |

### CDS-level

| Metric | Measures |
|--------|----------|
| `Custom/CDS/latency_ms` | CDS API round-trip time |
| `Custom/CDS/response_size_bytes` | CDS response size |
| `Custom/CDS/error_count` | Total CDS failures |
| `Custom/CDS/timeout_count` | CDS calls that timed out (all attempts) |
| `Custom/CDS/retry_count` | CDS calls that required at least one retry |

### Auth-level *(new)*

| Metric | Measures | Alert threshold |
|--------|----------|----------------|
| `Custom/Auth/client_registered_count` | OAuth client self-registrations | N/A (informational) |
| `Custom/Auth/token_issued_count` | Successful PKCE token issuances (30-day Bearer tokens) | Drop to 0 for > 5 min → PKCE flow broken |
| `Custom/Auth/session_login_count` | Successful session-based logins via `/auth/login` | Drop to 0 for > 5 min (if session auth is in use) |
| `Custom/Auth/auth_failure_count` | All auth failures across all flows — PKCE, session, missing params, CDS rejection | Rate > 5/min → credential attack or broken client |

**Where:** `record_metric()` calls in `auth_app/views.py`. `record_metric()` from `nr_utils.py` in `views.py`, `tools.py`, and `cds_client.py`.

---

## 6. Error Tracking — catch and classify failures

`notice_error()` sends an exception to NR with extra context. Combined with `error.*` and `mcp.error_category` attributes, you can answer:

- "How many errors were the AI client's fault vs CDS's fault?" → FACET `error.category`
- "Which CDS endpoint fails most?" → FACET `error.cds_endpoint`
- "Which tool breaks most?" → FACET `error.tool_name`

### Bug fix — error attributes now attached to the error event

`notice_err()` previously called `add_custom_attributes(pairs)` then `notice_error(exc)`.  The attributes were added to the **transaction** (queryable in `FROM Transaction`) but **not** to the error event itself.  This meant `FROM TransactionError WHERE error.category = 'timeout'` returned no results even though the attribute was set.

The fix passes attributes directly to the error event:
```python
_nr.notice_error(exc, attributes=dict(attrs))
```

`error.*` attributes (layer, cds_endpoint, http_status, retry_count, category) are now fully queryable in `FROM TransactionError`.

**Where:** `notice_err()` in every `except` block across all four files — the fix is in `nr_utils.py`.

---

## 7. Structured JSON Logs — forwarded to NR Logs with trace links

All Python `logging` output is emitted as **JSON** (via `python-json-logger`) and forwarded to New Relic Logs. Each log line is a JSON object with `asctime`, `levelname`, `name`, `message` as top-level fields. The NR agent automatically injects `trace.id` and `span.id` into each log record that is emitted inside an active transaction — enabling the "See logs" button to work directly from any APM trace.

Django loggers emit at WARNING+, app loggers at INFO+ (DEBUG in dev), so production logs are signal not noise.

**Where:** `LOGGING` config in `publive_mcp/settings.py` + `python-json-logger` in `requirements.txt`.

> **NR Logs NRQL benefit:** Because fields are JSON keys, you can now filter with `message.levelname = 'ERROR'` or `message.name = 'mcp_app.cds_client'` without custom parsing rules.

---

## 8. Distributed Tracing — follow a request end-to-end

`distributed_tracing.enabled = true` in `newrelic.ini`. The NR agent automatically propagates W3C trace context on outbound `requests.get()` calls to CDS. If CDS were also instrumented, the full chain would appear in one waterfall.

CDS span attributes (`cds.url`, `cds.path`, `cds.publisher_id`, `cds.timed_out`) are set on each span individually via `add_custom_span_attribute()` so they're visible per-call in the trace view.

**Where:** `newrelic.ini` + `cds_client.py`.

> **Service map:** CDS appears as an "External" node (not NR-instrumented). To give it a name in the service map, add `newrelic.agent.ExternalTrace(library="CDS", url=url)` as a context manager around the `requests.get()` call in `cds_client.py`.

---

## 9. Prompt Observability — see what AI clients are asking

The server extracts whatever prompt context the client sends and records it against every tool call. Useful for understanding real-world usage patterns.

Sources checked in order (first match wins):

1. **HTTP headers** (any of): `X-MCP-Prompt`, `X-User-Prompt`, `X-Prompt-Text`, `X-LLM-Prompt`
2. **JSON-RPC `_meta`** (body or params): keys `prompt`, `userMessage`, `user_message`, `message`
3. **`params.prompt`**
4. **`arguments._prompt`** then **`arguments.prompt`** — stripped from the args dict before the tool runs so the tool never sees them
5. **`tool_args`** — JSON snapshot of all tool arguments when no prompt key is found in any of the above
6. **`client_not_provided`** — no arguments and no prompt from any source (e.g. Claude Desktop calling a tool with no user context forwarded); label is `client_not_provided` (not empty string) so dashboards read clearly

The `mcp.prompt_source` transaction attribute records which source was used.

**Input token cost proxy:** `mcp.prompt_char_count ÷ 4 = mcp.estimated_prompt_tokens`. This server is a tool server — actual token consumption happens in the AI client. These fields approximate the cost signal without a tokenizer dependency.

**Output token cost proxy:** `mcp.estimated_output_tokens = mcp.tool_output_char_count ÷ 4` — set on the success path alongside the input estimate to give a full per-call token cost picture.

`MCPPrompt` events are capped at **1000/minute** to stay under NR's event limit. Transaction attributes (`mcp.prompt_text`, `mcp.prompt_source`, `mcp.prompt_char_count`) are always set regardless.

**Where:** `mcp_app/prompt_capture.py` + rate limiter in `views.py`.

---

## 10. Health Check & SSE Suppression — keep Apdex clean

Uptime probes and long-lived SSE sessions suppress Apdex and slow-transaction traces so they don't skew your score.

| View | Suppression | Reason |
|------|------------|--------|
| `health_check` (`GET /`) | `suppress_apdex()` + `suppress_trace()` | Railway uptime probes fire every few seconds |
| `auth_status` (`GET /auth/status`) | `suppress_apdex()` + `suppress_trace()` | Health-check endpoint; frequent polling |
| `mcp_endpoint` (`GET /mcp`, SSE) | `suppress_apdex()` + `suppress_trace()` *(new)* | SSE sessions run for minutes to hours; without suppression every session registers as "Frustrated" (Apdex T × 4 ≈ 2 s) and the slow-transaction list fills with SSE sessions, hiding real slow tool calls |

> **Why SSE Apdex suppression matters:** With a 0.5 s Apdex T (NR default), the Apdex threshold for "Frustrated" is 2 s. Every SSE session lasting > 2 s was previously counted as a Frustrated transaction, artificially collapsing the overall Apdex score.  All meaningful SSE telemetry is captured via custom events (`SSESessionOpen/Close`, `MCPSessionSummary`) and metrics — suppressing the Apdex measurement loses nothing.

**Where:** `mcp_app/views.py` → `health_check` and SSE path in `mcp_endpoint`; `auth_app/views.py` → `auth_status`; via `suppress_apdex()` / `suppress_trace()` helpers in `nr_utils.py`.

---

## 11. Deployment Markers — correlate deploys with changes

Every Railway deploy records a marker in NR. Markers appear as vertical lines on APM charts so you can instantly see "did this deploy cause the latency spike?"

```
release: ... && newrelic-admin record-deploy newrelic.ini \
  "${RAILWAY_GIT_COMMIT_SHA}" "${RAILWAY_GIT_COMMIT_MESSAGE}" "${RAILWAY_GIT_AUTHOR}" || true
```

**Where:** `Procfile` release step.

---

## 12. Rate Limiting Observability — unauthenticated probe detection

Unauthenticated requests to `/mcp` are rate-limited per IP (10 req/60 s sliding window). When an IP is limited it gets HTTP 429 with `Retry-After`.

| Signal | What it tells you |
|--------|-------------------|
| `Custom/MCP/unauth_tracker_size` | Unique IPs being tracked by the rate limiter — a rising value indicates an ongoing probe wave |
| `Custom/MCP/rate_limited_count` *(new)* | Total 429 responses issued — alert when non-zero during business hours |
| `MCPRateLimit` event *(new)* | Full context per rate-limit hit: `client_ip`, `retry_after_seconds`, `env`, `server_version` — enables "which IPs are probing us?" NRQL |

```sql
-- Rate-limited IP hit frequency (last 24 h)
SELECT count(*) FROM MCPRateLimit
FACET client_ip SINCE 24 hours ago ORDER BY count(*) DESC LIMIT 20

-- Rate-limit volume trend
SELECT rate(sum(Custom/MCP/rate_limited_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago
```

> **Multi-worker limitation:** `_unauth_hits` is in-memory per gunicorn worker. With N workers, each IP can send 10 × N requests before being rate-limited. `Custom/MCP/unauth_tracker_size` reflects one worker's view.

**Where:** `_is_unauth_rate_limited()` and the rate-limited response path in `views.py`.

---

## 13. Session-Level Debugging — one-click drill-down

### Architecture and correlation keys

```
SSESessionOpen (event)                SSESessionClose (event)
  trace_id = "abc123"                   session_trace_id = "abc123"
      │                                       │
      └──── mcp.session_trace_id = "abc123" ──┘
                  stamped on every mcp_message transaction
                            │
              ┌─────────────┼────────────────┐
              ▼             ▼                ▼
     mcp_message txn   mcp_message txn   mcp_message txn
     (tool call 1)     (tool call 2)     (tool call 3)
     trace.id = X1     trace.id = X2     trace.id = X3
     mcp.session_       mcp.session_      mcp.session_
       trace_id=abc123    trace_id=abc123   trace_id=abc123
              │             │                │
     MCPPrompt(trace_id=X1) MCPToolError    MCPToolDegraded
     MCPToolError (if any)  (trace_id=X2)   (trace_id=X3)
```

**Four correlation keys:**
- `mcp.session_id` — stable across all data types (Transactions, Events, Logs)
- `mcp.session_trace_id` — links all Transactions for one session (NRQL join key); now also present in `MCPToolError` and `MCPToolDegraded` events
- `trace_id` on custom events — lets you jump from an event to its APM trace waterfall
- `tool_sequence` in `MCPSessionSummary` — ordered comma-separated list of tool names; enables session replay without joining N Transaction records

**Limitation:** Log lines do **not** carry `session_id` as a separate JSON key. `logger.info("... session=%s ...", session_id)` embeds the value into the `message` string via Python `%`-interpolation — python-json-logger does not extract format args as top-level fields. Filter with `WHERE message LIKE '%session=X%'` in NR Logs (see Step 7 below). HTTP transport sessions have `mcp.session_id` on transactions but no lifecycle events (SSESessionOpen/Close) and no MCPSessionSummary.

**New: filter errors by session without string matching:**
```sql
-- Previously required session_id string match — now supported via session_trace_id:
SELECT * FROM MCPToolError WHERE session_trace_id = 'PASTE_SESSION_TRACE_ID'
SELECT * FROM MCPToolDegraded WHERE session_trace_id = 'PASTE_SESSION_TRACE_ID'
```

**New: instant session replay from one event:**
```sql
SELECT session_id, tool_sequence, tool_call_count, duration_ms
FROM MCPSessionSummary
WHERE session_id = 'PASTE_SESSION_ID' SINCE 24 hours ago LIMIT 1
-- tool_sequence shows e.g. "list_posts,get_post,get_category,get_author"
```

---

## NRQL Queries

### Tool performance

```sql
-- Average tool latency by tool name
SELECT average(mcp.tool_duration_ms)
FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name TIMESERIES SINCE 3 hours ago

-- p95 / p99 tool latency (transaction attributes support percentile())
SELECT percentile(mcp.tool_duration_ms, 95, 99)
FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- Tool call volume per minute
SELECT rate(sum(Custom/Tool/list_posts/call_count), 1 minute)
FROM Metric TIMESERIES SINCE 3 hours ago
-- Replace list_posts with any tool name, or use * and FACET

-- Per-tool error rate (%)
SELECT
  filter(count(*), WHERE mcp.tool_is_error = true) * 100.0
    / count(*) AS error_rate_pct
FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 1 hour ago
```

### CDS performance

```sql
-- CDS latency by endpoint
SELECT average(cds.latency_ms)
FROM Transaction
WHERE cds.endpoint IS NOT NULL
FACET cds.endpoint TIMESERIES SINCE 3 hours ago

-- p95 / p99 CDS latency
SELECT percentile(cds.latency_ms, 95, 99)
FROM Transaction
WHERE cds.endpoint IS NOT NULL
FACET cds.endpoint SINCE 24 hours ago

-- CDS timeout rate
SELECT rate(sum(Custom/CDS/timeout_count), 1 minute)
FROM Metric TIMESERIES SINCE 3 hours ago

-- CDS retry rate
SELECT rate(sum(Custom/CDS/retry_count), 1 minute)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Timeout transactions with context
SELECT timestamp, mcp.tool_name, cds.endpoint, cds.latency_ms, mcp.session_id
FROM Transaction
WHERE cds.timed_out = true
SINCE 6 hours ago ORDER BY timestamp DESC LIMIT 100
```

### Session debugging

```sql
-- All tool calls in a session (conversation replay)
SELECT timestamp, mcp.tool_name, mcp.session_tool_seq, mcp.tool_result_status,
       mcp.tool_duration_ms, mcp.error_category
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND mcp.tool_name IS NOT NULL
ORDER BY mcp.session_tool_seq ASC SINCE 24 hours ago

-- Session summary table
SELECT session_id, publisher_id, duration_ms, tool_call_count, tool_error_count
FROM MCPSessionSummary
SINCE 24 hours ago ORDER BY duration_ms DESC LIMIT 50

-- Active session count over time
SELECT latest(Custom/MCP/active_sessions)
FROM Metric TIMESERIES SINCE 3 hours ago

-- SSE session durations (p95)
SELECT percentile(duration_ms, 95) FROM SSESessionClose
SINCE 24 hours ago TIMESERIES
```

### Error analysis

```sql
-- Error breakdown by category (user vs upstream vs timeout)
SELECT count(*) FROM Transaction
WHERE mcp.error_category IS NOT NULL
FACET mcp.error_category SINCE 24 hours ago

-- Recent tool errors with full context
SELECT timestamp, tool_name, error_type, error_message, error_category, publisher_id
FROM MCPToolError
SINCE 3 hours ago ORDER BY timestamp DESC LIMIT MAX

-- Auth failures by reason
SELECT count(*) FROM Transaction
WHERE auth.result = 'failure'
FACET auth.failure_reason, auth.flow SINCE 24 hours ago

-- CDS 401 events (credential expiry mid-session)
SELECT count(*) FROM Transaction
WHERE mcp.tool_auth_error = true
FACET mcp.tool_name, cds.publisher_id SINCE 24 hours ago
```

### Fallback + retry

```sql
-- Fallback rate over time
SELECT rate(sum(Custom/MCP/fallback_count), 1 minute)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Transactions that triggered a fallback
SELECT timestamp, mcp.session_id, mcp.tool_fallback, mcp.tool_fallback_reason
FROM Transaction
WHERE mcp.tool_fallback IS NOT NULL
SINCE 24 hours ago ORDER BY timestamp DESC LIMIT 50
```

### Prompt / AI token proxy

```sql
-- Average estimated token usage per tool call
SELECT average(mcp.estimated_prompt_tokens)
FROM Transaction
WHERE mcp.prompt_char_count IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- Total estimated prompt token volume per hour
SELECT sum(estimated_prompt_tokens) FROM MCPPrompt
FACET publisher_id TIMESERIES 1 hour SINCE 7 days ago

-- Most expensive prompts (by character count)
SELECT timestamp, session_id, tool_name, prompt_char_count, prompt_text
FROM MCPPrompt
ORDER BY prompt_char_count DESC SINCE 24 hours ago LIMIT 50
```

### Concurrency + saturation

```sql
-- Thread saturation per worker
SELECT max(mcp.thread_active_count) FROM Transaction
WHERE mcp.thread_active_count IS NOT NULL TIMESERIES SINCE 3 hours ago

-- Queue depth over time (SSE backpressure)
SELECT max(mcp.session_queue_depth) FROM Transaction
WHERE mcp.session_queue_depth IS NOT NULL TIMESERIES SINCE 3 hours ago

-- Unauthenticated probe tracking
SELECT latest(Custom/MCP/unauth_tracker_size)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Rate-limited IP hits
SELECT count(*) FROM Transaction
WHERE mcp.rate_limited = true
SINCE 24 hours ago TIMESERIES
```

### Degraded responses

```sql
-- Degraded call rate over time (shows partial failures invisible before)
SELECT rate(sum(Custom/MCP/tool_degraded_count), 1 minute)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Per-tool degraded breakdown
SELECT count(*) FROM MCPToolDegraded
FACET tool_name, degraded_reason SINCE 24 hours ago

-- Three-way tool result status split (success / degraded / error)
SELECT
  filter(count(*), WHERE mcp.tool_result_status = 'success')    AS clean_success,
  filter(count(*), WHERE mcp.tool_result_status = 'degraded')   AS degraded,
  filter(count(*), WHERE mcp.tool_result_status = 'error')      AS error
FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- Degraded rate % per tool (auth_expired, upstream_timeout, etc.)
SELECT filter(count(*), WHERE mcp.tool_is_degraded = true) * 100.0 / count(*) AS degraded_pct
FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- Recent degraded events with full context
SELECT timestamp, tool_name, degraded_reason, publisher_id, duration_ms, tool_input
FROM MCPToolDegraded
SINCE 3 hours ago ORDER BY timestamp DESC LIMIT MAX
```

### Per-tool concurrency

```sql
-- Current in-flight concurrency per tool (saturation gauge)
SELECT latest(Custom/Tool/list_posts/active_calls)
FROM Metric TIMESERIES SINCE 1 hour ago
-- Replace list_posts with any tool name

-- Peak concurrency by tool
SELECT max(mcp.tool_concurrency) FROM Transaction
WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- Tool saturation: flag tools running at high concurrency
SELECT
  filter(count(*), WHERE mcp.tool_concurrency >= 5) AS high_concurrency_calls,
  count(*) AS total_calls
FROM Transaction WHERE mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 1 hour ago

-- Error duration vs clean-success duration (timeout calls drag p99 up)
SELECT
  average(mcp.tool_duration_ms) AS avg_success_ms,
  percentile(mcp.tool_duration_ms, 95, 99) AS p95_p99_success_ms
FROM Transaction
WHERE mcp.tool_result_status = 'success' AND mcp.tool_name IS NOT NULL
FACET mcp.tool_name SINCE 24 hours ago

-- p95 error latency (how long timed-out calls take before failing)
SELECT percentile(Custom/Tool/list_posts/error_duration_ms, 95)
FROM Metric SINCE 24 hours ago
-- Replace list_posts with any tool name
```

### Queue wait time (SSE backpressure)

```sql
-- Average queue wait time (should stay < 50 ms for healthy clients)
SELECT average(Custom/MCP/queue_wait_ms)
FROM Metric TIMESERIES SINCE 3 hours ago

-- p95 queue wait (spike here means one client is blocking the SSE drain)
SELECT percentile(Custom/MCP/queue_wait_ms, 95, 99)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Queue wait time alongside queue depth (correlate: depth causes wait)
-- NOTE: NRQL does not support multiple event types in FROM.
--       Run as two side-by-side widgets on the same dashboard row.

-- Widget A — queue depth over time:
SELECT max(mcp.session_queue_depth) AS max_depth
FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL
TIMESERIES SINCE 3 hours ago

-- Widget B — queue wait time over time:
SELECT average(Custom/MCP/queue_wait_ms) AS avg_wait_ms
FROM Metric TIMESERIES SINCE 3 hours ago
```

### Unknown methods (truly unexpected)

```sql
SELECT count(*) FROM MCPUnknownMethod FACET method TIMESERIES SINCE 24 hours ago
```

### Rate limiting (new)

```sql
-- Rate-limit volume over time
SELECT rate(sum(Custom/MCP/rate_limited_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Top offending IPs (last 24 h)
SELECT count(*) FROM MCPRateLimit
FACET client_ip SINCE 24 hours ago ORDER BY count(*) DESC LIMIT 20

-- Rate-limit events during business hours (probe vs legitimate misconfiguration)
SELECT count(*), client_ip, retry_after_seconds
FROM MCPRateLimit
SINCE 24 hours ago LIMIT MAX
```

### Session abandonment (new)

```sql
-- Session abandonment rate (sessions that opened but never called a tool)
-- NOTE: NRQL does not support multiple event types in FROM.
--       Use the metric-based approach which is also more accurate (13-month retention).
SELECT
  count(*) AS total_opens,
  sum(Custom/MCP/session_abandon_count) AS abandoned,
  sum(Custom/MCP/session_abandon_count) * 100.0
    / nullif(count(*), 0) AS abandon_pct
FROM SSESessionOpen SINCE 24 hours ago

-- Abandon rate % over time
SELECT rate(sum(Custom/MCP/session_abandon_count), 1 hour)
FROM Metric TIMESERIES SINCE 24 hours ago

-- Abandoned sessions by client (to identify misconfigured clients)
SELECT count(*), session_client_name, duration_ms
FROM MCPSessionAbandoned
SINCE 24 hours ago FACET session_client_name ORDER BY count(*) DESC LIMIT 20
```

### Prompt event observability health (new)

```sql
-- Prompt event drop rate (rising = MCPPrompt observability is degraded under load)
SELECT rate(sum(Custom/MCP/prompt_event_dropped_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Drop rate as % of total tool calls
SELECT
  sum(Custom/MCP/prompt_event_dropped_count) AS dropped,
  sum(Custom/MCP/tool_call_count) AS calls,
  sum(Custom/MCP/prompt_event_dropped_count) * 100.0
    / nullif(sum(Custom/MCP/tool_call_count), 0) AS drop_pct
FROM Metric SINCE 1 hour ago
```

### Multi-worker session routing (new)

```sql
-- Missing session count over time (cross-worker routing failures)
SELECT rate(sum(Custom/MCP/sse_session_missing_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Detail: which sessions are missing?
SELECT count(*), session_id
FROM MCPSessionMissing
SINCE 24 hours ago FACET session_id ORDER BY count(*) DESC LIMIT 20
```

### Queue health (new metrics)

```sql
-- Queue overflow events (slow SSE consumer, response dropped)
SELECT rate(sum(Custom/MCP/queue_overflow_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Sessions where queue overflow occurred
SELECT count(*) FROM Transaction
WHERE mcp.queue_overflow = true TIMESERIES SINCE 3 hours ago
```

### Auth flow health (new)

```sql
-- Token issuance rate (PKCE OAuth)
SELECT rate(sum(Custom/Auth/token_issued_count), 1 hour)
FROM Metric TIMESERIES SINCE 24 hours ago

-- Auth failure rate by failure reason
SELECT count(*) FROM Transaction
WHERE auth.result = 'failure'
FACET auth.failure_reason, auth.flow SINCE 24 hours ago

-- Auth failure metric trend
SELECT rate(sum(Custom/Auth/auth_failure_count), 1 min)
FROM Metric TIMESERIES SINCE 3 hours ago

-- Token issuance vs failure ratio
SELECT
  sum(Custom/Auth/token_issued_count) AS tokens_issued,
  sum(Custom/Auth/auth_failure_count) AS failures,
  sum(Custom/Auth/auth_failure_count) * 100.0
    / nullif(sum(Custom/Auth/token_issued_count) + sum(Custom/Auth/auth_failure_count), 0)
    AS failure_pct
FROM Metric SINCE 1 hour ago
```

### Error events with cross-session correlation (improved)

```sql
-- Find all errors for a session (now works via session_trace_id — no string matching needed)
SELECT timestamp, tool_name, error_type, error_message, error_category, duration_ms
FROM MCPToolError
WHERE session_trace_id = 'PASTE_SESSION_TRACE_ID'
SINCE 24 hours ago ORDER BY timestamp ASC

-- Find all degraded results for a session
SELECT timestamp, tool_name, degraded_reason, duration_ms
FROM MCPToolDegraded
WHERE session_trace_id = 'PASTE_SESSION_TRACE_ID'
SINCE 24 hours ago ORDER BY timestamp ASC

-- Error events from TransactionError (now includes error.* attributes)
SELECT timestamp, error.class, error.message, error.category, error.cds_endpoint
FROM TransactionError
WHERE error.category IS NOT NULL
SINCE 24 hours ago ORDER BY timestamp DESC LIMIT 50
```

### Session-level debugging (one-click drill-down)

Replace `'PASTE_SESSION_ID'` with any `session_id` value from NR Insights.

```sql
-- ── STEP 1: Session overview ───────────────────────────────────────────────
-- Get the session summary: total duration, tool counts, token usage, AI vs server time.
-- tool_sequence is now included — instant replay without joining Transaction records.
SELECT
  duration_ms,
  tool_call_count,
  tool_error_count,
  tool_degraded_count,
  total_tool_duration_ms,
  duration_ms - total_tool_duration_ms AS ai_think_total_ms,
  server_work_pct,
  total_estimated_input_tokens,
  total_estimated_output_tokens,
  total_estimated_tokens,
  session_client_name,
  tool_sequence          -- e.g. "list_posts,get_post,get_category"
FROM MCPSessionSummary
WHERE session_id = 'PASTE_SESSION_ID'
SINCE 24 hours ago LIMIT 1

-- ── STEP 2: Session timeline ──────────────────────────────────────────────
-- Ordered tool call list with timing offsets and AI think-time gaps
SELECT
  mcp.session_tool_seq     AS step,
  mcp.tool_name            AS tool,
  mcp.tool_result_status   AS status,
  mcp.tool_start_offset_ms AS started_at_ms,
  mcp.ai_think_time_ms     AS ai_gap_before_ms,
  mcp.tool_duration_ms     AS duration_ms,
  mcp.degraded_reason      AS degraded_reason,
  mcp.error_category       AS error_category,
  mcp.tool_input           AS input_args
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND mcp.tool_name IS NOT NULL
ORDER BY mcp.session_tool_seq ASC
SINCE 24 hours ago LIMIT MAX

-- ── STEP 3: All prompts in this session ───────────────────────────────────
-- See what the AI was trying to do at each step, with trace link
SELECT
  timestamp,
  tool_name,
  prompt_source,
  prompt_text,
  estimated_prompt_tokens,
  trace_id          -- click this in NR to open the APM trace waterfall
FROM MCPPrompt
WHERE session_id = 'PASTE_SESSION_ID'
SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX

-- ── STEP 4: All failures (exceptions + degraded) ──────────────────────────
-- NRQL does not support UNION — run as two separate queries side by side.

-- 4a. Exceptions (tool raised an exception):
-- Can now filter by session_trace_id (precise) OR session_id (string match).
SELECT
  timestamp,
  tool_name,
  error_type,
  error_message,
  error_category,
  duration_ms,
  tool_start_offset_ms,
  trace_id         -- click to open APM trace for this specific failure
FROM MCPToolError
WHERE session_id = 'PASTE_SESSION_ID'
   OR session_trace_id = 'PASTE_SESSION_TRACE_ID'
SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX

-- 4b. Degraded results (tool returned {"error":…} dict without raising):
SELECT
  timestamp,
  tool_name,
  degraded_reason,
  duration_ms,
  tool_start_offset_ms,
  trace_id
FROM MCPToolDegraded
WHERE session_id = 'PASTE_SESSION_ID'
   OR session_trace_id = 'PASTE_SESSION_TRACE_ID'
SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX

-- ── STEP 5: CDS retries within this session ───────────────────────────────
-- Which CDS calls needed retries, and how long they took
SELECT
  timestamp,
  mcp.tool_name,
  cds.endpoint,
  cds.latency_ms,
  cds.retry_count,
  cds.retried,
  cds.timed_out,
  cds.http_status
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND (cds.retried = true OR cds.timed_out = true)
SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX

-- ── STEP 6: All transactions for this session (span navigation) ───────────
-- Use mcp.session_trace_id to find all transactions even across SSE/HTTP
-- Get session_trace_id from SSESessionOpen first:
SELECT session_trace_id FROM SSESessionOpen
WHERE session_id = 'PASTE_SESSION_ID' SINCE 24 hours ago LIMIT 1
-- Then:
SELECT timestamp, name, mcp.tool_name, mcp.tool_result_status,
       mcp.tool_duration_ms, traceId
FROM Transaction
WHERE mcp.session_trace_id = 'PASTE_SESSION_TRACE_ID'
SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX
-- For each traceId, navigate to: APM → Distributed Tracing → search by trace.id

-- ── STEP 7: All logs for this session ─────────────────────────────────────
-- session_id is embedded in the message string, NOT a separate JSON key.
-- logger.info("... session=%s ...", session_id) → message = "... session=abc123 ..."
-- NR Logs UI filter:  message LIKE '%session=PASTE_SESSION_ID%'
-- Or via NRQL (NR Logs query builder):
SELECT timestamp, level, message
FROM Log
WHERE message LIKE '%session=PASTE_SESSION_ID%'
ORDER BY timestamp ASC SINCE 24 hours ago LIMIT MAX

-- ── STEP 8: Session token cost breakdown ──────────────────────────────────
SELECT
  sum(mcp.estimated_prompt_tokens)  AS total_input_tokens,
  sum(mcp.estimated_output_tokens)  AS total_output_tokens,
  sum(mcp.estimated_prompt_tokens)
    + sum(mcp.estimated_output_tokens) AS total_tokens_approx
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND mcp.tool_name IS NOT NULL
SINCE 24 hours ago

-- ── STEP 9: Per-step timing waterfall (timeline view) ─────────────────────
-- Use this data to draw a waterfall: offset = start_x, duration = width
SELECT
  mcp.session_tool_seq     AS seq,
  mcp.tool_name,
  mcp.tool_start_offset_ms AS x_start,
  mcp.tool_start_offset_ms
    + mcp.tool_duration_ms AS x_end,
  mcp.tool_duration_ms     AS width_ms,
  mcp.ai_think_time_ms     AS gap_before_ms,
  mcp.tool_result_status
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND mcp.session_tool_seq IS NOT NULL
ORDER BY mcp.session_tool_seq ASC
SINCE 24 hours ago LIMIT MAX
```

---

### Session debugging dashboard layout

Build a NR dashboard with a session_id template variable (`{{session_id}}`), then bind it across all widgets:

| Widget | Type | NRQL (replace `'PASTE_SESSION_ID'` with `{{session_id}}`) |
|--------|------|------|
| Session header | Billboard | `FROM MCPSessionSummary SELECT duration_ms, tool_call_count, server_work_pct, total_estimated_tokens WHERE session_id = {{session_id}} LIMIT 1` |
| Tool timeline table | Table | Step 2 query above |
| Failure list (errors) | Table | Step 4a query above (MCPToolError) |
| Failure list (degraded) | Table | Step 4b query above (MCPToolDegraded) |
| Token breakdown | Billboard | Step 8 query above |
| AI think-time vs tool time | Bar chart | `SELECT average(mcp.ai_think_time_ms) AS ai_gap_ms, average(mcp.tool_duration_ms) AS tool_ms FROM Transaction WHERE mcp.session_id = {{session_id}} FACET mcp.session_tool_seq` |
| CDS latency per step | Bar chart | `SELECT mcp.session_tool_seq, cds.latency_ms, cds.endpoint FROM Transaction WHERE mcp.session_id = {{session_id}} AND cds.endpoint IS NOT NULL ORDER BY mcp.session_tool_seq ASC LIMIT MAX` |
| Prompt text per step | Table | Step 3 query above |

---

## SLO / SLI Definitions

Use NR's Service Level Management (SLM) UI. Create a Service Level on the MCP entity with these good/total queries:

### SLI 1 — Tool Availability (target: 99%)
```sql
-- Good events: clean success (no exception AND no degraded result)
SELECT count(*) FROM Transaction
WHERE mcp.tool_name IS NOT NULL AND mcp.tool_result_status = 'success'

-- Total events
SELECT count(*) FROM Transaction
WHERE mcp.tool_name IS NOT NULL
```

> **Why `mcp.tool_result_status = 'success'` not `mcp.tool_is_error = false`:**
> Seven tools return structured `{"error":…}` dicts (degraded responses) that complete without raising.
> The old `mcp.tool_is_error = false` guard counted degraded calls as "good". The new guard excludes them.

### SLI 2 — Tool Latency (target: 95% of calls < 3 s)
```sql
-- Good events: clean calls that completed quickly (exclude error latency)
SELECT count(*) FROM Transaction
WHERE mcp.tool_name IS NOT NULL
  AND mcp.tool_result_status = 'success'
  AND mcp.tool_duration_ms < 3000

-- Total events
SELECT count(*) FROM Transaction
WHERE mcp.tool_name IS NOT NULL
```

### SLI 3 — CDS Availability (target: 99.5%)
```sql
-- Good events
SELECT count(*) FROM Transaction
WHERE cds.endpoint IS NOT NULL AND cds.http_status < 500

-- Total events
SELECT count(*) FROM Transaction
WHERE cds.endpoint IS NOT NULL
```

---

## Recommended Dashboards

### Dashboard maturity audit — 15 dimensions

| Dimension | Previous state | Now |
|-----------|---------------|-----|
| MCP server health | ⚠️ 4 widgets, no SLO status | ✅ Dashboard 1 — 14 widgets, SLO row, transport row |
| Tool performance | ⚠️ Latency only, no throughput | ✅ Dashboard 2 — per-tool throughput, status split, summary table |
| Session analytics | ⚠️ 2 billboards + queue widget | ✅ Dashboard 3 — lifecycle, quality, publisher breakdown |
| Retries/fallbacks | ❌ Missing | ✅ Dashboard 4 — retry/timeout/fallback trends + detail tables |
| Concurrency | ⚠️ Peak snapshot only | ✅ Dashboard 5 — concurrency time series, saturation tiers |
| Saturation | ⚠️ Implicit via concurrency | ✅ Dashboard 6 — headroom gauges, thread/queue/session ceilings |
| Workflow tracing | ⚠️ Queries in doc, no dashboard | ✅ Dashboard 7 — parameterized 6-row session drill-down |
| Latency heatmaps | ⚠️ 1 histogram widget | ✅ Dashboard 8 — 2D heatmaps, percentile bands, error vs success |
| Failure analysis | ⚠️ 3 widgets (pie, table, auth) | ✅ Dashboard 9 — category trends, publisher breakdown, root-cause |
| Active sessions | ⚠️ Count only | ✅ Dashboard 10 — open/close lifecycle, duration histogram |
| Queue metrics | ⚠️ 1 combined widget | ✅ Dashboard 11 — depth trend, wait time p50/p95, backpressure |
| AI telemetry | ⚠️ Token area chart only | ✅ Dashboard 12 — prompt patterns, think-time, cost by publisher |
| Memory/CPU | ❌ Missing | ✅ Dashboard 13 — in-process proxies + infra agent queries |
| Worker health | ❌ Missing | ✅ Dashboard 14 — thread lifecycle, load correlation, headroom |
| Error categories | ⚠️ Pie chart only | ✅ Dashboard 15 — category trends, per-tool, per-publisher |

> **How to create:** NR One → Dashboards → + Create a dashboard → Add page. One page per dashboard below. Paste NRQL directly into the query builder when adding widgets. For template variables (e.g. `publisher_id`, `session_id`): dashboard → kebab menu → Add variable → NRQL variable type → provide the query shown.
>
> **Default time window:** all queries use `SINCE 1 hour ago` unless labeled. Add a **time picker** filter to every dashboard so operators can change the window. Dashboards with template variables should also expose the variable as a filter bar at the top.
>
> **Heatmap widgets:** NR's "Heatmap" chart type renders `histogram(attr, ceiling, buckets) ... TIMESERIES` as a 2D view — x-axis time, y-axis latency bucket, color = density. Use `ceiling` = 10 000 ms and `buckets` = 40 for tool latency.
>
> **Custom metric percentiles:** `record_custom_metric()` stores summary metrics (min/max/avg/sum/count), not distributions. Use `average()` and `max()` for `FROM Metric` queries on `Custom/*` metrics. For true percentiles, query the matching Transaction attribute (e.g. `mcp.tool_duration_ms` instead of `Custom/Tool/{name}/duration_ms`).

---

### Dashboard 1 — MCP Server Health (Master Overview)
> **Purpose:** Executive-level health snapshot — error rate, SLO status, traffic shape, auth, transport.  
> **Filters:** time picker only (global view; no per-publisher filter so issues from all publishers are visible).

**Row 1 — Live KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Tool success rate % | Billboard | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success') * 100.0 / count(*) AS success_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 hour ago` |
| Tool error rate % | Billboard | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 hour ago` |
| Active SSE sessions | Billboard | `SELECT latest(Custom/MCP/active_sessions) FROM Metric` |
| Tool calls (last hour) | Billboard | `SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 hour ago` |

**Row 2 — Traffic & status trends**

| Widget | Type | NRQL |
|--------|------|------|
| Tool call rate | Line chart | `SELECT rate(count(*), 1 min) FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Result status split over time | Stacked area | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success') AS success, filter(count(*), WHERE mcp.tool_result_status = 'degraded') AS degraded, filter(count(*), WHERE mcp.tool_result_status = 'error') AS error FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| CDS timeout rate | Line chart | `SELECT rate(sum(Custom/CDS/timeout_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Active sessions trend | Line chart | `SELECT latest(Custom/MCP/active_sessions) FROM Metric TIMESERIES SINCE 3 hours ago` |

**Row 3 — SLO status (24-hour window)**

| Widget | Type | NRQL |
|--------|------|------|
| SLI 1 — Tool availability % | Billboard | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success') * 100.0 / count(*) AS availability_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 24 hours ago` |
| SLI 2 — Latency compliance % (< 3 s) | Billboard | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success' AND mcp.tool_duration_ms < 3000) * 100.0 / count(*) AS latency_slo_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 24 hours ago` |
| SLI 3 — CDS availability % | Billboard | `SELECT filter(count(*), WHERE cds.http_status < 500) * 100.0 / count(*) AS cds_availability_pct FROM Transaction WHERE cds.endpoint IS NOT NULL SINCE 24 hours ago` |

**Row 4 — Transport, auth, and rate-limiting**

| Widget | Type | NRQL |
|--------|------|------|
| Transport split | Pie chart | `SELECT count(*) FROM Transaction WHERE mcp.transport IS NOT NULL FACET mcp.transport SINCE 24 hours ago` |
| Auth failures by reason | Bar chart | `SELECT count(*) FROM Transaction WHERE auth.result = 'failure' FACET auth.failure_reason SINCE 24 hours ago` |
| Rate-limited requests | Line chart | `SELECT rate(sum(Custom/MCP/rate_limited_count), 1 min) FROM Metric TIMESERIES SINCE 24 hours ago` |
| Auth failure metric trend | Line chart | `SELECT rate(sum(Custom/Auth/auth_failure_count), 1 min) FROM Metric TIMESERIES SINCE 24 hours ago` |

**Row 5 — Observability health (new)**

| Widget | Type | NRQL |
|--------|------|------|
| Session abandon rate | Billboard | `SELECT rate(sum(Custom/MCP/session_abandon_count), 1 hour) AS abandoned_per_hour FROM Metric SINCE 1 hour ago` |
| Prompt event drop rate | Billboard | `SELECT rate(sum(Custom/MCP/prompt_event_dropped_count), 1 min) AS drops_per_min FROM Metric SINCE 1 hour ago` |
| SSE session missing count | Billboard | `SELECT sum(Custom/MCP/sse_session_missing_count) AS missing FROM Metric SINCE 1 hour ago` |
| Queue overflow count | Billboard | `SELECT sum(Custom/MCP/queue_overflow_count) AS overflows FROM Metric SINCE 1 hour ago` |

---

### Dashboard 2 — Tool Performance
> **Purpose:** Per-tool latency, throughput, and three-way status (success/degraded/error) over time.  
> **Template variable:** `tool_name` → `SELECT uniques(mcp.tool_name) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 day ago` (optional; leave unset to see all tools).

**Row 1 — Latency overview**

| Widget | Type | NRQL |
|--------|------|------|
| p50 / p95 / p99 by tool | Bar chart | `SELECT percentile(mcp.tool_duration_ms, 50, 95, 99) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| p95 latency trend by tool | Line chart | `SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name TIMESERIES SINCE 3 hours ago` |
| CDS p95 latency by endpoint | Bar chart | `SELECT percentile(cds.latency_ms, 95) FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint SINCE 1 hour ago` |

**Row 2 — Throughput and result status**

| Widget | Type | NRQL |
|--------|------|------|
| Call rate by tool (stacked) | Stacked area | `SELECT rate(count(*), 1 min) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name TIMESERIES SINCE 3 hours ago` |
| Three-way status by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success') AS success, filter(count(*), WHERE mcp.tool_result_status = 'degraded') AS degraded, filter(count(*), WHERE mcp.tool_result_status = 'error') AS error FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| Error rate % by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |

**Row 3 — Response characteristics**

| Widget | Type | NRQL |
|--------|------|------|
| Avg response size by tool | Bar chart | `SELECT average(mcp.tool_response_size) AS avg_bytes FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| Avg args count by tool | Bar chart | `SELECT average(mcp.tool_args_count) AS avg_args FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |

**Row 4 — Per-tool summary table (24 h)**

| Widget | Type | NRQL |
|--------|------|------|
| Tool summary | Table | `SELECT count(*) AS calls, average(mcp.tool_duration_ms) AS avg_ms, percentile(mcp.tool_duration_ms, 95) AS p95_ms, percentile(mcp.tool_duration_ms, 99) AS p99_ms, filter(count(*), WHERE mcp.tool_is_error = true) AS errors, filter(count(*), WHERE mcp.tool_is_degraded = true) AS degraded FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 24 hours ago LIMIT MAX` |

---

### Dashboard 3 — Session Analytics
> **Purpose:** SSE session lifecycle, session quality metrics, per-publisher breakdown.  
> **Template variable:** `publisher_id` → `SELECT uniques(publisher_id) FROM MCPSessionSummary SINCE 1 day ago`.

**Row 1 — Session volume KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Total sessions (24 h) | Billboard | `SELECT count(*) FROM MCPSessionSummary SINCE 24 hours ago` |
| Active sessions now | Billboard | `SELECT latest(Custom/MCP/active_sessions) FROM Metric` |
| Session open rate | Line chart | `SELECT rate(count(*), 1 min) FROM SSESessionOpen TIMESERIES SINCE 3 hours ago` |
| Avg session duration | Billboard | `SELECT average(duration_ms) AS avg_duration_ms FROM MCPSessionSummary SINCE 24 hours ago` |

**Row 2 — Session quality**

| Widget | Type | NRQL |
|--------|------|------|
| Tools per session distribution | Histogram | `SELECT histogram(tool_call_count, 30, 15) FROM MCPSessionSummary SINCE 24 hours ago` |
| Session error rate distribution | Histogram | `SELECT histogram(tool_error_count * 100.0 / tool_call_count, 100, 20) FROM MCPSessionSummary WHERE tool_call_count > 0 SINCE 24 hours ago` |
| Server work % over time | Line chart | `SELECT average(server_work_pct) FROM MCPSessionSummary TIMESERIES 1 hour SINCE 24 hours ago` |

**Row 3 — Publisher breakdown**

| Widget | Type | NRQL |
|--------|------|------|
| Sessions by publisher | Pie chart | `SELECT count(*) FROM MCPSessionSummary FACET publisher_id SINCE 24 hours ago` |
| Error rate by publisher | Bar chart | `SELECT sum(tool_error_count) * 100.0 / sum(tool_call_count) AS error_pct FROM MCPSessionSummary WHERE tool_call_count > 0 FACET publisher_id SINCE 24 hours ago` |
| Per-publisher session stats | Table | `SELECT count(*) AS sessions, average(duration_ms) AS avg_duration_ms, sum(tool_call_count) AS total_calls, sum(tool_error_count) AS total_errors, sum(tool_degraded_count) AS total_degraded FROM MCPSessionSummary FACET publisher_id SINCE 24 hours ago LIMIT MAX` |

**Row 4 — Session replay (new)**

| Widget | Type | NRQL |
|--------|------|------|
| Recent session summary with tool sequence | Table | `SELECT session_id, publisher_id, duration_ms, tool_call_count, tool_error_count, tool_degraded_count, server_work_pct, session_client_name, tool_sequence FROM MCPSessionSummary SINCE 24 hours ago ORDER BY duration_ms DESC LIMIT 50` |

**Row 5 — Session abandonment (new)**

| Widget | Type | NRQL |
|--------|------|------|
| Abandon rate trend | Line chart | `SELECT rate(sum(Custom/MCP/session_abandon_count), 1 hour) FROM Metric TIMESERIES SINCE 24 hours ago` |
| Abandoned sessions by client | Bar chart | `SELECT count(*) FROM MCPSessionAbandoned FACET session_client_name SINCE 24 hours ago` |
| Abandoned session detail | Table | `SELECT timestamp, session_id, publisher_id, duration_ms, session_client_name FROM MCPSessionAbandoned SINCE 24 hours ago ORDER BY timestamp DESC LIMIT 50` |

---

### Dashboard 4 — Retries & Fallbacks
> **Purpose:** CDS retry patterns, timeout distribution, fallback events — upstream instability signals.

**Row 1 — Retry and timeout trends**

| Widget | Type | NRQL |
|--------|------|------|
| CDS retry rate | Line chart | `SELECT rate(sum(Custom/CDS/retry_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| CDS timeout rate | Line chart | `SELECT rate(sum(Custom/CDS/timeout_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Retried call % of total | Line chart | `SELECT filter(count(*), WHERE cds.retried = true) * 100.0 / count(*) AS retry_pct FROM Transaction WHERE cds.endpoint IS NOT NULL TIMESERIES SINCE 3 hours ago` |

**Row 2 — Retry breakdown by endpoint**

| Widget | Type | NRQL |
|--------|------|------|
| Retry count by endpoint | Bar chart | `SELECT sum(cds.retry_count) AS retries FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint SINCE 1 hour ago` |
| Timeout count by endpoint | Bar chart | `SELECT filter(count(*), WHERE cds.timed_out = true) AS timeouts FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint SINCE 1 hour ago` |
| Retried calls detail | Table | `SELECT timestamp, mcp.tool_name, cds.endpoint, cds.latency_ms, cds.retry_count, cds.http_status FROM Transaction WHERE cds.retried = true SINCE 3 hours ago ORDER BY timestamp DESC LIMIT 50` |

**Row 3 — Fallback events**

| Widget | Type | NRQL |
|--------|------|------|
| Fallback rate | Line chart | `SELECT rate(sum(Custom/MCP/fallback_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Fallbacks by tool | Bar chart | `SELECT count(*) FROM Transaction WHERE mcp.tool_fallback IS NOT NULL FACET mcp.tool_fallback SINCE 24 hours ago` |
| Fallback events detail | Table | `SELECT timestamp, mcp.session_id, mcp.tool_name, mcp.tool_fallback, mcp.tool_fallback_reason FROM Transaction WHERE mcp.tool_fallback IS NOT NULL SINCE 3 hours ago ORDER BY timestamp DESC LIMIT 50` |

**Row 4 — Timeout latency profile**

| Widget | Type | NRQL |
|--------|------|------|
| Timed-out call duration histogram | Histogram | `SELECT histogram(mcp.tool_duration_ms, 15000, 30) FROM Transaction WHERE cds.timed_out = true SINCE 24 hours ago` |
| Timeout events by publisher | Bar chart | `SELECT count(*) FROM Transaction WHERE cds.timed_out = true FACET cds.publisher_id SINCE 24 hours ago` |

---

### Dashboard 5 — Concurrency
> **Purpose:** Per-tool in-flight concurrency over time, saturation tier breakdown, thread-vs-session correlation.

**Row 1 — Concurrency snapshot**

| Widget | Type | NRQL |
|--------|------|------|
| Peak concurrency by tool | Bar chart | `SELECT max(mcp.tool_concurrency) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| Concurrency over time by tool | Line chart | `SELECT max(mcp.tool_concurrency) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name TIMESERIES SINCE 3 hours ago` |
| Concurrency distribution by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_concurrency = 1) AS c1, filter(count(*), WHERE mcp.tool_concurrency BETWEEN 2 AND 4) AS c2_4, filter(count(*), WHERE mcp.tool_concurrency BETWEEN 5 AND 9) AS c5_9, filter(count(*), WHERE mcp.tool_concurrency >= 10) AS c10plus FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago LIMIT MAX` |

**Row 2 — Concurrency vs latency**

| Widget | Type | NRQL |
|--------|------|------|
| Avg latency by concurrency tier | Bar chart | `SELECT average(mcp.tool_duration_ms) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET cases(WHERE mcp.tool_concurrency = 1 AS 'solo', WHERE mcp.tool_concurrency BETWEEN 2 AND 4 AS 'low', WHERE mcp.tool_concurrency BETWEEN 5 AND 9 AS 'medium', WHERE mcp.tool_concurrency >= 10 AS 'high') SINCE 1 hour ago` |
| High-concurrency calls (≥ 5) | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_concurrency >= 5) AS high_concurrency_calls, count(*) AS total FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| High-concurrency events detail | Table | `SELECT timestamp, mcp.tool_name, mcp.tool_concurrency, mcp.tool_duration_ms, mcp.session_id FROM Transaction WHERE mcp.tool_concurrency >= 5 SINCE 1 hour ago ORDER BY mcp.tool_concurrency DESC LIMIT 50` |

**Row 3 — Thread-level concurrency**

| Widget | Type | NRQL |
|--------|------|------|
| Thread count over time | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` |
| Thread count vs active sessions overlay | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` *(add active_sessions series from Metric on second y-axis)* |
| Thread count distribution | Histogram | `SELECT histogram(mcp.thread_active_count, 60, 30) FROM Transaction SINCE 1 hour ago` |

---

### Dashboard 6 — Saturation
> **Purpose:** Capacity ceiling indicators across threads, queues, sessions, and per-tool concurrency. Answers "how close are we to the limit?"

**Row 1 — Saturation gauges (last 5 minutes)**

| Widget | Type | NRQL |
|--------|------|------|
| Thread utilisation % | Billboard | `SELECT max(mcp.thread_active_count) * 100.0 / 50 AS thread_util_pct FROM Transaction SINCE 5 min ago` *(adjust 50 to your gunicorn `--threads` value)* |
| Max queue depth | Billboard | `SELECT max(mcp.session_queue_depth) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL SINCE 5 min ago` |
| Max tool concurrency | Billboard | `SELECT max(mcp.tool_concurrency) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 5 min ago` |
| Avg queue wait ms | Billboard | `SELECT average(Custom/MCP/queue_wait_ms) FROM Metric SINCE 5 min ago` |

**Row 2 — Saturation trends**

| Widget | Type | NRQL |
|--------|------|------|
| Thread count vs critical threshold | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` *(add reference line at 45 in NR UI)* |
| Queue depth over time | Line chart | `SELECT max(mcp.session_queue_depth) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Queue wait time (avg + max) | Line chart | `SELECT average(Custom/MCP/queue_wait_ms) AS avg_ms, max(Custom/MCP/queue_wait_ms) AS max_ms FROM Metric TIMESERIES SINCE 3 hours ago` |

**Row 3 — Headroom and capacity**

| Widget | Type | NRQL |
|--------|------|------|
| Thread headroom (threads remaining) | Line chart | `SELECT 50 - max(mcp.thread_active_count) AS headroom FROM Transaction TIMESERIES SINCE 3 hours ago` |
| Active sessions vs ceiling | Line chart | `SELECT latest(Custom/MCP/active_sessions) AS active FROM Metric TIMESERIES SINCE 3 hours ago` *(add reference line at session ceiling in NR UI)* |
| Tool concurrency heatmap | Table | `SELECT filter(count(*), WHERE mcp.tool_concurrency = 1) AS solo, filter(count(*), WHERE mcp.tool_concurrency BETWEEN 2 AND 4) AS low, filter(count(*), WHERE mcp.tool_concurrency BETWEEN 5 AND 9) AS medium, filter(count(*), WHERE mcp.tool_concurrency >= 10) AS critical FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago LIMIT MAX` |

**Row 4 — Probe and rate-limit pressure**

| Widget | Type | NRQL |
|--------|------|------|
| Rate-limited request trend | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.rate_limited = true TIMESERIES SINCE 3 hours ago` |
| Unauth tracker size (leak proxy) | Line chart | `SELECT max(Custom/MCP/unauth_tracker_size) FROM Metric TIMESERIES SINCE 3 hours ago` |

---

### Dashboard 7 — Workflow Tracing (Session Drill-Down)
> **Purpose:** Full per-session workflow replay — timeline, prompts, failures, CDS calls, token cost.  
> **Template variable:** `session_id` → `SELECT uniques(session_id) FROM MCPSessionSummary SINCE 1 day ago LIMIT 200` — select from the dropdown or paste a session ID.

**Row 1 — Session header**

| Widget | Type | NRQL |
|--------|------|------|
| Session duration | Billboard | `SELECT duration_ms FROM MCPSessionSummary WHERE session_id = {{session_id}} SINCE 24 hours ago LIMIT 1` |
| Tool calls | Billboard | `SELECT tool_call_count FROM MCPSessionSummary WHERE session_id = {{session_id}} SINCE 24 hours ago LIMIT 1` |
| Errors / degraded | Billboard | `SELECT tool_error_count AS errors, tool_degraded_count AS degraded FROM MCPSessionSummary WHERE session_id = {{session_id}} SINCE 24 hours ago LIMIT 1` |
| Server work % | Billboard | `SELECT server_work_pct FROM MCPSessionSummary WHERE session_id = {{session_id}} SINCE 24 hours ago LIMIT 1` |

**Row 2 — Tool execution timeline**

| Widget | Type | NRQL |
|--------|------|------|
| Step-by-step timeline | Table | `SELECT mcp.session_tool_seq AS step, mcp.tool_name AS tool, mcp.tool_result_status AS status, mcp.tool_start_offset_ms AS started_at_ms, mcp.ai_think_time_ms AS ai_gap_before_ms, mcp.tool_duration_ms AS duration_ms, mcp.degraded_reason, mcp.error_category, mcp.tool_input FROM Transaction WHERE mcp.session_id = {{session_id}} AND mcp.tool_name IS NOT NULL ORDER BY mcp.session_tool_seq ASC SINCE 24 hours ago LIMIT MAX` |

**Row 3 — Prompt sequence**

| Widget | Type | NRQL |
|--------|------|------|
| Prompts per step | Table | `SELECT timestamp, tool_name, prompt_source, prompt_text, estimated_prompt_tokens, trace_id FROM MCPPrompt WHERE session_id = {{session_id}} SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX` |

**Row 4 — Failures**

| Widget | Type | NRQL |
|--------|------|------|
| Exceptions | Table | `SELECT timestamp, tool_name, error_type, error_message, error_category, duration_ms, tool_start_offset_ms, trace_id FROM MCPToolError WHERE session_id = {{session_id}} SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX` |
| Degraded results | Table | `SELECT timestamp, tool_name, degraded_reason, duration_ms, tool_start_offset_ms, trace_id FROM MCPToolDegraded WHERE session_id = {{session_id}} SINCE 24 hours ago ORDER BY timestamp ASC LIMIT MAX` |

**Row 5 — Token cost**

| Widget | Type | NRQL |
|--------|------|------|
| Total input tokens | Billboard | `SELECT sum(mcp.estimated_prompt_tokens) AS input_tokens FROM Transaction WHERE mcp.session_id = {{session_id}} AND mcp.tool_name IS NOT NULL SINCE 24 hours ago` |
| Total output tokens | Billboard | `SELECT sum(mcp.estimated_output_tokens) AS output_tokens FROM Transaction WHERE mcp.session_id = {{session_id}} AND mcp.tool_name IS NOT NULL SINCE 24 hours ago` |
| AI think time total | Billboard | `SELECT duration_ms - total_tool_duration_ms AS ai_think_total_ms FROM MCPSessionSummary WHERE session_id = {{session_id}} SINCE 24 hours ago LIMIT 1` |

**Row 6 — CDS performance per step**

| Widget | Type | NRQL |
|--------|------|------|
| CDS calls per step | Table | `SELECT mcp.session_tool_seq AS step, mcp.tool_name, cds.endpoint, cds.latency_ms, cds.retry_count, cds.timed_out, cds.http_status FROM Transaction WHERE mcp.session_id = {{session_id}} AND cds.endpoint IS NOT NULL ORDER BY mcp.session_tool_seq ASC SINCE 24 hours ago LIMIT MAX` |

---

### Dashboard 8 — Latency Heatmaps
> **Purpose:** Latency distribution shape over time — spot bimodal distributions, tail growth, and error-path drag.

**Row 1 — Tool latency 2D heatmap**

| Widget | Type | NRQL |
|--------|------|------|
| Tool duration heatmap (all tools) | Heatmap | `SELECT histogram(mcp.tool_duration_ms, 10000, 40) FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Tool duration snapshot by tool | Histogram | `SELECT histogram(mcp.tool_duration_ms, 10000, 40) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |

**Row 2 — Percentile band trends**

| Widget | Type | NRQL |
|--------|------|------|
| Tool latency percentile bands | Line chart | `SELECT percentile(mcp.tool_duration_ms, 10, 25, 50, 75, 95, 99) FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| CDS latency percentile bands | Line chart | `SELECT percentile(cds.latency_ms, 10, 25, 50, 75, 95, 99) FROM Transaction WHERE cds.endpoint IS NOT NULL TIMESERIES SINCE 3 hours ago` |

**Row 3 — Success vs error path comparison**

| Widget | Type | NRQL |
|--------|------|------|
| Success path histogram | Histogram | `SELECT histogram(mcp.tool_duration_ms, 10000, 40) FROM Transaction WHERE mcp.tool_result_status = 'success' AND mcp.tool_name IS NOT NULL SINCE 1 hour ago` |
| Error path histogram | Histogram | `SELECT histogram(mcp.tool_duration_ms, 10000, 40) FROM Transaction WHERE mcp.tool_result_status = 'error' AND mcp.tool_name IS NOT NULL SINCE 1 hour ago` |
| Latency by result status | Table | `SELECT average(mcp.tool_duration_ms) AS avg_ms, percentile(mcp.tool_duration_ms, 95) AS p95_ms, percentile(mcp.tool_duration_ms, 99) AS p99_ms FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET cases(WHERE mcp.tool_result_status = 'success' AS 'success', WHERE mcp.tool_result_status = 'degraded' AS 'degraded', WHERE mcp.tool_result_status = 'error' AS 'error') SINCE 1 hour ago` |

**Row 4 — CDS latency heatmap**

| Widget | Type | NRQL |
|--------|------|------|
| CDS latency heatmap | Heatmap | `SELECT histogram(cds.latency_ms, 10000, 40) FROM Transaction WHERE cds.endpoint IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| CDS p95 by endpoint over time | Line chart | `SELECT percentile(cds.latency_ms, 95) FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint TIMESERIES SINCE 3 hours ago` |

---

### Dashboard 9 — Failure Analysis
> **Purpose:** Failure root cause across categories, tools, publishers, and error types.  
> **Template variable:** `publisher_id` → `SELECT uniques(cds.publisher_id) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 day ago`.

**Row 1 — Failure overview KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Total exceptions (last hour) | Billboard | `SELECT count(*) FROM MCPToolError SINCE 1 hour ago` |
| Total degraded (last hour) | Billboard | `SELECT count(*) FROM MCPToolDegraded SINCE 1 hour ago` |
| Error rate % trend | Line chart | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Error category breakdown | Pie chart | `SELECT count(*) FROM Transaction WHERE mcp.error_category IS NOT NULL FACET mcp.error_category SINCE 1 hour ago` |

**Row 2 — Category trends**

| Widget | Type | NRQL |
|--------|------|------|
| Error category stacked trend | Stacked area | `SELECT filter(count(*), WHERE mcp.error_category = 'timeout') AS timeout, filter(count(*), WHERE mcp.error_category = 'auth_error') AS auth_error, filter(count(*), WHERE mcp.error_category = 'upstream_error') AS upstream_error, filter(count(*), WHERE mcp.error_category = 'client_error') AS client_error, filter(count(*), WHERE mcp.error_category = 'system_error') AS system_error FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Error rate % by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |

**Row 3 — Degraded analysis**

| Widget | Type | NRQL |
|--------|------|------|
| Degraded by reason | Bar chart | `SELECT count(*) FROM MCPToolDegraded FACET degraded_reason SINCE 24 hours ago` |
| Degraded rate % by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_degraded = true) * 100.0 / count(*) AS degraded_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 24 hours ago` |

**Row 4 — Publisher and CDS failure breakdown**

| Widget | Type | NRQL |
|--------|------|------|
| Error rate by publisher | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET cds.publisher_id SINCE 24 hours ago` |
| CDS errors by endpoint | Bar chart | `SELECT filter(count(*), WHERE cds.http_status >= 500) AS server_errors, filter(count(*), WHERE cds.http_status >= 400 AND cds.http_status < 500) AS client_errors FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint SINCE 24 hours ago` |

**Row 5 — Failure detail tables**

| Widget | Type | NRQL |
|--------|------|------|
| Recent MCPToolError events | Table | `SELECT timestamp, tool_name, error_type, error_message, error_category, publisher_id, duration_ms, session_id FROM MCPToolError SINCE 3 hours ago ORDER BY timestamp DESC LIMIT 50` |
| Auth failure details | Table | `SELECT timestamp, mcp.session_id, auth.failure_reason, auth.publisher_id, auth.flow FROM Transaction WHERE auth.result = 'failure' SINCE 3 hours ago ORDER BY timestamp DESC LIMIT 50` |

---

### Dashboard 10 — Active Sessions
> **Purpose:** Live SSE session state, open/close lifecycle, duration distribution, per-publisher session health.

**Row 1 — Live session KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Active sessions now | Billboard | `SELECT latest(Custom/MCP/active_sessions) FROM Metric` |
| Sessions opened (last hour) | Billboard | `SELECT count(*) FROM SSESessionOpen SINCE 1 hour ago` |
| Sessions closed (last hour) | Billboard | `SELECT count(*) FROM SSESessionClose SINCE 1 hour ago` |
| Avg session duration | Billboard | `SELECT average(duration_ms) AS avg_duration_ms FROM SSESessionClose SINCE 1 hour ago` |

**Row 2 — Lifecycle trends**

| Widget | Type | NRQL |
|--------|------|------|
| Session open rate | Line chart | `SELECT rate(count(*), 1 min) FROM SSESessionOpen TIMESERIES SINCE 3 hours ago` |
| Session close rate | Line chart | `SELECT rate(count(*), 1 min) FROM SSESessionClose TIMESERIES SINCE 3 hours ago` |
| Active sessions trend | Line chart | `SELECT latest(Custom/MCP/active_sessions) FROM Metric TIMESERIES SINCE 3 hours ago` |

**Row 3 — Session quality distribution**

| Widget | Type | NRQL |
|--------|------|------|
| Session duration histogram | Histogram | `SELECT histogram(duration_ms, 600000, 20) FROM SSESessionClose SINCE 24 hours ago` |
| Sessions by publisher | Pie chart | `SELECT count(*) FROM MCPSessionSummary FACET publisher_id SINCE 24 hours ago` |
| Sessions with errors | Table | `SELECT session_id, publisher_id, duration_ms, tool_call_count, tool_error_count, tool_degraded_count, session_client_name FROM MCPSessionSummary WHERE tool_error_count > 0 SINCE 24 hours ago ORDER BY tool_error_count DESC LIMIT 50` |

**Row 4 — Session health signals**

| Widget | Type | NRQL |
|--------|------|------|
| Missing-session events | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.sse_session_missing = true TIMESERIES SINCE 3 hours ago` |
| Sessions by AI client | Bar chart | `SELECT count(*) FROM MCPSessionSummary FACET session_client_name SINCE 24 hours ago` |

---

### Dashboard 11 — Queue Metrics
> **Purpose:** SSE message queue health — depth trend, wait time distribution, backpressure indicators.

**Row 1 — Queue state KPIs (last 5 minutes)**

| Widget | Type | NRQL |
|--------|------|------|
| Max queue depth | Billboard | `SELECT max(mcp.session_queue_depth) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL SINCE 5 min ago` |
| Avg queue wait ms | Billboard | `SELECT average(Custom/MCP/queue_wait_ms) FROM Metric SINCE 5 min ago` |
| Max queue wait ms | Billboard | `SELECT max(Custom/MCP/queue_wait_ms) FROM Metric SINCE 5 min ago` |
| Queue depth > 5 events | Billboard | `SELECT count(*) FROM Transaction WHERE mcp.session_queue_depth > 5 SINCE 1 hour ago` |

**Row 2 — Depth trends**

| Widget | Type | NRQL |
|--------|------|------|
| Queue depth over time | Line chart | `SELECT max(mcp.session_queue_depth) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Queue depth histogram | Histogram | `SELECT histogram(mcp.session_queue_depth, 50, 25) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL SINCE 1 hour ago` |
| High-depth events (> 5) | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.session_queue_depth > 5 TIMESERIES SINCE 3 hours ago` |

**Row 3 — Wait time trends**

| Widget | Type | NRQL |
|--------|------|------|
| Queue wait (avg + max) over time | Line chart | `SELECT average(Custom/MCP/queue_wait_ms) AS avg_ms, max(Custom/MCP/queue_wait_ms) AS max_ms FROM Metric TIMESERIES SINCE 3 hours ago` |
| Queue wait heatmap | Heatmap | `SELECT histogram(mcp.session_queue_depth, 50, 25) FROM Transaction WHERE mcp.session_queue_depth IS NOT NULL TIMESERIES SINCE 3 hours ago` *(queue depth proxy; true wait-time heatmap requires a transaction-level attribute)* |

---

### Dashboard 12 — AI Telemetry
> **Purpose:** Prompt patterns, token cost proxies, AI think-time analysis, publisher cost breakdown.  
> **Template variable:** `publisher_id` → `SELECT uniques(publisher_id) FROM MCPPrompt SINCE 1 day ago`.

**Row 1 — Token volume KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Total input tokens (last hour) | Billboard | `SELECT sum(estimated_prompt_tokens) FROM MCPPrompt SINCE 1 hour ago` |
| Total output tokens (last hour) | Billboard | `SELECT sum(mcp.estimated_output_tokens) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 hour ago` |
| Avg tokens per tool call | Billboard | `SELECT average(estimated_prompt_tokens) FROM MCPPrompt SINCE 1 hour ago` |
| Estimated total tokens (last 24 h) | Billboard | `SELECT sum(total_estimated_tokens) FROM MCPSessionSummary SINCE 24 hours ago` |

**Row 2 — Token trends**

| Widget | Type | NRQL |
|--------|------|------|
| Input token volume by publisher | Stacked area | `SELECT sum(estimated_prompt_tokens) FROM MCPPrompt FACET publisher_id TIMESERIES 1 hour SINCE 24 hours ago` |
| Output token volume by tool | Stacked area | `SELECT sum(mcp.estimated_output_tokens) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name TIMESERIES 1 hour SINCE 24 hours ago` |
| Token cost per session over time | Line chart | `SELECT average(total_estimated_tokens) FROM MCPSessionSummary TIMESERIES 1 hour SINCE 24 hours ago` |

**Row 3 — Prompt patterns**

| Widget | Type | NRQL |
|--------|------|------|
| Prompt source distribution | Pie chart | `SELECT count(*) FROM MCPPrompt FACET prompt_source SINCE 24 hours ago` |
| Prompt char length histogram | Histogram | `SELECT histogram(prompt_char_count, 2000, 20) FROM MCPPrompt SINCE 24 hours ago` |
| Most expensive prompts | Table | `SELECT timestamp, session_id, tool_name, prompt_char_count, estimated_prompt_tokens, prompt_text FROM MCPPrompt ORDER BY prompt_char_count DESC SINCE 24 hours ago LIMIT 20` |

**Row 4 — AI think-time analysis**

| Widget | Type | NRQL |
|--------|------|------|
| Avg AI think time by tool | Bar chart | `SELECT average(mcp.ai_think_time_ms) AS avg_think_ms FROM Transaction WHERE mcp.ai_think_time_ms IS NOT NULL FACET mcp.tool_name SINCE 24 hours ago` |
| Server work % vs AI think time % | Bar chart | `SELECT average(server_work_pct) AS server_pct, 100 - average(server_work_pct) AS ai_pct FROM MCPSessionSummary SINCE 24 hours ago` |
| Per-publisher token usage summary | Table | `SELECT count(*) AS sessions, sum(total_estimated_input_tokens) AS total_input, sum(total_estimated_output_tokens) AS total_output, sum(total_estimated_tokens) AS grand_total FROM MCPSessionSummary FACET publisher_id SINCE 24 hours ago LIMIT MAX` |

---

### Dashboard 13 — Memory & CPU (Infrastructure)
> **Purpose:** In-process resource proxies and infrastructure-agent metrics (if installed).  
> **Note:** True process RSS requires the NR Infrastructure agent. Queries marked *(infra agent)* need it installed and configured.

**Row 1 — In-process proxies**

| Widget | Type | NRQL |
|--------|------|------|
| Thread count (CPU proxy) | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` |
| Unauth tracker size (heap proxy) | Line chart | `SELECT max(Custom/MCP/unauth_tracker_size) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Active sessions (session-dict size proxy) | Line chart | `SELECT latest(Custom/MCP/active_sessions) FROM Metric TIMESERIES SINCE 3 hours ago` |

**Row 2 — Growth trend indicators**

| Widget | Type | NRQL |
|--------|------|------|
| Thread count rate-of-change | Line chart | `SELECT derivative(mcp.thread_active_count, 1 minute) FROM Transaction TIMESERIES SINCE 3 hours ago` |
| Session dict growth trend | Line chart | `SELECT latest(Custom/MCP/active_sessions) FROM Metric TIMESERIES SINCE 24 hours ago` |

**Row 3 — Infrastructure agent metrics *(install NR infra agent to unlock)***

| Widget | Type | NRQL |
|--------|------|------|
| Host memory used % | Line chart | `SELECT average(memoryUsedPercent) FROM SystemSample TIMESERIES SINCE 3 hours ago` *(infra agent)* |
| Host CPU % | Line chart | `SELECT average(cpuPercent) FROM SystemSample TIMESERIES SINCE 3 hours ago` *(infra agent)* |
| Process memory RSS (MB) | Line chart | `SELECT average(memoryResidentSizeBytes) / 1048576 AS rss_mb FROM ProcessSample WHERE processDisplayName LIKE '%gunicorn%' TIMESERIES SINCE 3 hours ago` *(infra agent)* |

---

### Dashboard 14 — Worker Health
> **Purpose:** Gunicorn worker thread lifecycle, load distribution, saturation headroom, capacity planning.

**Row 1 — Worker KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Current thread count | Billboard | `SELECT max(mcp.thread_active_count) AS active_threads FROM Transaction SINCE 1 min ago` |
| Thread utilisation % | Billboard | `SELECT max(mcp.thread_active_count) * 100.0 / 50 AS util_pct FROM Transaction SINCE 1 min ago` |
| Requests since 1 h | Billboard | `SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL OR mcp.transport IS NOT NULL SINCE 1 hour ago` |
| Rate-limited hits | Billboard | `SELECT count(*) FROM Transaction WHERE mcp.rate_limited = true SINCE 1 hour ago` |

**Row 2 — Worker load trends**

| Widget | Type | NRQL |
|--------|------|------|
| Thread count over time | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` |
| Thread count vs request rate | Line chart | `SELECT max(mcp.thread_active_count) AS threads FROM Transaction TIMESERIES SINCE 3 hours ago` *(pair with call rate chart on same page for visual correlation)* |
| Thread count histogram | Histogram | `SELECT histogram(mcp.thread_active_count, 60, 30) FROM Transaction SINCE 1 hour ago` |

**Row 3 — Health indicators**

| Widget | Type | NRQL |
|--------|------|------|
| Pre-exhaustion events (> 30 threads) | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.thread_active_count > 30 TIMESERIES SINCE 3 hours ago` |
| Critical saturation events (> 45 threads) | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.thread_active_count > 45 TIMESERIES SINCE 3 hours ago` |
| Worker request size distribution | Histogram | `SELECT histogram(mcp.request_size_bytes, 50000, 20) FROM Transaction WHERE mcp.request_size_bytes IS NOT NULL SINCE 1 hour ago` |

---

### Dashboard 15 — Error Categories
> **Purpose:** Error classification analysis — category trends, per-tool, per-publisher, resolution time.  
> **Template variable:** `error_category` → `SELECT uniques(mcp.error_category) FROM Transaction WHERE mcp.error_category IS NOT NULL SINCE 1 day ago`.

**Row 1 — Category overview**

| Widget | Type | NRQL |
|--------|------|------|
| Error category breakdown | Pie chart | `SELECT count(*) FROM Transaction WHERE mcp.error_category IS NOT NULL FACET mcp.error_category SINCE 1 hour ago` |
| Category count trend | Stacked area | `SELECT filter(count(*), WHERE mcp.error_category = 'timeout') AS timeout, filter(count(*), WHERE mcp.error_category = 'auth_error') AS auth_error, filter(count(*), WHERE mcp.error_category = 'upstream_error') AS upstream_error, filter(count(*), WHERE mcp.error_category = 'client_error') AS client_error, filter(count(*), WHERE mcp.error_category = 'system_error') AS system_error FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES SINCE 3 hours ago` |
| Total errors (last hour) | Billboard | `SELECT count(*) FROM Transaction WHERE mcp.error_category IS NOT NULL SINCE 1 hour ago` |
| Timeout % of all errors | Billboard | `SELECT filter(count(*), WHERE mcp.error_category = 'timeout') * 100.0 / count(*) AS timeout_pct FROM Transaction WHERE mcp.error_category IS NOT NULL SINCE 1 hour ago` |

**Row 2 — Per-tool error breakdown**

| Widget | Type | NRQL |
|--------|------|------|
| Error categories by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.error_category = 'timeout') AS timeout, filter(count(*), WHERE mcp.error_category = 'auth_error') AS auth_error, filter(count(*), WHERE mcp.error_category = 'upstream_error') AS upstream_error, filter(count(*), WHERE mcp.error_category = 'system_error') AS system_error FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| Error rate % by tool + category | Table | `SELECT count(*) AS error_count FROM Transaction WHERE mcp.error_category IS NOT NULL FACET mcp.tool_name, mcp.error_category SINCE 1 hour ago LIMIT MAX` |

**Row 3 — Per-publisher error breakdown**

| Widget | Type | NRQL |
|--------|------|------|
| Error rate by publisher | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*) AS error_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET cds.publisher_id SINCE 24 hours ago` |
| Auth error trend | Line chart | `SELECT count(*) FROM Transaction WHERE mcp.error_category = 'auth_error' TIMESERIES SINCE 3 hours ago` |

**Row 4 — Error resolution time**

| Widget | Type | NRQL |
|--------|------|------|
| Error duration by category | Bar chart | `SELECT average(mcp.tool_duration_ms) AS avg_ms, percentile(mcp.tool_duration_ms, 95) AS p95_ms FROM Transaction WHERE mcp.error_category IS NOT NULL FACET mcp.error_category SINCE 1 hour ago` |
| Timeout duration distribution | Histogram | `SELECT histogram(mcp.tool_duration_ms, 15000, 30) FROM Transaction WHERE mcp.error_category = 'timeout' SINCE 1 hour ago` |
| Recent errors full detail | Table | `SELECT timestamp, mcp.tool_name, mcp.error_category, mcp.tool_duration_ms, cds.publisher_id, mcp.session_id FROM Transaction WHERE mcp.error_category IS NOT NULL SINCE 1 hour ago ORDER BY timestamp DESC LIMIT 50` |

---

### Dashboard 16 — Auth Flow Health *(new)*
> **Purpose:** Token issuance rate, auth failure breakdown, session-login health.  Quickly answer "is the OAuth flow broken?" without trawling logs.  
> **Alert trigger:** Configure the token issuance billboard to pulse red when value = 0 for > 5 min during business hours.

**Row 1 — Issuance KPIs**

| Widget | Type | NRQL |
|--------|------|------|
| Tokens issued (1 h) | Billboard | `SELECT sum(Custom/Auth/token_issued_count) FROM Metric SINCE 1 hour ago` |
| Session logins (1 h) | Billboard | `SELECT sum(Custom/Auth/session_login_count) FROM Metric SINCE 1 hour ago` |
| Auth failure count (1 h) | Billboard | `SELECT sum(Custom/Auth/auth_failure_count) FROM Metric SINCE 1 hour ago` |
| Failure % | Billboard | `SELECT sum(Custom/Auth/auth_failure_count) * 100.0 / nullif(sum(Custom/Auth/token_issued_count) + sum(Custom/Auth/auth_failure_count), 0) AS failure_pct FROM Metric SINCE 1 hour ago` |

**Row 2 — Trend over time**

| Widget | Type | NRQL |
|--------|------|------|
| Token issuance rate | Line chart | `SELECT rate(sum(Custom/Auth/token_issued_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Auth failure rate | Line chart | `SELECT rate(sum(Custom/Auth/auth_failure_count), 1 min) FROM Metric TIMESERIES SINCE 3 hours ago` |
| Client registrations | Line chart | `SELECT rate(sum(Custom/Auth/client_registered_count), 1 hour) FROM Metric TIMESERIES SINCE 24 hours ago` |

**Row 3 — Failure breakdown**

| Widget | Type | NRQL |
|--------|------|------|
| Auth failures by reason | Bar chart | `SELECT count(*) FROM Transaction WHERE auth.result = 'failure' FACET auth.failure_reason SINCE 24 hours ago` |
| Failures by flow | Pie chart | `SELECT count(*) FROM Transaction WHERE auth.result = 'failure' FACET auth.flow SINCE 24 hours ago` |
| Recent auth failures | Table | `SELECT timestamp, auth.flow, auth.failure_reason, auth.publisher_id, auth.client_id FROM Transaction WHERE auth.result = 'failure' SINCE 3 hours ago ORDER BY timestamp DESC LIMIT 50` |

---

### Dashboard 17 — Session Funnel & Abandonment *(new)*
> **Purpose:** Track the full session lifecycle from open → first tool call → completion vs abandonment.

**Row 1 — Funnel overview**

| Widget | Type | NRQL |
|--------|------|------|
| Sessions opened | Billboard | `SELECT count(*) FROM SSESessionOpen SINCE 1 hour ago` |
| Sessions completed | Billboard | `SELECT count(*) FROM MCPSessionSummary WHERE tool_call_count > 0 SINCE 1 hour ago` |
| Sessions abandoned | Billboard | `SELECT count(*) FROM MCPSessionAbandoned SINCE 1 hour ago` |
| Abandon rate % | Billboard | `SELECT sum(Custom/MCP/session_abandon_count) * 100.0 / nullif(count(*), 0) AS abandon_pct FROM SSESessionOpen SINCE 1 hour ago` |

**Row 2 — Abandonment patterns**

| Widget | Type | NRQL |
|--------|------|------|
| Abandon trend | Line chart | `SELECT rate(sum(Custom/MCP/session_abandon_count), 1 hour) FROM Metric TIMESERIES SINCE 24 hours ago` |
| Abandoned sessions by client | Bar chart | `SELECT count(*) FROM MCPSessionAbandoned FACET session_client_name SINCE 24 hours ago` |
| Abandoned sessions by publisher | Bar chart | `SELECT count(*) FROM MCPSessionAbandoned FACET publisher_id SINCE 24 hours ago` |

**Row 3 — Detail table**

| Widget | Type | NRQL |
|--------|------|------|
| Abandoned session detail | Table | `SELECT timestamp, session_id, publisher_id, duration_ms, session_client_name FROM MCPSessionAbandoned SINCE 24 hours ago ORDER BY duration_ms DESC LIMIT 50` |

---

## Alert Conditions (with NRQL)

### Coverage Audit — updated dimensions

| Dimension | Coverage | Alerts |
|-----------|----------|--------|
| p95 latency spikes | ✅ CDS + per-tool | #3, #12 |
| p99 latency spikes | ✅ Added | #13, #14 |
| retry spikes | ✅ Added | #15 |
| timeout spikes | ✅ Covered | #2 |
| tool failures | ✅ Rate + burst | #1, #16 |
| saturation | ✅ Thread critical + pre-warning | #4, #17 |
| queue buildup | ✅ Depth + wait time | #6, #10 |
| queue overflow | ✅ New metric `Custom/MCP/queue_overflow_count` | add alert: > 0 for 5 min |
| memory growth | ⚠️ In-process proxy only — see note below #18 | #18 |
| worker exhaustion | ✅ Critical + pre-warning tiers | #4, #17 |
| session failure rate | ✅ Added | #19 |
| session abandonment | ✅ New metric `Custom/MCP/session_abandon_count` | add alert: > 20% of opens |
| auth flow broken | ✅ New metric `Custom/Auth/token_issued_count` | add alert: = 0 for > 5 min |
| rate limiting | ✅ New metric `Custom/MCP/rate_limited_count` | add alert: > 0 during business hours |
| prompt observability | ✅ New metric `Custom/MCP/prompt_event_dropped_count` | add alert: drop rate > 10% of calls |
| cross-worker routing | ✅ New metric `Custom/MCP/sse_session_missing_count` | add alert: > 0 in multi-worker deploy |
| degraded workflows | ✅ Global rate + per-tool anomaly | #9, Anomaly-D |
| abnormal traffic | ✅ Spike + sustained drop + signal loss | Anomaly-C, Anomaly-E, signal loss |
| error bursts | ✅ Added short-window burst | #16 |

> **Noise reduction philosophy applied throughout:** every threshold alert uses a sliding window long enough to absorb single-spike noise (≥ 5 minutes for fast signals, ≥ 10 minutes for latency). Per-tool `FACET` alerts use a `gap_filling_strategy: none` so sparse tools don't produce zero-value false fires. Anomaly alerts are set to 3 σ (upper) — tighter than NR's default 2 σ — to reduce chatter from minor seasonal variation.

---

### Threshold Alerts — Critical (page immediately)

```yaml
# ── 1. Tool error rate ────────────────────────────────────────────────────────
# Why 5%: at low traffic 1-2 errors should not page, but sustained 5%+ means
#         something is structurally wrong (bad deploy, CDS down, auth expired).
# Noise reduction: 10-minute window + evaluation_delay absorbs deploy spikes.
#                  Filter mcp.tool_name IS NOT NULL so health-check & auth
#                  transactions don't dilute the denominator.
name: MCP Tool Error Rate
query: |
  SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*)
  FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
threshold: > 5 (%)
window: 10 minutes
evaluation_delay: 90s   # let the deploy settle before scoring it

# ── 2. CDS timeout spike ──────────────────────────────────────────────────────
# Why 5/min: each timeout costs 5 s of thread time + a retry; 5/min with a
#            50-thread worker means ~8% of capacity wasted on retries.
# Noise reduction: 5-minute window absorbs bursty CDS hiccups.
name: CDS Timeout Rate
query: |
  SELECT rate(sum(Custom/CDS/timeout_count), 1 minute) FROM Metric
threshold: > 5 per minute
window: 5 minutes

# ── 3. CDS p95 latency ────────────────────────────────────────────────────────
# Why 4000 ms: CDS timeout per attempt is 5 s; p95 > 4 s means 5% of calls are
#              near-timeout — upstream is struggling.
# Noise reduction: 10-minute window avoids brief upstream GC pauses.
name: CDS p95 Latency
query: |
  SELECT percentile(cds.latency_ms, 95) FROM Transaction
  WHERE cds.endpoint IS NOT NULL
threshold: > 4000 ms
window: 10 minutes

# ── 4. Worker thread saturation — critical ────────────────────────────────────
# Why 45: gunicorn default is threads=50 per worker. > 45 means the worker
#         will reject or queue new requests within seconds.
# Noise reduction: max() over 5 minutes; single-spike threads resolve fast.
# Note: this is per-worker only (see Worker Saturation Note section below).
name: Worker Thread Saturation — Critical
query: |
  SELECT max(mcp.thread_active_count) FROM Transaction
threshold: > 45
window: 5 minutes

# ── 6. SSE queue depth high ───────────────────────────────────────────────────
# Why 10: a queue > 10 messages deep means the SSE consumer is not reading the
#         stream — likely a stalled client holding a thread with no drain.
# Noise reduction: 3-minute window; brief depth spikes on burst tool calls are normal.
name: SSE Queue Depth High
query: |
  SELECT max(mcp.session_queue_depth) FROM Transaction
  WHERE mcp.session_queue_depth IS NOT NULL
threshold: > 10
window: 3 minutes

# ── 10. Queue wait time p95 ───────────────────────────────────────────────────
# Why 500 ms: the SSE generator should drain the queue in < 25 ms under normal
#             load. p95 > 500 ms means the generator thread is consistently
#             behind — CPU-bound or the client is slow-reading.
# Noise reduction: p95 over 5 minutes; NR metric flush adds ~100 ms jitter.
name: SSE Queue Wait Time High
query: |
  SELECT percentile(Custom/MCP/queue_wait_ms, 95) FROM Metric
threshold: > 500 ms
window: 5 minutes

# ── 12. Tool p95 latency — per tool (NEW) ────────────────────────────────────
# Why 4500 ms: tool wraps CDS (5 s timeout); p95 > 4.5 s means most of the
#              95th-percentile budget is already consumed in CDS, leaving no
#              room for processing overhead.
# Noise reduction: FACET mcp.tool_name so a slow tool doesn't mask a fast one.
#                  10-minute window absorbs CDS cold-start jitter on first call.
name: MCP Tool p95 Latency Spike
query: |
  SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
  FACET mcp.tool_name
threshold: > 4500 ms
window: 10 minutes
gap_filling_strategy: none   # sparse tools: no fill → no false fires at zero

# ── 13. Tool p99 latency (NEW) ────────────────────────────────────────────────
# Why 7000 ms: at p99, the call has almost certainly hit the 5 s CDS timeout
#              plus retry backoff (1 s). 7 s p99 means ~1% of calls are full
#              double-timeout failures — user-visible hangs.
# Anomaly alternative also provided below (Anomaly-B) for tools with seasonal
# traffic patterns where a fixed threshold would fire at night.
name: MCP Tool p99 Latency Spike
query: |
  SELECT percentile(mcp.tool_duration_ms, 99) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
  FACET mcp.tool_name
threshold: > 7000 ms
window: 10 minutes
gap_filling_strategy: none

# ── 14. CDS p99 latency (NEW) ─────────────────────────────────────────────────
# Why 6000 ms: CDS timeout is 5 s with 1-retry + 1 s backoff = 11 s max.
#              p99 > 6 s signals the retry path is routinely triggering.
name: CDS p99 Latency Spike
query: |
  SELECT percentile(cds.latency_ms, 99) FROM Transaction
  WHERE cds.endpoint IS NOT NULL
  FACET cds.endpoint
threshold: > 6000 ms
window: 10 minutes
gap_filling_strategy: none

# ── 15. CDS retry rate spike (NEW) ───────────────────────────────────────────
# Why 5/min: each retry adds 1 s of latency and doubles the load on CDS.
#            5 retries/min means upstream instability is ongoing — distinct from
#            the timeout alert because retries can succeed (no timeout counted).
# Noise reduction: 5-minute window; single-retry events on a cold boot are normal.
name: CDS Retry Rate Spike
query: |
  SELECT rate(sum(Custom/CDS/retry_count), 1 minute) FROM Metric
threshold: > 5 per minute
window: 5 minutes

# ── 16. Tool error burst — short window (NEW) ─────────────────────────────────
# Why this is different from #1: alert #1 catches sustained elevated error rate
# (> 5% for 10 min). This catches sudden bursts — 10+ errors/min in a 2-minute
# window — that #1 would absorb into its longer window before firing.
# Why 10/min: at 100 calls/min baseline, 10 errors = 10%; at lower traffic the
#             rate guard below prevents false pages on cold traffic.
# Noise reduction: add a WHERE clause requiring at least 1 call/min baseline
#                  to avoid firing during cold/zero-traffic periods.
name: MCP Tool Error Burst
query: |
  SELECT rate(sum(Custom/MCP/tool_error_count), 1 minute) FROM Metric
threshold: > 10 per minute
window: 2 minutes

# ── 19. Session failure rate (NEW) ────────────────────────────────────────────
# Why: MCPSessionSummary gives a per-session error rollup. A session where > 50%
#      of tool calls failed means the publisher/client is in a broken state —
#      more actionable than a global error rate because it's session-scoped.
# Why 30%: some degraded results per session are expected (auth_expired, not_found).
#          30% means more than 1 in 3 tool calls ended in exception — structural.
# Why 15-minute window: sessions vary in length; need enough summaries to aggregate.
name: MCP Session High Error Rate
query: |
  SELECT
    sum(tool_error_count) * 100.0 / sum(tool_call_count)
  FROM MCPSessionSummary
  WHERE tool_call_count > 0
threshold: > 30 (%)
window: 15 minutes
```

---

### Threshold Alerts — Warning (pre-escalation, do not page out-of-hours)

```yaml
# ── 5. SSE session count high ─────────────────────────────────────────────────
# Hard threshold as a ceiling guard; anomaly version (Anomaly-C) is preferred.
# Why 100: tune to 2× your expected peak concurrent users. Hard ceiling = runaway
#          session leak (e.g. sessions not being cleaned up on disconnect).
name: SSE Session Count High
query: |
  SELECT latest(Custom/MCP/active_sessions) FROM Metric
threshold: > 100
window: 5 minutes

# ── 7. Publisher data fallback rate ──────────────────────────────────────────
# Why 3/min: fallbacks return degraded data; 3/min sustained means a CDS
#            endpoint is consistently unavailable for a publisher.
name: Publisher Data Fallback Rate
query: |
  SELECT rate(sum(Custom/MCP/fallback_count), 1 minute) FROM Metric
threshold: > 3 per minute
window: 10 minutes

# ── 8. Auth failure rate ──────────────────────────────────────────────────────
# Why 20 in 10 min: sporadic auth failures are expected (token expiry).
#                   20 failures in 10 min suggests a credential rotation issue
#                   or a client misconfiguration.
name: Auth Failure Rate
query: |
  SELECT count(*) FROM Transaction WHERE auth.result = 'failure'
threshold: > 20
window: 10 minutes

# ── 9. Degraded response rate ─────────────────────────────────────────────────
# Why 5/min: degraded results are partial failures (structured {"error":…} dict).
#            5/min sustained means upstream data is consistently unavailable.
name: MCP Tool Degraded Rate
query: |
  SELECT rate(sum(Custom/MCP/tool_degraded_count), 1 minute) FROM Metric
threshold: > 5 per minute
window: 5 minutes

# ── 11. Tool concurrency saturation ──────────────────────────────────────────
# Why 10: > 10 in-flight calls to a single tool means that tool is monopolising
#         gunicorn threads. Combine with p99 latency to distinguish a slow tool
#         from a traffic surge.
name: Tool Concurrency Spike
query: |
  SELECT max(mcp.tool_concurrency) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
  FACET mcp.tool_name
threshold: > 10 (concurrent calls to one tool)
window: 5 minutes
gap_filling_strategy: none

# ── 17. Worker thread pre-exhaustion — warning tier (NEW) ────────────────────
# Why 30: fires at ~60% of a 50-thread gunicorn config. Gives time to scale
#         before alert #4 (critical at 45) fires. Adjust both thresholds to
#         match your actual gunicorn --threads setting.
# Noise reduction: 5-minute window; short thread spikes on burst requests are normal.
name: Worker Thread Pre-Exhaustion Warning
query: |
  SELECT max(mcp.thread_active_count) FROM Transaction
threshold: > 30
window: 5 minutes

# ── 18. In-process memory growth proxy (NEW) ──────────────────────────────────
# True process RSS requires the NR Infrastructure agent (install alongside the
# Python agent to get host-level memory/CPU). Without it, use these two in-process
# signals as leading indicators of memory leaks:
#
# Proxy A — rate-limiter dict size: grows if old IPs are not being pruned.
#            > 500 unique IPs in the tracker is a proxy for dict memory leak.
name: Unauth Tracker Dict Leak Proxy
query: |
  SELECT max(Custom/MCP/unauth_tracker_size) FROM Metric
threshold: > 500
window: 10 minutes
#
# Proxy B — SSE session dict growing without cleanup:
#            if active_sessions climbs but no SSESessionClose events fire,
#            the _sessions dict is leaking. Combine with alert #5 and the
#            SSESessionClose event rate in a dashboard.
#
# ⚠️  For production: install NR Infrastructure agent and alert on
#      system.memory.usedPercent > 85 FROM SystemSample instead.
```

---

### Baseline (Anomaly) Alerts

Anomaly alerts learn your traffic pattern and fire when the signal deviates significantly from the historical baseline — no fixed threshold to tune. Use these for signals with strong time-of-day or day-of-week patterns.

```yaml
# ── Anomaly-A. Tool average latency (existing) ────────────────────────────────
# Why anomaly not threshold: tool latency varies by tool type and traffic volume.
#   A fixed threshold would either miss spikes for fast tools or cry wolf for
#   inherently slow ones. Anomaly detection adapts per-tool per-hour.
# 3 σ (not default 2 σ): tighter filter reduces chatter from minor traffic shifts.
name: MCP Tool Latency Anomaly
query: |
  SELECT average(mcp.tool_duration_ms) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
  FACET mcp.tool_name
type: baseline
deviation: 3 standard deviations above
window: 10 minutes

# ── Anomaly-B. Tool p99 latency anomaly ──────────────────────────────────────
# Complements alert #13 (fixed threshold). Use this for tools whose p99 varies
# significantly by time of day (e.g. high baseline p99 during peak hours means
# the fixed threshold would page during normal peak load).
name: MCP Tool p99 Latency Anomaly
query: |
  SELECT percentile(mcp.tool_duration_ms, 99) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
  FACET mcp.tool_name
type: baseline
deviation: 3 standard deviations above
window: 10 minutes

# ── Anomaly-C. Request volume spike — abnormal traffic (NEW) ─────────────────
# Why anomaly: traffic volume has strong hourly and daily patterns. A fixed
#   threshold would page on every Monday morning ramp-up.
# Why 4 σ: at 4 σ the false-positive rate is ~0.006% — tight enough for a
#   volume spike that genuinely means something (DDoS, runaway client, viral content).
# Noise reduction: 5-minute window; upper direction only so organic traffic growth
#   doesn't page (that's a success, not an incident).
name: MCP Traffic Spike — Volume Anomaly
query: |
  SELECT rate(count(*), 1 minute) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
type: baseline
deviation: 4 standard deviations above
window: 5 minutes
direction: upper_only

# ── Anomaly-D. Degraded rate per tool (NEW) ───────────────────────────────────
# Alert #9 fires on global degraded rate. This catches a single tool degrading
# abnormally while global rate stays low (e.g. only get_post is auth_expired).
# Why anomaly: degraded rate per tool varies — some tools rarely degrade, some
#   routinely degrade during off-hours when CDS is quieter. Baseline adapts.
name: Per-Tool Degraded Rate Anomaly
query: |
  SELECT rate(count(*), 1 minute) FROM MCPToolDegraded
  FACET tool_name
type: baseline
deviation: 3 standard deviations above
window: 10 minutes

# ── Anomaly-E. Traffic drop — sustained below-normal (NEW) ───────────────────
# The signal-loss alert fires at zero traffic. This catches sustained below-normal
# traffic (e.g. 80% traffic drop from a broken load balancer) before it hits zero.
# Why 3 σ below: fires when traffic drops more than 3 standard deviations below
#   the baseline. Lower bound only — don't page for organic traffic reduction.
# Why 15-minute window: avoids firing on brief quiet spells (deployments, cron gaps).
name: MCP Traffic Drop — Below Normal
query: |
  SELECT rate(count(*), 1 minute) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL
type: baseline
deviation: 3 standard deviations below
window: 15 minutes
direction: lower_only

# ── Anomaly-F. CDS error rate (existing, improved) ────────────────────────────
# Kept as anomaly (not threshold) because CDS error rate varies by endpoint.
# 3 σ above; per-endpoint FACET added to distinguish endpoint-specific failures.
name: CDS Error Rate Anomaly
query: |
  SELECT rate(sum(Custom/CDS/error_count), 1 minute) FROM Metric
type: baseline
deviation: 3 standard deviations above
window: 5 minutes
```

---

### Signal Loss Alert

```yaml
# ── Signal loss: MCP server completely unresponsive ───────────────────────────
# Fires when zero tool transactions arrive for 5 minutes. Covers:
#   - process crash / OOM kill
#   - Railway container restart loop
#   - WSGI worker all-dead (gunicorn master alive but workers gone)
# Noise reduction: 5-minute timer avoids false fires on rolling deploys that
#   complete in < 3 minutes. Increase to 10 minutes if your deploys take longer.
name: MCP Server No Traffic
query: |
  SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL
signal_loss_timer: 5 minutes
action: open_violation_on_signal_loss
```

---

### Additional Alert Specs — from Coverage Audit

These six conditions were listed in the Coverage Audit as "add alert:" but had no formal YAML spec. They are now fully defined.

```yaml
# ── 20. SSE queue overflow ────────────────────────────────────────────────────
# Why: a queue overflow means the AI client is not draining the SSE stream and a
#      tool response was silently dropped. Even one occurrence is user-impacting.
# Why 2-minute window: overflow is immediate and critical — don't wait.
name: SSE Queue Overflow
query: |
  SELECT rate(sum(Custom/MCP/queue_overflow_count), 1 min) FROM Metric
threshold: > 0
window: 2 minutes

# ── 21. Session abandonment rate high ────────────────────────────────────────
# Why 20%: > 20% of sessions opening with zero tool calls means a client
#           misconfiguration, auth loop, or network blip is preventing tool use.
#           Some abandonment (< 20%) is normal for exploratory or probe connections.
# Why 30-minute window: need enough session opens to make the ratio meaningful.
name: Session Abandonment Rate High
query: |
  SELECT
    sum(Custom/MCP/session_abandon_count) * 100.0
      / nullif(count(*), 0) AS abandon_pct
  FROM SSESessionOpen
threshold: > 20 (%)
window: 30 minutes

# ── 22. OAuth token issuance stopped ─────────────────────────────────────────
# Why signal loss: if token_issued_count goes to zero for 5 min during expected
#                  business hours, the PKCE flow is broken — new clients cannot connect.
# Alternative threshold: rate(sum(Custom/Auth/token_issued_count), 1 min) = 0
#                        for 5 minutes if signal-loss alerts are not available on Metric.
name: OAuth Token Issuance Stopped
query: |
  SELECT rate(sum(Custom/Auth/token_issued_count), 1 min) FROM Metric
signal_loss_timer: 5 minutes
action: open_violation_on_signal_loss

# ── 23. Unauthenticated probe wave ────────────────────────────────────────────
# Why: rate_limited_count > 0 means IPs are hitting the unauthenticated rate limiter.
#      Non-zero during business hours is either a misconfigured client or a probe wave.
# Why 5-minute window: a single accidental 429 is noise; 5 min of sustained rate-limit
#                      hits is a real event worth investigating.
name: Unauthenticated Probe Wave
query: |
  SELECT rate(sum(Custom/MCP/rate_limited_count), 1 min) FROM Metric
threshold: > 0
window: 5 minutes

# ── 24. MCPPrompt observability degraded ─────────────────────────────────────
# Why 10%: prompt_event_dropped_count > 10% of tool calls means the 1 000/min
#           MCPPrompt rate limiter is undersized for current traffic. At this rate
#           prompt-pattern dashboards become statistically unreliable.
# Fix: increase _PROMPT_EVENT_MAX_PER_MIN in views.py or reduce traffic.
name: MCPPrompt Event Drop Rate High
query: |
  SELECT
    sum(Custom/MCP/prompt_event_dropped_count) * 100.0
      / nullif(sum(Custom/MCP/tool_call_count), 0) AS drop_pct
  FROM Metric
threshold: > 10 (%)
window: 5 minutes

# ── 25. Cross-worker SSE session routing failures ─────────────────────────────
# Why: sse_session_missing_count > 0 in a multi-worker deploy means a POST /mcp/message
#      arrived at a worker that does not hold the matching SSE session. The AI client
#      receives an error and the tool call is dropped.
# In a single-worker deploy this should never fire; treat any non-zero as an alert.
# In a multi-worker deploy, tune threshold to expected baseline (each worker handles
# its own sessions; only cross-worker routing failures should count).
name: SSE Session Routing Failure
query: |
  SELECT rate(sum(Custom/MCP/sse_session_missing_count), 1 min) FROM Metric
threshold: > 0
window: 2 minutes
```

---

### Memory Growth — Note on Instrumentation Gap

True process RSS (resident set size) is **not available** from the New Relic Python agent alone. It requires the **NR Infrastructure agent** running alongside the app process on the same host. Once installed it provides `SystemSample` events with `system.memory.usedPercent`, `processSample` events with `memoryResidentSizeBytes` per PID, and host-level CPU saturation.

**Without the Infrastructure agent, use these in-process proxies:**

| Proxy signal | Metric | What it detects |
|---|---|---|
| Unauth IP tracker size | `Custom/MCP/unauth_tracker_size` | `_unauth_hits` dict not being pruned (alert #18A) |
| SSE session dict growth | `Custom/MCP/active_sessions` rising without close events | Session objects not being GC'd after disconnect (alert #5) |
| Thread count growth | `mcp.thread_active_count` creeping up between requests | Thread leak from unjoined threads or unclosed SSE streams (alert #4) |

**Recommended action:** Add the Railway NR Infrastructure agent layer and create:
```yaml
name: Host Memory High
query: SELECT average(memoryUsedPercent) FROM SystemSample
threshold: > 85 (%)
window: 5 minutes
```

---

### Alert Noise Reduction Playbook

| Technique | When to apply |
|-----------|--------------|
| `evaluation_delay: 90s` | Any alert that could fire on a rolling deploy |
| `gap_filling_strategy: none` | Per-tool `FACET` alerts — sparse tools emit no data between calls; filling with zero creates false fires |
| Longer window (10–15 min) | Latency and session alerts — single slow calls are not incidents |
| Shorter window (2–3 min) | Queue depth and error burst — these need fast response |
| `direction: upper_only` | Traffic and concurrency anomalies — organic growth is not an alert |
| Anomaly (3 σ) over fixed threshold | Any signal with strong time-of-day patterns (traffic, latency, degraded rate) |
| Minimum volume guard in query | Error rate % alerts — add `WHERE rate(count(*), 1 min) > 5` or use `sum(error_count)` rate to avoid 1/1 = 100% error rate fires on a single cold call |
| `FACET` + per-entity thresholds | Tool concurrency and p95/p99 latency — prevents a slow tool from masking a fast one |

---

## Worker Saturation Note

`mcp.thread_active_count` reports `threading.active_count()` **within a single gunicorn worker process**. With multiple workers, each reports independently. This is a per-worker thread count, not a cross-worker utilisation metric. To get cross-worker saturation, use the NR Infrastructure agent or configure `--statsd-host` on gunicorn to export worker metrics to StatsD → NR.

---

## Thread Profiler — CPU call-tree snapshots

Enabled via `thread_profiler.enabled = true` in `newrelic.ini`. The thread profiler captures a statistical call tree of all active threads every 100 ms during a scheduled session. It is the fastest way to diagnose CPU-bound or blocked tool calls without adding any code.

**How to schedule a session:**  
`APM → Publive MCP → More Views → Thread Profiler → Schedule profiling session` → set duration (2–5 minutes) → trigger during live traffic.

**What it shows:**  
A call-tree where wide branches represent functions consuming wall time. Look for:
- `cds_get` branches that are wide (CDS round-trip dominating)
- `threading.Event.wait` or `queue.get` branches (threads blocked on I/O or SSE queue drain)
- Any unexpected application code consuming CPU outside of tool execution

**When to use:**  
- p99 latency is elevated but CDS latency looks normal → a CPU-bound segment in the tool pipeline
- Thread count creeps up without a corresponding traffic increase → blocked thread leak

---

## Django Auto-Instrumentation — built-in APM visibility

The `WSGIApplicationWrapper` in `wsgi.py` activates full Django auto-instrumentation. No additional wiring is required. The following Django internals are auto-captured as segments in every transaction waterfall:

| Segment | What it times |
|---|---|
| `Django/ORM/Model/get` | `OAuthToken.objects.get()` and every other ORM query |
| `Django/ORM/Model/filter` / `create` / `delete` | OAuthClient, OAuthCode, OAuthToken write operations |
| `Django/View/*` | Time spent in each view function |
| `Django/URL/Routing` | URL dispatcher resolution overhead |
| `Django/Middleware/*` | Each middleware in the MIDDLEWARE list |

**Where to see this in NR UI:**  
`APM → Publive MCP → Monitor → Databases` — ORM query breakdown, slow query list, call counts per table.  
`APM → Publive MCP → Monitor → Transactions → [click any tool-call trace] → Databases tab` — per-request DB timing.

**Most useful query:**
```sql
-- Find slow OAuthToken lookups (DB latency on the _get_credentials path)
SELECT average(duration) AS avg_ms, max(duration) AS max_ms, count(*) AS calls
FROM Transaction
WHERE name LIKE '%/mcp%'
  AND databaseDuration IS NOT NULL
FACET databaseCallCount SINCE 24 hours ago
```

---

## SQL Query Tracing — slow query capture with obfuscation

Configured in `newrelic.ini`:
- `transaction_tracer.record_sql = obfuscated` — SQL statements are captured with literals stripped (e.g. `WHERE token = ?`) so no PII or credentials appear in NR traces
- `transaction_tracer.explain_enabled = true` — auto-captures `EXPLAIN` output for queries slower than 0.5 s
- `transaction_tracer.explain_threshold = 0.5` — the 0.5 s explain-plan threshold

**Where to see this in NR UI:**  
`APM → Publive MCP → Monitor → Databases → [click a slow query]` — shows the obfuscated SQL, call count, avg/max latency.  
`APM → Publive MCP → Monitor → Transaction Traces → [click a trace] → Database Queries tab` — per-request SQL with explain plan if the query exceeded 0.5 s.

**Practical use:**  
If `OAuthToken.objects.get(token=...)` starts appearing in slow query lists, it means the token index is missing or the SQLite file is under I/O pressure. NR will show the explain plan automatically for queries > 0.5 s.

**Note:** SQLite (the dev/staging DB) does not support `EXPLAIN` in NR's format. Explain plans only appear when PostgreSQL or MySQL is in use.

---

## Audit Status

| # | Concern | Status |
|---|---------|--------|
| 1 | Distributed tracing | ✅ Enabled; CDS spans attributed |
| 2 | Tool-level tracing | ✅ All tools have fn_trace segments + per-tool metrics |
| 3 | Span correlation | ✅ CDS spans; tool spans carry transaction attrs |
| 4 | Session-level debugging | ✅ session_id + MCPSessionSummary + session_tool_seq |
| 5 | p95/p99 latency | ✅ Via `percentile()` on mcp.tool_duration_ms / cds.latency_ms |
| 6 | Retry tracking | ✅ cds.retry_count, cds.retried, Custom/CDS/retry_count |
| 7 | Timeout tracking | ✅ cds.timed_out, Custom/CDS/timeout_count |
| 8 | Fallback tracking | ✅ mcp.tool_fallback*, Custom/MCP/fallback_count |
| 9 | Structured JSON logs | ✅ python-json-logger, LOGGING config in settings.py |
| 10 | Trace-log correlation | ✅ NR agent injects trace.id into JSON log records |
| 11 | Tool invocation metrics | ✅ Custom/Tool/{name}/call_count per tool |
| 12 | Tool failure metrics | ✅ Custom/Tool/{name}/error_count per tool |
| 13 | Concurrent session monitoring | ✅ Custom/MCP/active_sessions |
| 14 | Queue depth monitoring | ✅ mcp.session_queue_depth + Custom/MCP/session_queue_depth |
| 15 | Queue wait time monitoring | ✅ Custom/MCP/queue_wait_ms emitted per SSE message dequeue |
| 16 | Memory leak monitoring | ✅ active_sessions + unauth_tracker_size as leak proxies |
| 17 | Worker saturation monitoring | ⚠️ Per-worker only; cross-worker needs gunicorn StatsD |
| 18 | NRQL dashboards | ✅ 17 dashboards, ~175 widgets, all 17 observability dimensions covered |
| 19 | Intelligent alerting | ✅ 19 threshold + 6 anomaly + signal-loss + 6 additional specs (queue overflow, session abandonment, auth broken, rate-limit probe, prompt drop, cross-worker routing); all 18 production dimensions covered |
| 20 | SLO/SLI monitoring | ✅ Three SLIs defined; SLI 1 updated to exclude degraded results |
| 21 | AI token/cost monitoring | ✅ mcp.prompt_char_count + estimated_prompt_tokens |
| 22 | Service maps | ✅ Auto-generated via DT; CDS as "External" node |
| 23 | Error categorization | ✅ error.category + mcp.error_category across all layers |
| 24 | Session replay/debugging | ✅ session_tool_seq + MCPSessionSummary + NRQL query |
| 25 | Custom business events | ✅ 10 event types: MCPPrompt, MCPToolError, MCPToolDegraded, MCPUnknownMethod, MCPSessionSummary, SSESessionOpen, SSESessionClose, MCPRateLimit, MCPSessionAbandoned, MCPSessionMissing |
| 26 | Alert noise reduction | ✅ gap_filling_strategy, evaluation_delay, direction guards, FACET per-entity thresholds, anomaly at 3 σ |
| 27 | MCP workflow visualization | ✅ Via session_tool_seq + MCPSessionSummary table widget |
| 28 | Degraded response tracking | ✅ MCPToolDegraded event, mcp.tool_is_degraded, Custom/Tool/{name}/degraded_count |
| 29 | Per-tool concurrency tracking | ✅ mcp.tool_concurrency, Custom/Tool/{name}/active_calls, _active_tool_calls + lock in tools.py |
| 30 | Error duration metric | ✅ Custom/Tool/{name}/error_duration_ms emitted in exception path |
| 31 | SSE open NameError fix | ✅ _sessions_lock block moved before record_event("SSESessionOpen") |
| 32 | Event ↔ trace correlation | ✅ trace_id + span_id on MCPPrompt, MCPToolError, MCPToolDegraded, SSESessionOpen via get_linking_metadata() |
| 33 | Session timeline reconstruction | ✅ mcp.tool_start_offset_ms and mcp.ai_think_time_ms on every tool transaction |
| 34 | Output token estimation | ✅ mcp.estimated_output_tokens = result_size ÷ 4 on success path |
| 35 | Session-level token totals | ✅ MCPSessionSummary.total_estimated_input/output_tokens, total_estimated_tokens |
| 36 | Session server-work ratio | ✅ MCPSessionSummary.server_work_pct = total_tool_duration_ms / duration_ms × 100 |
| 37 | Cross-transaction session join | ✅ mcp.session_trace_id stamped on all mcp_message transactions; also in SSESessionOpen/Close events |
| 38 | AI think-time measurement | ✅ mcp.ai_think_time_ms = gap between last tool response enqueue and next tool call start |
| 39 | One-click session debug dashboard | ✅ 9 parameterized NRQL queries in NewRelic.md §NRQL → Session-level debugging |
| 40 | Session client name in summary | ✅ MCPSessionSummary.session_client_name captured on first tool call User-Agent |
| 41 | p95 latency alerting — per tool | ✅ Alert #12: percentile(mcp.tool_duration_ms, 95) FACET mcp.tool_name > 4500 ms |
| 42 | p99 latency alerting — per tool | ✅ Alert #13: percentile(mcp.tool_duration_ms, 99) FACET mcp.tool_name > 7000 ms |
| 43 | p99 latency alerting — CDS | ✅ Alert #14: percentile(cds.latency_ms, 99) FACET cds.endpoint > 6000 ms |
| 44 | CDS retry rate alerting | ✅ Alert #15: rate(sum(Custom/CDS/retry_count), 1 min) > 5/min |
| 45 | Error burst alerting | ✅ Alert #16: rate(sum(Custom/MCP/tool_error_count), 1 min) > 10/min, 2-min window |
| 46 | Session failure rate alerting | ✅ Alert #19: sum(tool_error_count)/sum(tool_call_count) > 30% from MCPSessionSummary |
| 47 | Worker pre-exhaustion warning | ✅ Alert #17: thread_active_count > 30 (warning tier at ~60% of max) |
| 48 | Memory leak proxies | ⚠️ Alert #18 (in-process proxy only); Infrastructure agent needed for true RSS |
| 49 | Traffic spike alerting | ✅ Anomaly-C: 4 σ volume anomaly, upper direction only |
| 50 | Traffic drop alerting | ✅ Anomaly-E: 3 σ below baseline, 15-min window + signal loss at zero |
| 51 | Per-tool degraded anomaly | ✅ Anomaly-D: per-tool degraded rate from MCPToolDegraded FACET tool_name |
| 52 | MCP server health dashboard | ✅ Dashboard 1: SLO row, traffic trends, auth/transport breakdown, 14 widgets |
| 53 | Tool performance dashboard | ✅ Dashboard 2: p50/p95/p99 by tool, throughput, three-way status, summary table |
| 54 | Session analytics dashboard | ✅ Dashboard 3: session lifecycle, quality distribution, publisher breakdown |
| 55 | Retries/fallbacks dashboard | ✅ Dashboard 4: retry/timeout trends, endpoint breakdown, fallback events |
| 56 | Concurrency dashboard | ✅ Dashboard 5: per-tool concurrency time series, saturation tiers, thread correlation |
| 57 | Saturation dashboard | ✅ Dashboard 6: headroom gauges, thread/queue/session ceilings, rate-limit pressure |
| 58 | Workflow tracing dashboard | ✅ Dashboard 7: parameterized 6-row session drill-down with template variable |
| 59 | Latency heatmaps dashboard | ✅ Dashboard 8: 2D heatmaps, p10–p99 bands, success vs error path comparison |
| 60 | Failure analysis dashboard | ✅ Dashboard 9: category trends, publisher breakdown, root-cause tables |
| 61 | Active sessions dashboard | ✅ Dashboard 10: open/close lifecycle, duration histogram, per-publisher health |
| 62 | Queue metrics dashboard | ✅ Dashboard 11: depth trend, wait time avg/max, backpressure indicators |
| 63 | AI telemetry dashboard | ✅ Dashboard 12: token volume, prompt patterns, think-time, publisher cost |
| 64 | Memory/CPU dashboard | ✅ Dashboard 13: in-process proxies + infra agent queries (SystemSample, ProcessSample) |
| 65 | Worker health dashboard | ✅ Dashboard 14: thread lifecycle, saturation events, load distribution |
| 66 | Error categories dashboard | ✅ Dashboard 15: category trends, per-tool/publisher breakdown, resolution time |
| 67 | Auth flow health dashboard | ✅ Dashboard 16: token issuance KPIs, failure breakdown, trend over time |
| 68 | Session funnel & abandonment dashboard | ✅ Dashboard 17: open → tool-call → abandon funnel, patterns by client/publisher |
| 69 | Auth-level metrics | ✅ `Custom/Auth/token_issued_count`, `auth_failure_count`, `session_login_count`, `client_registered_count` in `auth_app/views.py` |
| 70 | Queue overflow metric + event | ✅ `Custom/MCP/queue_overflow_count` emitted when bounded SSE queue is full after 30 s |
| 71 | Thread profiler | ✅ `thread_profiler.enabled = true` in `newrelic.ini`; schedule via APM → More Views → Thread Profiler |
| 72 | Django ORM auto-instrumentation | ✅ All ORM queries auto-captured as segments; visible in APM → Databases and transaction traces |
| 73 | SQL tracing with obfuscation | ✅ `transaction_tracer.record_sql = obfuscated`; `explain_enabled = true` at 0.5 s threshold |
| 74 | Deployment markers | ✅ `Procfile` release step runs `newrelic-admin record-deploy` with `:-unknown` fallback defaults on every Railway deploy |
| 75 | SIGTERM harvest flush | ✅ `wsgi.py` SIGTERM handler calls `newrelic.agent.shutdown_agent(timeout=10)` so in-flight custom events are not lost on Railway container kill |
| 76 | Six additional alert specs | ✅ Alerts #20–25: queue overflow, session abandonment, OAuth stopped, probe wave, prompt drop, cross-worker routing — defined in §Additional Alert Specs |
