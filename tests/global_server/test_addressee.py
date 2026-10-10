"""Tests for :class:`socialhome.global_server.addressee.GfsAddressee`."""

from __future__ import annotations

import asyncio
import logging

from socialhome.global_server import addressee as addressee_mod
from socialhome.global_server.addressee import ALIAS_LOG_INTERVAL_S, GfsAddressee


def test_the_instance_id_is_accepted_and_others_are_not():
    a = GfsAddressee("gfs-shared", ("gfs-0", "gfs-1"))
    assert a.accepts("gfs-shared")
    assert not a.accepts("gfs-9")
    assert not a.accepts("")


def test_an_alias_is_accepted():
    a = GfsAddressee("gfs-shared", ("gfs-0",))
    assert a.accepts("gfs-0")
    assert a.instance_id == "gfs-shared"
    assert a.aliases == frozenset({"gfs-0"})


def test_empty_and_self_aliases_are_dropped():
    a = GfsAddressee("gfs-shared", ("", "gfs-shared", "gfs-0"))
    assert a.aliases == frozenset({"gfs-0"})
    assert not a.accepts("")


def test_alias_use_is_logged_at_info_rate_limited_with_a_count(caplog):
    now = [1000.0]
    a = GfsAddressee("gfs-shared", ("gfs-0",), clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="socialhome.global_server.addressee"):
        a.accepts("gfs-0")
        for _ in range(4):
            a.accepts("gfs-0")
        # The primary id is never logged.
        a.accepts("gfs-shared")
        now[0] += ALIAS_LOG_INTERVAL_S + 1
        a.accepts("gfs-0")
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(lines) == 2
    assert lines[0].startswith(
        "gfs: node -: 1 request(s) addressed to the alias 'gfs-0'"
    )
    # The four suppressed hits plus the one that logged.
    assert lines[1].startswith(
        "gfs: node -: 5 request(s) addressed to the alias 'gfs-0'"
    )


def test_a_bound_server_key_must_be_ours():
    """L2: an alias may be another operator's real id — a request bound to
    another server's key is refused even when its id is accepted."""
    a = GfsAddressee("gfs-shared", ("gfs-0",), public_key_hex="AB" * 32)
    assert a.accepts("gfs-0", "ab" * 32)
    assert a.accepts("gfs-shared", "ab" * 32)
    assert not a.accepts("gfs-0", "cd" * 32)
    assert not a.accepts("gfs-shared", "cd" * 32)
    # Unbound (an older household): judged by the id alone.
    assert a.accepts("gfs-0")
    # No key wired: a bound request can never match.
    assert not GfsAddressee("gfs-shared").accepts("gfs-shared", "ab" * 32)


def test_flush_logs_the_stragglers_with_the_node_id(caplog):
    """L3: hits counted after the last line are logged by ``flush`` (the
    timer / shutdown), naming this alloc's node_id."""
    now = [1000.0]
    a = GfsAddressee("gfs-shared", ("gfs-0",), node_id="gfs-2", clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger="socialhome.global_server.addressee"):
        a.accepts("gfs-0")  # logged at once
        a.accepts("gfs-0")
        a.accepts("gfs-0")  # two stragglers, rate-limited
        a.flush()
        a.flush()  # nothing pending: silent
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert lines[1].startswith("gfs: node gfs-2: 2 request(s) addressed to the alias")


async def test_stop_flushes_pending_counts(caplog):
    now = [1000.0]
    a = GfsAddressee("gfs-shared", ("gfs-0",), clock=lambda: now[0])
    await a.start()
    await a.start()  # idempotent
    a.accepts("gfs-0")
    a.accepts("gfs-0")
    with caplog.at_level(logging.INFO, logger="socialhome.global_server.addressee"):
        await a.stop()
    assert any("1 request(s)" in r.getMessage() for r in caplog.records)


async def test_the_timer_flushes(monkeypatch, caplog):
    monkeypatch.setattr(addressee_mod, "ALIAS_LOG_INTERVAL_S", 0.01)
    now = [1000.0]
    a = GfsAddressee("gfs-shared", ("gfs-0",), clock=lambda: now[0])
    a.accepts("gfs-0")
    a.accepts("gfs-0")
    with caplog.at_level(logging.INFO, logger="socialhome.global_server.addressee"):
        await a.start()
        for _ in range(100):
            if any("1 request(s)" in r.getMessage() for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await a.stop()
    assert any("1 request(s)" in r.getMessage() for r in caplog.records)


async def test_without_aliases_no_timer_runs():
    a = GfsAddressee("gfs-shared")
    await a.start()
    assert a._task is None
    await a.stop()
