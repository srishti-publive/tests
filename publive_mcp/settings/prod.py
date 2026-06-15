# Responsibility: Production overrides for Railway deployment — DEBUG off, secure cookies.
from .base import *  # noqa: F401, F403

DEBUG = False
SESSION_COOKIE_SECURE = True

# SSE sessions, message queues, session stats, and rate limiting are all backed by
# the database now (see mcp_app/models.py and the DatabaseCache config in base.py),
# so the only hard prod dependency is DATABASE_URL — no separate Redis service.
