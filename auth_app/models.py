import secrets
from django.db import models


class OAuthClient(models.Model):
    client_id = models.CharField(max_length=64, unique=True)
    redirect_uris = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "oauth_client"


class OAuthCode(models.Model):
    code = models.CharField(max_length=128, unique=True)
    client_id = models.CharField(max_length=64)
    redirect_uri = models.TextField()
    code_challenge = models.CharField(max_length=256)
    credentials = models.JSONField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "oauth_code"


class OAuthToken(models.Model):
    token = models.CharField(max_length=128, unique=True)
    credentials = models.JSONField()
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "oauth_token"
