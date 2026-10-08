"""Tests for :mod:`socialhome.federation.pairing_gfs_reach`.

The GFS-facing half of a pairing code's ``reach``: which connection a code
names, matching a scanned code's server against our own, our own key-wrap
fields, verifying the peer's, and handing a sealed pairing body to a
server. Stand-ins at the boundary (connection repo, capability check,
relay sender); the key-wrap binding is real crypto.
"""

from __future__ import annotations

import logging

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.domain.federation import (
    GfsConnection,
    GfsNotConnectedError,
    PairingKeywrapInvalidError,
    PairingReachInvalidError,
)
from socialhome.federation.keywrap_seal import KEM_SUITE_X25519
from socialhome.federation.pairing_gfs_reach import (
    PairingGfsReach,
    gfs_block,
    parse_reach,
    verified_peer_keywrap,
)


def _conn(cid: str, url: str, *, gid: str = "", status: str = "active"):
    return GfsConnection(
        id=cid,
        gfs_instance_id=gid or f"gi-{cid}",
        display_name=cid,
        public_key="pk",
        inbox_url=url,
        status=status,
        paired_at="2026-01-01T00:00:00+00:00",
    )


class _Repo:
    def __init__(self, conns) -> None:
        self.conns = list(conns)

    async def list_active(self):
        return list(self.conns)


class _Sender:
    def __init__(self, *, result=True, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, dict, str]] = []
        self.result = result
        self.raises = raises

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        self.calls.append((to_instance_id, envelope, gfs_url))
        if self.raises is not None:
            raise self.raises
        return self.result


def _reach(conns=(), *, capable=None, sender=None, probe=None) -> PairingGfsReach:
    async def _supported(conn) -> bool:
        if capable is None:
            return True
        return capable(conn)

    return PairingGfsReach(
        gfs_connection_repo=_Repo(conns),
        envelope_relay_supported=_supported,
        relay_sender=sender or _Sender(),
        keywrap_public_key=b"\x01" * 32,
        keywrap_sig="sig",
        probe_peer=probe,
    )


class SimpleIdentity:
    def __init__(self, *, instance_id: str, identity_pk: str, fields: dict) -> None:
        self.instance_id = instance_id
        self.identity_pk = identity_pk
        self.fields = fields


def _bound_keywrap():
    ident = generate_identity_keypair()
    keywrap = generate_x25519_keypair()
    return SimpleIdentity(
        instance_id=derive_instance_id(ident.public_key),
        identity_pk=ident.public_key.hex(),
        fields={
            "keywrap_pk": keywrap.public_key.hex(),
            "keywrap_sig": b64url_encode(
                sign_ed25519(ident.private_key, keywrap.public_key)
            ),
            "keywrap_suite": KEM_SUITE_X25519,
        },
    )


# ── parse_reach / gfs_block ──


@pytest.mark.parametrize("raw", [None, ""])
def test_a_missing_reach_is_the_classic_code(raw):
    assert parse_reach(raw) == "url"


@pytest.mark.parametrize("raw", ["url", "url_gfs", "gfs"])
def test_known_reaches_pass_through(raw):
    assert parse_reach(raw) == raw


@pytest.mark.parametrize("raw", ["GFS", "mesh", 1, ["gfs"], {"r": 1}])
def test_an_unknown_reach_is_refused_not_defaulted(raw):
    with pytest.raises(PairingReachInvalidError):
        parse_reach(raw)


def test_gfs_block_names_only_the_url_and_the_pinned_id():
    assert gfs_block(_conn("c1", "https://g.example", gid="gfs-1")) == {
        "url": "https://g.example",
        "instance_id": "gfs-1",
    }


# ── verified_peer_keywrap ──


def test_a_bound_keywrap_key_is_returned():
    ident = _bound_keywrap()
    assert (
        verified_peer_keywrap(
            ident.fields,
            instance_id=ident.instance_id,
            identity_pk=ident.identity_pk,
        )
        == ident.fields["keywrap_pk"]
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.pop("keywrap_suite"),
        lambda f: f.update(keywrap_suite="x25519+mlkem768"),
        lambda f: f.pop("keywrap_pk"),
        lambda f: f.update(keywrap_sig=123),
        lambda f: f.update(keywrap_pk=generate_x25519_keypair().public_key.hex()),
        lambda f: f.update(keywrap_pk="not-hex"),
    ],
)
def test_an_unbound_or_unlabelled_keywrap_key_fails_closed(mutate):
    ident = _bound_keywrap()
    mutate(ident.fields)
    with pytest.raises(PairingKeywrapInvalidError):
        verified_peer_keywrap(
            ident.fields,
            instance_id=ident.instance_id,
            identity_pk=ident.identity_pk,
        )


def test_a_keywrap_key_bound_to_another_identity_fails_closed():
    ident = _bound_keywrap()
    other = generate_identity_keypair()
    with pytest.raises(PairingKeywrapInvalidError):
        verified_peer_keywrap(
            ident.fields,
            instance_id=derive_instance_id(other.public_key),
            identity_pk=other.public_key.hex(),
        )


# ── PairingGfsReach ──


def test_own_keywrap_fields_carry_the_suite_tag():
    assert _reach().own_keywrap_fields() == {
        "keywrap_pk": "01" * 32,
        "keywrap_sig": "sig",
        "keywrap_suite": KEM_SUITE_X25519,
    }


async def test_bootstrap_connection_prefers_the_named_one():
    reach = _reach([_conn("c1", "https://a.example"), _conn("c2", "https://b.example")])
    assert (await reach.bootstrap_connection("c2")).id == "c2"


async def test_bootstrap_connection_falls_back_to_the_first_eligible():
    reach = _reach(
        [
            _conn("c0", "https://z.example", status="pending"),
            _conn("cx", ""),
            _conn("c1", "https://a.example"),
            _conn("c2", "https://b.example"),
        ],
        capable=lambda c: c.id != "c2",
    )
    assert (await reach.bootstrap_connection(None)).id == "c1"
    # A named connection that can't relay is not chosen.
    assert (await reach.bootstrap_connection("c2")).id == "c1"
    assert (await reach.bootstrap_connection("nope")).id == "c1"


async def test_bootstrap_connection_without_a_relay_capable_server(caplog):
    def _boom(conn):
        raise RuntimeError("info fetch exploded")

    reach = _reach([_conn("c1", "https://a.example")], capable=_boom)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(GfsNotConnectedError):
            await reach.bootstrap_connection(None)
    assert "capability check failed" in caplog.text
    with pytest.raises(GfsNotConnectedError):
        await _reach([]).bootstrap_connection(None)


async def test_shared_connection_matches_the_pinned_id_first():
    reach = _reach(
        [
            _conn("c1", "https://g.example", gid="other"),
            _conn("c2", "https://moved.example", gid="gfs-g"),
        ]
    )
    shared = await reach.shared_connection(
        {"url": "https://g.example", "instance_id": "gfs-g"}
    )
    assert shared is not None and shared.id == "c2"


async def test_shared_connection_falls_back_to_the_normalized_url():
    reach = _reach([_conn("c1", "https://G.example", gid="pinned-else")])
    shared = await reach.shared_connection(
        {"url": "https://g.example/", "instance_id": "gfs-g"}
    )
    assert shared is not None and shared.id == "c1"


@pytest.mark.parametrize(
    "block",
    [
        None,
        "https://g.example",
        {},
        {"url": "https://h.example", "instance_id": "gfs-h"},
        {"url": 5, "instance_id": 7},
    ],
)
async def test_shared_connection_none_when_not_on_that_server(block):
    reach = _reach([_conn("c1", "https://g.example", gid="gfs-g")])
    assert await reach.shared_connection(block) is None


async def test_shared_connection_ignores_a_server_that_cannot_relay():
    reach = _reach(
        [_conn("c1", "https://g.example", gid="gfs-g")],
        capable=lambda c: False,
    )
    assert (
        await reach.shared_connection(
            {"url": "https://g.example", "instance_id": "gfs-g"}
        )
        is None
    )


async def test_send_sealed_goes_through_the_named_connection():
    sender = _Sender()
    reach = _reach(
        [_conn("c1", "https://a.example"), _conn("c2", "https://b.example")],
        sender=sender,
    )
    envelope = {"to_instance": "p", "sealed": {"x": 1}}
    assert await reach.send_sealed(
        to_instance_id="p", envelope=envelope, gfs_connection_id="c2"
    )
    assert sender.calls == [("p", envelope, "https://b.example")]


async def test_send_sealed_false_when_the_connection_is_gone(caplog):
    sender = _Sender()
    reach = _reach(
        [_conn("c1", "https://a.example", status="suspended")], sender=sender
    )
    with caplog.at_level(logging.WARNING):
        assert not await reach.send_sealed(
            to_instance_id="p", envelope={}, gfs_connection_id="c1"
        )
    assert sender.calls == []
    assert "no longer active" in caplog.text


@pytest.mark.parametrize(
    "sender",
    [_Sender(result=False), _Sender(raises=RuntimeError("throttled"))],
)
async def test_send_sealed_false_when_the_relay_refuses(sender, caplog):
    reach = _reach([_conn("c1", "https://a.example")], sender=sender)
    with caplog.at_level(logging.WARNING):
        assert not await reach.send_sealed(
            to_instance_id="p", envelope={}, gfs_connection_id="c1"
        )
    assert "https://a.example" in caplog.text


async def test_probe_is_best_effort(caplog):
    seen: list[str] = []

    async def _probe(iid: str) -> int:
        seen.append(iid)
        return 1

    await _reach(probe=_probe).probe("peer")
    assert seen == ["peer"]
    await _reach().probe("peer")  # no discovery wired: a no-op

    async def _boom(iid: str) -> int:
        raise RuntimeError("nope")

    with caplog.at_level(logging.WARNING):
        await _reach(probe=_boom).probe("peer")
    assert "route probe for peer failed" in caplog.text
