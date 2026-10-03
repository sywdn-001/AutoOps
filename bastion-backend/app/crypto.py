"""资产凭据的对称加密（Fernet / AES-128-CBC + HMAC）。"""

from __future__ import annotations

import threading

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

_lock = threading.Lock()
_instances: dict[str, Fernet] = {}


def _fernet() -> Fernet:
    key = current_app.config["FERNET_KEY"]
    key_str = key.decode() if isinstance(key, bytes) else str(key)
    inst = _instances.get(key_str)
    if inst is None:
        with _lock:
            inst = _instances.get(key_str)
            if inst is None:
                inst = Fernet(key_str.encode("ascii"))
                _instances[key_str] = inst
    return inst


def encrypt(plaintext: str | None) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return ""


def mask(plaintext: str | None, keep: int = 2) -> str:
    """日志/接口回显用的掩码。"""
    if not plaintext:
        return ""
    if len(plaintext) <= keep:
        return "*" * len(plaintext)
    return plaintext[:keep] + "*" * max(4, len(plaintext) - keep)
