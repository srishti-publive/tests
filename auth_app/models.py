# Responsibility: Auth data models — OAuth 2.0 PKCE flow only.
from django.db import models


class OAuthClient(models.Model):
    """
    A dynamically-registered OAuth 2.0 client (one per AI client install).
    """

    client_id    = models.CharField(max_length=64, unique=True, db_index=True)
    redirect_uri = models.CharField(max_length=512, blank=True, default="")
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "oauth_client"
        ordering = ["-created_at"]
        verbose_name = "OAuth Client"
        verbose_name_plural = "OAuth Clients"


class OAuth_Pkce_Code(models.Model):
    """Single-use PKCE authorization code. Valid for 10 minutes, deleted on exchange."""

    code           = models.CharField(max_length=128, unique=True)
    client_id      = models.CharField(max_length=64, db_index=True)
    redirect_uri   = models.TextField()
    code_challenge = models.CharField(max_length=256)
    credentials    = models.JSONField()
    expires_at     = models.DateTimeField()

    class Meta:
        db_table = "oauth_code"
        ordering = ["-expires_at"]
        verbose_name = "OAuth Code"
        verbose_name_plural = "OAuth Codes"


class OAuthToken(models.Model):
    """
    Long-lived bearer token issued after successful PKCE auth.
    credentials stores {publisherId, apiKey, apiSecret} — the Publive API credentials
    """

    token         = models.CharField(max_length=128, unique=True)
    client_id     = models.CharField(max_length=64, db_index=True, blank=True, default="")
    publisher_id  = models.CharField(max_length=64, db_index=True, blank=True, default="")
    refresh_token = models.CharField(max_length=128, unique=True, null=True, blank=True)
    credentials   = models.JSONField()
    created_at    = models.DateTimeField(auto_now_add=True, null=True)

    class Meta:
        db_table = "oauth_token"
        ordering = ["-created_at"]
        verbose_name = "OAuth Token"
        verbose_name_plural = "OAuth Tokens"
