"""Unit tests for :mod:`socialhome.login_throttle` — failures-only login and
password-reset limits."""

from __future__ import annotations

import pytest

from socialhome.login_throttle import (
    FAILURE_WINDOW_S,
    LOGIN_IP_MAX_FAILURES,
    LOGIN_USER_MAX_FAILURES,
    RESET_IP_MAX_FAILURES,
    LoginThrottle,
    normalize_username,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def throttle(clock: _Clock) -> LoginThrottle:
    return LoginThrottle(clock=clock)


def _fail_logins(throttle: LoginThrottle, n: int, *, ip: str, user: str) -> None:
    for _ in range(n):
        assert throttle.begin_login(ip, user) == 0


def test_limits_match_the_approved_budget():
    assert LOGIN_IP_MAX_FAILURES == 10
    assert LOGIN_USER_MAX_FAILURES == 5
    assert RESET_IP_MAX_FAILURES == 10
    assert FAILURE_WINDOW_S == 15 * 60


def test_successful_logins_do_not_count(throttle: LoginThrottle):
    # A family setting up many phones from one address: every login is right.
    for i in range(50):
        assert throttle.begin_login("10.0.0.1", f"member{i % 6}") == 0
        throttle.login_succeeded("10.0.0.1", f"member{i % 6}")
    assert throttle.begin_login("10.0.0.1", "member0") == 0


def test_ten_failures_per_ip_lock_that_ip(throttle: LoginThrottle, clock: _Clock):
    # Different usernames, so only the per-IP bucket fills.
    for i in range(LOGIN_IP_MAX_FAILURES):
        assert throttle.begin_login("10.0.0.1", f"u{i}") == 0
    wait = throttle.begin_login("10.0.0.1", "someone-else")
    assert 0 < wait <= FAILURE_WINDOW_S
    # Another client is unaffected.
    assert throttle.begin_login("10.0.0.2", "someone-else") == 0


def test_five_failures_per_username_lock_that_name(throttle: LoginThrottle):
    # Five addresses, one name: the per-username bucket fills.
    for i in range(LOGIN_USER_MAX_FAILURES):
        assert throttle.begin_login(f"10.0.0.{i}", "anna") == 0
    assert throttle.begin_login("10.0.9.9", "anna") > 0
    # Another name from the same fresh address is fine.
    assert throttle.begin_login("10.0.9.9", "ben") == 0


def test_username_bucket_is_normalized(throttle: LoginThrottle):
    for i, spelling in enumerate(["Anna", "ANNA", " anna", "anna ", "Anna"]):
        assert throttle.begin_login(f"10.0.0.{i}", spelling) == 0
    assert throttle.begin_login("10.0.9.9", "anna") > 0


def test_normalize_username_folds_case_width_and_space():
    assert normalize_username(" Anna ") == normalize_username("anna")
    # NFKC: a full-width spelling is the same name.
    assert normalize_username("ａｎｎａ") == normalize_username("anna")
    assert normalize_username("anna") != normalize_username("anne")


def test_success_clears_that_usernames_failures(throttle: LoginThrottle):
    _fail_logins(throttle, LOGIN_USER_MAX_FAILURES - 1, ip="10.0.0.1", user="anna")
    assert throttle.begin_login("10.0.0.1", "anna") == 0
    throttle.login_succeeded("10.0.0.1", "anna")
    # The name's budget is whole again.
    for i in range(LOGIN_USER_MAX_FAILURES):
        assert throttle.begin_login(f"10.0.1.{i}", "anna") == 0


def test_success_refunds_only_its_own_attempt_on_the_ip(throttle: LoginThrottle):
    # Nine failures from the shared address, then one right password: the
    # address keeps its nine, so the tenth wrong one still locks it.
    for i in range(LOGIN_IP_MAX_FAILURES - 1):
        assert throttle.begin_login("10.0.0.1", f"u{i}") == 0
    assert throttle.begin_login("10.0.0.1", "anna") == 0
    throttle.login_succeeded("10.0.0.1", "anna")
    assert throttle.begin_login("10.0.0.1", "zed") == 0
    assert throttle.begin_login("10.0.0.1", "zed2") > 0


def test_locked_attempt_charges_nothing(throttle: LoginThrottle, clock: _Clock):
    _fail_logins(throttle, LOGIN_USER_MAX_FAILURES, ip="10.0.0.1", user="anna")
    for _ in range(20):
        assert throttle.begin_login("10.0.0.1", "anna") > 0
    # The address spent five, not twenty-five: five more names fit.
    for i in range(LOGIN_IP_MAX_FAILURES - LOGIN_USER_MAX_FAILURES):
        assert throttle.begin_login("10.0.0.1", f"x{i}") == 0


def test_retry_after_is_when_the_oldest_counted_failure_ages_out(
    throttle: LoginThrottle, clock: _Clock
):
    assert throttle.begin_login("10.0.0.1", "anna") == 0
    clock.now += 100
    _fail_logins(throttle, LOGIN_USER_MAX_FAILURES - 1, ip="10.0.0.1", user="anna")
    assert throttle.begin_login("10.0.0.1", "anna") == FAILURE_WINDOW_S - 100
    clock.now += FAILURE_WINDOW_S - 100
    # The first failure aged out: one try is free again.
    assert throttle.begin_login("10.0.0.1", "anna") == 0
    assert throttle.begin_login("10.0.0.1", "anna") > 0


def test_unknown_and_known_usernames_are_counted_alike(throttle: LoginThrottle):
    # The throttle never learns whether a name exists — it only sees the
    # name — so it cannot answer differently for one.
    for user in ("anna", "ghost"):
        for i in range(LOGIN_USER_MAX_FAILURES):
            assert throttle.begin_login(f"10.{i}.0.1", user) == 0
        assert throttle.begin_login("10.9.9.9", user) > 0


def test_reset_failures_lock_the_ip(throttle: LoginThrottle):
    for _ in range(RESET_IP_MAX_FAILURES):
        assert throttle.begin_reset("10.0.0.1") == 0
    assert throttle.begin_reset("10.0.0.1") > 0
    assert throttle.begin_reset("10.0.0.2") == 0


def test_reset_and_login_budgets_are_separate(throttle: LoginThrottle):
    for _ in range(RESET_IP_MAX_FAILURES):
        assert throttle.begin_reset("10.0.0.1") == 0
    assert throttle.begin_login("10.0.0.1", "anna") == 0


def test_reset_success_is_refunded_and_clears_the_login_lockout(
    throttle: LoginThrottle,
):
    # Anna locked herself out, an admin gave her a reset link.
    _fail_logins(throttle, LOGIN_USER_MAX_FAILURES, ip="10.0.0.1", user="anna")
    assert throttle.begin_login("10.0.0.1", "anna") > 0
    for _ in range(RESET_IP_MAX_FAILURES - 1):
        assert throttle.begin_reset("10.0.0.1") == 0
    assert throttle.begin_reset("10.0.0.1") == 0
    throttle.reset_succeeded("10.0.0.1", "anna")
    # Her new password works at once.
    assert throttle.begin_login("10.0.0.2", "anna") == 0
    # The successful redeem was refunded: one more reset fits.
    assert throttle.begin_reset("10.0.0.1") == 0
    assert throttle.begin_reset("10.0.0.1") > 0


def test_idle_keys_are_pruned(throttle: LoginThrottle, clock: _Clock):
    for i in range(30):
        assert throttle.begin_login(f"10.0.0.{i}", f"u{i}") == 0
    assert throttle.key_count == 60
    clock.now += FAILURE_WINDOW_S + 1
    assert throttle.begin_login("10.1.0.1", "late") == 0
    assert throttle.key_count == 2


def test_key_table_is_bounded(clock: _Clock):
    throttle = LoginThrottle(clock=clock, max_keys=10)
    for i in range(20):
        throttle.begin_login(f"10.0.0.{i}", f"u{i}")
        clock.now += 1
    assert throttle.key_count <= 10


def test_username_keys_do_not_hold_the_raw_name(throttle: LoginThrottle):
    long_name = "x" * 100_000
    assert throttle.begin_login("10.0.0.1", long_name) == 0
    assert all(len(k) < 200 for k in throttle.bucket_keys())
