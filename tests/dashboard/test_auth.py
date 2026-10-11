"""Tests for the dashboard session cookie + login throttle."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from socialhome.dashboard.auth import (
    COOKIE_NAME,
    KEEP_TTL_S,
    RENEW_AFTER_S,
    SESSION_TTL_S,
    THROTTLE_MAX_FAILURES,
    THROTTLE_WINDOW_S,
    DashboardAuth,
    SessionClaims,
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


async def _forge(
    settings_service, payload: object, *, secret: bytes | None = None
) -> str:
    """A cookie with a *valid* signature over an arbitrary payload."""
    key = secret if secret is not None else await settings_service.cookie_secret()
    body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return f"{_b64(body)}.{_b64(sig)}"


@pytest.fixture
def auth(settings_service, clock) -> DashboardAuth:
    return DashboardAuth(settings_service, clock=clock)


def test_cookie_name():
    assert COOKIE_NAME == "sh_dash"


# ─── issue / verify ──────────────────────────────────────────────────────


@pytest.mark.parametrize("keep", [True, False])
async def test_round_trip(auth, clock, keep):
    raw = await auth.issue(keep)
    claims = await auth.verify(raw)
    assert claims == SessionClaims(epoch=0, issued_at=int(clock.now), keep=keep)


async def test_payload_shape(auth, clock):
    raw = await auth.issue(True)
    body = raw.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload == {"v": 1, "e": 0, "i": int(clock.now), "k": True}


async def test_tampered_payload_rejected(auth, clock):
    raw = await auth.issue(False)
    body, sig = raw.split(".")
    forged = json.dumps({"v": 1, "e": 0, "i": int(clock.now), "k": True}).encode()
    assert await auth.verify(f"{_b64(forged)}.{sig}") is None


async def test_tampered_signature_rejected(auth):
    raw = await auth.issue(True)
    body, sig = raw.split(".")
    bad = _b64(bytes(32))
    assert await auth.verify(f"{body}.{bad}") is None


async def test_wrong_secret_rejected(auth, settings_service, clock):
    raw = await _forge(
        settings_service,
        {"v": 1, "e": 0, "i": int(clock.now), "k": True},
        secret=b"x" * 32,
    )
    assert await auth.verify(raw) is None


async def test_forged_with_right_secret_accepted(auth, settings_service, clock):
    """Sanity for the forge helper the strict-parse tests rely on."""
    raw = await _forge(
        settings_service, {"v": 1, "e": 0, "i": int(clock.now), "k": False}
    )
    assert await auth.verify(raw) is not None


async def test_epoch_bump_invalidates(auth, settings_service):
    raw = await auth.issue(True)
    await settings_service.revoke_sessions(actor_is_admin=True)
    assert await auth.verify(raw) is None
    fresh = await auth.issue(True)
    claims = await auth.verify(fresh)
    assert claims is not None and claims.epoch == 1


async def test_non_keep_expires_after_12h(auth, clock):
    raw = await auth.issue(False)
    clock.advance(SESSION_TTL_S)
    assert await auth.verify(raw) is not None
    clock.advance(1)
    assert await auth.verify(raw) is None


async def test_keep_expires_after_365d(auth, clock):
    assert KEEP_TTL_S == 365 * 86400
    raw = await auth.issue(True)
    clock.advance(SESSION_TTL_S + 1)
    assert await auth.verify(raw) is not None
    clock.advance(KEEP_TTL_S)
    assert await auth.verify(raw) is None


async def test_future_issued_at_beyond_skew_rejected(auth, settings_service, clock):
    ok = await _forge(
        settings_service, {"v": 1, "e": 0, "i": int(clock.now) + 300, "k": True}
    )
    assert await auth.verify(ok) is not None
    bad = await _forge(
        settings_service, {"v": 1, "e": 0, "i": int(clock.now) + 301, "k": True}
    )
    assert await auth.verify(bad) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "nodot",
        "a.b.c",
        ".",
        "abc.",
        ".abc",
        "!!!.???",
        "a b.c d",
        "abcde.abcd",  # base64 length ≡ 1 (mod 4) can't decode
        "x" * 5000,
    ],
)
async def test_garbage_strings_rejected(auth, raw):
    assert await auth.verify(raw) is None


@pytest.mark.parametrize(
    "payload",
    [
        [1, 0, 0, True],
        "string",
        None,
        {"v": 1, "e": 0, "i": "STR", "k": True},
        {"v": 2, "e": 0, "i": "NOW", "k": True},
        {"v": True, "e": 0, "i": "NOW", "k": True},
        {"v": 1, "e": False, "i": "NOW", "k": True},
        {"v": 1, "e": "0", "i": "NOW", "k": True},
        {"v": 1, "e": 0, "i": 1.5, "k": True},
        {"v": 1, "e": 0, "i": True, "k": True},
        {"v": 1, "e": 0, "i": "NOW", "k": 1},
        {"v": 1, "e": 0, "i": "NOW", "k": "true"},
        {"v": 1, "e": 0, "i": "NOW"},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "x": 1},
    ],
)
async def test_wrong_shapes_rejected(auth, settings_service, clock, payload):
    if isinstance(payload, dict):
        stamp = {"NOW": int(clock.now), "STR": str(int(clock.now))}
        payload = {**payload, "i": stamp.get(payload["i"], payload["i"])}
    raw = await _forge(settings_service, payload)
    assert await auth.verify(raw) is None


async def test_non_json_payload_rejected(auth, settings_service):
    key = await settings_service.cookie_secret()
    body = b"\xff\xfenot json"
    sig = hmac.new(key, body, hashlib.sha256).digest()
    assert await auth.verify(f"{_b64(body)}.{_b64(sig)}") is None


async def test_padded_base64_rejected(auth):
    raw = await auth.issue(True)
    body, sig = raw.split(".")
    assert await auth.verify(f"{body}==.{sig}") is None


# ─── renewal ─────────────────────────────────────────────────────────────


async def test_needs_renewal_threshold(auth, clock):
    raw = await auth.issue(True)
    claims = await auth.verify(raw)
    assert claims is not None
    clock.advance(RENEW_AFTER_S)
    assert auth.needs_renewal(claims) is False
    clock.advance(1)
    assert auth.needs_renewal(claims) is True


# ─── last seen ───────────────────────────────────────────────────────────


def test_touch_sets_last_seen(auth, clock):
    assert auth.last_seen_at is None
    auth.touch()
    assert auth.last_seen_at == clock.now


# ─── throttle ────────────────────────────────────────────────────────────


def test_throttle_locks_after_five_failures(auth, clock):
    assert THROTTLE_MAX_FAILURES == 5
    assert THROTTLE_WINDOW_S == 15 * 60
    for _ in range(4):
        auth.record_failure("1.2.3.4")
        clock.advance(10)
    assert auth.throttle_remaining("1.2.3.4") == 0
    auth.record_failure("1.2.3.4")
    # First failure was 40 s ago → 860 s until it leaves the window.
    assert auth.throttle_remaining("1.2.3.4") == THROTTLE_WINDOW_S - 40
    assert auth.throttle_remaining("5.6.7.8") == 0


def test_throttle_window_expiry(auth, clock):
    for _ in range(5):
        auth.record_failure("k")
    clock.advance(THROTTLE_WINDOW_S - 0.5)
    assert auth.throttle_remaining("k") == 1
    clock.advance(0.5)
    assert auth.throttle_remaining("k") == 0


def test_throttle_reset_on_success(auth):
    for _ in range(5):
        auth.record_failure("k")
    auth.record_success("k")
    assert auth.throttle_remaining("k") == 0
    auth.record_success("never-seen")


def test_throttle_key_cap(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=3)
    for key in ("a", "b", "c"):
        auth.record_failure(key)
    assert auth.throttle_key_count == 3
    clock.advance(THROTTLE_WINDOW_S + 1)
    # All stale: adding a fourth prunes them first.
    auth.record_failure("d")
    assert auth.throttle_key_count == 1
    for key in ("e", "f"):
        auth.record_failure(key)
    # Full of live keys: the oldest live one is evicted.
    auth.record_failure("g")
    assert auth.throttle_key_count == 3
    for _ in range(4):
        auth.record_failure("g")
    assert auth.throttle_remaining("g") > 0
