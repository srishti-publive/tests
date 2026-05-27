from django.urls import path
from . import views

urlpatterns = [
    path("", views.health_check, name="health_check"),
    path("mcp", views.mcp_endpoint, name="mcp_endpoint"),
    path("mcp/message", views.mcp_message, name="mcp_message"),
]
