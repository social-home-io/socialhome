"""Tests for :mod:`socialhome.global_server.node_route` — the ids that name
the node holding a public-highlight session, and the misroute WARNING."""

from __future__ import annotations

import logging

import pytest

from socialhome.global_server.node_route import (
    MISROUTE_WARN_INTERVAL_S,
    MisrouteWarning,
    route_tag_of,
    scoped_id,
    valid_route_tag,
)


@pytest.mark.parametrize(
    "tag",
    ["a", "gfs-0", "0b1c2d3e-aaaa-bbbb-cccc-0123456789ab", "X" * 64],
)
def test_usable_route_tags(tag):
    assert valid_route_tag(tag)


@pytest.mark.parametrize(
    "tag",
    ["", "X" * 65, "a.b", "a/b", "a b", "ä", "a_b", None, 7, "a\n"],
)
def test_unusable_route_tags(tag):
    """Nothing a balancer could not put in a backend name, and never the
    separator itself."""
    assert not valid_route_tag(tag)


def test_a_scoped_id_names_its_node_and_is_unguessable():
    first, second = scoped_id("tag-a"), scoped_id("tag-a")
    assert first.startswith("tag-a.")
    assert len(first.partition(".")[2]) == 32
    assert first != second
    assert route_tag_of(first) == "tag-a"


def test_without_a_tag_an_id_names_no_node():
    sid = scoped_id("")
    assert "." not in sid and len(sid) == 32
    assert route_tag_of(sid) == ""


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "plain-uuid-0b1c2d3e-aaaa-bbbb",
        ".abc",
        "bad tag.abc",
        "X" * 65 + ".abc",
    ],
)
def test_ids_that_name_no_usable_node(raw):
    assert route_tag_of(raw) == ""


def test_a_misroute_is_counted_and_warned_once_a_minute(caplog):
    now = [1000.0]
    misroutes = MisrouteWarning("tag-a", clock=lambda: now[0])
    with caplog.at_level(logging.WARNING):
        assert misroutes.check("tag-b.0123") is True
        assert misroutes.check("tag-b.4567") is True
        now[0] += MISROUTE_WARN_INTERVAL_S
        assert misroutes.check("tag-c.89ab") is True
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2
    assert "1 public-highlight request" in warnings[0]
    # The second warning counts what arrived since the first.
    assert "2 public-highlight request" in warnings[1]
    assert "route_tag" in warnings[0]


@pytest.mark.parametrize("sid", ["tag-a.0123", "no-tag", "", "bad tag.0"])
def test_own_or_untagged_ids_are_no_misroute(caplog, sid):
    """A session this node lost (restart, expiry) or an id from before the
    tags is just gone — nothing for the operator to fix."""
    misroutes = MisrouteWarning("tag-a")
    with caplog.at_level(logging.WARNING):
        assert misroutes.check(sid) is False
    assert caplog.records == []
