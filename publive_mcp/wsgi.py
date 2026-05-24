import os
from pathlib import Path
import newrelic.agent
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "publive_mcp.settings")

# Resolve newrelic.ini relative to the project root (parent of this package dir)
_NR_CONFIG = Path(__file__).resolve().parent.parent / "newrelic.ini"
newrelic.agent.initialize(str(_NR_CONFIG))
application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
