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

# ── Database ──────────────────────────────────────────────────────────────────

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR}/db.sqlite3",
        conn_max_age=600,
    )
}

# ── Cache ─────────────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "")

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

# Redirect URIs allowed during dynamic client registration.
OAUTH_ALLOWED_REDIRECT_PREFIXES = [
    "https://claude.ai/",
    "http://localhost:",
    "http://127.0.0.1:",
]

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
