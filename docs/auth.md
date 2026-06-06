# Auth & Database

## Two auth paths

The server supports two client types with fundamentally different lifecycles, so they use different flows.

### 1. OAuth 2.0 + PKCE — for AI clients (Claude Desktop, Cursor)

**Flow:**
```
Client → POST /register          → issues client_id (no secret)
Client → GET  /authorize         → renders login form
User   → POST /authorize         → validates CDS creds, issues auth code (10 min TTL)
Client → POST /token             → PKCE verifier check, issues bearer token + refresh token
Client → Bearer <token>          → resolved on every MCP tool call
```

**Why PKCE (not client secret):** AI clients like Claude Desktop are public clients — there is nowhere safe to store a secret. PKCE proves the code was requested by the same party that redeems it without any secret.

**Why dynamic registration (`/register`):** Claude Desktop generates a new client_id per install. Requiring pre-registration would block every new user.

**Token model:**
- `OAuthToken` stores `{publisherId, apiKey, apiSecret}` encrypted at rest.
- Tokens have **no expiry** — the user's Publive credentials are the authority. Revoke via `/revoke`.
- `publisher_id` is denormalised as a plain indexed column so MCP dispatch can look up tokens by `(client_id, publisher_id)` without decrypting every row.
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
Browser → POST /auth/login       → validates CDS creds, stores encrypted creds in Django session
Browser → session cookie         → resolved on every MCP tool call
```

**Why a separate browser flow:** The OAuth redirect round-trip is invisible inside a browser tab. `/connect` gives a direct login page for human users who want to use the MCP server via a browser without going through an AI client.

**Session TTL options** (set at login via `remember_for_days`):

| `remember_for_days` | Behaviour |
|---|---|
| `-1` | Browser session only — expires when tab closes |
| `0` | Django controls expiry (`SESSION_COOKIE_AGE = 90d`) |
| `N` | Absolute deadline: `login_time + N*86400s`, enforced server-side |

Server-side TTL check (`check_session_ttl`) re-reads `session_created_at` on every request. This catches sessions Django's cookie TTL would miss (e.g. if `SESSION_SAVE_EVERY_REQUEST` is off).

---

## Credential resolution on every MCP call

`resolve_credentials()` in `mcp_app/protocol/auth.py` checks in order:
1. `Authorization: Bearer <token>` → look up `OAuthToken` by token value.
2. Django session cookie → decrypt session credentials.

Neither path re-validates against the CDS API on each call — that would add ~500 ms per tool call. Credentials are validated once at login/authorize time.

---

## Credential encryption at rest

`{publisherId, apiKey, apiSecret}` is never stored as plaintext. It goes through `EncryptedJSONField` → `auth_app/crypto.py` → Fernet symmetric encryption on every DB write.

**Key:** `CREDENTIALS_ENCRYPTION_KEY` env var (32-byte URL-safe base64 Fernet key).

**Why Fernet over hashing:** Credentials must be recoverable (forwarded to CDS/CMS on every tool call). Hashing is one-way and cannot be used here.

**What happens without the key:** A random ephemeral key is generated at startup. Encryption works, but all tokens become unreadable after a restart — users must re-authenticate. Set `CREDENTIALS_ENCRYPTION_KEY` in production.

Generate a key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

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
| `oauth_token` | Long-lived bearer tokens with encrypted credentials |
| `django_session` | Browser sessions with encrypted credentials |

---

## Security controls

**Rate limiting** (sliding window, in-process cache):

| Endpoint | Limit | Window | Key |
|---|---|---|---|
| `POST /auth/login` | 10 req | 60s | IP |
| `POST /register` | 20 req | 60s | IP |
| `/authorize` | 20 req | 60s | IP |
| `POST /token` | 20 req | 60s | IP |
| `/mcp` | 300 req | 60s | Bearer token prefix |

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
| `CREDENTIALS_ENCRYPTION_KEY` | Yes (prod) | Fernet key for encrypting stored credentials |
| `BASE_URL` | Yes (prod) | Used in OAuth metadata discovery endpoints |
| `CDS_BASE_URL` | No | CDS host template (default: `https://cds-beta.thepublive.com/publisher/{publisher_id}`) |
| `CMS_BASE_URL` | No | CMS host template (default: `https://cms-beta.thepublive.com/publisher/{publisher_id}`) |
| `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` | No | Extra redirect URIs for non-standard AI clients |
