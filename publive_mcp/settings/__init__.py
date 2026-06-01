# Responsibility: Settings entry-point — auto-selects local vs production config.
# DJANGO_SETTINGS_MODULE=publive_mcp.settings continues to resolve here.
# To pin an environment explicitly, set DJANGO_SETTINGS_MODULE to:
#   publive_mcp.settings.local   — local development
#   publive_mcp.settings.prod    — Railway / production
import os

_env = os.environ.get("RAILWAY_ENVIRONMENT", os.environ.get("DJANGO_ENV", "")).lower()

if _env in ("production", "prod"):
    from .prod import *  # noqa: F401, F403
else:
    from .local import *  # noqa: F401, F403
