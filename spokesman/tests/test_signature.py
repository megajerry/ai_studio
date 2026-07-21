"""X-Hub-Signature-256 verification (valid + invalid)."""

from __future__ import annotations

import hashlib
import hmac

from spokesman.whatsapp import verify_signature

from .conftest import APP_SECRET


def _sign(body: bytes, secret: str = APP_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_valid_signature_accepted() -> None:
    body = b'{"hello":"world"}'
    assert verify_signature(body, _sign(body), APP_SECRET) is True


def test_wrong_signature_rejected() -> None:
    body = b'{"hello":"world"}'
    assert verify_signature(body, _sign(b"tampered"), APP_SECRET) is False


def test_wrong_secret_rejected() -> None:
    body = b'{"hello":"world"}'
    bad = _sign(body, secret="other-secret")
    assert verify_signature(body, bad, APP_SECRET) is False


def test_missing_or_malformed_header_rejected() -> None:
    body = b"{}"
    assert verify_signature(body, None, APP_SECRET) is False
    assert verify_signature(body, "", APP_SECRET) is False
    assert verify_signature(body, "deadbeef", APP_SECRET) is False  # no prefix


def test_empty_secret_rejected() -> None:
    body = b"{}"
    assert verify_signature(body, _sign(body), "") is False
