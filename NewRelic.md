# New Relic — Publive MCP

What New Relic does for this project and where each piece lives in the code.

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
| `Auth/pkce_authorize`, `Auth/pkce_token` | OAuth PKCE flow |
| `Auth/oauth_register` | Client self-registration |
| `Auth/session_login`, `Auth/session_verify` | Session auth |

**Where:** `set_txn_name()` calls in `mcp_app/views.py` and `auth_app/views.py`.

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

| Namespace | Key examples | Answers |
|-----------|-------------|---------|
| `mcp.*` | `tool_name`, `tool_duration_ms`, `tool_result_status`, `session_id`, `client_name`, `thread_active_count`, `request_size_bytes` | Which tool is slow? Which client calls it? How loaded is the server? |
| `cds.*` | `endpoint`, `latency_ms`, `http_status`, `response_size_bytes`, `publisher_id` | Is CDS slow for a specific publisher or endpoint? |
| `auth.*` | `flow`, `result`, `failure_reason`, `publisher_id`, `cds_validation_ms` | Why do logins fail? How long does CDS validation take? |
| `error.*` | `layer`, `tool_name`, `cds_endpoint`, `http_status` | Which layer threw? Which CDS endpoint failed? |

**Where:** `add_attrs()` calls throughout `views.py`, `tools.py`, `cds_client.py`, `auth_app/views.py`.

---

## 4. Custom Events — dedicated tables for specific things

Separate event types you query with `FROM <EventType>`. Useful for things that don't map neatly to a single transaction.

| Event | When it fires | Key fields |
|-------|--------------|------------|
| `MCPPrompt` | Every tool call | `prompt_text`, `tool_name`, `session_id`, `publisher_id`, `prompt_source` |
| `MCPToolError` | Tool call fails | `tool_name`, `error_type`, `error_message`, `publisher_id`, `duration_ms` |
| `MCPUnknownMethod` | Client sends unrecognised JSON-RPC method | `method`, `session_id` |
| `SSESessionOpen` | SSE client connects | `session_id`, `publisher_id`, `active_threads` |
| `SSESessionClose` | SSE stream ends | `session_id`, `publisher_id`, `duration_ms` |

**Where:** `record_event()` calls in `views.py` and `prompt_capture.py`.

---

## 5. Custom Metrics — long-retention numbers for alerting

Unlike events (30-day retention), custom metrics keep data for 13 months and can drive alert policies directly.

| Metric | Measures |
|--------|----------|
| `Custom/Tool/{name}/duration_ms` | Per-tool call latency |
| `Custom/MCP/tool_call_count` | Total successful tool calls |
| `Custom/MCP/tool_error_count` | Total tool errors |
| `Custom/MCP/active_threads` | Thread saturation (limit is 50) |
| `Custom/CDS/latency_ms` | CDS API round-trip time |
| `Custom/CDS/response_size_bytes` | CDS response size |
| `Custom/CDS/error_count` | CDS failures |

**Where:** `newrelic.agent.record_custom_metric()` in `views.py` and `cds_client.py`.

---

## 6. Error Tracking — catch and classify failures

`notice_error()` sends an exception to NR with extra context. Combined with `error.*` attributes, you can filter errors by layer, tool, or CDS endpoint.

**Where:** `notice_err()` in every `except` block across all four files.

---

## 7. Application Logs — forwarded to NR Logs with trace links

All Python `logging` output is captured and sent to New Relic Logs. Each log line is automatically decorated with the trace ID so you can jump from an APM trace directly to the correlated logs.

Every module has `logger = logging.getLogger(__name__)` with `INFO` on success paths and `ERROR` with `exc_info=True` on failures.

**Where:** `newrelic.ini` (all four `application_logging.*` settings enabled) + logger usage in all four modules.

---

## 8. Distributed Tracing — follow a request end-to-end

`distributed_tracing.enabled = true` in `newrelic.ini`. The NR agent automatically propagates W3C trace context on outbound `requests.get()` calls to CDS. If CDS were also instrumented, the full chain would appear in one waterfall.

CDS span attributes (`cds.url`, `cds.path`, `cds.publisher_id`) are set on each span individually via `add_custom_span_attribute()` so they're visible per-call in the trace view.

**Where:** `newrelic.ini` + `cds_client.py`.

---

## 9. Prompt Observability — see what AI clients are asking

The server extracts whatever prompt context the client sends and records it against every tool call. Useful for understanding real-world usage patterns.

Sources checked in order: `X-MCP-Prompt` header → `_meta.prompt` in JSON-RPC → `params.prompt` → tool `arguments._prompt` → fallback to raw arguments JSON.

`MCPPrompt` events are capped at **1000/minute** to stay under NR's event limit. Transaction attributes (`mcp.prompt_text`, `mcp.prompt_source`) are always set regardless.

**Where:** `mcp_app/prompt_capture.py` + rate limiter in `views.py`.

---

## 10. Health Check Suppression — keep Apdex clean

Railway probes `GET /auth/status` every few seconds. Without suppression these flood the transaction list and drag down Apdex.

`auth_status` calls `suppress_apdex_metric()` and `suppress_transaction_trace()` so health checks don't affect your score or slow-transaction traces.

**Where:** `auth_app/views.py` → `auth_status`.

---

## 11. Deployment Markers — correlate deploys with changes

Every Railway deploy records a marker in NR. Markers appear as vertical lines on APM charts so you can instantly see "did this deploy cause the latency spike?"

```
release: ... && newrelic-admin record-deploy newrelic.ini \
  "${RAILWAY_GIT_COMMIT_SHA}" "${RAILWAY_GIT_COMMIT_MESSAGE}" "${RAILWAY_GIT_AUTHOR}" || true
```

**Where:** `Procfile` release step.

---

## Useful NRQL queries

```sql
-- Tool latency by tool
SELECT average(mcp.tool_duration_ms) FROM Transaction
WHERE mcp.tool_name IS NOT NULL FACET mcp.tool_name TIMESERIES

-- CDS latency by endpoint
SELECT average(cds.latency_ms) FROM Transaction
WHERE cds.endpoint IS NOT NULL FACET cds.endpoint TIMESERIES

-- Auth failures by reason
SELECT count(*) FROM Transaction
WHERE auth.result = 'failure' FACET auth.failure_reason, auth.flow

-- Recent tool errors with message
SELECT timestamp, tool_name, error_type, error_message, publisher_id
FROM MCPToolError SINCE 3 hours ago ORDER BY timestamp DESC LIMIT MAX

-- All tool calls in a session
SELECT timestamp, mcp.tool_name, mcp.tool_result_status, mcp.tool_duration_ms
FROM Transaction
WHERE mcp.session_id = 'PASTE_SESSION_ID'
  AND mcp.tool_name IS NOT NULL
ORDER BY timestamp ASC SINCE 6 hours ago

-- Thread saturation
SELECT max(mcp.thread_active_count) FROM Transaction
WHERE mcp.thread_active_count IS NOT NULL TIMESERIES

-- Unknown methods from bad clients
SELECT count(*) FROM MCPUnknownMethod FACET method TIMESERIES
```

---

## Alert recommendations

| Alert on | Threshold |
|----------|-----------|
| `Custom/MCP/tool_error_count` | > 10 in 5 min |
| `Custom/CDS/latency_ms` (average) | > 3000 ms |
| `Custom/CDS/error_count` | > 5 in 5 min |
| `Custom/MCP/active_threads` (max) | > 45 |
| Auth failures (`auth.result = 'failure'`) | > 20 in 10 min |
