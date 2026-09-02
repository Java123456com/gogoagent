"""Credential encryption and full-boundary sensitive-data redaction."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend.config import get_settings


def new_token() -> str:
    """Generate a random 128-bit application token."""
    return secrets.token_hex(16)


_KV_SECRET_PATTERN = (
    r"((?:api[_-]?key|access[_-]?key(?:[_-]?(?:id|secret))?|client[_-]?secret|"
    r"secret[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|pwd)"
    r"[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',;}]+)"
)

_CHINESE_NAME_PATTERN = (
    r"(?i)(姓名|中文名|乘客|联系人|乘机人|chinese_?name)"
    r"([\"']?\s*[:=：]?\s*[\"']?)([\u4e00-\u9fa5]{2,4})"
)
_PINYIN_NAME_PATTERN = r"(?<![A-Za-z])[A-Z][A-Za-z]{1,10}(?:\s+[A-Z][A-Za-z]{1,10}){1,3}(?![A-Za-z])"

_PATTERNS: list[tuple[str, str]] = [
    (r"(?<!\d)(\d{6})(\d{8})(\d{3})([\dXx])(?!\d)", r"\1********\3\4"),
    (r"(?<!\d)\d{12,15}(\d{4})(?!\d)", r"****\1"),
    (r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)", r"\1****\3"),
    (r"([a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]*@", r"\1***@"),
    (r"sk-[A-Za-z0-9]{6,}", r"****"),
    (_CHINESE_NAME_PATTERN, r"\1\2****"),
    (_PINYIN_NAME_PATTERN, r"****"),
    (_KV_SECRET_PATTERN, r"\1****"),
    (r"(authorization\s*[:=]\s*[\"']?bearer\s+)([^\s\"',;}]+)", r"\1****"),
    (r"(bearer\s+)([A-Za-z0-9._~+/=-]{8,})", r"\1****"),
    (r"sk_[A-Za-z0-9]{6,}", r"****"),
]


def mask_sensitive(text: str | None) -> str:
    if not text:
        return text or ""
    result = text
    for pattern, replacement in _PATTERNS:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


_SENSITIVE_KEYS = {
    "apikey", "accesskey", "accesskeyid", "accesskeysecret", "secret", "secretkey",
    "clientsecret", "password", "passwd", "token", "accesstoken", "refreshtoken",
    "authorization", "cookie", "idnumber", "idcard", "bankcard", "chinesename",
    "namepinyin", "phone", "mobile", "contactphone", "email",
}


def sanitize_sensitive(value: Any) -> Any:
    """Recursively redact secrets and PII without changing the payload shape."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = re.sub(r"[-_]", "", str(key).lower())
            if normalized in _SENSITIVE_KEYS or any(
                marker in normalized
                for marker in ("apikey", "secret", "password", "token", "authorization")
            ):
                cleaned[key] = "***"
            else:
                cleaned[key] = sanitize_sensitive(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_sensitive(item) for item in value)
    if isinstance(value, str):
        return mask_sensitive(value)
    return value


class SensitiveLoggingFilter(logging.Filter):
    """Python equivalent of the Java Logback SensitiveMaskingConverter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask_sensitive(str(record.msg))
        if isinstance(record.args, dict):
            record.args = sanitize_sensitive(record.args)
        elif record.args:
            record.args = tuple(
                mask_sensitive(item) if isinstance(item, str) else item
                for item in record.args
            )
        return True


def install_sensitive_logging() -> None:
    root = logging.getLogger()
    if any(isinstance(item, SensitiveLoggingFilter) for item in root.filters):
        return
    boundary = SensitiveLoggingFilter()
    root.addFilter(boundary)
    for handler in root.handlers:
        handler.addFilter(boundary)


_GCM_PREFIX = "gcm:v1:"
_GCM_NONCE_BYTES = 12


def _aes_key() -> bytes:
    return hashlib.sha256(get_settings().api_key_encrypt_secret.encode("utf-8")).digest()


def encrypt_api_key(plaintext: str) -> str:
    """AES-256-GCM with a random 96-bit nonce and authentication tag."""
    if plaintext is None:
        raise ValueError("plaintext cannot be None")
    nonce = secrets.token_bytes(_GCM_NONCE_BYTES)
    encrypted = AESGCM(_aes_key()).encrypt(nonce, plaintext.encode("utf-8"), None)
    return _GCM_PREFIX + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_api_key(ciphertext: str) -> str:
    if not ciphertext:
        raise ValueError("ciphertext cannot be empty")
    if ciphertext.startswith(_GCM_PREFIX):
        combined = base64.urlsafe_b64decode(ciphertext[len(_GCM_PREFIX):].encode("ascii"))
        if len(combined) <= _GCM_NONCE_BYTES:
            raise ValueError("invalid AES-GCM ciphertext")
        nonce, encrypted = combined[:_GCM_NONCE_BYTES], combined[_GCM_NONCE_BYTES:]
        return AESGCM(_aes_key()).decrypt(nonce, encrypted, None).decode("utf-8")

    # Read compatibility for data written by the initial XOR/Base64 Python port.
    secret = get_settings().api_key_encrypt_secret.encode("utf-8")
    xored = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    return "".join(chr(byte ^ secret[index % len(secret)]) for index, byte in enumerate(xored))


def encrypted_json(value: Any) -> str:
    return encrypt_api_key(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def decrypted_json(value: str) -> Any:
    return json.loads(decrypt_api_key(value))
