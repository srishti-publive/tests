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


class OAuthAuthorizationCode(models.Model):
    """
    Single-use OAuth 2.0 PKCE authorization code: minted at /oauth/authorize,
    redeemed once at /oauth/token, then deleted.

    Replaces the prior Redis store (GETDEL). Single-use is enforced by an atomic
    select-for-update-then-delete in oauth_code_store.pop_code. There is no expiry:
    a code lives until it is redeemed (then deleted). credentials stores
    {publisherId, apiKey, apiSecret}.
    """

    code           = models.CharField(max_length=128, unique=True, db_index=True)
    client_id      = models.CharField(max_length=64, blank=True, default="")
    redirect_uri   = models.CharField(max_length=512, blank=True, default="")
    code_challenge = models.CharField(max_length=128, blank=True, default="")
    credentials    = models.JSONField()
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "oauth_authorization_code"
        ordering = ["-created_at"]
        verbose_name = "OAuth Authorization Code"
        verbose_name_plural = "OAuth Authorization Codes"
