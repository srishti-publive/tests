# Responsibility: URL routing for OAuth 2.0 PKCE flow and session-based auth.
from django.urls import path

from . import views

urlpatterns = [
    # ── OAuth discovery (well-known endpoints) ────────────────────────────────
    # Finding #13: Two paths for oauth_protected_resource are intentional.
    # RFC 9728 §3 allows sub-resource metadata under the base path.
    # resource_path is accepted by the view but currently unused — the same
    # response is returned regardless.  Both paths are required for
    # standards-compliant OAuth 2.0 Protected Resource discovery.
    path(".well-known/oauth-protected-resource", views.oauth_protected_resource, name="oauth-protected-resource"),
    path(".well-known/oauth-protected-resource/<path:resource_path>", views.oauth_protected_resource, name="oauth-protected-resource-path"),

    # Finding #12: Two paths for oauth_server_metadata are intentional aliases.
    # RFC 8414 mandates /.well-known/oauth-authorization-server.
    # OpenID Connect mandates /.well-known/openid-configuration.
    # Both must resolve to the same metadata document.
    path(".well-known/oauth-authorization-server", views.oauth_server_metadata, name="oauth-server-metadata"),
    path(".well-known/openid-configuration", views.oauth_server_metadata, name="openid-configuration"),

    # ── OAuth 2.0 PKCE flow ───────────────────────────────────────────────────
    path("register", views.oauth_register, name="oauth-register"),
    path("oauth/authorize", views.oauth_authorize, name="oauth-authorize"),
    path("oauth/token", views.oauth_token, name="oauth-token"),

    # ── Session-based auth (browser users) ───────────────────────────────────
    path("connect", views.connect, name="connect"),
    path("auth/login", views.auth_login, name="auth_login"),
    path("auth/success", views.auth_success, name="auth_success"),
    path("auth/status", views.auth_status, name="auth_status"),
    path("auth/logout", views.auth_logout, name="auth_logout"),
]
