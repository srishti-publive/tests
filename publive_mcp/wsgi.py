import os
import signal
import sys
from pathlib import Path

import newrelic.agent
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "publive_mcp.settings")

# ── New Relic ─────────────────────────────────────────────────────────────────
# Bootstrap the agent BEFORE wrapping the app. Without this call the
# WSGIApplicationWrapper below wraps an agent that was never started — a silent
# no-op that reports nothing (APM, custom events, forwarded logs). This block was
# accidentally dropped on 2026-06-04, which stopped all NR reporting.

_NR_CONFIG = Path(__file__).resolve().parent.parent / "newrelic.ini"
if _NR_CONFIG.is_file():
    newrelic.agent.initialize(str(_NR_CONFIG))
else:
    # Don't crash-loop the whole server over a missing observability config —
    # but make the degradation loud, not silent (it's what hid this for days).
    print(
        f"[startup] WARNING: New Relic config not found at {_NR_CONFIG} — "
        "agent NOT initialized, no telemetry will be reported.",
        file=sys.stderr,
        flush=True,
    )


# ── Graceful NR harvest on SIGTERM ────────────────────────────────────────────
# Railway sends SIGTERM before killing a dyno.  Without this handler the NR agent
# may not flush its pending harvest (custom events, metrics recorded since the
# last 60-second harvest cycle) — SSE session summaries and last-minute tool call
# metrics would be silently lost.
def _sigterm_handler(sig, frame):
    """Flush NR pending harvest, then exit cleanly."""
    try:
        newrelic.agent.shutdown_agent(timeout=10)
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)

# ── WSGI application ──────────────────────────────────────────────────────────

application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
