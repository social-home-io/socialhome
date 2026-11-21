"""Tests for :mod:`socialhome.platform.local_credentials`."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.platform import local_credentials
from socialhome.platform.local_credentials import (
    LocalCredentialStore,
    hash_password,
    verify_password,
)


class _Db:
    """The two calls ``issue_bearer_token`` makes, in memory."""

    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows
        self.writes: list[tuple] = []

    async def fetchone(self, sql: str, params: tuple):
        if "platform_users" not in sql:
            return None  # no ``users`` row to mirror the token to
        h = self.rows.get(params[0])
        return None if h is None else {"password_hash": h}

    async def enqueue(self, sql: str, params: tuple) -> None:
        self.writes.append((sql, params))


def test_verify_password_roundtrip():
    h = hash_password("secret")
    assert verify_password("secret", h) is True
    assert verify_password("nope", h) is False


@pytest.mark.security
async def test_unknown_user_costs_the_same_password_check(monkeypatch):
    """No timing oracle: an unknown username runs the same scrypt check as
    a known one with a wrong password."""
    checked: list[str] = []
    real = local_credentials.verify_password

    def spy(password: str, stored: str) -> bool:
        checked.append(stored)
        return real(password, stored)

    monkeypatch.setattr(local_credentials, "verify_password", spy)
    store = LocalCredentialStore(_Db({"anna": hash_password("right")}))
    assert await store.issue_bearer_token("ghost", "wrong") is None
    assert await store.issue_bearer_token("anna", "wrong") is None
    assert len(checked) == 2
    # Same scrypt parameters for both.
    assert checked[0].split("$")[:4] == checked[1].split("$")[:4]


@pytest.mark.security
async def test_password_check_runs_off_the_event_loop(monkeypatch):
    """scrypt is ~50 ms of CPU: it must not stall every coroutine."""
    threads: list[object] = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        threads.append(fn)
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(local_credentials.asyncio, "to_thread", spy_to_thread)
    store = LocalCredentialStore(_Db({"anna": hash_password("right")}))
    assert await store.issue_bearer_token("anna", "right") is not None
    assert threads, "verify_password was not run via asyncio.to_thread"


@pytest.mark.security
async def test_password_checks_are_bounded(monkeypatch):
    """A flood of logins runs at most VERIFY_CONCURRENCY scrypt checks at
    once, so it can't take over the shared thread pool."""
    running = 0
    peak = 0

    async def slow_to_thread(fn, *args, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return False

    monkeypatch.setattr(local_credentials.asyncio, "to_thread", slow_to_thread)
    store = LocalCredentialStore(_Db({}))
    await asyncio.gather(*(store.issue_bearer_token(f"u{i}", "x") for i in range(20)))
    assert peak == local_credentials.VERIFY_CONCURRENCY
