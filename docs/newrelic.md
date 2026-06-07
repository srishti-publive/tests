# New Relic Observability

## Agent setup

The New Relic Python agent is wrapped at the WSGI level (`publive_mcp/wsgi.py`):

```python
application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
```

**Why WSGI-level, not middleware:** The WSGI wrapper instruments every request before Django's middleware stack runs, giving accurate wall-clock timings that include middleware overhead. A Django middleware would miss time spent in earlier middleware.

All New Relic calls go through `mcp_app/nr_utils.py`, which guards every call with `if _nr is None` and `if _current_transaction()`. The app runs cleanly with zero NR configuration — no `NEW_RELIC_LICENSE_KEY`, no `newrelic.ini`, no crashes. Tests and local dev work without the agent.

**Config:** `newrelic.ini` in the repo root. Key settings:

| Setting | Value | Why |
|---|---|---|
| `app_name` | `Publive MCP` | Groups all Railway instances under one app in NR |
| `distributed_tracing.enabled` | `true` | Links MCP tool spans to the AI client's trace |
| `transaction_tracer.record_sql` | `obfuscated` | Captures slow queries without leaking credentials in bind params |
| `error_collector.ignore_classes` | `BrokenPipeError, ConnectionResetError` | SSE clients disconnect mid-stream; these are normal, not errors |
| `browser_monitoring.auto_instrument` | `false` | No HTML pages served by the MCP layer; prevents injecting JS into JSON responses |

---

## Custom events

All custom events carry `env` and `server_version` fields so NRQL queries can filter by environment or release.

### `MCPPrompt`
Emitted on every `tools/call`. Captures what the user/LLM asked and links it to the APM trace.

| Field | Purpose |
|---|---|
| `prompt_id` | UUID; joins this event to `MCPToolError`/`MCPToolDegraded` for the same call |
| `prompt_text` | Extracted user prompt (truncated to 2000 chars) |
| `prompt_source` | Where the prompt came from: `header`, `meta.prompt`, `arguments._prompt`, `tool_args`, `client_not_provided` |
| `tool_name` | Which tool was called |
| `publisher_id` | Publisher making the call |
| `estimated_prompt_tokens` | `char_count ÷ 4` — cost proxy without a tokenizer dependency |
| `trace_id`, `span_id` | NR linking metadata — click from this event directly to the APM trace waterfall |

**Why prompt extraction has a priority chain:** Clients send prompts in different ways. The chain (`header → _meta → params.prompt → arguments._prompt → tool_args`) means any client works without needing to agree on one field. `_prompt` is stripped from `arguments` before the tool runs so tools never see the internal field.

**Rate-limited:** If the event queue is full, `MCPPrompt` is dropped and `Custom/MCP/prompt_event_dropped_count` is incremented. The tool still runs — observability is sacrificed, not functionality.

---

### `MCPToolError`
Emitted when a tool raises an uncaught exception (hard failure — the tool crashed).

Key fields: `tool_name`, `publisher_id`, `error_type`, `error_message`, `error_category`, `duration_ms`, `prompt_text`, `prompt_id`, `session_id`, `trace_id`, `span_id`.

---

### `MCPToolDegraded`
Emitted when a tool returns successfully but the result contains an `error` or `error_type` key (soft failure — the tool ran but the upstream API rejected it or returned an error).

**Why separate from `MCPToolError`:** Degraded calls are not exceptions — they complete the request/response cycle normally. Distinguishing them lets you alert on upstream API degradation separately from application crashes.

Key fields: same as `MCPToolError` plus `degraded_reason`.

---

### SSE session lifecycle events

| Event | When |
|-------|------|
| `SSESessionOpen` | Client connects via `GET /mcp` (SSE transport) |
| `SSESessionClose` | Client disconnects or server closes the stream |
| `MCPSessionAbandoned` | Session closed with zero tool calls (client connected but never used) |
| `MCPSessionSummary` | Always on close — full session roll-up |
| `MCPSessionMissing` | `POST /mcp/message` arrived for an unknown `sessionId` |

**`MCPSessionSummary` key fields:**

| Field | Purpose |
|-------|---------|
| `duration_ms` | Total session wall time |
| `tool_call_count` | How many tools were called |
| `tool_error_count` / `tool_degraded_count` | Quality signal per session |
| `total_tool_duration_ms` | Time the server spent working (vs waiting for the AI) |
| `server_work_pct` | `total_tool_duration_ms / duration_ms × 100` — what fraction of the session was server work |
| `ai_think_time_ms` | Time between the last tool response and the next tool call (AI processing time) |
| `tool_sequence` | Comma-separated list of tools called in order (capped at 500 chars) |
| `total_estimated_tokens` | Input + output token proxy for the whole session |
| `session_trace_id` | Links all events from this session to each other |

**Why `MCPSessionAbandoned`:** Connects without tool calls indicate OAuth flow failures, client misconfiguration, or network drops before auth completes. Alerting on a spike in abandoned sessions surfaces auth regressions before users report them.

---

### `MCPUnknownMethod`
Emitted when the JSON-RPC dispatcher receives a method name it doesn't recognise. Useful for detecting undocumented clients or a version mismatch between client and server.

---

## Custom metrics

All metrics follow the `Custom/` prefix required by New Relic for custom metric visibility.

| Metric | What it tracks |
|---|---|
| `Custom/MCP/tool_call_count` | Total tool calls |
| `Custom/MCP/tool_success_count` | Successful tool calls |
| `Custom/MCP/tool_error_count` | Hard failures |
| `Custom/MCP/tool_degraded_count` | Soft failures (upstream API errors) |
| `Custom/MCP/tool_validation_error_count` | Calls rejected by inputSchema validation |
| `Custom/MCP/active_sessions` | SSE sessions open right now |
| `Custom/MCP/session_abandon_count` | Sessions that closed with 0 tool calls |
| `Custom/MCP/queue_wait_ms` | Time a message waited in the SSE queue before delivery |
| `Custom/MCP/prompt_event_dropped_count` | `MCPPrompt` events dropped due to NR queue pressure |
| `Custom/Tool/<name>/call_count` | Per-tool call count |
| `Custom/Tool/<name>/duration_ms` | Per-tool latency (success path) |
| `Custom/Tool/<name>/error_count` | Per-tool hard failures |
| `Custom/Tool/<name>/degraded_count` | Per-tool soft failures |
| `Custom/CDS/latency_ms` | CDS API response time |
| `Custom/CDS/timeout_count` | CDS timeouts |
| `Custom/CDS/error_count` | CDS errors (after retry) |
| `Custom/CDS/retry_count` | CDS retry attempts |
| `Custom/CMS/latency_ms` | CMS API response time |
| `Custom/CMS/timeout_count` | CMS timeouts |
| `Custom/CMS/error_count` | CMS errors |
| `Custom/Auth/client_registered_count` | OAuth client registrations |
| `Custom/Auth/auth_failure_count` | Auth failures (all flows) |
| `Custom/Auth/token_refresh_count` | Token refresh operations |

---

## Transaction naming

Every request is renamed with `set_txn_name()` so the APM transaction list is readable instead of showing raw URL paths.

| Group | Pattern | Example |
|---|---|---|
| `Transport` | Entry points | `Transport/mcp_endpoint` |
| `MCP` | Per tool after dispatch | `MCP/get_posts` |
| `Auth` | Auth endpoints | `Auth/pkce_authorize` |
| `CDS` | Per CDS path | `CDS/posts` |

**Why rename after dispatch (not at entry):** The entry point `mcp_endpoint` doesn't know which tool will be called. Renaming inside `_handle_tool_call` means the transaction shows `MCP/get_posts` rather than `MCP/mcp_endpoint` — you can alert on a specific tool's error rate.

---

## Apdex and trace suppression

Two transaction types are explicitly suppressed:

| Transaction | Why suppressed |
|---|---|
| SSE session (`GET /mcp`) | Lasts minutes; a single long session would permanently tank the Apdex score |
| Health check (`GET /`) | Polled every ~30s by Railway; counts as thousands of "fast" transactions, distorting Apdex baseline |

Suppression uses `newrelic.agent.suppress_apdex_metric()` and `suppress_transaction_trace()`. The transactions are still recorded — just excluded from Apdex scoring and slow-transaction trace collection.

---

## Transaction attributes

Every tool call attaches these attributes (queryable via `FROM Transaction`):

```
mcp.tool_name, mcp.tool_input, mcp.tool_result_status,
mcp.tool_duration_ms, mcp.tool_response_size,
mcp.tool_is_error, mcp.tool_is_degraded,
mcp.prompt_text, mcp.prompt_source, mcp.prompt_id,
mcp.session_id, mcp.ai_think_time_ms, mcp.tool_start_offset_ms,
mcp.estimated_output_tokens
```

Auth transactions attach: `auth.flow`, `auth.result`, `auth.publisher_id`, `auth.failure_reason`.

CDS requests attach: `cds.endpoint`, `cds.http_status`, `cds.latency_ms`, `cds.retried`.

CMS requests attach: `cms.path`, `cms.method`, `cms.http_status`, `cms.latency_ms`.

---

## Required env vars

| Var | Purpose |
|---|---|
| `NEW_RELIC_LICENSE_KEY` | Agent activation. Without it the agent is disabled and all NR calls are no-ops. |
| `NEW_RELIC_APP_NAME` | Overrides `app_name` in `newrelic.ini` (useful for separating staging vs prod in the NR UI) |
| `SERVER_VERSION` | Attached to every custom event. Defaults to `1.0.0`. Set to the git SHA or release tag at deploy time. |
| `RAILWAY_ENVIRONMENT` | Read as `SERVER_ENV` on every event. Set automatically by Railway. |
