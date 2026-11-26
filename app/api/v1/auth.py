from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from app.auth.db import db, public_user
from app.auth.deps import COOKIE_NAME, current_user
from app.auth.security import create_token, hash_password, verify_against_dummy, verify_password
from app.core.config import settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip()
        if not _EMAIL.match(v):
            raise ValueError("Enter a valid e-mail address")
        return v


class SignupBody(Credentials):
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=256)


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


# --- brute-force protection: 5 failed logins per (ip, email) per 5 minutes -----------------
_FAILS: Dict[str, List[float]] = defaultdict(list)
_WINDOW, _MAX_FAILS = 300, 5


def _throttle_key(request: Request, email: str) -> str:
    return f"{request.client.host if request.client else '?'}|{email.lower()}"


def _check_throttle(key: str) -> None:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS[key] if now - t < _WINDOW]
    if len(_FAILS[key]) >= _MAX_FAILS:
        retry = int(_WINDOW - (now - _FAILS[key][0])) + 1
        raise HTTPException(429, f"Too many failed attempts. Try again in {retry}s.", headers={"Retry-After": str(retry)})


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, max_age=settings.auth_token_ttl_minutes * 60, httponly=True,
        samesite="lax", secure=settings.environment == "production", path="/",
    )


def _signup_open() -> bool:
    return settings.auth_allow_signup or db().count_users() == 0


@router.get("/config")
async def auth_config():
    """Public: tells the login screen what to offer."""
    first = db().count_users() == 0 if settings.auth_required else False
    return {
        "auth_required": settings.auth_required,
        "signup_enabled": settings.auth_required and _signup_open(),
        "first_user": first,
    }


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(body: SignupBody, response: Response):
    if not settings.auth_required:
        raise HTTPException(400, "Authentication is disabled (WARDEN_AUTH_REQUIRED=false)")
    if not _signup_open():
        raise HTTPException(403, "Sign-ups are disabled on this server")
    if db().user_by_email(body.email):
        raise HTTPException(409, "An account with this e-mail already exists")
    pw_hash = await asyncio.to_thread(hash_password, body.password)
    uid = db().create_user(body.email, body.name, pw_hash)
    row = db().user_by_id(uid)
    _set_cookie(response, create_token(uid, row["token_version"]))
    return public_user(row)


@router.post("/login")
async def login(body: Credentials, request: Request, response: Response):
    if not settings.auth_required:
        raise HTTPException(400, "Authentication is disabled (WARDEN_AUTH_REQUIRED=false)")
    key = _throttle_key(request, body.email)
    _check_throttle(key)
    row = db().user_by_email(body.email)
    ok = bool(row) and await asyncio.to_thread(verify_password, body.password, row["password_hash"])
    if not row:
        await asyncio.to_thread(verify_against_dummy, body.password)
    if not ok:
        _FAILS[key].append(time.time())
        raise HTTPException(401, "Incorrect e-mail or password")
    _FAILS.pop(key, None)
    db().touch_login(row["id"])
    _set_cookie(response, create_token(row["id"], row["token_version"]))
    return public_user(db().user_by_id(row["id"]))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    return {**user, "auth_required": settings.auth_required}


@router.patch("/me")
async def update_me(body: ProfileBody, user: dict = Depends(current_user)):
    if not settings.auth_required:
        raise HTTPException(400, "Authentication is disabled; there is no account to edit")
    db().update_name(user["id"], body.name)
    return public_user(db().user_by_id(user["id"]))


@router.post("/password")
async def change_password(body: PasswordBody, response: Response, user: dict = Depends(current_user)):
    if not settings.auth_required:
        raise HTTPException(400, "Authentication is disabled; there is no account to edit")
    row = db().user_by_id(user["id"])
    if not await asyncio.to_thread(verify_password, body.current_password, row["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    new_hash = await asyncio.to_thread(hash_password, body.new_password)
    version = db().set_password(user["id"], new_hash)   # signs out every other session
    _set_cookie(response, create_token(user["id"], version))
    return {"status": "password_changed"}
