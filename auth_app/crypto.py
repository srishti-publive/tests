"""Fernet symmetric encryption for credentials stored at rest.

Key management:
    Set CREDENTIALS_ENCRYPTION_KEY to a Fernet key (URL-safe base64, 32 bytes).
    Generate one with:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    If the key is absent in production, credentials encrypted with an ephemeral key
    will be unreadable after restart. Always set this before running migrations.
"""
import json
import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken  # noqa: F401 — re-exported for callers

logger = logging.getLogger(__name__)

_fernet: Optional[Fernet] = None


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY", "")
        if not key:
            generated = Fernet.generate_key()
            logger.warning(
                "CREDENTIALS_ENCRYPTION_KEY is not set — using an ephemeral key. "
                "Credentials will be unreadable after restart. "
                "Set this env var before running in production."
            )
            _fernet = Fernet(generated)
        else:
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_json(data: dict) -> str:
    """JSON-serialize and Fernet-encrypt a credentials dict. Returns a URL-safe base64 token."""
    plaintext = json.dumps(data, separators=(",", ":"))
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_json(token: str) -> dict:
    """Fernet-decrypt and JSON-parse an encrypted credentials token.

    Raises cryptography.fernet.InvalidToken if the token is malformed or the key is wrong.
    """
    plaintext = get_fernet().decrypt(token.encode())
    return json.loads(plaintext)
