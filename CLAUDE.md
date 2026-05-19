# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Projects in this repo

There are two implementations of the same Publive CDS MCP server:

| Directory | Stack | Status |
|-----------|-------|--------|
| `server.js` + `public/` | Node.js / Express | Original |
| `publive_mcp/` | Python / Django | Railway-ready port |

Both expose the same 12 MCP tools via the same auth flow and endpoint paths.

---

## Node.js implementation (`server.js`)

**Start:** `npm install && npm start`

**Environment variables:**
```
PORT=3000
SESSION_SECRET=change-me-in-production
BASE_URL=http://localhost:3000
```

**Entry point:** `server.js` — single-file Express + MCP SDK server.  
**Auth flow:** session cookie; credentials validated against `cds.thepublive.com` on login.  
**MCP transport:** `@modelcontextprotocol/sdk` SSE transport. Active transports tracked in a `Map` keyed by session ID.  
**Key note:** ES modules only (`"type": "module"`). Add new tools in `TOOLS` array + `callTool` switch.

---

## Django implementation (`publive_mcp/`)

**Local setup:**
```bash
cd publive_mcp
pip install -r requirements.txt
cp .env.example .env        # fill in DJANGO_SECRET_KEY
python manage.py migrate
python manage.py runserver
```

**Environment variables:**
```
DJANGO_SECRET_KEY=<long random string>
BASE_URL=http://localhost:8000
DEBUG=True
DATABASE_URL=                # auto-set by Railway Postgres; defaults to SQLite locally
```

**Structure:**
- `auth_app/` — auth endpoints + HTML templates (`connect.html`, `success.html`)
- `mcp_app/` — SSE endpoint, JSON-RPC dispatcher, tool definitions, CDS HTTP client
- `publive_mcp/` — Django settings, root URL conf, WSGI

**Auth flow** (same as Node.js):
1. Claude Desktop hits `GET /mcp` → 401 + `authUrl: /connect`
2. User authenticates in the embedded webview → session cookie set for Claude Desktop's HTTP session
3. Claude Desktop retries `GET /mcp` → credentials found → SSE stream opens

**MCP transport:** implemented manually (no SDK). `GET /mcp` returns an SSE stream; server sends an `endpoint` event pointing to `/mcp/message?sessionId=<uuid>`. Claude Desktop POSTs JSON-RPC there; responses are pushed back onto the SSE queue. Active sessions live in `mcp_app/views.py:_sessions` (in-memory dict — requires single gunicorn worker).

**Adding a new tool:** add an entry to `TOOLS` in `mcp_app/tools.py` and a matching `if name == "..."` branch in `call_tool`.  
**Adding a new CDS API call:** use `cds_get(credentials, "/path/", params)` from `mcp_app/cds_client.py`.

**Deployment (Railway):**
- `Procfile` runs: `gunicorn publive_mcp.wsgi -w 1 --threads 50`
- Single worker is required — the SSE session queue is in-process memory
- Add a Railway Postgres plugin; `DATABASE_URL` is injected automatically
- Set `DJANGO_SECRET_KEY`, `BASE_URL`, `DEBUG=False`
