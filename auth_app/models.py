# Responsibility: OAuth 2.0 PKCE data models — clients, short-lived auth codes, bearer tokens.
# Also: AIClient model for direct AI-client identity registration (non-PKCE path).
import secrets  # noqa: F401 — kept for potential future use in model defaults
import uuid

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


class AIClient(models.Model):
    """A directly-registered AI client (Claude, ChatGPT, any MCP agent).

    Registration is open — any client calls /ai/register with client_name (and
    optionally Publive credentials) and receives a UUID v4 client_id that serves
    as its sole bearer credential on every subsequent MCP request.

    One-ID-per-client is a POLICY contract enforced by admin oversight of the
    audit log (registration_ip, last_seen_at), NOT a technical constraint.
    Admins block misbehaving clients via /admin/clients/:id/block; the block
    takes effect on the very next request from that client.
    """

    STATUS_ACTIVE = "active"
    STATUS_BLOCKED = "blocked"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_BLOCKED, "Blocked"),
    ]

    client_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_name = models.CharField(max_length=255)
    contact = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True
    )
    # Publive publisher credentials stored at registration; required for making tool calls.
    credentials = models.JSONField(null=True, blank=True)
    registered_at = models.DateTimeField(auto_now_add=True)
    registration_ip = models.GenericIPAddressField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "ai_client"
        ordering = ["-registered_at"]
        verbose_name = "AI Client"
        verbose_name_plural = "AI Clients"

    def __str__(self):
        return f"{self.client_name} ({self.client_id})"
