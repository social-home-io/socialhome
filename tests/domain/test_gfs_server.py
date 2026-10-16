"""Tests for the owner's GFS server list domain shapes (``spaces.gfs_servers``)."""

from __future__ import annotations

import json

import pytest

from socialhome.domain.gfs_server import (
    GFS_KEY_SUITE_ED25519,
    GFS_SERVER_WIRE_KEYS,
    GFS_SERVERS_SIG_SUITE_ED25519,
    MAX_GFS_DISPLAY_NAME_LENGTH,
    MAX_GFS_SERVERS,
    SIGNED_GFS_SERVER_LIST_KEYS,
    SUPPORTED_GFS_KEY_SUITES,
    SUPPORTED_GFS_SERVERS_SIG_SUITES,
    GfsSelectionOutcome,
    GfsServer,
    SignedGfsServerList,
    UnsupportedGfsKeySuite,
    UnsupportedGfsServersSigSuite,
    canonical_json,
    gfs_servers_from_wire,
    gfs_servers_to_wire,
    signed_gfs_server_list_from_column,
    signed_gfs_server_list_to_column,
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


def _n(i: int) -> dict:
    return _wire(
        gfs_instance_id=f"gfs-{i}",
        inbox_url=f"https://g{i}.example",
        public_key=f"{i:02x}" * 32,
    )


def test_suite_constants() -> None:
    assert GFS_KEY_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_GFS_KEY_SUITES == frozenset({"ed25519"})
    assert GFS_SERVERS_SIG_SUITE_ED25519 == "ed25519"
    assert SUPPORTED_GFS_SERVERS_SIG_SUITES == frozenset({"ed25519"})
    assert issubclass(UnsupportedGfsKeySuite, ValueError)
    assert issubclass(UnsupportedGfsServersSigSuite, ValueError)
    assert MAX_GFS_SERVERS == 8


# ── One entry ────────────────────────────────────────────────────────────


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
    assert server.route == ("gfs-a", "https://gfs-a.example")


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


def test_display_name_missing_defaults_empty_and_long_is_truncated() -> None:
    wire = _wire()
    del wire["display_name"]
    assert GfsServer.from_wire(wire).display_name == ""
    long = GfsServer.from_wire(_wire(display_name="n" * 500))
    assert len(long.display_name) == MAX_GFS_DISPLAY_NAME_LENGTH


def test_display_name_control_chars_stripped() -> None:
    assert GfsServer.from_wire(_wire(display_name="A\nB\x00C")).display_name == "ABC"


# ── Lists ────────────────────────────────────────────────────────────────


def test_list_drops_invalid_and_unknown_suite_and_counts_them() -> None:
    servers, dropped = gfs_servers_from_wire(
        [
            _n(1),
            {**_n(2), "gfs_key_suite": "future"},
            {**_n(3), "inbox_url": "http://public.example"},
            "junk",
            _n(4),
        ]
    )
    assert [s.gfs_instance_id for s in servers] == ["gfs-1", "gfs-4"]
    assert dropped == 3


@pytest.mark.parametrize(
    "dupe",
    [
        {"inbox_url": "https://other.example"},  # same id
        {"gfs_instance_id": "gfs-x", "public_key": "cd" * 32},  # same url
        {"gfs_instance_id": "gfs-x", "inbox_url": "https://other.example"},  # key
    ],
)
def test_list_dedupes_by_id_url_and_key_keeping_first(dupe) -> None:
    servers, dropped = gfs_servers_from_wire([_wire(), _wire(**dupe)])
    assert len(servers) == 1
    assert servers[0] == GfsServer.from_wire(_wire())
    assert dropped == 1


def test_list_is_truncated_before_parsing() -> None:
    class _Boom(dict):
        def get(self, *a, **kw):  # pragma: no cover — must never be read
            raise AssertionError("entries past the cap must not be parsed")

    raw = [_n(i) for i in range(MAX_GFS_SERVERS)] + [_Boom()] * 10_000
    servers, dropped = gfs_servers_from_wire(raw)
    assert len(servers) == MAX_GFS_SERVERS
    assert dropped == 10_000


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9000",
        "https://127.0.0.1",
        "http://[::1]:9000",
        "http://10.1.2.3",
        "http://192.168.1.10",
        "http://169.254.169.254",
        "http://[fd00::1]",
        "http://localhost:8080",
    ],
)
def test_private_hosts_are_dropped_unless_an_own_connection_matches(url) -> None:
    entry = _wire(inbox_url=url)
    assert gfs_servers_from_wire([entry]) == ((), 1)
    servers, dropped = gfs_servers_from_wire([entry], own_routes={("gfs-a", url)})
    assert dropped == 0 and servers[0].inbox_url == url
    # The SAME id at another URL (or another id at this URL) is not ours.
    assert gfs_servers_from_wire([entry], own_routes={("gfs-b", url)}) == ((), 1)
    assert gfs_servers_from_wire(
        [entry], own_routes={("gfs-a", "http://127.0.0.2")}
    ) == ((), 1)


@pytest.mark.parametrize("raw", [None, "x", {"a": 1}, 3])
def test_list_not_a_list_is_none(raw) -> None:
    assert gfs_servers_from_wire(raw) == (None, 0)


def test_empty_list_is_empty_tuple_not_none() -> None:
    assert gfs_servers_from_wire([]) == ((), 0)


def test_to_wire_list() -> None:
    assert gfs_servers_to_wire((GfsServer.from_wire(_wire()),)) == [_wire()]


# ── Signed list (structure only; the signature is gfs_server_list_sig's) ──


def _signed(**over) -> dict:
    wire = {
        "servers": [_n(1), _n(2)],
        "issued_at": 1_700_000_000_000,
        "owner_pk": "ef" * 32,
        "gfs_servers_sig_suite": "ed25519",
        "gfs_servers_sig": "c2lnbmF0dXJl",
    }
    wire.update(over)
    return wire


def test_signed_round_trip_keeps_the_exact_entries() -> None:
    wire = _signed(servers=[{**_n(1), "future_field": [1, 2]}, "junk"])
    signed = SignedGfsServerList.from_wire(wire)
    assert signed.to_wire() == wire
    assert set(signed.to_wire()) == SIGNED_GFS_SERVER_LIST_KEYS
    assert signed.servers_json == canonical_json(wire["servers"])
    servers, dropped = signed.servers()
    assert [s.gfs_instance_id for s in servers] == ["gfs-1"]
    assert dropped == 1


@pytest.mark.parametrize("suite", ["x", "", None, 1])
def test_signed_unknown_suite_rejected(suite) -> None:
    with pytest.raises(UnsupportedGfsServersSigSuite):
        SignedGfsServerList.from_wire(_signed(gfs_servers_sig_suite=suite))


@pytest.mark.parametrize(
    "over",
    [
        {"servers": "x"},
        {"servers": [_n(i) for i in range(MAX_GFS_SERVERS + 1)]},
        {"servers": [{"pad": "x" * 40_000}]},
        {"issued_at": -1},
        {"issued_at": True},
        {"issued_at": "5"},
        {"issued_at": 2**63},
        {"owner_pk": "ab"},
        {"owner_pk": None},
        {"gfs_servers_sig": ""},
        {"gfs_servers_sig": "not base64!"},
    ],
)
def test_signed_bad_fields_rejected(over) -> None:
    with pytest.raises(ValueError):
        SignedGfsServerList.from_wire(_signed(**over))


def test_signed_column_round_trip() -> None:
    signed = SignedGfsServerList.from_wire(_signed())
    text = signed_gfs_server_list_to_column(signed)
    assert isinstance(text, str)
    assert isinstance(json.loads(text), dict)
    assert signed_gfs_server_list_from_column(text) == signed
    assert signed_gfs_server_list_to_column(None) is None
    assert signed_gfs_server_list_from_column(None) is None


@pytest.mark.parametrize("text", ["not json", "[]", "3", '{"servers": []}'])
def test_signed_column_garbage_reads_as_unknown(text) -> None:
    assert signed_gfs_server_list_from_column(text) is None


def test_signed_public_keys_in_list_order_including_private_hosts() -> None:
    private = {**_n(3), "inbox_url": "http://192.168.1.5:8765"}
    upper = {**_n(10), "public_key": _n(10)["public_key"].upper()}
    wire = _signed(
        servers=[_n(2), private, "junk", {**_n(5), "public_key": "short"}, upper, _n(2)]
    )
    keys = SignedGfsServerList.from_wire(wire).public_keys()
    assert keys == (
        _n(2)["public_key"],
        private["public_key"],
        _n(10)["public_key"].lower(),
    )


def test_selection_outcome_wire_shape():
    outcome = GfsSelectionOutcome(listed=("g1",), failed=(("g2", "publish_failed"),))
    assert outcome.to_dict() == {
        "listed": ["g1"],
        "failed": [{"gfs_id": "g2", "reason": "publish_failed"}],
    }
    assert GfsSelectionOutcome(listed=()).to_dict() == {"listed": [], "failed": []}
