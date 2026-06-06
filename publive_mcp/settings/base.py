# Responsibility: Shared settings for all environments. Never import this directly —
# use settings.local or settings.prod which extend this file.
import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ── Security ──────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
ALLOWED_HOSTS = ["*"]
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

# ── Publive API hosts ─────────────────────────────────────────────────────────
# Base URL templates for the Publive CDS (read) and CMS (write) APIs. Override per
# environment via env vars, e.g. to target production:
#   CDS_BASE_URL=https://cds.thepublive.com/publisher/{publisher_id}
#   CMS_BASE_URL=https://cms.thepublive.com/publisher/{publisher_id}
# The "{publisher_id}" placeholder is filled per request. Do NOT add a trailing
# slash — handler paths are appended with a leading "/" (e.g. "/posts/").
CDS_BASE_URL = os.environ.get(
    "CDS_BASE_URL", "https://cds-beta.thepublive.com/publisher/{publisher_id}"
)
CMS_BASE_URL = os.environ.get(
    "CMS_BASE_URL", "https://cms-beta.thepublive.com/publisher/{publisher_id}"
)

# ── Apps & Middleware ─────────────────────────────────────────────────────────

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
    "mcp_app.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "mcp_app.middleware.SecurityHeadersMiddleware",
    "mcp_app.middleware.RateLimitMiddleware",
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

# ── Database ──────────────────────────────────────────────────────────────────

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

# ── Sessions ──────────────────────────────────────────────────────────────────

# Sessions stored in Postgres (via DATABASE_URL on Railway) so they survive redeploys.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
# 90-day ceiling; per-session TTL is set dynamically via session.set_expiry().
SESSION_COOKIE_AGE = 90 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# Refresh TTL on every request so active sessions never expire mid-use.
SESSION_SAVE_EVERY_REQUEST = True

# ── OAuth security ────────────────────────────────────────────────────────────

# Origins allowed to call /oauth/token, /register, and /oauth/authorize (POST).
# Desktop MCP clients do not send Origin — those are unconditionally allowed.
OAUTH_ALLOWED_ORIGINS = [
    "https://claude.ai",
    "https://api.claude.ai",
]

# Exact redirect URIs allowed during dynamic client registration (OAuth 2.1).
# Extend at deploy time via OAUTH_ALLOWED_REDIRECT_URIS_EXTRA (comma-separated).
OAUTH_ALLOWED_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
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

# ── Structured JSON logging ───────────────────────────────────────────────────
# Emits each log line as JSON so New Relic Logs can index individual fields.
# The NR agent injects trace.id and span.id automatically, enabling APM → Logs links.

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
        "django": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "mcp_app": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "auth_app": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
