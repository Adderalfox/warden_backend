import hashlib
import hmac

import pytest
from fastapi import HTTPException

from app.core.config import settings
from app.core.security import verify_webhook_signature


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(settings.webhook_signing_secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_passes():
    body = b'{"service_name": "x"}'
    verify_webhook_signature(body, _sign(body))  # should not raise


def test_invalid_signature_rejected():
    body = b'{"service_name": "x"}'
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(body, "sha256=deadbeef")
    assert exc.value.status_code == 401


def test_missing_signature_rejected():
    with pytest.raises(HTTPException) as exc:
        verify_webhook_signature(b"{}", None)
    assert exc.value.status_code == 401
