# Responsibility: URL routing for OAuth 2.0 PKCE flow, session-based auth,
# AI client direct registration, and admin client management.
from django.urls import path

from . import views

urlpatterns = [
    # OAuth discovery (well-known endpoints)
    path(".well-known/oauth-protected-resource", views.oauth_protected_resource, name="oauth-protected-resource"),
    path(".well-known/oauth-protected-resource/<path:resource_path>", views.oauth_protected_resource, name="oauth-protected-resource-path"),
    path(".well-known/oauth-authorization-server", views.oauth_server_metadata, name="oauth-server-metadata"),
    path(".well-known/openid-configuration", views.oauth_server_metadata, name="openid-configuration"),
    # OAuth 2.0 PKCE flow (AI clients via Claude Desktop / Cursor / SDK)
    path("register", views.oauth_register, name="oauth-register"),
    path("authorize", views.oauth_authorize, name="oauth-authorize-root"),
    path("oauth/authorize", views.oauth_authorize, name="oauth-authorize"),
    path("token", views.oauth_token, name="oauth-token-root"),
    path("oauth/token", views.oauth_token, name="oauth-token"),
    # Session-based auth (human browser users)
    path("connect", views.connect, name="connect"),
    path("auth/login", views.auth_login, name="auth_login"),
    path("auth/success", views.auth_success, name="auth_success"),
    path("auth/status", views.auth_status, name="auth_status"),
    path("auth/logout", views.auth_logout, name="auth_logout"),
    # AI client direct registration (open, rate-limited)
    path("ai/register", views.ai_client_register, name="ai-client-register"),
    # Admin — AI client management (requires ADMIN_SECRET_KEY bearer token)
    path("admin/clients", views.admin_clients_list, name="admin-clients-list"),
    path("admin/clients/<str:client_id>/block", views.admin_client_block, name="admin-client-block"),
    path("admin/clients/<str:client_id>/unblock", views.admin_client_unblock, name="admin-client-unblock"),
    path("admin/clients/<str:client_id>", views.admin_client_delete, name="admin-client-delete"),
]
