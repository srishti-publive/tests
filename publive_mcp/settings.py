import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = os.environ.get("DEBUG", "False") == "True"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "auth_app",
    "mcp_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "publive_mcp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "publive_mcp.wsgi.application"
\

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR}/db.sqlite3",
        conn_max_age=600,
    )
}

# ── Cache ─────────────────────────────────────────────────────────────────────

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Sessions are stored in the database (PostgreSQL in production, SQLite in dev).
# With DATABASE_URL pointing to Railway Postgres, sessions and OAuthTokens
# survive every redeploy because the data lives on the persistent postgres-volume,
# not inside the container.
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# ── Sessions ──────────────────────────────────────────────────────────────────

SESSION_COOKIE_AGE = 10 * 365 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
# Disabled so that Django does NOT roll the session expiry forward on each request.
# check_session_ttl() / _resolve_session() still enforce session_ttl_seconds for
# any pre-existing sessions that were created with a finite TTL before this change.
SESSION_SAVE_EVERY_REQUEST = False

# ── HTTPS / TLS ───────────────────────────────────────────────────────────────
# Railway terminates TLS at its edge proxy and forwards requests over plain
# HTTP internally.  Tell Django to trust the forwarded-proto header so that
# SESSION_COOKIE_SECURE and other HTTPS checks work correctly.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31536000          # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# ── OAuth security ────────────────────────────────────────────────────────────

# Origins allowed to call /oauth/token, /oauth/authorize (POST), and /register.
# Desktop MCP clients (Claude Desktop) do not send an Origin header — those are
# always allowed through. This list only governs browser-origin requests.
OAUTH_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://api.claude.ai",
    "https://claude.com",
    "https://chatgpt.com",
]

# Exact redirect URIs allowed during dynamic client registration (OAuth 2.1).
OAUTH_ALLOWED_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
]
_extra_redirect_uris = os.environ.get("OAUTH_ALLOWED_REDIRECT_URIS_EXTRA", "")
if _extra_redirect_uris:
    OAUTH_ALLOWED_REDIRECT_URIS.extend(
        uri.strip() for uri in _extra_redirect_uris.split(",") if uri.strip()
    )

# ── Static files ──────────────────────────────────────────────────────────────

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Structured JSON Logging ───────────────────────────────────────────────────
# Emits every log line as a JSON object so New Relic Logs can index individual
# fields (level, name, message) without custom parsing rules.
# The NR agent automatically injects trace.id and span.id into each forwarded
# log record, enabling direct APM-trace → Logs navigation in the NR UI.

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        # Django internals: WARNING+ only (avoid noisy request/SQL logs in prod)
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "mcp_app": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "auth_app": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
