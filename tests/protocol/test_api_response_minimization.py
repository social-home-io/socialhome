"""§27.9: API responses must NEVER expose SENSITIVE_FIELDS.

``sanitise_for_api`` (in security.py) is the single source of truth
— these tests pin the contract at the primitive level. End-to-end
HTTP coverage of individual surfaces lives next to each route in
``tests/routes/`` (e.g. test_users.py asserts /api/me shape, test_push.py
asserts the push subscription endpoint never echoes the secret blob).
"""

from __future__ import annotations

import pytest

from social_home.security import SENSITIVE_FIELDS, sanitise_for_api


pytestmark = pytest.mark.security


# ─── sanitise_for_api invariants ────────────────────────────────────────


def test_sanitise_strips_top_level_sensitive_keys():
    raw = {
        "username": "alice",
        "user_id": "alice-id",
        "email": "alice@example.com",
        "phone": "+15551234",
        "date_of_birth": "2010-01-01",
    }
    out = sanitise_for_api(raw)
    assert "email" not in out
    assert "phone" not in out
    assert "date_of_birth" not in out
    # Allowed fields stay.
    assert out["username"] == "alice"


def test_sanitise_recurses_into_nested_dicts():
    raw = {
        "user": {
            "username": "alice",
            "email": "leaked@example.com",
        },
        "list": [{"phone": "+1"}],
    }
    out = sanitise_for_api(raw)
    assert "email" not in out["user"]
    assert "phone" not in out["list"][0]


def test_sanitise_strips_federation_envelope_material():
    raw = {
        "msg_id": "x",
        "encrypted_payload": "secret",
        "signature": "sig",
        "session_key": "very-secret",
    }
    out = sanitise_for_api(raw)
    assert "encrypted_payload" not in out
    assert "signature" not in out
    assert "session_key" not in out
    assert out["msg_id"] == "x"


def test_sanitise_strips_push_subscription_secrets():
    raw = {
        "id": "sub-1",
        "endpoint": "https://push.example/abc",
        "p256dh": "secret-key",
        "auth_secret": "also-secret",
    }
    out = sanitise_for_api(raw)
    assert "endpoint" not in out
    assert "p256dh" not in out
    assert "auth_secret" not in out
    assert out["id"] == "sub-1"


def test_sensitive_fields_includes_critical_keys():
    """Smoke test: the allowlist must include the non-negotiables."""
    for key in (
        "email",
        "phone",
        "date_of_birth",
        "identity_private_key",
        "routing_secret",
        "auth_secret",
        "p256dh",
        "endpoint",
        "encrypted_payload",
        "signature",
        "private_key",
        "session_key",
    ):
        assert key in SENSITIVE_FIELDS, f"§27.9: {key!r} must be in SENSITIVE_FIELDS"
