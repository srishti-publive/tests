# New Relic — Publive MCP Server
## Production Observability Reference

What New Relic does for this project, where each piece lives in the code, and how to query it.

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

| Event | When it fires | Key fields |
|-------|--------------|------------|
| `MCPPrompt` | Every tool call | `prompt_text`, `tool_name`, `session_id`, `publisher_id`, `prompt_source`, `prompt_char_count`, `estimated_prompt_tokens`, **`trace_id`**, **`span_id`** |
| `MCPToolError` | Tool call raises an exception | `tool_name`, `error_type`, `error_message`, `error_category`, `publisher_id`, `duration_ms`, `tool_start_offset_ms`, `ai_think_time_ms`, **`trace_id`**, **`span_id`** |
| `MCPToolDegraded` | Tool returns structured `{"error":…}` dict (no exception raised) | `tool_name`, `degraded_reason`, `publisher_id`, `session_id`, `prompt_id`, `duration_ms`, `tool_start_offset_ms`, `ai_think_time_ms`, **`trace_id`**, **`span_id`** |
| `MCPUnknownMethod` | Client sends truly unknown JSON-RPC method (not in known-unimplemented set) | `method`, `session_id` |
| `MCPSessionSummary` | SSE session closes | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms`, `total_estimated_input_tokens`, `total_estimated_output_tokens`, `total_estimated_tokens`, `server_work_pct`, `session_client_name`, `session_trace_id`, `active_sessions_remaining` |
| `SSESessionOpen` | SSE client connects | `session_id`, `publisher_id`, `active_threads`, `active_sessions`, `trace_id`, `span_id` |
| `SSESessionClose` | SSE stream ends | `session_id`, `publisher_id`, `duration_ms`, `tool_call_count`, `tool_error_count`, `tool_degraded_count`, `total_tool_duration_ms`, `session_trace_id` |

> **Bolded fields** are new in this iteration — they enable direct event → APM trace navigation and session timeline reconstruction.

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
| `Custom/MCP/active_threads` | Thread count per worker at request time |
| `Custom/MCP/active_sessions` | Active SSE sessions (emitted on open and close) |
| `Custom/MCP/session_queue_depth` | SSE message queue depth after each enqueue |
| `Custom/MCP/queue_wait_ms` | Time a message sat in the SSE queue before the generator consumed it |
| `Custom/MCP/fallback_count` | Times a tool fell back to an alternate endpoint |
| `Custom/MCP/unauth_tracker_size` | Unique IPs in the rate-limiter dict (leak proxy) |

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

**Where:** `newrelic.agent.record_custom_metric()` in `views.py`, `tools.py`, and `cds_client.py`.

---

## 6. Error Tracking — catch and classify failures

`notice_error()` sends an exception to NR with extra context. Combined with `error.*` and `mcp.error_category` attributes, you can answer:

- "How many errors were the AI client's fault vs CDS's fault?" → FACET `error.category`
- "Which CDS endpoint fails most?" → FACET `error.cds_endpoint`
- "Which tool breaks most?" → FACET `error.tool_name`

**Where:** `notice_err()` in every `except` block across all four files.

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

Sources checked in order: `X-MCP-Prompt` header → `_meta.prompt` in JSON-RPC → `params.prompt` → `arguments._prompt` → fallback to raw arguments JSON.

**Token cost proxy:** `mcp.prompt_char_count ÷ 4 = mcp.estimated_prompt_tokens`. This server is a tool server — actual token consumption happens in the AI client. These fields approximate the cost signal without a tokenizer dependency.

`MCPPrompt` events are capped at **1000/minute** to stay under NR's event limit. Transaction attributes (`mcp.prompt_text`, `mcp.prompt_source`, `mcp.prompt_char_count`) are always set regardless.

**Where:** `mcp_app/prompt_capture.py` + rate limiter in `views.py`.

---

## 10. Health Check Suppression — keep Apdex clean

Uptime probes hitting `/`, `/auth/status` suppress Apdex and slow-transaction traces so they don't affect your score.

| View | Suppression |
|------|------------|
| `health_check` (`GET /`) | `suppress_apdex_metric()` + `suppress_transaction_trace()` |
| `auth_status` (`GET /auth/status`) | `suppress_apdex_metric()` + `suppress_transaction_trace()` |

**Where:** `mcp_app/views.py` → `health_check`, `auth_app/views.py` → `auth_status`.

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

Unauthenticated requests to `/mcp` are rate-limited per IP (10 req/60 s sliding window). When an IP is limited it gets HTTP 429 with `Retry-After`. The limiter emits `Custom/MCP/unauth_tracker_size` on every check so you can graph the number of unique IPs currently being tracked — a rising value indicates an ongoing probe wave.

**Where:** `_is_unauth_rate_limited()` in `views.py`.

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

**Three correlation keys:**
- `mcp.session_id` — stable across all data types (Transactions, Events, Logs)
- `mcp.session_trace_id` — links all Transactions for one session (NRQL join key)
- `trace_id` on custom events — lets you jump from an event to its APM trace waterfall

**Limitation:** Logs correlate via `session_id` field (not `trace.id`), because SSE generator logs run outside any active NR transaction. HTTP transport sessions have `mcp.session_id` on transactions but no lifecycle events (SSESessionOpen/Close) and no session summary.

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
SELECT
  max(mcp.session_queue_depth) AS max_depth,
  average(Custom/MCP/queue_wait_ms) AS avg_wait_ms
FROM Metric, Transaction TIMESERIES SINCE 3 hours ago
```

### Unknown methods (truly unexpected)

```sql
SELECT count(*) FROM MCPUnknownMethod FACET method TIMESERIES SINCE 24 hours ago
```

### Session-level debugging (one-click drill-down)

Replace `'PASTE_SESSION_ID'` with any `session_id` value from NR Insights.

```sql
-- ── STEP 1: Session overview ───────────────────────────────────────────────
-- Get the session summary: total duration, tool counts, token usage, AI vs server time
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
  session_client_name
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
-- Errors with trace links — one click to the full APM trace
SELECT
  timestamp,
  'error'          AS type,
  tool_name,
  error_type,
  error_message,
  error_category,
  duration_ms,
  tool_start_offset_ms,
  trace_id         -- click to open APM trace for this specific failure
FROM MCPToolError
WHERE session_id = 'PASTE_SESSION_ID'
SINCE 24 hours ago

UNION

SELECT
  timestamp,
  'degraded'       AS type,
  tool_name,
  degraded_reason  AS error_type,
  ''               AS error_message,
  ''               AS error_category,
  duration_ms,
  tool_start_offset_ms,
  trace_id
FROM MCPToolDegraded
WHERE session_id = 'PASTE_SESSION_ID'
SINCE 24 hours ago
ORDER BY timestamp ASC

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
-- In NR Logs UI:  attributes.session_id = "PASTE_SESSION_ID"
-- Or via NRQL (NR Logs query builder):
SELECT timestamp, level, message
FROM Log
WHERE session_id = 'PASTE_SESSION_ID'
ORDER BY timestamp ASC SINCE 24 hours ago LIMIT MAX
-- Note: use the JSON key 'session_id' (set by python-json-logger from the
-- 'session=%s' argument in every log call).

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
| Failure list | Table | Step 4 (UNION) query above |
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

## Recommended Dashboard Widgets

### Row 1 — Health at a glance
| Widget | Type | NRQL |
|--------|------|------|
| Tool call rate | Line chart | `SELECT rate(sum(Custom/MCP/tool_call_count), 1 min) FROM Metric TIMESERIES` |
| Tool result status split | Stacked bar | `SELECT filter(count(*), WHERE mcp.tool_result_status = 'success') AS success, filter(count(*), WHERE mcp.tool_result_status = 'degraded') AS degraded, filter(count(*), WHERE mcp.tool_result_status = 'error') AS error FROM Transaction WHERE mcp.tool_name IS NOT NULL TIMESERIES` |
| Active SSE sessions | Line chart | `SELECT latest(Custom/MCP/active_sessions) FROM Metric TIMESERIES` |
| CDS timeout rate | Line chart | `SELECT rate(sum(Custom/CDS/timeout_count), 1 min) FROM Metric TIMESERIES` |

### Row 2 — Latency
| Widget | Type | NRQL |
|--------|------|------|
| p95 tool latency by tool | Bar chart | `SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| CDS p99 latency | Line chart | `SELECT percentile(cds.latency_ms, 99) FROM Transaction WHERE cds.endpoint IS NOT NULL FACET cds.endpoint TIMESERIES` |
| Tool duration heatmap | Histogram | `SELECT histogram(mcp.tool_duration_ms, 20, 20) FROM Transaction WHERE mcp.tool_name IS NOT NULL SINCE 1 hour ago` |

### Row 3 — Errors
| Widget | Type | NRQL |
|--------|------|------|
| Error category breakdown | Pie chart | `SELECT count(*) FROM Transaction WHERE mcp.error_category IS NOT NULL FACET mcp.error_category SINCE 1 hour ago` |
| Recent tool errors | Table | `SELECT timestamp, tool_name, error_category, error_message FROM MCPToolError SINCE 3 hours ago LIMIT 20` |
| Auth failures | Billboard | `SELECT count(*) FROM Transaction WHERE auth.result = 'failure' SINCE 1 hour ago` |

### Row 4 — Session intelligence
| Widget | Type | NRQL |
|--------|------|------|
| Session duration p95 | Billboard | `SELECT percentile(duration_ms, 95) FROM MCPSessionSummary SINCE 24 hours ago` |
| Tools per session (avg) | Billboard | `SELECT average(tool_call_count) FROM MCPSessionSummary SINCE 24 hours ago` |
| Queue depth + wait time | Line chart | `SELECT max(mcp.session_queue_depth) AS depth, average(Custom/MCP/queue_wait_ms) AS wait_ms FROM Metric, Transaction TIMESERIES` |
| Estimated token volume | Area chart | `SELECT sum(estimated_prompt_tokens) FROM MCPPrompt FACET publisher_id TIMESERIES 1 hour SINCE 7 days ago` |

### Row 5 — Tool concurrency + degraded
| Widget | Type | NRQL |
|--------|------|------|
| Peak concurrency by tool | Bar chart | `SELECT max(mcp.tool_concurrency) FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 1 hour ago` |
| Degraded rate % by tool | Bar chart | `SELECT filter(count(*), WHERE mcp.tool_is_degraded = true) * 100.0 / count(*) AS degraded_pct FROM Transaction WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name SINCE 24 hours ago` |
| Recent degraded events | Table | `SELECT timestamp, tool_name, degraded_reason, publisher_id FROM MCPToolDegraded SINCE 3 hours ago LIMIT 20` |
| p95 error duration | Bar chart | `SELECT percentile(mcp.tool_duration_ms, 95) FROM Transaction WHERE mcp.tool_result_status = 'error' FACET mcp.tool_name SINCE 24 hours ago` |

---

## Alert Conditions (with NRQL)

### Threshold Alerts

```yaml
# 1. Tool error rate spike
name: MCP Tool Error Rate
query: |
  SELECT filter(count(*), WHERE mcp.tool_is_error = true) * 100.0 / count(*)
  FROM Transaction WHERE mcp.tool_name IS NOT NULL
threshold: > 10 (%)
window: 5 minutes
evaluation_delay: 60s   # avoid false fires on deploys

# 2. CDS timeout spike
name: CDS Timeout Rate
query: |
  SELECT rate(sum(Custom/CDS/timeout_count), 1 minute) FROM Metric
threshold: > 5 per minute
window: 5 minutes

# 3. CDS average latency (use p95 not avg to avoid masking spikes)
name: CDS p95 Latency
query: |
  SELECT percentile(cds.latency_ms, 95) FROM Transaction
  WHERE cds.endpoint IS NOT NULL
threshold: > 4000 ms
window: 10 minutes

# 4. Thread saturation
name: Worker Thread Saturation
query: |
  SELECT max(mcp.thread_active_count) FROM Transaction
threshold: > 45
window: 5 minutes

# 5. Active session leak
name: SSE Session Count Anomaly
query: |
  SELECT latest(Custom/MCP/active_sessions) FROM Metric
threshold: > 100  # tune to your expected max concurrent users
window: 5 minutes

# 6. Queue depth — client not draining SSE
name: SSE Queue Depth High
query: |
  SELECT max(mcp.session_queue_depth) FROM Transaction
  WHERE mcp.session_queue_depth IS NOT NULL
threshold: > 10
window: 3 minutes

# 7. Fallback rate rise
name: Publisher Data Fallback Rate
query: |
  SELECT rate(sum(Custom/MCP/fallback_count), 1 minute) FROM Metric
threshold: > 3 per minute
window: 10 minutes

# 8. Auth failures
name: Auth Failure Rate
query: |
  SELECT count(*) FROM Transaction WHERE auth.result = 'failure'
threshold: > 20
window: 10 minutes

# 9. Degraded response rate (partial failures now visible)
name: MCP Tool Degraded Rate
query: |
  SELECT rate(sum(Custom/MCP/tool_degraded_count), 1 minute) FROM Metric
threshold: > 5 per minute
window: 5 minutes

# 10. Queue wait time (SSE consumer not draining fast enough)
name: SSE Queue Wait Time High
query: |
  SELECT percentile(Custom/MCP/queue_wait_ms, 95) FROM Metric
threshold: > 500 ms
window: 5 minutes

# 11. Tool concurrency saturation (single tool monopolising workers)
name: Tool Concurrency Spike
query: |
  SELECT max(mcp.tool_concurrency) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name
threshold: > 10 (concurrent calls to one tool)
window: 5 minutes
```

### Baseline (Anomaly) Alerts

```yaml
# Tool latency anomaly — no fixed threshold needed
name: MCP Tool Latency Anomaly
query: |
  SELECT average(mcp.tool_duration_ms) FROM Transaction
  WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name
type: baseline
deviation: 3 standard deviations above
window: 10 minutes

# CDS error count anomaly
name: CDS Error Rate Anomaly
query: |
  SELECT rate(sum(Custom/CDS/error_count), 1 minute) FROM Metric
type: baseline
deviation: 3 standard deviations above
window: 5 minutes
```

### Signal Loss Alert

```yaml
# If no MCP transactions arrive for 5 minutes, the server is down or broken
name: MCP Server No Traffic
query: |
  SELECT count(*) FROM Transaction WHERE mcp.tool_name IS NOT NULL
signal_loss_timer: 5 minutes
action: open_violation_on_signal_loss
```

---

## Worker Saturation Note

`mcp.thread_active_count` reports `threading.active_count()` **within a single gunicorn worker process**. With multiple workers, each reports independently. This is a per-worker thread count, not a cross-worker utilisation metric. To get cross-worker saturation, use the NR Infrastructure agent or configure `--statsd-host` on gunicorn to export worker metrics to StatsD → NR.

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
| 18 | NRQL dashboards | ✅ Full query bank + 5-row widget definitions above |
| 19 | Intelligent alerting | ✅ Threshold + baseline + signal-loss + degraded/concurrency conditions |
| 20 | SLO/SLI monitoring | ✅ Three SLIs defined; SLI 1 updated to exclude degraded results |
| 21 | AI token/cost monitoring | ✅ mcp.prompt_char_count + estimated_prompt_tokens |
| 22 | Service maps | ✅ Auto-generated via DT; CDS as "External" node |
| 23 | Error categorization | ✅ error.category + mcp.error_category across all layers |
| 24 | Session replay/debugging | ✅ session_tool_seq + MCPSessionSummary + NRQL query |
| 25 | Custom business events | ✅ 7 event types including MCPToolDegraded |
| 26 | Alert noise reduction | ✅ Apdex suppression, event cap, DEBUG for unimplemented methods, deque pruning |
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
