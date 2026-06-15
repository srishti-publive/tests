"""Database-backed store for single-use OAuth PKCE authorization codes.

Authorization codes are minted at ``/oauth/authorize`` and redeemed once at
``/oauth/token``. Backed by the ``OAuthAuthorizationCode`` model. Single-use is
enforced by an atomic select-for-update-then-delete (replacing Redis ``GETDEL``).
There is no expiry/TTL: a code lives until it is redeemed, then it is deleted.
"""
from typing import Optional

from django.db import transaction

from .models import OAuthAuthorizationCode


def store_code(code: str, client_id: str, redirect_uri: str,
               code_challenge: str, credentials: dict) -> None:
    """Persist a freshly-minted authorization code."""
    OAuthAuthorizationCode.objects.update_or_create(
        code=code,
        defaults={
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "credentials": credentials,
        },
    )


def pop_code(code: str) -> Optional[dict]:
    """Atomically fetch-and-delete a code (single-use).

    Returns the stored dict, or None if the code is unknown. The
    select-for-update-then-delete guarantees a second redemption of the same code
    finds nothing, so each code can be exchanged at most once.
    """
    with transaction.atomic():
        row = (
            OAuthAuthorizationCode.objects
            .select_for_update(skip_locked=True)
            .filter(code=code)
            .first()
        )
        if row is None:
            return None
        payload = {
            "client_id": row.client_id,
            "redirect_uri": row.redirect_uri,
            "code_challenge": row.code_challenge,
            "credentials": row.credentials,
        }
        row.delete()
        return payload
