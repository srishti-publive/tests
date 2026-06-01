# Responsibility: OAuth 2.0 PKCE data models — clients, short-lived auth codes, bearer tokens.
import secrets  # noqa: F401 — kept for potential future use in model defaults

from django.db import models


class OAuthClient(models.Model):
    """A dynamically-registered OAuth 2.0 client (one per AI client install)."""

    client_id = models.CharField(max_length=64, unique=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "oauth_client"
        ordering = ["-created_at"]
        verbose_name = "OAuth Client"
        verbose_name_plural = "OAuth Clients"


class OAuthCode(models.Model):
    """A single-use PKCE authorization code, valid for 10 minutes after issuance."""

    code = models.CharField(max_length=128, unique=True)
    client_id = models.CharField(max_length=64)
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=256)
    credentials = models.JSONField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "oauth_code"
        ordering = ["-expires_at"]
        verbose_name = "OAuth Code"
        verbose_name_plural = "OAuth Codes"


class OAuthToken(models.Model):
    """A long-lived bearer token issued to an AI client after successful PKCE auth."""

    token = models.CharField(max_length=128, unique=True)
    client_id = models.CharField(max_length=64, db_index=True, blank=True, default="")
    refresh_token = models.CharField(max_length=128, unique=True, null=True, blank=True)
    credentials = models.JSONField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "oauth_token"
        ordering = ["-expires_at"]
        verbose_name = "OAuth Token"
        verbose_name_plural = "OAuth Tokens"
