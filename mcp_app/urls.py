from django.urls import path

from mcp_app.views import mcp_endpoint
from mcp_app.views.health import health_check

urlpatterns = [
    path("",            health_check, name="health_check"),
    path("mcp",         mcp_endpoint, name="mcp_endpoint"),
]
