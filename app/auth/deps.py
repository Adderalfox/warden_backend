"""FastAPI dependency that resolves the logged-in user from the session cookie or a Bearer token."""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.auth.db import db, public_user
from app.auth.security import decode_token
from app.core.config import settings

COOKIE_NAME = "warden_session"
DEV_USER = {"id": 0, "email": "dev@local", "name": "Local dev", "created_at": 0.0, "last_login_at": None}


def _token_from(request: Request) -> str | None:
    if tok := request.cookies.get(COOKIE_NAME):
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


async def current_user(request: Request) -> dict:
    if not settings.auth_required:
        return DEV_USER
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    token = _token_from(request)
    payload = decode_token(token) if token else None
    if not payload:
        raise unauthorized
    try:
        row = db().user_by_id(int(payload["sub"]))
    except (ValueError, KeyError):
        raise unauthorized from None
    if row is None or row["token_version"] != payload.get("ver"):
        raise unauthorized
    return public_user(row)
