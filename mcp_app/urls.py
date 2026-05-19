from django.urls import path
from . import views

urlpatterns = [
    path("mcp", views.mcp_endpoint, name="mcp_endpoint"),
    path("mcp/message", views.mcp_message, name="mcp_message"),
]
