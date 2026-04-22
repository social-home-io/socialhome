"""Tests for socialhome.security."""

from __future__ import annotations

from socialhome.security import SENSITIVE_FIELDS, error_response, sanitise_for_api


def test_strips_sensitive():
    """Known sensitive fields like email and password_hash are removed."""
    data = {"username": "alice", "email": "a@b.com", "password_hash": "x"}
    clean = sanitise_for_api(data)
    assert "email" not in clean
    assert "password_hash" not in clean
    assert clean["username"] == "alice"


def test_recursive_nested():
    """Sensitive fields inside nested dicts are stripped recursively."""
    data = {"nested": {"phone": "+41", "name": "x"}}
    assert sanitise_for_api(data) == {"nested": {"name": "x"}}


def test_list_of_dicts():
    """Sensitive fields are stripped from dicts inside lists."""
    data = {"items": [{"p256dh": "zzz", "ok": 1}, "literal"]}
    clean = sanitise_for_api(data)
    assert clean == {"items": [{"ok": 1}, "literal"]}


def test_empty_dict():
    """An empty dict passes through unchanged."""
    assert sanitise_for_api({}) == {}


def test_all_sensitive_stripped():
    """A dict containing only sensitive fields becomes empty after sanitisation."""
    data = {k: "val" for k in SENSITIVE_FIELDS}
    assert sanitise_for_api(data) == {}


def test_error_response_status_is_preserved():
    """error_response returns an aiohttp Response with the requested HTTP status."""
    resp = error_response(404, "NOT_FOUND", "gone")
    assert resp.status == 404
