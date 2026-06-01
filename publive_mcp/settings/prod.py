# Responsibility: Production overrides for Railway deployment — DEBUG off, secure cookies.
from .base import *  # noqa: F401, F403

DEBUG = False
SESSION_COOKIE_SECURE = True
