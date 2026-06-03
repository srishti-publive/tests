# Publive MCP Server — Authentication Reference

## Overview

The server supports two completely separate auth flows for two caller types. They do not share session logic, token stores, or expiry mechanisms.

| Caller Type | Flow | Credential |
|---|---|---|
| Human Users | Browser session via `/connect` | Session cookie |
| AI Clients (PKCE) | OAuth 2.0 PKCE via `/authorize` → `/token` | Bearer `OAuthToken` |
| AI Clients (Direct) | Self-registration via `/ai/register` | Bearer `client_id` (UUID v4) |

---

## Human User Sessions

### Login

```
POST /auth/login
Content-Type: application/json

{
  "publisherId": "3567",
  "apiKey": "...",
  "apiSecret": "...",
  "remember_for_days": 90
}
```

#### Session Duration Options

| `remember_for_days` | Meaning | Server behaviour |
|---|---|---|
| `-1` | Always — never expires | `set_expiry(10 years)` |
| `90` | 90 days from login **(default)** | `set_expiry(90 * 86400)` |
| Any positive int | Custom N days from login | `set_expiry(N * 86400)` |
| `0` | This session only — expires when browser closes | `set_expiry(0)` |

The UI pre-selects **90 days**. Custom durations can be any positive integer.

### How Expiry is Enforced

`SESSION_SAVE_EVERY_REQUEST = False` — sessions are **not** rolled forward on each request. The original TTL is an absolute deadline.

At login, two fields are stored in the session:

| Field | Type | Value |
|---|---|---|
| `session_created_at` | `int` (Unix epoch) | `int(timezone.now().timestamp())` |
| `session_ttl_seconds` | `int` | `-1`, `0`, or `N * 86400` |

On every authenticated request, `check_session_ttl(session)` in `auth_app/services.py` computes:

```
expired = time.time() > session_created_at + session_ttl_seconds
```

- `ttl_seconds == -1` → always valid, skip check
- `ttl_seconds == 0` → browser-controlled, skip server check
- `ttl_seconds > 0` → enforce absolute deadline

If expired: session is flushed server-side and the request receives `401 SESSION_EXPIRED`.

### Session Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/connect` | Renders the browser login form |
| `POST` | `/auth/login` | Validates credentials, creates session |
| `GET` | `/auth/status` | Returns session state + `session_expires_in_seconds` |
| `POST` | `/auth/logout` | Flushes session |
| `GET` | `/auth/success` | Post-login confirmation page |

### Session Status Response

```json
{
  "authenticated": true,
  "publisherId": "3567",
  "authenticatedAt": "2026-06-03T11:00:00+00:00",
  "remember_for_days": 90,
  "session_expires_in_seconds": 7257600
}
```

When a session has expired server-side:

```json
{
  "authenticated": false,
  "error": "SESSION_EXPIRED"
}
```

---

## AI Clients — OAuth 2.0 PKCE Flow

Used by Claude Desktop, Cursor, Anthropic SDK, and other MCP clients that support the full OAuth 2.0 authorization code + PKCE flow.

### Flow

```
1. POST /register          → client_id (OAuthClient, 90-day TTL)
2. GET  /authorize         → credential entry form
3. POST /authorize         → redirect with auth code (10-min TTL)
4. POST /token             → access_token + refresh_token (30-day TTL)
5. POST /token (refresh)   → rotate refresh token, keep access_token
```

### Discovery Documents

| Path | Standard |
|---|---|
| `/.well-known/oauth-authorization-server` | RFC 8414 |
| `/.well-known/openid-configuration` | OpenID Connect |
| `/.well-known/oauth-protected-resource` | RFC 9728 |

### Token Store — `OAuthToken`

| Field | Notes |
|---|---|
| `token` | `secrets.token_urlsafe(32)` — used as bearer |
| `client_id` | Links to `OAuthClient` |
| `refresh_token` | Rotated on every refresh |
| `credentials` | `{publisherId, apiKey, apiSecret}` |
| `expires_at` | 30 days from issuance |

Upsert behaviour: re-authorising the same `client_id` + `publisher_id` returns the existing valid token (stable token identity).

---

## AI Clients — Direct Registration Flow

A simpler alternative to OAuth PKCE. Any programmatic caller can self-register instantly, receive a UUID v4 `client_id`, and use it as a sole bearer credential.

> **One-ID-per-client is a POLICY contract, not a technical constraint.** The server cannot verify client identity at registration time. Multiple registrations from the same IP are logged and visible to admins, who can revoke any ID.

### Registration

```
POST /ai/register
Content-Type: application/json

{
  "client_name": "Claude Desktop – Srishti",
  "contact": "optional@email.com",
  "publisher_id": "3567",
  "api_key": "...",
  "api_secret": "..."
}
```

- `client_name` — required
- `contact` — optional, stored for audit
- `publisher_id` / `api_key` / `api_secret` — optional; validated against CDS if provided; stored for tool calls

**Response `201`:**

```json
{
  "client_id": "550e8400-e29b-41d4-a716-446655440000",
  "client_name": "Claude Desktop – Srishti",
  "issued_at": 1748945400
}
```

The `client_id` UUID is the **sole credential** for all subsequent requests:

```
Authorization: Bearer 550e8400-e29b-41d4-a716-446655440000
```

A client that loses its `client_id` must re-register (new ID issued; old one is not recoverable).

### Rate Limiting

Maximum **5 registrations per IP per hour**. The 6th attempt returns:

```json
HTTP 429
{
  "error": "rate_limited",
  "error_description": "Maximum 5 registrations per IP per hour. Try again later."
}
```

### AI Client Store — `AIClient`

| Field | Type | Notes |
|---|---|---|
| `client_id` | UUID v4 | Primary key, opaque |
| `client_name` | string | Provided at registration |
| `contact` | string? | Optional, for audit |
| `status` | `active` \| `blocked` | Checked on every request |
| `credentials` | JSON? | `{publisherId, apiKey, apiSecret}` if provided |
| `registered_at` | timestamp | Auto-set |
| `registration_ip` | string | Captured server-side |
| `last_seen_at` | timestamp | Updated on every successful request (one place: middleware) |

---

## Per-Request Auth Routing

`mcp_app/protocol/auth.resolve_credentials(request)` returns a 3-tuple:

```python
(credentials_dict | None, token_expires_at | None, error_code | None)
```

**Routing logic:**

```
Bearer present?
├── UUID v4 format?  →  AIClient table
│   ├── Not found    →  (None, None, "INVALID_CLIENT_ID")
│   ├── Blocked      →  (None, None, "CLIENT_BLOCKED")
│   └── Active       →  (credentials, None, None)
│
└── Other format     →  OAuthToken table
    ├── Valid        →  (credentials, expires_at, None)
    └── Expired/unknown → falls through to session

No Bearer           →  Django session
    ├── TTL expired  →  flush session → (None, None, "SESSION_EXPIRED")
    └── Valid        →  (credentials, None, None)
```

---

## Typed 401 Reason Codes

Every `401` response includes a machine-readable `error` field. Generic 401s are not used.

| `error` | Trigger | Meaning |
|---|---|---|
| `SESSION_EXPIRED` | Session has passed `created_at + ttl_seconds` | User must log in again |
| `CLIENT_BLOCKED` | Admin blocked this AI client | Contact the server admin |
| `INVALID_CLIENT_ID` | UUID not found in `ai_client` table | Re-register at `/ai/register` |

**Example response:**

```json
HTTP 401
WWW-Authenticate: Bearer realm="...", resource_metadata="..."

{
  "error": "CLIENT_BLOCKED",
  "error_description": "This AI client has been blocked by an administrator. Contact the server admin.",
  "authUrl": "https://your-server/connect"
}
```

---

## Admin — AI Client Management

All admin endpoints require:

```
Authorization: Bearer <ADMIN_SECRET_KEY>
```

`ADMIN_SECRET_KEY` is set via the `ADMIN_SECRET_KEY` environment variable. If not set, all admin endpoints return `503 admin_not_configured`.

Admin credentials are intentionally separate from AI client `client_id` tokens — admin actions cannot be performed with a client bearer token.

### Endpoints

#### List all clients
```
GET /admin/clients
Authorization: Bearer <admin-key>
```

```json
{
  "clients": [
    {
      "client_id": "550e8400-e29b-41d4-a716-446655440000",
      "client_name": "Claude Desktop – Srishti",
      "contact": "srishti@example.com",
      "status": "active",
      "registered_at": "2026-06-03T11:00:00+00:00",
      "registration_ip": "1.2.3.4",
      "last_seen_at": "2026-06-03T12:30:00+00:00"
    }
  ],
  "count": 1
}
```

#### Block a client (effective on next request)
```
POST /admin/clients/{client_id}/block
Authorization: Bearer <admin-key>
```

#### Unblock a client
```
POST /admin/clients/{client_id}/unblock
Authorization: Bearer <admin-key>
```

#### Permanently delete a client
```
DELETE /admin/clients/{client_id}
Authorization: Bearer <admin-key>
```

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | Yes | Django secret key |
| `ADMIN_SECRET_KEY` | Yes (for admin features) | Static key for `/admin/clients/*` endpoints |
| `BASE_URL` | Yes | Public base URL (e.g. `https://mcp.thepublive.com`) |
| `DATABASE_URL` | No | Defaults to SQLite |
| `OAUTH_ALLOWED_REDIRECT_URIS_EXTRA` | No | Comma-separated extra OAuth redirect URIs |

---

## Settings Reference

| Setting | Value | Reason |
|---|---|---|
| `SESSION_COOKIE_AGE` | `10 * 365 * 24 * 3600` | Ceiling for "Always" sessions |
| `SESSION_SAVE_EVERY_REQUEST` | `False` | Prevents silent TTL roll-forward; absolute TTL enforced via `session_created_at` |
| `SESSION_ENGINE` | `django.contrib.sessions.backends.db` | DB-backed; survives redeploys |
| `SESSION_COOKIE_SECURE` | `True` in production | HTTPS only |
| `SESSION_COOKIE_HTTPONLY` | `True` | JS cannot read cookie |
| `SESSION_COOKIE_SAMESITE` | `Lax` | CSRF protection |

---

## Files Changed in Auth Audit

| File | Change |
|---|---|
| `auth_app/models.py` | Added `AIClient` model |
| `auth_app/migrations/0005_aiclient.py` | New migration for `ai_client` table |
| `auth_app/services.py` | Added `check_session_ttl()` helper |
| `auth_app/views.py` | Updated `auth_login` (new durations + `session_created_at`); updated `auth_status` (TTL check); added `ai_client_register`, `admin_clients_list`, `admin_client_block`, `admin_client_unblock`, `admin_client_delete` |
| `auth_app/urls.py` | Added `/ai/register`, `/admin/clients/*` routes |
| `auth_app/templates/connect.html` | New duration picker: 90 days default, Always, Custom, This session only |
| `mcp_app/protocol/auth.py` | `resolve_credentials` returns 3-tuple; UUID→AIClient routing; `_resolve_session` TTL check; typed error codes; `build_unauthorized_response` accepts `error_code` |
| `mcp_app/views.py` | Unpacks 3-tuple; passes `error_code` to `build_unauthorized_response` |
| `publive_mcp/settings.py` | `SESSION_SAVE_EVERY_REQUEST=False`; `ADMIN_SECRET_KEY`; `SESSION_COOKIE_AGE` updated |
| `auth_app/tests/test_session.py` | Extended for new durations, TTL enforcement, `SESSION_EXPIRED` |
| `auth_app/tests/test_ai_client.py` | New — 27 tests for registration, rate limiting, MCP auth, admin endpoints |
| `mcp_app/tests/test_auth.py` | Updated for 3-tuple; new tests for all typed error codes |
