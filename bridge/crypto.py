"""Symmetric encryption for upstream credentials.

The key is derived from ``BRIDGE_ENCRYPTION_KEY`` when provided, otherwise from
``SECRET_KEY`` so a fresh checkout still works out of the box.  Stored values
are Fernet tokens; plaintext never touches the database.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    passphrase = (settings.BRIDGE or {}).get("ENCRYPTION_KEY") or settings.SECRET_KEY
    digest = hashlib.sha256(passphrase.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """Return the plaintext, or ``""`` for malformed/legacy values."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""
