# Architecture: Layered Design of `mcp_app`

## Is this a 3-tier architecture?

Not in the classic sense (presentation / business logic / data access). There's no
local data tier to speak of — Postgres only stores OAuth/session state, and the
"data" the server actually serves comes from Publive's remote CDS/CMS APIs.

Instead, `mcp_app` is structured as a **four-layer pipeline** purpose-built for an
MCP server, where each layer has exactly one job and only talks to the layer
directly below it:

```
┌─────────────────────────────────────────────────────────────┐
│  Transport layer        mcp_app/transport/                   │
│  sse.py · http.py       Wire protocol: SSE stream vs.        │
│                         stateless HTTP POST. No business     │
│                         logic — just gets JSON-RPC in/out.   │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  Protocol layer         mcp_app/protocol/                    │
│  auth.py · dispatch.py  Resolves credentials, routes         │
│  session.py             JSON-RPC methods, validates tool     │
│  session_store.py       args against inputSchema, enforces   │
│                         per-session write limits.            │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  Tool layer             mcp_app/cds/ · mcp_app/cms/          │
│  posts.py · authors.py  The actual business logic per MCP    │
│  categories.py · ...    tool. Data-driven: each tool is a    │
│                         {name, description, inputSchema,     │
│                         handler} entry in TOOLS/CMS_TOOLS.   │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  HTTP client layer      mcp_app/clients/                     │
│  cds.py · cms.py        Talks to the remote Publive CDS/CMS  │
│  shared.py              REST APIs. Owns timeouts, retries,   │
│                         auth headers, error normalization.   │
└──────────────────────────────┬────────────────────────────────┘
                               ▼
                    Publive CDS / CMS REST APIs
                    (cds-beta.thepublive.com,
                     cms-beta.thepublive.com)
```

`auth_app` (OAuth + session login) and observability (`nr_utils.py`,
`prompt_capture.py`, `middleware.py`) sit alongside this pipeline as
cross-cutting concerns rather than layers in the request path.

---

## Why this shape, not 3-tier

- **No local persistence of domain data.** Posts, authors, categories etc. live
  in Publive's CDS/CMS, not in this server's database. Postgres here only backs
  `OAuthClient`/`OAuthCode`/`OAuthToken`/sessions — infrastructure state, not
  the "data tier" a 3-tier diagram implies.
- **Two transports, one protocol.** SSE (`transport/sse.py`) and stateless HTTP
  POST (`transport/http.py`) both speak JSON-RPC, so the transport concern had
  to be split out from routing/dispatch — a split a classic 3-tier app doesn't
  need.
- **Tools are plugins, not "business logic" in the usual sense.** Each tool is a
  declarative `{name, description, inputSchema, handler}` entry; `dispatch.py`
  never changes when a tool is added (see "Adding a New Tool" in `CLAUDE.md`).
  That data-driven registration is closer to a plugin architecture than a
  monolithic service layer.
- **The "data access" layer talks to a remote API, not a DB.** `clients/cds.py`
  and `clients/cms.py` own HTTP concerns (Basic Auth, timeouts, retries, error
  classification) the way a repository layer would own SQL — but the backing
  store is an external HTTP API.

## Request flow through the layers

A `tools/call` over SSE crosses every layer in order:

1. **Transport** (`transport/sse.py`) — receives the JSON-RPC message on the
   session's queue, no decoding of `method`/`params` beyond JSON parsing.
2. **Protocol** (`protocol/dispatch.py`) — `resolve_credentials()` looks up the
   Bearer token or session cookie, `_validate_tool_args()` checks the payload
   against the tool's `inputSchema`, then `_handle_tool_call()` looks up the
   handler in `TOOLS`/`CMS_TOOLS` by name.
3. **Tool** (e.g. `cms/posts.py`) — the handler builds the request to Publive,
   applies the dry-run/confirm tiered-safety rules for write tools, and shapes
   the MCP content response.
4. **HTTP client** (`clients/cms.py`) — issues the authenticated request to
   `cms-beta.thepublive.com`, classifies errors, and returns parsed JSON or a
   normalized error dict back up to the tool handler.

See `docs/hld.md` for the full system diagram and `docs/lld.md` for
function-level detail on each module.
