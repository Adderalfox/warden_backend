"""Password hashing (scrypt, stdlib) and session tokens (HS256 JWT)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Optional

import jwt

from app.core.config import settings

_N, _R, _P = 2**14, 8, 1
DEFAULT_SECRET = "change-me-dev-auth-secret"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        calc = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:  # noqa: BLE001 - malformed hash => not a match
        return False


# Verified against when the e-mail is unknown, so response time doesn't reveal which e-mails exist.
_DUMMY_HASH = hash_password("not-a-real-password")


def verify_against_dummy(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


def create_token(user_id: int, token_version: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": str(user_id), "ver": token_version, "iat": now, "exp": now + settings.auth_token_ttl_minutes * 60},
        settings.auth_secret,
        algorithm="HS256",
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, settings.auth_secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        return None
