# Responsibility: Local development overrides — DEBUG on, insecure cookies allowed.
from .base import *  # noqa: F401, F403

DEBUG = True
SESSION_COOKIE_SECURE = False
RATE_LIMIT_ENABLED = False

# Use DEBUG-level logging for application code in local dev.
LOGGING["loggers"]["mcp_app"]["level"] = "DEBUG"  # noqa: F405
