"""Encrypt per-account API credentials at rest.

The Fernet master key must be supplied by the deployment environment and must
never be included in Git, a response payload, or a database dump.
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


def _cipher() -> Fernet:
    key = os.environ.get("STOCK_USER_CREDENTIAL_KEY", "").strip()
    if not key:
        raise RuntimeError("user_credential_encryption_key_not_configured")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("user_credential_encryption_key_invalid") from exc


def encrypt_credential(value: str) -> str:
    if not value:
        return ""
    return _cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_credential(value: str) -> str:
    if not value:
        return ""
    try:
        return _cipher().decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("user_credential_decryption_failed") from exc
