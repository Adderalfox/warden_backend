import time

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import auth as auth_api
from app.auth.security import create_token, hash_password, verify_password
from app.core.config import settings


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "auth_allow_signup", True)
    monkeypatch.setattr(settings, "metrics_source", "demo")
    auth_api._FAILS.clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


SIGNUP = {"email": "ada@example.com", "name": "Ada", "password": "correct horse battery"}


def test_password_hash_roundtrip_and_salting():
    h1, h2 = hash_password("hunter2hunter2"), hash_password("hunter2hunter2")
    assert h1 != h2 and verify_password("hunter2hunter2", h1) and not verify_password("wrong", h1)
    assert not verify_password("x", "garbage")


def test_protected_routes_require_login(client):
    for path in ("/api/v1/incidents", "/api/v1/metrics/status", "/api/v1/analytics/overview",
                 "/api/v1/auth/me"):
        assert client.get(path).status_code == 401, path
    assert client.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 401
    # public: health + auth config
    assert client.get("/health").status_code == 200
    cfg = client.get("/api/v1/auth/config").json()
    assert cfg["auth_required"] and cfg["signup_enabled"] and cfg["first_user"]


def test_signup_sets_httponly_cookie_and_logs_in(client):
    r = client.post("/api/v1/auth/signup", json=SIGNUP)
    assert r.status_code == 201 and "password" not in r.text
    set_cookie = r.headers["set-cookie"].lower()
    assert "warden_session=" in set_cookie and "httponly" in set_cookie and "samesite=lax" in set_cookie
    me = client.get("/api/v1/auth/me").json()
    assert me["email"] == "ada@example.com" and me["name"] == "Ada"
    assert client.get("/api/v1/metrics/status").status_code == 200       # cookie carries the session
    assert client.get("/api/v1/auth/config").json()["first_user"] is False


def test_signup_validation_and_duplicates(client):
    assert client.post("/api/v1/auth/signup", json={**SIGNUP, "password": "short"}).status_code == 422
    assert client.post("/api/v1/auth/signup", json={**SIGNUP, "email": "nope"}).status_code == 422
    assert client.post("/api/v1/auth/signup", json=SIGNUP).status_code == 201
    assert client.post("/api/v1/auth/signup", json={**SIGNUP, "email": "ADA@example.com"}).status_code == 409


def test_login_logout_flow(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    client.post("/api/v1/auth/logout")
    assert client.get("/api/v1/auth/me").status_code == 401
    bad = client.post("/api/v1/auth/login", json={"email": SIGNUP["email"], "password": "wrong-password"})
    assert bad.status_code == 401 and bad.json()["detail"] == "Incorrect e-mail or password"
    unknown = client.post("/api/v1/auth/login", json={"email": "who@example.com", "password": "whatever123"})
    assert unknown.json()["detail"] == bad.json()["detail"]              # no account enumeration
    ok = client.post("/api/v1/auth/login", json={"email": SIGNUP["email"], "password": SIGNUP["password"]})
    assert ok.status_code == 200 and client.get("/api/v1/auth/me").status_code == 200


def test_login_throttling(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    client.post("/api/v1/auth/logout")
    body = {"email": SIGNUP["email"], "password": "bad-password!"}
    codes = [client.post("/api/v1/auth/login", json=body).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    # even the right password is refused while throttled
    assert client.post("/api/v1/auth/login", json={**body, "password": SIGNUP["password"]}).status_code == 429


def test_password_change_invalidates_other_sessions(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    old_token = client.cookies.get("warden_session")
    assert client.post("/api/v1/auth/password", json={"current_password": "nope-nope", "new_password": "brand new pass 1"}).status_code == 400
    assert client.post("/api/v1/auth/password", json={"current_password": SIGNUP["password"], "new_password": "brand new pass 1"}).status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 200              # this session was re-issued
    other = TestClient(client.app)
    assert other.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old_token}"}).status_code == 401


def test_profile_update_and_tampered_or_expired_tokens(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    assert client.patch("/api/v1/auth/me", json={"name": "Ada L."}).json()["name"] == "Ada L."
    other = TestClient(client.app)
    assert other.get("/api/v1/auth/me", headers={"Authorization": "Bearer abc.def.ghi"}).status_code == 401
    tok = client.cookies.get("warden_session")
    assert other.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}x"}).status_code == 401
    assert other.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_expired_token_rejected(client, monkeypatch):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    monkeypatch.setattr(settings, "auth_token_ttl_minutes", -5)
    expired = create_token(1, 0)
    other = TestClient(client.app)
    assert other.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_signup_can_be_disabled_after_first_user(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_allow_signup", False)
    assert client.post("/api/v1/auth/signup", json=SIGNUP).status_code == 201     # first account always allowed
    other = TestClient(client.app)
    assert other.post("/api/v1/auth/signup", json={**SIGNUP, "email": "b@example.com"}).status_code == 403
    assert other.get("/api/v1/auth/config").json()["signup_enabled"] is False


def test_auth_disabled_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "metrics_source", "demo")
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/auth/me").json()["auth_required"] is False
        assert c.get("/api/v1/metrics/status").status_code == 200
        assert c.post("/api/v1/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 400
