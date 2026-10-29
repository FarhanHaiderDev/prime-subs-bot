"""Encryption of purchased credentials at rest (Fernet; key(s) from ENCRYPTION_KEY).

Stored credentials never sit in MongoDB as plaintext: the order holds only the
encrypted token. ENCRYPTION_KEY is a Fernet key. To rotate, put the NEW key first and
keep the old one(s) after it, comma-separated: new data is encrypted with the first key
and everything encrypted with any listed key still decrypts.

Error messages never contain the key or any decrypted data.
"""

import json
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


class CredentialKeyError(Exception):
    """ENCRYPTION_KEY is missing or not a valid Fernet key."""


class CredentialDecryptError(Exception):
    """Stored credentials could not be decrypted (wrong key or corrupt data)."""


class CredentialBox:
    def __init__(self, keys: str):
        parts = [part.strip() for part in (keys or "").split(",") if part.strip()]
        if not parts:
            raise CredentialKeyError("ENCRYPTION_KEY is empty")
        try:
            self._fernet = MultiFernet([Fernet(part) for part in parts])
        except (ValueError, TypeError):
            raise CredentialKeyError("ENCRYPTION_KEY is not a valid Fernet key") from None

    @classmethod
    def from_env(cls) -> "CredentialBox":
        keys = os.getenv("ENCRYPTION_KEY")
        if not keys:
            raise CredentialKeyError("ENCRYPTION_KEY environment variable is not set")
        return cls(keys)

    def encrypt_list(self, items: list[str]) -> str:
        return self._fernet.encrypt(json.dumps(list(items)).encode("utf-8")).decode("ascii")

    def decrypt_list(self, token: str) -> list[str]:
        try:
            data = json.loads(self._fernet.decrypt(token.encode("ascii")))
        except (InvalidToken, ValueError, AttributeError):
            raise CredentialDecryptError(
                "stored credentials could not be decrypted (wrong ENCRYPTION_KEY or corrupt data)"
            ) from None
        if not isinstance(data, list):
            raise CredentialDecryptError("stored credentials have an unexpected format")
        return [str(item) for item in data]
