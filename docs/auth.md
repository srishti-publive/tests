# Auth & Database

## Two auth paths

The server supports two client types with fundamentally different lifecycles, so they use different flows.

### 1. OAuth 2.0 + PKCE — for AI clients (Claude Desktop, Cursor)

**Flow:**
```
Client → POST /register          → issues client_id 
Client → GET  /authorize         → renders login form
User   → POST /authorize         → validates CDS creds, issues auth code (10 min TTL)
Client → POST /token             → PKCE verifier check, issues bearer token + refresh token
Client → Bearer <token>          → resolved on every MCP tool call
```

**Why PKCE (not client secret):** AI clients like Claude Desktop are public clients — there is nowhere safe to store a secret. PKCE proves the code was requested by the same party that redeems it without any secret.

**Why dynamic registration (`/register`):** Claude Desktop generates a new client_id per install. Requiring pre-registration would block every new user.

**Token model:**
- `OAuthToken` stores `{publisherId, apiKey, apiSecret}` as plain JSON.
- Tokens have **no expiry** — the user's Publive credentials are the authority. Revoke via `/revoke`.
- `publisher_id` is denormalised as a plain indexed column so MCP dispatch can look up tokens by `(client_id, publisher_id)` directly.
- Upsert pattern: same `client_id + publisher_id` always returns the same token. Re-authorising doesn't break in-flight sessions.
- `refresh_token` rotates on every use (atomic DB transaction). Stolen refresh tokens become single-use.

**Auth code model (`OAuthCode`):**
- Single-use, 10-minute TTL.
- Deleted on redemption — replaying the code returns `invalid_grant`.

---

### 2. Session auth — for browser users

**Flow:**
```
Browser → GET  /connect          → renders login form
Browser → POST /auth/login       → validates CDS creds, stores creds in Django session
Browser → session cookie         → resolved on every MCP tool call
```

**Why a separate browser flow:** The OAuth redirect round-trip is invisible inside a browser tab. `/connect` gives a direct login page for human users who want to use the MCP server via a browser without going through an AI client.

**Session lifetime:** Sessions never self-expire. `auth_login()` always sets `session_ttl_seconds = -1` ("never expires — only `/auth/logout` ends the session") and calls `request.session.set_expiry(10 * 365 * 24 * 3600)` — a 10-year cookie ceiling so Django keeps the session alive indefinitely. There is no per-login configurable TTL (`remember_for_days` does not exist in the code); the only way to end a session is an explicit `POST /auth/logout`.

`check_session_ttl()` re-reads `session_created_at` / `session_ttl_seconds` on every request and returns `True` (expired) only when `session_ttl_seconds > 0` and the absolute deadline has passed — which never happens with the `-1` value the login flow always writes. It exists to enforce a server-side absolute deadline for any session that *did* get a positive TTL (e.g. legacy rows from before this change), catching cases Django's cookie TTL would miss (e.g. if `SESSION_SAVE_EVERY_REQUEST` is off).

---

## Data lifecycle: where it comes from, where it lands

### OAuth 2.0 + PKCE (Claude Desktop, Cursor, etc.)

1. **`POST /register`** — client sends `redirect_uri`. Server mints `client_id = secrets.token_urlsafe(24)`.
   → **Written to `oauth_client`**: `client_id`, `redirect_uri`, `created_at` — plaintext (not secrets, permanent row).

2. **`GET /authorize`** — client redirects the user's browser here with `client_id`, `redirect_uri`, `code_challenge`, `state`. Nothing is persisted; the server just renders `authorize.html`, echoing these values back as hidden form fields.

3. **`POST /authorize`** — the user types `publisherId` / `apiKey` / `apiSecret` into that form.
   - These are validated **live against the CDS API** (`validate_cds_credentials`) first — nothing is written if validation fails.
   - On success the server mints a single-use `code = secrets.token_urlsafe(32)`.
   → **Written to `oauth_code`**: `code`, `client_id`, `redirect_uri`, `code_challenge`, `expires_at = now + 10 min`, and `credentials = {publisherId, apiKey, apiSecret}` stored as plain JSON.
   - The browser is redirected back to the client's `redirect_uri` with only the opaque `?code=...&state=...` — the credentials themselves never travel over this hop.

4. **`POST /token`** (`grant_type=authorization_code`) — client exchanges `code` + `code_verifier`.
   - Server fetches the `oauth_code` row by `code`, checks `expires_at`, verifies `code_challenge` against the PKCE `code_verifier`, checks `redirect_uri`.
   - On success the row is **deleted immediately** (`auth_code.delete()`) — single-use, which is also why the `oauth_code` table is normally empty.
   - Server mints `token` + `refresh_token` (or reuses an existing one — see the upsert pattern keyed on `client_id + publisher_id`).
   → **Written to `oauth_token`**: `token`, `client_id`, `publisher_id` (denormalised indexed column for fast lookup), `refresh_token`, `created_at`, and `credentials` (plain JSON, copied from the auth code row). There is **no `expires_at`** column here; the row is permanent until revoked or upserted.
   - Response to the client is `{access_token, token_type, refresh_token}` — the stored credentials never leave the server.

5. **`Authorization: Bearer <token>`** on every MCP tool call — `resolve_credentials()` (`mcp_app/protocol/auth.py`) looks up `oauth_token` by `token`, reads `credentials` as `{publisherId, apiKey, apiSecret}`, and hands that dict to the tool handler, which forwards it as Basic Auth to the Publive CDS/CMS APIs. Nothing is written back to the DB on a normal call.

6. **`POST /token`** (`grant_type=refresh_token`) — rotates `refresh_token` on the existing `oauth_token` row inside an atomic transaction (`select_for_update`); `token`/`credentials` are untouched.

7. **`POST /revoke`** (or client disconnect) — deletes the matching `oauth_token` row by `token` or `refresh_token` (RFC 7009; always returns 200 regardless of whether a row matched).

### Session auth (browser users via `/connect`)

1. **`GET /connect`** — renders `connect.html`. Nothing persisted.

2. **`POST /auth/login`** — browser submits `publisherId` / `apiKey` / `apiSecret` (+ `remember_for_days`).
   - Validated live against the CDS API, exactly like step 3 above — nothing persisted on failure.
   - On success, `set_session_credentials()` stores `{publisherId, apiKey, apiSecret}` as a plain dict under `request.session["credentials"]`, alongside `session_created_at`, `session_ttl_seconds`, `authenticatedAt`.
   → Django's DB session backend (`SESSION_ENGINE = django.contrib.sessions.backends.db`) serialises that whole dict and **writes it to `django_session`**: `session_key` (PK), `session_data` (`base64(pickle({...}))`), `expire_date`.
   - The browser only ever receives a `sessionid` cookie pointing at that row — credentials never reach the cookie.

3. **Session cookie** on every later request (browser page or MCP call without a `Bearer` header) — `resolve_credentials()` reads `request.session["credentials"]`, checks `check_session_ttl()`, and hands the dict to the tool handler for that single request only.

4. **`POST /auth/logout`** — flushes the session, deleting the `django_session` row outright.

---

## Credential resolution on every MCP call

`resolve_credentials()` in `mcp_app/protocol/auth.py` checks in order:
1. `Authorization: Bearer <token>` → look up `OAuthToken` by token value.
2. Django session cookie → decrypt session credentials.

Neither path re-validates against the CDS API on each call — that would add ~500 ms per tool call. Credentials are validated once at login/authorize time.

---

## Database

**Engine:** PostgreSQL in production (via `DATABASE_URL`), SQLite fallback for local dev.

**Why Postgres for sessions:** Django's DB session backend stores sessions in `django_session`. With SQLite (ephemeral in the container), every deploy wipes all sessions — users get logged out. Postgres persists sessions across deploys.

**Connection pool:** `conn_max_age=600` — connections are reused for up to 10 minutes per thread. The server runs 1 worker + 50 threads; without pooling each thread would open a new connection on every request.

**Tables:**

| Table | Purpose |
|---|---|
| `oauth_client` | Registered OAuth clients (one per AI client install) |
| `oauth_code` | Short-lived PKCE auth codes (10 min, deleted on use) |
| `oauth_token` | Long-lived bearer tokens with credentials (plain JSON) |
| `django_session` | Browser sessions with credentials (plain JSON) |

---

## Security controls

**Rate limiting** (sliding window, in-process cache):

| Endpoint           | Limit | Window | Key |
|--------------------|---------|-----|-----|
| `POST /auth/login` | 10 req  | 60s | IP |
| `POST /register`   | 20 req  | 60s | IP |
| `/authorize`       | 20 req  | 60s | IP |
| `POST /token`      | 20 req  | 60s | IP |
| `/mcp`             | 300 req | 60s | Bearer token prefix |

Fails open — a cache outage never blocks traffic.

**Origin check:** `POST /register`, `POST /token`, and `POST /authorize` reject browser `Origin` headers not in `OAUTH_ALLOWED_ORIGINS`. Desktop MCP clients don't send `Origin` and are unconditionally allowed. Configurable via `OAUTH_ALLOWED_ORIGINS` in settings.

**Redirect URI allowlist:** Dynamic registration only accepts URIs in `OAUTH_ALLOWED_REDIRECT_URIS`. Extend without code change via `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` (comma-separated env var).

**Implicit grant disabled:** Only `response_type=code` is accepted. Implicit grant exposes tokens in the URL and has no PKCE equivalent.

---

## Required env vars

| Var | Required | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | Yes | Django cryptographic signing |
| `DATABASE_URL` | Yes (prod) | Postgres connection string; falls back to SQLite |
| `BASE_URL` | Yes (prod) | Used in OAuth metadata discovery endpoints |
| `CDS_BASE_URL` | No | CDS host template (default: `https://cds-beta.thepublive.com/publisher/{publisher_id}`) |
| `CMS_BASE_URL` | No | CMS host template (default: `https://cms-beta.thepublive.com/publisher/{publisher_id}`) |
| `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` | No | Extra redirect URIs for non-standard AI clients |
