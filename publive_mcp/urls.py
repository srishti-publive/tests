from django.urls import path, include

urlpatterns = [
    path("", include("auth_app.urls")),
    path("", include("mcp_app.urls")),
]
