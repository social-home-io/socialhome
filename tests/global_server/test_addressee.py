"""Tests for :class:`socialhome.global_server.addressee.GfsAddressee`."""

from __future__ import annotations

import logging

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
    assert lines[0].startswith("gfs: 1 request(s) addressed to the alias 'gfs-0'")
    # The four suppressed hits plus the one that logged.
    assert lines[1].startswith("gfs: 5 request(s) addressed to the alias 'gfs-0'")
