"""
    Fernet symmetric encryption for credentials stored at rest. Fernet key (URL-safe base64, 32 bytes).
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
            _fernet = Fernet(generated)
        else:
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt_json(data: dict) -> str:
    plaintext = json.dumps(data, separators=(",", ":"))
    return get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_json(token: str) -> dict:
    plaintext = get_fernet().decrypt(token.encode())
    return json.loads(plaintext)
