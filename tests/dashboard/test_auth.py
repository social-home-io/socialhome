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
    PAIR_CODE_MAX_FAILURES,
    PAIR_CODE_WINDOW_S,
    DISPLAY_IMAGE_MAX,
    DISPLAY_READ_MAX,
    DISPLAY_WINDOW_S,
    MAP_RENDER_MAX,
    PAIR_MAX,
    PAIR_WINDOW_S,
    ACK_MAX,
    ACK_WINDOW_S,
    TILE_MAX,
    TILE_WINDOW_S,
    COOKIE_NAME,
    GLOBAL_MAX_FAILURES,
    PRUNE_INTERVAL_S,
    KEEP_TTL_S,
    RENEW_AFTER_S,
    REVOKED_LOCKED,
    REVOKED_SIGNED_OUT,
    SESSION_TTL_S,
    THROTTLE_MAX_FAILURES,
    THROTTLE_WINDOW_S,
    DashboardAuth,
    SessionClaims,
    StaleSessionError,
    _FailureTable,
)
from socialhome.domain.household_dashboard import DashboardDisplay
from socialhome.services.dashboard_settings_service import DashboardSettingsService


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
    nonce = payload.pop("n")
    assert payload == {"v": 1, "e": 0, "i": int(clock.now), "k": True, "l": False}
    assert isinstance(nonce, str) and len(nonce) == 22


async def test_cookies_of_the_same_second_differ(auth):
    """A per-cookie nonce: revoking or throttling one tablet's cookie
    never touches a twin minted in the same second."""
    assert await auth.issue(True) != await auth.issue(True)


async def test_six_key_cookie_verifies(auth, settings_service, clock):
    raw = await _forge(
        settings_service,
        {"v": 1, "e": 0, "i": int(clock.now), "k": True, "l": True, "n": "A" * 22},
    )
    claims = await auth.verify(raw)
    assert claims is not None and claims.locked is True


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
        {"v": 1, "e": 0, "i": "NOW", "k": True, "n": "A" * 22},  # n needs l
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": 5},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "short"},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "A" * 33},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "a b" * 8},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "A" * 21 + "="},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "A" * 22 + "\n"},
        {"v": 1, "e": 0, "i": "NOW", "k": True, "l": False, "n": "A" * 22, "x": 1},
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
        _FailureTable,
        "_make_room",
        autospec=True,
        side_effect=_FailureTable._make_room,
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


async def test_pin_throttle_key_is_the_session_id_alone(auth, clock):
    """Per lock, never per address: rotating client addresses (an IPv6
    /64 each) can't buy a fresh bucket, and tablets behind one proxy
    still don't share one."""
    a = await auth.verify(await auth.issue(True, locked=True))
    b = await auth.verify(await auth.issue(True, locked=True))  # same second
    key_a = DashboardAuth.pin_throttle_key(a)
    assert key_a == f"pin:{a.session_id}"
    assert key_a != DashboardAuth.pin_throttle_key(b)
    _fail(auth, key_a, THROTTLE_MAX_FAILURES)
    assert auth.failure_count(DashboardAuth.pin_throttle_key(a)) == 5


def test_failure_count(auth, clock):
    assert auth.failure_count("k") == 0
    _fail(auth, "k", 3)
    assert auth.failure_count("k") == 3
    clock.advance(THROTTLE_WINDOW_S)
    assert auth.failure_count("k") == 0


def test_attempt_wait_counts_nothing(auth):
    _fail(auth, "k", THROTTLE_MAX_FAILURES)
    wait = auth.attempt_wait("k", count_global=False)
    assert wait > 0
    assert auth.failure_count("k") == THROTTLE_MAX_FAILURES
    assert auth.attempt_wait("other", count_global=False) == 0
    assert auth.failure_count("other") == 0


def test_attempt_wait_sees_a_full_table(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=1)
    _fail(auth, "a", THROTTLE_MAX_FAILURES)
    assert auth.attempt_wait("newcomer", count_global=False) == 0  # not tried yet
    assert auth.begin_attempt("newcomer", count_global=False) > 0
    assert auth.attempt_wait("newcomer", count_global=False) > 0
    assert auth.failure_count("newcomer") == 0
    clock.advance(PRUNE_INTERVAL_S)
    assert auth.attempt_wait("newcomer", count_global=False) == 0


def test_attempt_wait_global_brake(auth):
    for i in range(GLOBAL_MAX_FAILURES + 1):
        auth.begin_attempt(f"c{i}")
    assert auth.attempt_wait("fresh") > 0
    assert auth.attempt_wait("fresh", count_global=False) == 0


# ─── revocation ──────────────────────────────────────────────────────────


def _payload(raw: str) -> dict:
    body = raw.split(".")[0]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


async def test_claims_carry_the_session_id_outside_equality(auth, clock):
    raw = await auth.issue(True)
    claims = await auth.verify(raw)
    assert claims.session_id == _payload(raw)["n"]
    assert claims == SessionClaims(epoch=0, issued_at=int(clock.now), keep=True)
    assert "session_id" not in repr(claims)


async def test_issue_carries_a_given_session_id(auth):
    first = await auth.verify(await auth.issue(True))
    again = await auth.issue(False, locked=True, session_id=first.session_id)
    assert _payload(again)["n"] == first.session_id
    assert (await auth.verify(again)).session_id == first.session_id


@pytest.mark.parametrize("locked", [None, True])
async def test_legacy_cookie_session_id_is_derived_from_its_signature(
    auth, settings_service, clock, locked
):
    payload = {"v": 1, "e": 0, "i": int(clock.now), "k": True}
    if locked is not None:
        payload["l"] = locked
    raw = await _forge(settings_service, payload)
    claims = await auth.verify(raw)
    sig = base64.urlsafe_b64decode(raw.split(".")[1] + "==")
    assert claims.session_id == hashlib.sha256(sig).hexdigest()[:22]
    # A renewal carries it, and the result verifies with the same id.
    renewed = await auth.issue(True, session_id=claims.session_id)
    assert (await auth.verify(renewed)).session_id == claims.session_id


async def test_revocation_covers_every_cookie_of_the_session(auth):
    first = await auth.issue(True, locked=True)
    claims = await auth.verify(first)
    sibling = await auth.issue(True, locked=True, session_id=claims.session_id)
    await auth.revoke(claims, REVOKED_SIGNED_OUT)
    assert await auth.verify(first) is None
    assert await auth.verify(sibling) is None


async def test_revocation_entry_outlives_every_older_sibling(auth, clock):
    """A sibling issued before the revocation can't outlive the entry."""
    raw = await auth.issue(False)
    claims = await auth.verify(raw)
    clock.advance(SESSION_TTL_S - 10)
    await auth.revoke(claims, REVOKED_SIGNED_OUT)
    sibling = await auth.issue(False, session_id=claims.session_id)
    clock.advance(SESSION_TTL_S - 1)
    assert await auth.verify(sibling) is None


async def test_revoked_locked_cookie_verifies_locked(auth):
    raw = await auth.issue(True)
    claims = await auth.verify(raw)
    assert auth.revocation(claims) is None
    await auth.revoke(claims, REVOKED_LOCKED)
    assert auth.revocation(claims) == REVOKED_LOCKED
    again = await auth.verify(raw)
    assert again is not None and again.locked is True and again.keep is True
    # Other cookies are untouched.
    other = await auth.verify(await auth.issue(True))
    assert other.locked is False


async def test_revoked_signed_out_cookie_no_longer_verifies(auth):
    raw = await auth.issue(True, locked=True)
    await auth.revoke(await auth.verify(raw), REVOKED_SIGNED_OUT)
    assert await auth.verify(raw) is None


async def test_signed_out_is_never_downgraded_to_locked(auth):
    raw = await auth.issue(True)
    claims = await auth.verify(raw)
    await auth.revoke(claims, REVOKED_SIGNED_OUT)
    await auth.revoke(claims, REVOKED_LOCKED)
    assert await auth.verify(raw) is None
    assert auth.revoked_count == 1


async def test_locked_can_be_upgraded_to_signed_out(auth):
    raw = await auth.issue(True)
    claims = await auth.verify(raw)
    await auth.revoke(claims, REVOKED_LOCKED)
    await auth.revoke(claims, REVOKED_SIGNED_OUT)
    assert await auth.verify(raw) is None


async def test_revoke_rejects_an_unknown_reason(auth):
    claims = SessionClaims(epoch=0, issued_at=0, keep=True, session_id="x")
    with pytest.raises(ValueError):
        await auth.revoke(claims, "whatever")


@pytest.mark.parametrize(("keep", "ttl"), [(True, KEEP_TTL_S), (False, SESSION_TTL_S)])
async def test_revocation_expires_with_the_cookie(auth, clock, keep, ttl):
    claims = await auth.verify(await auth.issue(keep))
    await auth.revoke(claims, REVOKED_LOCKED)
    clock.advance(ttl)
    assert auth.revocation(claims) == REVOKED_LOCKED
    clock.advance(1)
    assert auth.revocation(claims) is None
    assert auth.revoked_count == 0


def _sess(clock, sid: str, *, keep: bool = True, epoch: int = 0) -> SessionClaims:
    return SessionClaims(
        epoch=epoch, issued_at=int(clock.now), keep=keep, session_id=sid
    )


async def test_full_revocation_set_drops_expired_entries_first(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=2)
    old = _sess(clock, "o", keep=False)
    await auth.revoke(old, REVOKED_LOCKED)
    keep = _sess(clock, "k")
    await auth.revoke(keep, REVOKED_LOCKED)
    secret = await settings_service.cookie_secret()
    clock.advance(SESSION_TTL_S + 1)
    new = _sess(clock, "n")
    await auth.revoke(new, REVOKED_LOCKED)
    assert auth.revocation(keep) == REVOKED_LOCKED
    assert auth.revocation(new) == REVOKED_LOCKED
    assert auth.revoked_count == 2
    # Pruning was enough: nobody was signed out.
    assert await settings_service.cookie_secret() == secret


async def test_full_revocation_set_drops_entries_of_an_older_epoch(
    settings_service, clock
):
    """After "Sign out all tablets" an old epoch's entries are dead
    weight — no cookie of that epoch verifies — so they make room."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=2)
    await auth.revoke(_sess(clock, "a"), REVOKED_SIGNED_OUT)
    await auth.revoke(_sess(clock, "b"), REVOKED_SIGNED_OUT)
    await settings_service.revoke_sessions(actor_is_admin=True)
    secret = await settings_service.cookie_secret()
    new = _sess(clock, "n", epoch=1)
    await auth.revoke(new, REVOKED_SIGNED_OUT)
    assert auth.revoked_count == 1
    assert auth.revocation(new) == REVOKED_SIGNED_OUT
    assert await settings_service.cookie_secret() == secret


@pytest.mark.parametrize("old_reason", [REVOKED_SIGNED_OUT, REVOKED_LOCKED])
async def test_full_revocation_set_never_resurrects_a_revoked_cookie(
    settings_service, clock, old_reason
):
    """Regression: a full list used to drop its oldest entry, so a
    signed-out cookie verified again (and a locked one's pre-lock copy
    came back unlocked). Now every revoked cookie stays dead."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=2)
    raws = [await auth.issue(True, locked=True) for _ in range(3)]
    for raw in raws[:2]:
        await auth.revoke(await auth.verify(raw), old_reason)
    await auth.revoke(await auth.verify(raws[2]), REVOKED_SIGNED_OUT)
    for raw in raws:
        assert await auth.verify(raw) is None


async def test_full_revocation_set_signs_every_tablet_out(settings_service, clock):
    """Still full of live entries: the cookie key is replaced — every
    tablet cookie stops verifying, the list is moot and cleared, and a
    fresh sign-in works."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=2)
    bystander = await auth.issue(True)
    for sid in ("a", "b"):
        await auth.revoke(_sess(clock, sid), REVOKED_SIGNED_OUT)
    old_secret = await settings_service.cookie_secret()
    await auth.revoke(_sess(clock, "c"), REVOKED_SIGNED_OUT)
    assert await settings_service.cookie_secret() != old_secret
    assert auth.revoked_count == 0
    assert await auth.verify(bystander) is None
    fresh = await auth.verify(await auth.issue(True))
    assert fresh is not None and fresh.locked is False


async def test_full_revocation_set_keeps_wall_panels_and_epoch(settings_service, clock):
    """The fallback signs out tablets only: no epoch bump, so paired
    wall panels (whose tokens die with the epoch) stay paired."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=1)
    panel = DashboardDisplay(
        id="d1",
        name="Hall",
        token_hash="0" * 64,
        created_at="2026-10-13T08:00:00+00:00",
    )
    await settings_service.add_display(panel)
    await auth.revoke(_sess(clock, "a"), REVOKED_LOCKED)
    await auth.revoke(_sess(clock, "b"), REVOKED_LOCKED)
    assert auth.revoked_count == 0  # the key was replaced
    assert await settings_service.epoch() == 0
    assert [d.id for d in await settings_service.displays()] == [panel.id]


async def test_key_replacement_survives_a_restart(settings_service, clock):
    """The fallback is durable: a fresh ``DashboardAuth`` (a restart)
    over the same stored settings still refuses the old cookies."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=1)
    raw = await auth.issue(True, locked=True)
    await auth.revoke(await auth.verify(raw), REVOKED_SIGNED_OUT)
    await auth.revoke(_sess(clock, "b"), REVOKED_SIGNED_OUT)
    reloaded = DashboardSettingsService(
        settings_service._repo, fixed_port=None, main_port=8080
    )
    reloaded.attach_key_manager(settings_service._key_manager)
    restarted = DashboardAuth(reloaded, clock=clock)
    assert await restarted.verify(raw) is None


async def test_claims_from_before_a_key_replacement_count_as_signed_out(
    settings_service, clock
):
    """A request in flight across the replacement can't renew or revoke
    its way back in: its claims count as signed out."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=1)
    claims = await auth.verify(await auth.issue(True))
    assert auth.revocation(claims) is None
    await auth.revoke(_sess(clock, "a"), REVOKED_LOCKED)
    await auth.revoke(_sess(clock, "b"), REVOKED_LOCKED)
    assert auth.revocation(claims) == REVOKED_SIGNED_OUT
    await auth.revoke(claims, REVOKED_LOCKED)  # its key is gone: no entry
    assert auth.revoked_count == 0


async def test_no_revoked_cookie_slips_through_the_key_replacement_window(
    settings_service, clock
):
    """Regression (review): a verify that starts while the new key is
    being stored still holds the old key; it must not resume after the
    list is cleared and count as unrevoked — nor be renewed under the new
    key."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=2)
    gone = await auth.issue(True, locked=True)
    await auth.revoke(await auth.verify(gone), REVOKED_SIGNED_OUT)
    prelock = await auth.issue(True)
    prelock_claims = await auth.verify(prelock)
    await auth.revoke(prelock_claims, REVOKED_LOCKED)
    repo = settings_service._repo
    gate = asyncio.Event()
    real_set = repo.set

    async def slow_set(key, value):
        await gate.wait()
        await real_set(key, value)

    repo.set = slow_set
    rotation = asyncio.create_task(auth.revoke(_sess(clock, "c"), REVOKED_LOCKED))
    await asyncio.sleep(0)
    in_window = [asyncio.create_task(auth.verify(raw)) for raw in (gone, prelock)]
    await asyncio.sleep(0)
    gate.set()
    await rotation
    gone_claims, prelock_again = await asyncio.gather(*in_window)
    assert auth.revoked_count == 0  # the key was replaced
    assert gone_claims is None
    if prelock_again is not None:  # verified under the old key, locked
        assert prelock_again.locked is True
        assert auth.revocation(prelock_again) == REVOKED_SIGNED_OUT
        with pytest.raises(StaleSessionError):
            await auth.issue(
                True, session_id=prelock_again.session_id, continuing=prelock_again
            )
    assert await auth.verify(gone) is None
    assert await auth.verify(prelock) is None


async def test_issue_refuses_to_continue_a_session_of_a_replaced_key(
    settings_service, clock
):
    auth = DashboardAuth(settings_service, clock=clock)
    claims = await auth.verify(await auth.issue(True))
    assert await auth.issue(True, continuing=claims)  # same key: fine
    await settings_service.rotate_cookie_secret()
    with pytest.raises(StaleSessionError):
        await auth.issue(True, session_id=claims.session_id, continuing=claims)
    assert await auth.issue(True)  # a fresh sign-in is fine


async def test_revoke_reports_whether_it_recorded(auth):
    claims = await auth.verify(await auth.issue(True))
    assert await auth.revoke(claims, REVOKED_LOCKED) is True
    assert await auth.revoke(claims, REVOKED_LOCKED) is False
    assert await auth.revoke(claims, REVOKED_SIGNED_OUT) is True
    assert await auth.revoke(claims, REVOKED_LOCKED) is False


async def test_concurrent_revokes_of_one_session_record_it_once(
    settings_service, clock
):
    """Even when the first must make room (an await), a concurrent call
    for the same session doesn't record it a second time."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=1)
    await auth.revoke(_sess(clock, "old", keep=False), REVOKED_LOCKED)
    clock.advance(SESSION_TTL_S + 1)
    claims = _sess(clock, "x")
    results = await asyncio.gather(
        auth.revoke(claims, REVOKED_LOCKED), auth.revoke(claims, REVOKED_LOCKED)
    )
    assert sorted(results) == [False, True]
    assert auth.revoked_count == 1


async def test_failed_key_replacement_keeps_every_revocation(settings_service, clock):
    """If the new key can't be stored, nothing is forgotten."""
    auth = DashboardAuth(settings_service, clock=clock, max_revocations=1)
    raw = await auth.issue(True, locked=True)
    await auth.revoke(await auth.verify(raw), REVOKED_SIGNED_OUT)
    with (
        patch.object(
            DashboardSettingsService,
            "rotate_cookie_secret",
            side_effect=OSError("disk"),
        ),
        pytest.raises(OSError),
    ):
        await auth.revoke(_sess(clock, "b"), REVOKED_SIGNED_OUT)
    assert auth.revoked_count == 1
    assert await auth.verify(raw) is None


async def test_restart_forgets_in_memory_revocations(settings_service, clock):
    """Documented: revocations are memory-only. After a restart a
    signed-out lock is a plain *locked* cookie again — still behind the
    PIN — and only "Sign out all tablets" (the epoch) is durable."""
    auth = DashboardAuth(settings_service, clock=clock)
    raw = await auth.issue(True, locked=True)
    await auth.revoke(await auth.verify(raw), REVOKED_SIGNED_OUT)
    restarted = DashboardAuth(settings_service, clock=clock)
    again = await restarted.verify(raw)
    assert again is not None and again.locked is True
    await settings_service.revoke_sessions(actor_is_admin=True)
    assert await restarted.verify(raw) is None


def test_now_is_the_auth_clock(auth, clock):
    assert auth.now() == clock.now
    clock.advance(5)
    assert auth.now() == clock.now


def test_global_failure_count(auth):
    assert auth.global_failure_count == 0
    auth.begin_attempt("a")
    auth.begin_attempt("b", count_global=False)
    assert auth.global_failure_count == 1


# ─── "Got it" (acknowledgement) rate limit ───────────────────────────────


async def test_ack_limit_is_per_session_and_frees_up_after_the_window(auth, clock):
    a = await auth.verify(await auth.issue(True))
    b = await auth.verify(await auth.issue(True))
    assert a is not None and b is not None
    for _ in range(ACK_MAX):
        assert auth.begin_ack(a) == 0
    wait = auth.begin_ack(a)
    assert 0 < wait <= ACK_WINDOW_S
    # Another tablet (session) has its own budget.
    assert auth.begin_ack(b) == 0
    # The password / PIN throttle is untouched.
    assert auth.throttle_key_count == 0
    clock.advance(ACK_WINDOW_S + 1)
    assert auth.begin_ack(a) == 0


async def test_tile_limit_is_per_session_and_apart_from_acks(auth, clock):
    a = await auth.verify(await auth.issue(True))
    b = await auth.verify(await auth.issue(True))
    assert a is not None and b is not None
    for _ in range(TILE_MAX):
        assert auth.begin_tile(a) == 0
    wait = auth.begin_tile(a)
    assert 0 < wait <= TILE_WINDOW_S
    assert auth.begin_tile(b) == 0
    assert auth.begin_ack(a) == 0  # its own budget
    clock.advance(TILE_WINDOW_S + 1)
    assert auth.begin_tile(a) == 0


def test_pair_start_limit_is_per_client(auth, clock):
    """Anonymous pairing starts (a wall panel asking for a code) have a
    small budget per client address, apart from the login throttle."""
    for _ in range(PAIR_MAX):
        assert auth.begin_pair("10.0.0.5") == 0
    wait = auth.begin_pair("10.0.0.5")
    assert 0 < wait <= PAIR_WINDOW_S
    assert auth.begin_pair("10.0.0.6") == 0
    assert auth.throttle_key_count == 0
    assert auth.global_failure_count == 0
    clock.advance(PAIR_WINDOW_S + 1)
    assert auth.begin_pair("10.0.0.5") == 0


def test_pair_limiter_forgets_quiet_addresses(auth, clock):
    for i in range(50):
        auth.begin_pair(f"10.0.0.{i}")
    assert auth.pair_key_count == 50
    clock.advance(PAIR_WINDOW_S + 1)
    auth.begin_pair("10.0.1.1")
    assert auth.pair_key_count == 1


# ─── wrong pairing codes ─────────────────────────────────────────────────


def _wrong_codes(auth, n: int) -> None:
    for _ in range(n):
        assert auth.begin_pair_code() == 0


def test_pair_code_bucket_locks_after_five_and_ages_out(auth, clock):
    _wrong_codes(auth, PAIR_CODE_MAX_FAILURES)
    wait = auth.begin_pair_code()
    assert 0 < wait <= PAIR_CODE_WINDOW_S
    # A refused check counts nothing.
    assert auth.pair_code_failure_count == PAIR_CODE_MAX_FAILURES
    clock.advance(PAIR_CODE_WINDOW_S)
    assert auth.begin_pair_code() == 0


def test_pair_code_success_clears_the_bucket(auth):
    _wrong_codes(auth, PAIR_CODE_MAX_FAILURES - 1)
    auth.record_pair_code_success()
    assert auth.pair_code_failure_count == 0
    _wrong_codes(auth, PAIR_CODE_MAX_FAILURES)


def test_pair_code_bucket_is_bounded(auth, clock):
    """One fixed-size bucket: however many checks run, it never grows."""
    for _ in range(1000):
        auth.begin_pair_code()
        clock.advance(PAIR_CODE_WINDOW_S / 4)
    assert auth.pair_code_failure_count <= PAIR_CODE_MAX_FAILURES


def test_login_failures_never_hold_pairing_back(settings_service, clock):
    """An attacker filling the login table with locked-out clients — and
    tripping the global brake — must not block an admin's code entry."""
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=3)
    for n in range(3):
        _fail(auth, f"10.0.0.{n}", THROTTLE_MAX_FAILURES)
    for n in range(GLOBAL_MAX_FAILURES):
        auth.begin_attempt(f"10.1.0.{n}")
    assert auth.begin_attempt("newcomer") > 0  # login table full + brake on
    assert auth.begin_pair_code() == 0
    assert auth.pair_code_failure_count == 1


def test_wrong_pairing_codes_never_hold_logins_back(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=1)
    _wrong_codes(auth, PAIR_CODE_MAX_FAILURES)
    assert auth.begin_pair_code() > 0
    # Nothing landed in the login table or the global brake…
    assert auth.throttle_key_count == 0
    assert auth.global_failure_count == 0
    # …so the one free slot still takes a login.
    assert auth.begin_attempt("10.0.0.1") == 0
    auth.record_pair_code_success()
    assert auth.failure_count("10.0.0.1") == 1


def test_display_reads_are_budgeted_per_panel_and_kind(auth, clock):
    for _ in range(DISPLAY_READ_MAX):
        assert auth.begin_display_read("d1") == 0
    wait = auth.begin_display_read("d1")
    assert 0 < wait <= DISPLAY_WINDOW_S
    assert auth.begin_display_read("d2") == 0
    assert auth.begin_display_read("d1", image=True) == 0
    for _ in range(DISPLAY_IMAGE_MAX - 1):
        auth.begin_display_read("d1", image=True)
    assert auth.begin_display_read("d1", image=True) > 0
    clock.advance(DISPLAY_WINDOW_S + 1)
    assert auth.begin_display_read("d1") == 0


# ─── wall panels: "Got it" budget and PIN bucket ──────────────────────────


async def test_display_ack_limit_is_per_panel_and_apart_from_tablets(auth, clock):
    tablet = await auth.verify(await auth.issue(True))
    assert tablet is not None
    for _ in range(ACK_MAX):
        assert auth.begin_display_ack("d1") == 0
    wait = auth.begin_display_ack("d1")
    assert 0 < wait <= ACK_WINDOW_S
    assert auth.begin_display_ack("d2") == 0  # another panel
    assert auth.begin_ack(tablet) == 0  # a tablet session
    assert auth.throttle_key_count == 0
    clock.advance(ACK_WINDOW_S + 1)
    assert auth.begin_display_ack("d1") == 0


def test_display_pin_bucket_is_per_panel_and_apart_from_tablets():
    key = DashboardAuth.display_pin_throttle_key("d1")
    assert key != DashboardAuth.display_pin_throttle_key("d2")
    claims = SessionClaims(epoch=0, issued_at=0, keep=True, session_id="d1")
    assert key != DashboardAuth.pin_throttle_key(claims)


def test_map_renders_are_budgeted_per_panel(auth, clock):
    for _ in range(MAP_RENDER_MAX):
        assert auth.begin_map_render("d1") == 0
    wait = auth.begin_map_render("d1")
    assert 0 < wait <= DISPLAY_WINDOW_S
    # Another panel, and the panel's other budgets, are untouched.
    assert auth.begin_map_render("d2") == 0
    assert auth.begin_display_read("d1", image=True) == 0
    clock.advance(DISPLAY_WINDOW_S + 1)
    assert auth.begin_map_render("d1") == 0


# ─── PIN buckets can't be evicted (security) ─────────────────────────────


def _pin_keys() -> list[tuple[str, str]]:
    claims = SessionClaims(epoch=0, issued_at=0, keep=True, session_id="s" * 22)
    return [
        ("tablet", DashboardAuth.pin_throttle_key(claims)),
        ("panel", DashboardAuth.display_pin_throttle_key("d1")),
    ]


@pytest.mark.parametrize("kind", ["tablet", "panel"])
def test_a_login_flood_cannot_evict_a_pin_bucket(settings_service, clock, kind):
    """Regression: a full throttle table used to evict any bucket under
    the limit — a lock's 4 wrong PINs too — so flooding the table with
    fresh client addresses bought the PIN guesser a fresh budget."""
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=3)
    key = dict(_pin_keys())[kind]
    for _ in range(THROTTLE_MAX_FAILURES - 1):
        assert auth.begin_attempt(key, count_global=False) == 0
    # The flood: many client addresses, one wrong try each (setup-link
    # guesses skip the global brake, so nothing stops them early).
    for n in range(100):
        auth.begin_attempt(f"2001:db8:{n:x}::/64", count_global=False)
    assert auth.failure_count(key) == THROTTLE_MAX_FAILURES - 1
    # The fifth wrong PIN is the last: the guesser gets no sixth.
    assert auth.begin_attempt(key, count_global=False) == 0
    assert auth.begin_attempt(key, count_global=False) > 0
    assert auth.failure_count(key) == THROTTLE_MAX_FAILURES


def test_total_pin_guesses_stay_bounded_under_any_flood(settings_service, clock):
    """Interleave guesses with floods: one lock never gets more than
    THROTTLE_MAX_FAILURES wrong PINs inside a window."""
    auth = DashboardAuth(settings_service, clock=clock, max_throttle_keys=2)
    key = DashboardAuth.display_pin_throttle_key("d1")
    allowed = 0
    for round_ in range(20):
        if auth.begin_attempt(key, count_global=False) == 0:
            allowed += 1
        for n in range(10):
            auth.begin_attempt(f"10.{round_}.{n}.1", count_global=False)
            auth.begin_attempt(f"pin:other-{round_}-{n}", count_global=False)
    assert allowed == THROTTLE_MAX_FAILURES


def test_full_pin_table_refuses_newcomers_never_forgets_live_ones(
    settings_service, clock
):
    auth = DashboardAuth(settings_service, clock=clock, max_pin_keys=2)
    _, tablet = _pin_keys()[0]
    panel = DashboardAuth.display_pin_throttle_key("d1")
    assert auth.begin_attempt(tablet, count_global=False) == 0  # 1 failure
    assert auth.begin_attempt(panel, count_global=False) == 0  # 1 failure
    newcomer = DashboardAuth.display_pin_throttle_key("d2")
    wait = auth.begin_attempt(newcomer, count_global=False)
    assert wait > 0  # refused, not admitted by forgetting someone
    assert auth.failure_count(newcomer) == 0
    assert auth.failure_count(tablet) == 1 and auth.failure_count(panel) == 1
    assert auth.attempt_wait(newcomer, count_global=False) > 0
    # A lock already in the table keeps guessing within its own budget.
    assert auth.begin_attempt(panel, count_global=False) == 0
    # Once the live failures age out, the room is free again.
    clock.advance(THROTTLE_WINDOW_S)
    assert auth.begin_attempt(newcomer, count_global=False) == 0
    assert auth.failure_count(newcomer) == 1


def test_a_pin_flood_cannot_crowd_out_logins(settings_service, clock):
    """The two tables are apart: PIN buckets never take login room, and
    logins never take PIN room."""
    auth = DashboardAuth(
        settings_service, clock=clock, max_throttle_keys=1, max_pin_keys=1
    )
    assert auth.begin_attempt("pin:a", count_global=False) == 0
    assert auth.begin_attempt("10.0.0.1") == 0
    assert auth.begin_attempt("pin:b", count_global=False) > 0
    assert auth.failure_count("10.0.0.1") == 1
    assert auth.failure_count("pin:a") == 1
    assert auth.throttle_key_count == 2


def test_record_success_frees_a_pin_slot(settings_service, clock):
    auth = DashboardAuth(settings_service, clock=clock, max_pin_keys=1)
    assert auth.begin_attempt("pin:a", count_global=False) == 0
    assert auth.begin_attempt("pin:b", count_global=False) > 0
    auth.record_success("pin:a", count_global=False)
    assert auth.begin_attempt("pin:b", count_global=False) == 0
