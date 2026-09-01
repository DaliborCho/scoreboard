"""Password hashing, opaque API/display tokens, and secret encryption at rest.

Two separate concerns live here on purpose:

* Tokens we issue (API keys, display tokens) are stored as hashes. We can
  verify them but never display them again after creation.
* Credentials we hold on a customer's behalf (a Tableau PAT) must be
  recoverable, so they are encrypted rather than hashed.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from scoreboard.config import settings

_hasher = PasswordHasher()

TOKEN_PREFIX_LENGTH = 8


# ---------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, Exception):
        return False


# ---------------------------------------------------------------- opaque tokens
def generate_token(kind: str) -> tuple[str, str, str]:
    """Return (full_token, prefix, hash).

    The full token is shown to the user exactly once. Only prefix and hash
    are persisted, so a database leak does not hand over working keys.
    """
    raw = secrets.token_urlsafe(32)
    token = f"{kind}_{raw}"
    return token, token[:TOKEN_PREFIX_LENGTH], hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


# ---------------------------------------------------------------- secrets at rest
def _fernet() -> Fernet:
    key = settings().encryption_key
    if not key:
        raise RuntimeError(
            "SCOREBOARD_ENCRYPTION_KEY is not set. Generate one with:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Stored credential could not be decrypted.") from exc
