"""Tests for the owner's GFS server list domain shape (``spaces.gfs_servers``)."""

from __future__ import annotations

import json

import pytest

from socialhome.domain.gfs_server import (
    GFS_KEY_SUITE_ED25519,
    GFS_SERVER_WIRE_KEYS,
    MAX_GFS_DISPLAY_NAME_LENGTH,
    MAX_GFS_SERVERS,
    SUPPORTED_GFS_KEY_SUITES,
    GfsServer,
    UnsupportedGfsKeySuite,
    gfs_servers_from_column,
    gfs_servers_from_wire,
    gfs_servers_to_column,
    gfs_servers_to_wire,
)

_PK = "ab" * 32


def _wire(**over) -> dict:
    wire = {
        "gfs_instance_id": "gfs-a",
        "inbox_url": "https://gfs-a.example",
        "public_key": _PK,
        "gfs_key_suite": "ed25519",
        "display_name": "GFS A",
    }
    wire.update(over)
    return wire


def test_suite_constants() -> None:
    assert GFS_KEY_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_GFS_KEY_SUITES == frozenset({"ed25519"})
    assert issubclass(UnsupportedGfsKeySuite, ValueError)
    assert MAX_GFS_SERVERS == 8


def test_round_trip() -> None:
    server = GfsServer.from_wire(_wire())
    assert server == GfsServer(
        gfs_instance_id="gfs-a",
        inbox_url="https://gfs-a.example",
        public_key=_PK,
        gfs_key_suite="ed25519",
        display_name="GFS A",
    )
    assert server.to_wire() == _wire()
    assert set(server.to_wire()) == GFS_SERVER_WIRE_KEYS


def test_is_frozen_and_slotted() -> None:
    server = GfsServer.from_wire(_wire())
    with pytest.raises(AttributeError):
        server.inbox_url = "https://other.example"  # type: ignore[misc]
    assert not hasattr(server, "__dict__")


def test_public_key_is_lowercased() -> None:
    assert GfsServer.from_wire(_wire(public_key="AB" * 32)).public_key == _PK


@pytest.mark.parametrize("suite", ["x25519", "ed25519+mldsa65", "", None, 7])
def test_unknown_suite_is_rejected_not_defaulted(suite) -> None:
    with pytest.raises(UnsupportedGfsKeySuite):
        GfsServer.from_wire(_wire(gfs_key_suite=suite))


def test_missing_suite_is_rejected() -> None:
    wire = _wire()
    del wire["gfs_key_suite"]
    with pytest.raises(UnsupportedGfsKeySuite):
        GfsServer.from_wire(wire)


@pytest.mark.parametrize(
    "over",
    [
        {"gfs_instance_id": ""},
        {"gfs_instance_id": 5},
        {"gfs_instance_id": "x" * 129},
        {"gfs_instance_id": "new\nline"},
        {"gfs_instance_id": "ctl\x00"},
        {"public_key": ""},
        {"public_key": "ab" * 31},
        {"public_key": "zz" * 32},
        {"public_key": None},
        # Plain http on a public host — no trustworthy key to pin.
        {"inbox_url": "http://gfs-a.example"},
        {"inbox_url": "ftp://gfs-a.example"},
        {"inbox_url": "https://user:pw@gfs-a.example"},
        {"inbox_url": ""},
        {"inbox_url": None},
        {"display_name": 5},
    ],
)
def test_invalid_fields_rejected(over) -> None:
    with pytest.raises(ValueError):
        GfsServer.from_wire(_wire(**over))


def test_non_dict_rejected() -> None:
    with pytest.raises(ValueError):
        GfsServer.from_wire(["not", "a", "dict"])


def test_private_http_url_allowed() -> None:
    server = GfsServer.from_wire(_wire(inbox_url="http://127.0.0.1:9000"))
    assert server.inbox_url == "http://127.0.0.1:9000"


def test_display_name_missing_defaults_empty_and_long_is_truncated() -> None:
    wire = _wire()
    del wire["display_name"]
    assert GfsServer.from_wire(wire).display_name == ""
    long = GfsServer.from_wire(_wire(display_name="n" * 500))
    assert len(long.display_name) == MAX_GFS_DISPLAY_NAME_LENGTH


def test_display_name_control_chars_stripped() -> None:
    assert GfsServer.from_wire(_wire(display_name="A\nB\x00C")).display_name == "ABC"


# ── Lists ────────────────────────────────────────────────────────────────


def test_list_drops_invalid_and_unknown_suite_and_reports_them() -> None:
    servers, dropped = gfs_servers_from_wire(
        [
            _wire(),
            _wire(gfs_instance_id="gfs-b", gfs_key_suite="future"),
            _wire(gfs_instance_id="gfs-c", inbox_url="http://public.example"),
            "junk",
            _wire(gfs_instance_id="gfs-d", inbox_url="https://gfs-d.example"),
        ]
    )
    assert [s.gfs_instance_id for s in servers] == ["gfs-a", "gfs-d"]
    assert len(dropped) == 3


def test_list_dedupes_by_gfs_instance_id_keeping_first() -> None:
    servers, dropped = gfs_servers_from_wire(
        [_wire(), _wire(inbox_url="https://other.example")]
    )
    assert len(servers) == 1
    assert servers[0].inbox_url == "https://gfs-a.example"
    assert len(dropped) == 1


def test_list_capped_at_max() -> None:
    raw = [
        _wire(gfs_instance_id=f"gfs-{i}", inbox_url=f"https://g{i}.example")
        for i in range(MAX_GFS_SERVERS + 3)
    ]
    servers, dropped = gfs_servers_from_wire(raw)
    assert len(servers) == MAX_GFS_SERVERS
    assert [s.gfs_instance_id for s in servers] == [
        f"gfs-{i}" for i in range(MAX_GFS_SERVERS)
    ]
    assert len(dropped) == 3


@pytest.mark.parametrize("raw", [None, "x", {"a": 1}, 3])
def test_list_not_a_list_is_none(raw) -> None:
    assert gfs_servers_from_wire(raw) == (None, ())


def test_empty_list_is_empty_tuple_not_none() -> None:
    assert gfs_servers_from_wire([]) == ((), ())


def test_to_wire_list() -> None:
    server = GfsServer.from_wire(_wire())
    assert gfs_servers_to_wire((server,)) == [_wire()]


# ── Column codec ─────────────────────────────────────────────────────────


def test_column_round_trip() -> None:
    servers = (
        GfsServer.from_wire(_wire()),
        GfsServer.from_wire(
            _wire(gfs_instance_id="gfs-b", inbox_url="https://gfs-b.example")
        ),
    )
    text = gfs_servers_to_column(servers)
    assert isinstance(text, str)
    assert isinstance(json.loads(text), list)
    assert gfs_servers_from_column(text) == servers


def test_column_none_and_empty() -> None:
    assert gfs_servers_to_column(None) is None
    assert gfs_servers_from_column(None) is None
    assert gfs_servers_to_column(()) == "[]"
    assert gfs_servers_from_column("[]") == ()


@pytest.mark.parametrize("text", ["not json", '{"a": 1}', "3"])
def test_column_garbage_reads_as_unknown(text) -> None:
    assert gfs_servers_from_column(text) is None
