"""Bounded cryptographic and redaction primitives for Web-only state."""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

REDACTED = "[REDACTED]"
TRUNCATED = "…[truncated]"
DEFAULT_REDACTION_LIMIT = 4096
MAX_CREDENTIAL_BYTES = 64 * 1024
MAX_CIPHERTEXT_BYTES = 128 * 1024
_SENSITIVE_KEY = re.compile(
    r"(?:authorization|cookie|credential|password|secret|token|api[_-]?key)", re.IGNORECASE
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)"
    r"\s*[:=]\s*[^\s,;]+"
)


class CredentialCipher:
    """Versioned Fernet encryption primitive with bounded inputs.

    A mapping may be supplied for later key rotation. New ciphertext uses the
    active key ID while decrypt accepts every configured key ID.
    """

    def __init__(
        self,
        keys: str | Mapping[str, str],
        *,
        active_key_id: str = "v1",
    ) -> None:
        configured = {active_key_id: keys} if isinstance(keys, str) else dict(keys)
        if not configured or active_key_id not in configured:
            raise ValueError("an active credential encryption key is required")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", key_id) for key_id in configured):
            raise ValueError("credential key IDs must be short opaque identifiers")
        self._active_key_id = active_key_id
        self._ciphers = {
            key_id: Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))
            for key_id, secret in configured.items()
        }

    def encrypt(self, plaintext: str) -> str:
        encoded = plaintext.encode("utf-8")
        if not encoded or len(encoded) > MAX_CREDENTIAL_BYTES:
            raise ValueError("credential plaintext size is invalid")
        token = self._ciphers[self._active_key_id].encrypt(encoded).decode("ascii")
        return f"{self._active_key_id}:{token}"

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext or len(ciphertext.encode("utf-8")) > MAX_CIPHERTEXT_BYTES:
            raise ValueError("credential ciphertext size is invalid")
        key_id, separator, token = ciphertext.partition(":")
        cipher = self._ciphers.get(key_id)
        if not separator or cipher is None:
            raise ValueError("credential ciphertext key is unavailable")
        try:
            plaintext = cipher.decrypt(token.encode("ascii"))
            return plaintext.decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as error:
            raise ValueError("credential ciphertext is invalid") from error


def _bounded(value: str, limit: int) -> str:
    if limit < len(TRUNCATED):
        return TRUNCATED[: max(0, limit)]
    if len(value) <= limit:
        return value
    return value[: limit - len(TRUNCATED)] + TRUNCATED


def redact_text(
    value: object,
    *,
    secrets: Sequence[str] = (),
    limit: int = DEFAULT_REDACTION_LIMIT,
) -> str:
    """Return bounded text with explicit and recognizable credentials removed."""
    if limit < 0:
        raise ValueError("redaction limit must be non-negative")
    text = str(value)
    for secret in sorted((item for item in secrets if item), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _BEARER.sub(lambda match: f"{match.group(1)} {REDACTED}", text)
    text = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    return _bounded(text, limit)


def redact_mapping(
    value: Mapping[str, Any],
    *,
    secrets: Sequence[str] = (),
    limit: int = DEFAULT_REDACTION_LIMIT,
    max_depth: int = 4,
    max_items: int = 64,
) -> dict[str, Any]:
    """Recursively sanitize a bounded mapping without preserving secret fields."""

    def sanitize(item: Any, depth: int) -> Any:
        if depth > max_depth:
            return TRUNCATED
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for index, (raw_key, child) in enumerate(item.items()):
                if index >= max_items:
                    result[TRUNCATED] = TRUNCATED
                    break
                key = redact_text(raw_key, secrets=secrets, limit=128)
                result[key] = (
                    REDACTED if _SENSITIVE_KEY.search(str(raw_key)) else sanitize(child, depth + 1)
                )
            return result
        if isinstance(item, (list, tuple)):
            return [sanitize(child, depth + 1) for child in item[:max_items]]
        return redact_text(item, secrets=secrets, limit=limit)

    return sanitize(value, 0)
