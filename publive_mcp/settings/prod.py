# Responsibility: Production overrides for Railway deployment — DEBUG off, secure cookies.
import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403

DEBUG = False
SESSION_COOKIE_SECURE = True

# Fail fast, don't degrade silently: Redis backs SSE sessions, message queues,
# session stats, and rate limiting. Booting without it would accept connections
# and then lose every message (the failure mode that made the silent-fallback
# credential-encryption rollout go bad — see git history).
if not os.environ.get("REDIS_URL"):
    raise ImproperlyConfigured(
        "REDIS_URL must be set in production (Redis backs SSE sessions, queues, "
        "stats, and rate limits). Provision a Redis instance and set the env var."
    )
