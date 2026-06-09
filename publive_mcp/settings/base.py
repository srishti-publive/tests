# Responsibility: Shared settings for all environments. 
# Never import this directly use settings.local or settings.prod which extend this file.

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


CDS_BASE_URL = os.environ.get(
    "CDS_BASE_URL", "https://cds-beta.thepublive.com/publisher/{publisher_id}"
)
CMS_BASE_URL = os.environ.get(
    "CMS_BASE_URL", "https://cms-beta.thepublive.com/publisher/{publisher_id}"
)

# /*  
#     Apps - self-contained module that implements a specific feature 
#     Middleware - runs for every request and response, checkpoints before and after your views.
# */

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "auth_app",
    "mcp_app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "mcp_app.middleware.RequestIDMiddleware",
    "mcp_app.middleware.SecurityHeadersMiddleware",
    "mcp_app.middleware.RateLimitMiddleware",
]

ROOT_URLCONF = "publive_mcp.urls"


# ### # 
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



DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR}/db.sqlite3",
        conn_max_age=600,
    )
}

# ── Cache ─────────────────────────────────────────────────────────────────────
# Redis-backed so the cache is shared across gunicorn workers/replicas

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}


# ── Sessions ──────────────────────────────────────────────────────────────────

# Sessions stored in Postgres (via DATABASE_URL on Railway) so they survive redeploys.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = 10 * 356 * 24 * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = True



# ── OAuth security ────────────────────────────────────────────────────────────
OAUTH_ALLOWED_ORIGINS = [
    # Anthropic / Claude (web)
    "https://claude.ai",
    "https://api.claude.ai",
    # OpenAI / ChatGPT (web)
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://platform.openai.com",
    # Google Gemini (web)
    "https://gemini.google.com",
    "https://aistudio.google.com",
    # Microsoft Copilot (web)
    "https://copilot.microsoft.com",
    "https://www.bing.com",
]

# Dynamic client registration (RFC 7591 / OAuth 2.1) is open to any client —
# redirect_uri just has to be https:// or a loopback address. See
# auth_app.services.is_registrable_redirect_uri.

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Structured JSON logging ───────────────────────────────────────────────────
# Emits each log line as JSON so New Relic Logs can index individual fields.
# The NR agent injects trace.id and span.id automatically, enabling APM → Logs links.


# ###### #
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
