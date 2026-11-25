"""
Real HMAC-SHA256 webhook signature verification (Alertmanager/Sentry-style
`X-Signature: sha256=<hex>` header convention) and a simple API-key gate for
the human-approval endpoint. No mocking -- this is the actual verification
logic used at runtime.
"""
import hashlib
import hmac

from fastapi import Header, HTTPException, status

from app.core.config import settings


def verify_webhook_signature(payload: bytes, signature_header: str | None) -> None:
    if not signature_header:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Signature header")

    if "=" in signature_header:
        _, _, sig_hex = signature_header.partition("=")
    else:
        sig_hex = signature_header

    expected = hmac.new(
        settings.webhook_signing_secret.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, sig_hex.strip()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")


def require_approval_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.approval_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")
