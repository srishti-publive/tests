import os
import dj_database_url
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ──────────────────────────────────────────────────────────────────

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = os.environ.get("DEBUG", "False") == "True"
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

# ── Redis & Cache ─────────────────────────────────────────────────────────────

REDIS_URL = os.environ.get("REDIS_URL", "")

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "OPTIONS": {
                "CLIENT_CLASS": "django_redis.client.DefaultClient",
                # Fail fast rather than hang — gunicorn timeout is 60 s
                "SOCKET_CONNECT_TIMEOUT": 5,
                "SOCKET_TIMEOUT": 5,
                "RETRY_ON_TIMEOUT": True,
                "CONNECTION_POOL_KWARGS": {"max_connections": 20},
            },
            "KEY_PREFIX": "publive_mcp",
            # Match session lifetime so cache entries don't expire before cookies
            "TIMEOUT": int(os.environ.get("SESSION_COOKIE_AGE", str(7 * 24 * 3600))),
        }
    }
    # cached_db: Redis is primary (fast read/write), Postgres is fallback.
    # Sessions survive Redis restarts — they're also written to the DB table.
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
    SESSION_CACHE_ALIAS = "default"
else:
    # No Redis configured (local dev without Redis) — fall back to DB sessions.
    # The app will still work; just won't survive redeploys until Redis is wired up.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

# ── Sessions ──────────────────────────────────────────────────────────────────

# Override via Railway env var if you want shorter/longer sessions.
# Default: 7 days (604800 s). Previous value was 1 day (86400 s).
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", str(7 * 24 * 3600)))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"

# ── Static files ──────────────────────────────────────────────────────────────

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
