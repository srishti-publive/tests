# Publive MCP Server

Django server that exposes Publive's CDS (read) and CMS (write) APIs as MCP tools for AI clients like Claude Desktop and Cursor.

---

## Auth

**OAuth 2.0 + PKCE** — for desktop/CLI clients. Loopback redirect URIs (`localhost`/`127.0.0.1`) accepted on any port.

```
POST /register          → register client
GET|POST /oauth/authorize → validate credentials, issue PKCE code
POST /oauth/token       → exchange code for Bearer token (no expiry)
```

Add the server to Claude Code:
```bash
claude mcp add --transport http publive https://<your-server>/mcp
# or with a static token:
claude mcp add --transport http publive https://<your-server>/mcp \
  --header "Authorization: Bearer <token>"
```

**Session auth** — for browser (`/connect`):
```bash
POST /auth/login   {"publisherId":"123","apiKey":"key","apiSecret":"secret"}
GET  /auth/status
POST /auth/logout
```

---

## Tools

**61 tools total** — 22 CDS (read-only) + 39 CMS (read/write).

| Category | CDS | CMS |
|---|---|---|
| Posts | 5 | 5 |
| Categories | 2 | 5 |
| Tags | 2 | 5 |
| Authors | 2 | — |
| Live Blog | 1 | 5 |
| Media | — | 5 |
| Component Schemas | — | 5 |
| Content Type Schemas | — | 5 |
| Validation | — | 4 |
| Site/Sitemaps/Static | 8 | — |

### Safety model for CMS writes

| Tier | Operation | Default behaviour |
|---|---|---|
| 1 | List / Get / Validate | Executes immediately |
| 2 | Create | `dry_run=true` shows preview; set `false` to commit |
| 3 | Update | `dry_run=true` shows field diff; set `false` to apply |
| 3+ | Delete | Requires `dry_run=false` **and** `confirm_delete=true` |

> Draft posts skip dry-run (private, low-risk). Publishing requires an additional `confirm_publish=true`.

Per-session write cap: **100 create ops** and **100 update/delete ops** independently — returns `rate_limit` error when exceeded.

---

## Endpoints

| Endpoint | Notes |
|---|---|
| `GET /mcp` | SSE stream (long-lived) |
| `POST /mcp/message?sessionId=X` | Send JSON-RPC to active SSE session |
| `POST /mcp` | Stateless HTTP transport (MCP 2025-03-26) |
| `GET /` | Health check (no DB, used by Railway) |

---

## Running Locally

**Prerequisites:** Python 3.12. No Redis — all cross-worker state lives in the database. Postgres is optional locally — the app falls back to SQLite when `DATABASE_URL` is unset.

```bash
# 1. Configure environment
cp .env.example .env          # fill in DJANGO_SECRET_KEY at minimum

# 2. Install dependencies
pip install -r requirements.txt

# 3. Apply migrations, create the cache table, and run the dev server
python manage.py migrate
python manage.py createcachetable   # backs rate limiting (Django DatabaseCache)
python manage.py runserver          # http://localhost:8000
```

The dev server uses `publive_mcp.settings.local` (set in `manage.py`), which keeps `DEBUG=on`.

### With Docker (recommended — no local Postgres needed)

`docker compose up` builds the image and starts **Postgres + the web server** together, runs migrations + `createcachetable`, and serves on port 8000:

```bash
docker compose up            # add --build after changing the Dockerfile/requirements
```

Open http://localhost:8000 — `GET /` is the health check. Stop with `Ctrl+C`, or `docker compose down -v` to also wipe the Postgres volume.

> The deployed container needs no external services beyond its database; `entrypoint.sh` runs `migrate` then `createcachetable` on start.

---

## Deployment (Railway)

Built from `Dockerfile`, configured in `railway.toml`. On every container start, `entrypoint.sh` runs:
```sh
python manage.py migrate --noinput
exec gunicorn publive_mcp.wsgi -w 1 --threads 50 -b 0.0.0.0:${PORT:-8000} --timeout 60
```

`-w 1` is intentional — SSE sessions are in-process; multiple workers would break session routing.

### Required env vars

| Variable | Description |
|---|---|
| `DJANGO_SECRET_KEY` | Django secret key |
| `BASE_URL` | Public URL of this server |
| `DATABASE_URL` | PostgreSQL connection string (Railway sets this automatically) |

### Optional env vars

| Variable | Default | Description |
|---|---|---|
| `CDS_BASE_URL` | `https://cds-beta.thepublive.com/publisher/{publisher_id}` | CDS API base |
| `CMS_BASE_URL` | `https://cms-beta.thepublive.com/publisher/{publisher_id}` | CMS API base |
| `MCP_QUEUE_MAXSIZE` | `100` | Per-session SSE queue cap |
| `NEW_RELIC_LICENSE_KEY` | — | Enables New Relic observability |
| `RAILWAY_ENVIRONMENT` / `DJANGO_ENV` | — | Set to `production` to activate prod settings |
| `SERVER_VERSION` | `1.0.0` | Stamped on New Relic events |

---

## Docs

Detailed design docs in [`docs/`](docs/): `architecture.md`, `hld.md`, `lld.md`, `auth.md`, `mcp-protocol.md`, `deployment.md`, `newrelic.md`.
