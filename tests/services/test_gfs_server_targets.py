"""Tests for :mod:`socialhome.services.gfs_server_targets` (v_57)."""

from __future__ import annotations

import logging

import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.domain.gfs_server import GFS_KEY_SUITE_ED25519, GfsServer
from socialhome.outbound_fetch import OutboundFetchRefused
from socialhome.services.gfs_server_targets import (
    LISTING_TARGET_PREFIX,
    is_listing_target_id,
    listing_target,
    listing_target_id,
    normalized_url,
    refusal_outcome,
    resolve_server_targets,
)

KEY_A = "aa" * 32
KEY_B = "bb" * 32


def _server(gid: str, key: str, url: str = "") -> GfsServer:
    return GfsServer(
        gfs_instance_id=gid,
        inbox_url=url or f"https://{gid}.example",
        public_key=key,
        gfs_key_suite=GFS_KEY_SUITE_ED25519,
        display_name=gid.upper(),
    )


def _conn(cid: str, gid: str, key: str, status: str = "active") -> GfsConnection:
    return GfsConnection(
        id=cid,
        gfs_instance_id=gid,
        display_name=gid,
        public_key=key,
        inbox_url=f"https://{gid}.example",
        status=status,
        paired_at="",
    )


def _ids(conn: GfsConnection) -> frozenset[str]:
    return frozenset({conn.gfs_instance_id})


def test_listing_target_is_unregistered_and_carries_the_owner_entry():
    t = listing_target(_server("gfs-b", KEY_B))
    assert t.registered is False
    assert t.id == listing_target_id(_server("gfs-b", KEY_B))
    assert t.id.startswith(LISTING_TARGET_PREFIX)
    assert is_listing_target_id(t.id)
    assert not is_listing_target_id("c-uuid")
    assert (t.gfs_instance_id, t.public_key, t.inbox_url, t.status) == (
        "gfs-b",
        KEY_B,
        "https://gfs-b.example",
        "active",
    )


def test_own_connection_is_used_listing_target_for_the_rest():
    own = _conn("c1", "gfs-a", KEY_A)
    targets = resolve_server_targets(
        [_server("gfs-a", KEY_A), _server("gfs-b", KEY_B)], [own], known_ids=_ids
    )
    assert [t.id for t in targets] == [
        "c1",
        listing_target_id(_server("gfs-b", KEY_B)),
    ]
    assert [t.registered for t in targets] == [True, False]


def test_a_replaced_id_still_matches_our_connection():
    own = _conn("c1", "gfs-shared", KEY_A)
    targets = resolve_server_targets(
        [_server("gfs-node-0", KEY_A)],
        [own],
        known_ids=lambda c: frozenset({"gfs-shared", "gfs-node-0"}),
    )
    assert [t.id for t in targets] == ["c1"]


def test_the_same_key_under_an_id_we_have_not_read_yet_is_our_connection():
    own = _conn("c1", "gfs-node-0", KEY_A)
    targets = resolve_server_targets(
        [_server("gfs-shared", KEY_A)], [own], known_ids=_ids
    )
    assert [t.id for t in targets] == ["c1"]


def test_a_key_mismatch_uses_the_owner_entry_and_warns_once(caplog):
    own = _conn("c1", "gfs-a", KEY_B)
    warned: set = set()
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            targets = resolve_server_targets(
                [_server("gfs-a", KEY_A)], [own], known_ids=_ids, warned=warned
            )
            assert [(t.id, t.public_key) for t in targets] == [
                (listing_target_id(_server("gfs-a", KEY_A)), KEY_A)
            ]
    assert caplog.text.count("other than the one this household pinned") == 1


@pytest.mark.parametrize("status", ["pending", "suspended"])
def test_an_inactive_connection_is_not_a_registered_target(status):
    own = _conn("c1", "gfs-a", KEY_A, status=status)
    targets = resolve_server_targets([_server("gfs-a", KEY_A)], [own], known_ids=_ids)
    assert [t.registered for t in targets] == [False]


def test_two_entries_resolving_to_one_connection_are_sent_once():
    own = _conn("c1", "gfs-a", KEY_A)
    targets = resolve_server_targets(
        [_server("gfs-a", KEY_A), _server("gfs-a2", KEY_A, "https://x.example")],
        [own],
        known_ids=_ids,
    )
    assert [t.id for t in targets] == ["c1"]


def test_an_empty_list_has_no_targets():
    assert resolve_server_targets([], [_conn("c1", "g", KEY_A)], known_ids=_ids) == []


@pytest.mark.parametrize(
    ("reason", "kind"),
    [
        ("dns", "transient"),
        ("timeout", "transient"),
        ("network", "transient"),
        ("private_address", "permanent"),
        ("malformed", "permanent"),
        ("scheme", "permanent"),
    ],
)
def test_refusal_outcome(reason, kind):
    assert refusal_outcome(OutboundFetchRefused(reason)).kind == kind


def test_a_listing_target_id_covers_id_key_and_url():
    """Two spaces naming one server id under another key or URL never share
    a target (caches, retry queue); spellings of one URL do."""
    base = listing_target_id(_server("gfs-x", KEY_A, "https://x.example"))
    assert base != listing_target_id(_server("gfs-x", KEY_B, "https://x.example"))
    assert base != listing_target_id(_server("gfs-x", KEY_A, "https://y.example"))
    assert base != listing_target_id(_server("gfs-y", KEY_A, "https://x.example"))
    assert base == listing_target_id(_server("gfs-x", KEY_A, "https://X.example/"))


@pytest.mark.parametrize(
    ("url", "want"),
    [
        ("HTTPS://GFS.Example/", "https://gfs.example"),
        ("https://gfs.example:8443/base/", "https://gfs.example:8443/base"),
        ("http://[::1]:18767", "http://[::1]:18767"),
    ],
)
def test_normalized_url(url, want):
    assert normalized_url(url) == want
