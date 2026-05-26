import os
from pathlib import Path

import newrelic.agent
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "publive_mcp.settings")

# ── New Relic ─────────────────────────────────────────────────────────────────

_NR_CONFIG = Path(__file__).resolve().parent.parent / "newrelic.ini"
newrelic.agent.initialize(str(_NR_CONFIG))

# ── Redis connection check ────────────────────────────────────────────────────
# Fail loudly at startup if REDIS_URL is set but Redis is unreachable.
# Without this the app starts silently on DB sessions and login loss is silent.

_REDIS_URL = os.environ.get("REDIS_URL", "")
if _REDIS_URL:
    try:
        import redis as _redis
        _r = _redis.from_url(_REDIS_URL, socket_connect_timeout=5, socket_timeout=5)
        _r.ping()
        del _r
    except Exception as _exc:
        raise RuntimeError(
            f"[startup] Redis unreachable at {_REDIS_URL!r}: {_exc}\n"
            "Fix REDIS_URL or remove it to fall back to DB sessions."
        ) from _exc

# ── WSGI application ──────────────────────────────────────────────────────────

application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
