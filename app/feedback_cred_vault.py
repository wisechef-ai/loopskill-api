"""app/feedback_cred_vault.py — Encrypted PAT storage for user-routable feedback.

Fernet-symmetric encryption: the key (FEEDBACK_CRED_KEY) is an env var that
MUST be set in production.  The ciphertext is stored in
``cookbooks.feedback_pat_enc``.  The plaintext PAT is decrypted only in-memory
during dispatch and is NEVER logged.

Key generation (run once at deploy):
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    → set as WR_FEEDBACK_CRED_KEY env var

Security properties:
  - No plaintext secret persisted to disk or DB.
  - Token is decrypted immediately before the HTTP call and garbage-collected
    after the call returns.
  - Logs never contain the token — _dispatch masks it via _safe_token().
  - Fails closed: if the env key is missing or the ciphertext is corrupt,
    the function raises ValueError and dispatch is rejected.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_KEY_ENV = "WR_FEEDBACK_CRED_KEY"
_MAX_PAT_LEN = 200


def _get_fernet() -> "Fernet":  # type: ignore[return]
    """Return a Fernet instance backed by WR_FEEDBACK_CRED_KEY.

    Raises ValueError if the env var is missing or malformed.
    """
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401

    raw = os.environ.get(_KEY_ENV, "")
    if not raw:
        raise ValueError(
            f"{_KEY_ENV} is not set — cannot encrypt/decrypt feedback PAT credentials. "
            "Generate a key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(raw.encode())


def encrypt_pat(plaintext: str) -> str:
    """Encrypt a GitHub PAT using Fernet.  Returns base64url ciphertext string.

    Raises ValueError if WR_FEEDBACK_CRED_KEY is missing or the PAT is empty/too long.
    """
    if not plaintext:
        raise ValueError("PAT must not be empty")
    if len(plaintext) > _MAX_PAT_LEN:
        raise ValueError(f"PAT exceeds maximum length ({_MAX_PAT_LEN} chars)")
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt_pat(ciphertext: str) -> str:
    """Decrypt a Fernet-encrypted PAT ciphertext.

    Returns the plaintext PAT string.
    Raises ValueError if decryption fails (bad key, corrupt ciphertext).
    The caller is responsible for not logging the return value.
    """
    if not ciphertext:
        raise ValueError("ciphertext is empty — no PAT stored")
    f = _get_fernet()
    from cryptography.fernet import InvalidToken

    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("PAT decryption failed — key mismatch or corrupt ciphertext") from exc


def _safe_token(token: str) -> str:
    """Return a safe log-representation of a token (first 4 chars + ***).

    NEVER call this on the full token in a real log line — only use it when
    you want to confirm a token was resolved without logging the secret.
    """
    if len(token) < 4:
        return "***"
    return token[:4] + "***"
