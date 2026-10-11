"""Tests for the dashboard session cookie + login throttle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from unittest.mock import patch

import pytest

from socialhome.dashboard.auth import (
    COOKIE_NAME,
    GLOBAL_MAX_FAILURES,
    PRUNE_INTERVAL_S,
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
    assert claims == SessionClaims(
        epoch=0, issued_at=int(clock.now), keep=keep, locked=False
    )


async def test_payload_shape(auth, clock):
    raw = await auth.issue(True)
    body = raw.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert payload == {"v": 1, "e": 0, "i": int(clock.now), "k": True, "l": False}


@pytest.mark.parametrize("keep", [True, False])
async def test_locked_round_trip(auth, clock, keep):
    claims = await auth.verify(await auth.issue(keep, locked=True))
    assert claims == SessionClaims(
        epoch=0, issued_at=int(clock.now), keep=keep, locked=True
    )


async def test_pre_lock_four_key_cookie_verifies_unlocked(
    auth, settings_service, clock
):
    """Cookies minted before the lock claim existed keep working."""
    raw = await _forge(
        settings_service, {"v": 1, "e": 0, "i": int(clock.now), "k": True}
    )
    claims = await auth.verify(raw)
    assert claims == SessionClaims(
        epoch=0, issued_at=int(clock.now), keep=True, locked=False
    )


async def test_five_key_cookie_with_lock_verifies_locked(auth, settings_service, clock):
    raw = await _forge(
        settings_service,
        {"v": 1, "e": 0, "i": int(clock.now), "k": False, "l": True},
    )
    claims = await auth.verify(raw)
    assert claims is not None and claims.locked is True and claims.keep is False


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
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": 1},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": 0},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": "true"},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": None},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "x": 1},
        {"v": 1, "e": 0, "i": "NOW", "l": True},
        {"v": 1, "e": 0, "i": None, "k": True, "l": True},
        {"v": 1, "e": 0, "i": "NOW", "l": False, "x": True},
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


def _fail(auth, key: str, n: int = 1) -> None:
    """``n`` attempts that each turn out wrong (counted up front)."""
    for _ in range(n):
        assert auth.begin_attempt(key) == 0


def test_throttle_locks_after_five_failures(auth, clock):
    assert THROTTLE_MAX_FAILURES == 5
    assert THROTTLE_WINDOW_S == 15 * 60
    for _ in range(4):
        _fail(auth, "1.2.3.4")
        clock.advance(10)
    assert auth.throttle_remaining("1.2.3.4") == 0
    _fail(auth, "1.2.3.4")
    # First failure was 40 s ago → 860 s until it leaves the window.
    assert auth.throttle_remaining("1.2.3.4") == THROTTLE_WINDOW_S - 40
    assert auth.begin_attempt("1.2.3.4") == THROTTLE_WINDOW_S - 40
    assert auth.throttle_remaining("5.6.7.8") == 0


def test_throttled_attempt_is_not_counted(auth, clock):
    _fail(auth, "k", 5)
    for _ in range(10):
        assert auth.begin_attempt("k") > 0
    clock.advance(THROTTLE_WINDOW_S)
    assert auth.begin_attempt("k") == 0


def test_throttle_window_expiry(auth, clock):
    _fail(auth, "k", 5)
    clock.advance(THROTTLE_WINDOW_S - 0.5)
    assert auth.throttle_remaining("k") == 1
    clock.advance(0.5)
    assert auth.throttle_remaining("k") == 0


def test_throttle_reset_on_success(auth):
    auth.record_success("nothing-recorded-yet")
    _fail(auth, "k", 5)
    auth.record_success("k")
    assert auth.throttle_remaining("k") == 0
    auth.record_success("never-seen")


# ─── global brake ────────────────────────────────────────────────────────


def test_global_brake_trips_after_thirty_failures(auth, clock, caplog):
    assert GLOBAL_MAX_FAILURES == 30
    # 30 failures spread over many addresses (4 each, so none is locked).
    for n in range(30):
        _fail(auth, f"10.0.0.{n // 4}")
        clock.advance(1)
    assert auth.begin_attempt("10.9.9.9") == 0  # the 31st failure trips it
    clock.advance(1)
    with caplog.at_level("WARNING", logger="socialhome.dashboard.auth"):
        # Every client — even a brand-new one — is now held back until
        # the oldest failure leaves the window (it was 31 s ago).
        assert auth.begin_attempt("192.168.1.50") == THROTTLE_WINDOW_S - 31
        assert auth.throttle_remaining("192.168.1.51") == THROTTLE_WINDOW_S - 31
        assert auth.begin_attempt("192.168.1.52") > 0
    assert sum("global" in r.message for r in caplog.records) == 1
    clock.advance(THROTTLE_WINDOW_S - 31)
    assert auth.begin_attempt("192.168.1.50") == 0


def test_global_brake_warns_again_after_draining(auth, clock, caplog):
    with caplog.at_level("WARNING", logger="socialhome.dashboard.auth"):
        for _round in range(2):
            for n in range(31):
                _fail(auth, f"r{_round}-{n}")
            assert auth.begin_attempt("x") > 0
            clock.advance(THROTTLE_WINDOW_S)
    assert sum("global" in r.message for r in caplog.records) == 2


def test_success_does_not_feed_the_global_brake(auth):
    for n in range(100):
        assert auth.begin_attempt(f"tablet-{n}") == 0
        auth.record_success(f"tablet-{n}")
    assert auth.begin_attempt("another") == 0


def test_setup_link_attempts_skip_the_global_window(auth):
    """``/enter`` tokens are 256-bit: per-client limits suffice, and cheap
    token guesses must neither trip nor be blocked by the global brake."""
    for n in range(40):
        assert auth.begin_attempt(f"10.1.{n}.1", count_global=False) == 0
    assert auth.begin_attempt("192.168.1.2") == 0  # login brake not tripped
    for n in range(31):
        auth.begin_attempt(f"10.2.{n}.1")
    assert auth.begin_attempt("192.168.1.3") > 0  # login brake tripped …
    assert auth.begin_attempt("192.168.1.4", count_global=False) == 0  # … not /enter
    # The per-client limit still applies to setup-link attempts.
    for _ in range(4):
        auth.begin_attempt("192.168.1.4", count_global=False)
    assert auth.begin_attempt("192.168.1.4", count_global=False) > 0


def test_setup_link_success_leaves_the_global_window_alone(auth):
    for n in range(31):
        auth.begin_attempt(f"10.2.{n}.1")
    auth.begin_attempt("tablet", count_global=False)
    auth.record_success("tablet", count_global=False)
    assert auth.begin_attempt("192.168.1.3") > 0  # still tripped


# ─── key cap ─────────────────────────────────────────────────────────────


def test_throttle_key_cap_prunes_stale_keys(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=3)
    for key in ("a", "b", "c"):
        _fail(auth, key)
    assert auth.throttle_key_count == 3
    clock.advance(THROTTLE_WINDOW_S + 1)
    _fail(auth, "d")  # "a" was stale → dropped to make room
    assert auth.throttle_key_count == 3
    clock.advance(1)
    _fail(auth, "e")
    _fail(auth, "f")
    assert auth.throttle_key_count == 3


def test_throttle_key_cap_evicts_oldest_unlocked_never_locked(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=3)
    _fail(auth, "locked", 5)
    _fail(auth, "b")
    _fail(auth, "c")
    _fail(auth, "d")  # full → evicts "b" (oldest unlocked), keeps "locked"
    assert auth.throttle_key_count == 3
    assert auth.throttle_remaining("locked") > 0
    _fail(auth, "b")  # "b" starts over (evicts "c")
    assert auth.throttle_remaining("locked") > 0


def test_throttle_full_of_locked_keys_fails_closed(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=2)
    _fail(auth, "a", 5)
    _fail(auth, "b", 5)
    remaining = auth.begin_attempt("newcomer")
    assert remaining > 0
    assert auth.throttle_key_count == 2
    assert auth.throttle_remaining("a") > 0 and auth.throttle_remaining("b") > 0


def test_throttle_full_table_rescans_at_most_once_per_interval(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=2)
    _fail(auth, "a", 5)
    _fail(auth, "b", 5)
    with patch.object(
        DashboardAuth,
        "_make_room",
        autospec=True,
        side_effect=DashboardAuth._make_room,
    ) as make_room:
        for _ in range(50):
            assert auth.begin_attempt("newcomer") > 0
        assert make_room.call_count == 1
        clock.advance(PRUNE_INTERVAL_S)
        assert auth.begin_attempt("newcomer") > 0
        assert make_room.call_count == 2
    # Once the locked keys age out, a newcomer gets in again.
    clock.advance(THROTTLE_WINDOW_S)
    assert auth.begin_attempt("newcomer") == 0


# ─── password verify ─────────────────────────────────────────────────────


class _SlowSettings:
    """Records how many password checks run at once."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def verify_password(self, password: str) -> bool:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return password == "right"


async def test_password_checks_run_one_at_a_time(clock):
    slow = _SlowSettings()
    auth = DashboardAuth(slow, clock=clock)  # type: ignore[arg-type]
    results = await asyncio.gather(
        *(auth.check_password(p) for p in ["a", "right", "b", "c"])
    )
    assert results == [False, True, False, False]
    assert slow.peak == 1


# ─── PIN verify + throttle key ───────────────────────────────────────────


class _SlowPinSettings(_SlowSettings):
    async def verify_pin(self, pin: str) -> bool:
        return await self.verify_password(pin)


async def test_pin_and_password_checks_share_one_slot(clock):
    """scrypt either way — PIN checks queue behind password checks too."""
    slow = _SlowPinSettings()
    auth = DashboardAuth(slow, clock=clock)  # type: ignore[arg-type]
    results = await asyncio.gather(
        auth.check_pin("a"),
        auth.check_password("right"),
        auth.check_pin("right"),
        auth.check_password("b"),
    )
    assert results == [False, True, True, False]
    assert slow.peak == 1


def test_pin_throttle_key_is_its_own_bucket(auth):
    assert DashboardAuth.pin_throttle_key("10.0.0.5") == "pin:10.0.0.5"
    _fail(auth, DashboardAuth.pin_throttle_key("10.0.0.5"), THROTTLE_MAX_FAILURES)
    assert auth.throttle_remaining("10.0.0.5") == 0


def test_now_is_the_auth_clock(auth, clock):
    assert auth.now() == clock.now
    clock.advance(5)
    assert auth.now() == clock.now


def test_global_failure_count(auth):
    assert auth.global_failure_count == 0
    auth.begin_attempt("a")
    auth.begin_attempt("b", count_global=False)
    assert auth.global_failure_count == 1
