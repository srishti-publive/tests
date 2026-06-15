"""Redis-backed store for single-use OAuth PKCE authorization codes.

Authorization codes are ephemeral: minted at ``/oauth/authorize``, redeemed once
at ``/oauth/token`` within 10 minutes, then gone. Redis fits this far better than a
durable table — TTL handles expiry for free, and ``GETDEL`` enforces true single-use
atomically across processes/replicas (no double-redemption race). Reuses the shared
client from ``mcp_app.redis_client``; no expiry timestamp is stored because the TTL
is the expiry.
"""
import json
from typing import Optional

from mcp_app.redis_client import get_redis_client

_TTL_SECONDS = 600  # 10 minutes
_PREFIX = "oauth:code:"


def _key(code: str) -> str:
    return f"{_PREFIX}{code}"


def store_code(code: str, client_id: str, redirect_uri: str,
               code_challenge: str, credentials: dict) -> None:
    """Persist a freshly-minted authorization code with a 10-minute TTL."""
    payload = json.dumps({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "credentials": credentials,
    })
    get_redis_client().set(_key(code), payload, ex=_TTL_SECONDS)


def pop_code(code: str) -> Optional[dict]:
    """Atomically fetch-and-delete a code (single-use).

    Returns the stored dict, or None if the code is unknown or TTL-expired.
    The atomic GETDEL guarantees a second redemption of the same code finds
    nothing, so each code can be exchanged at most once.
    """
    raw = get_redis_client().getdel(_key(code))
    if raw is None:
        return None
    return json.loads(raw)
