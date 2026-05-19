from django.urls import path
from . import views

urlpatterns = [
    # OAuth discovery
    path(".well-known/oauth-protected-resource", views.oauth_protected_resource),
    path(".well-known/oauth-protected-resource/<path:resource_path>", views.oauth_protected_resource),
    path(".well-known/oauth-authorization-server", views.oauth_server_metadata),
    path(".well-known/openid-configuration", views.oauth_server_metadata),
    # OAuth flow
    path("register", views.oauth_register),
    path("oauth/authorize", views.oauth_authorize),
    path("oauth/token", views.oauth_token),
    # Legacy session auth
    path("connect", views.connect, name="connect"),
    path("auth/login", views.auth_login, name="auth_login"),
    path("auth/success", views.auth_success, name="auth_success"),
    path("auth/status", views.auth_status, name="auth_status"),
    path("auth/logout", views.auth_logout, name="auth_logout"),
]
