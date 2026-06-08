import os

from django.core.wsgi import get_wsgi_application
import newrelic.agent

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "publive_mcp.settings")


# This line is wrapping your Django WSGI application with New Relic's monitoring middleware.

application = newrelic.agent.WSGIApplicationWrapper(get_wsgi_application())
