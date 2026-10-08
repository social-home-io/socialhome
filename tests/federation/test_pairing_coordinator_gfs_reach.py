"""PairingCoordinator — pairing codes with a GFS ``reach``.

Edge cases of the coordinator itself (the two-household round trip over
the relay is ``tests/protocol/test_gfs_pairing_reach.py``). Real SQLite and
real crypto; the HTTPS inbox and the relay are recording stand-ins.
"""

from __future__ import annotations

import json
import os

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    GfsConnection,
    GfsNotConnectedError,
    PairingKeywrapInvalidError,
    PairingReachInvalidError,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.federation.keywrap_seal import KEM_SUITE_X25519, open_keywrap
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.federation.pairing_gfs_reach import PairingGfsReach
from socialhome.federation.peer_pairing_client import (
    PeerPairingClient,
    pairing_body_from_relay,
    sign_peer_body,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo

URL_G = "https://gfs-g.example.org"


class _Resp:
    def __init__(self, status: int) -> None:
        self.status = status

        class _C:
            async def read(self, _n):
                return b""

        self.content = _C()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _Http:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[str] = []

    def post(self, url, **_kw):
        self.posts.append(url)
        return _Resp(self.status)


class _Relay:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict, str]] = []

    async def send_sealed_envelope(self, *, to_instance_id, envelope, gfs_url=""):
        self.sent.append((to_instance_id, envelope, gfs_url))
        return True


class _Side:
    """One household: coordinator + repos + its stand-in wires."""

    def __init__(self, db, *, http_status=200, on_g=True) -> None:
        self.db = db
        self.ident = generate_identity_keypair()
        self.iid = derive_instance_id(self.ident.public_key)
        self.keywrap = generate_x25519_keypair()
        self.repo = SqliteFederationRepo(db)
        self.gfs_repo = SqliteGfsConnectionRepo(db)
        self.http = _Http(http_status)
        self.relay = _Relay()
        self.probed: list[str] = []
        self.on_g = on_g
        self.coord = PairingCoordinator(
            self.repo, KeyManager(os.urandom(32)), self.ident.public_key
        )

        async def _factory():
            return self.http

        self.coord.attach_peer_pairing_client(
            PeerPairingClient(
                own_identity_seed=self.ident.private_key, client_factory=_factory
            )
        )

        async def _capable(_c):
            return True

        async def _probe(iid):
            self.probed.append(iid)
            return 0

        self.coord.attach_gfs_reach(
            PairingGfsReach(
                gfs_connection_repo=self.gfs_repo,
                envelope_relay_supported=_capable,
                relay_sender=self.relay,
                keywrap_public_key=self.keywrap.public_key,
                keywrap_sig=b64url_encode(
                    sign_ed25519(self.ident.private_key, self.keywrap.public_key)
                ),
                probe_peer=_probe,
            )
        )

    async def connect(self, name: str) -> str:
        cid = f"{name}-g"
        await self.gfs_repo.save(
            GfsConnection(
                id=cid,
                gfs_instance_id="gfs-g",
                display_name="G",
                public_key="ab" * 32,
                inbox_url=URL_G,
                status="active",
                paired_at="2026-01-01T00:00:00+00:00",
            )
        )
        return cid


@pytest.fixture
async def sides(tmp_path):
    made = []

    async def make(name, **kw):
        db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
        await db.startup()
        side = _Side(db, **kw)
        side.conn_id = await side.connect(name) if kw.get("on_g", True) else None
        made.append(side)
        return side

    yield make
    for s in made:
        await s.db.shutdown()


def _open(side: _Side, envelope: dict) -> dict:
    plain = json.loads(
        open_keywrap(
            sealed=envelope["sealed"], recipient_keywrap_priv=side.keywrap.private_key
        )
    )
    return pairing_body_from_relay(plain)[1]


# ── initiate ──


async def test_a_url_code_carries_no_gfs_information(sides):
    a = await sides("a")
    qr = await a.coord.initiate("https://a.example/inbox")
    assert not {"reach", "gfs", "keywrap_pk", "keywrap_sig", "keywrap_suite"} & set(qr)
    assert "proto_version" not in qr
    session = await a.repo.get_pairing(qr["token"])
    assert session.relay_via is None


async def test_a_gfs_code_names_one_server_and_our_keywrap_key(sides):
    a = await sides("a")
    qr = await a.coord.initiate("https://a.example/inbox", reach="gfs")
    assert qr["inbox_url"] == ""  # the base is ignored
    assert qr["reach"] == "gfs"
    assert qr["gfs"] == {"url": URL_G, "instance_id": "gfs-g"}
    assert qr["keywrap_pk"] == a.keywrap.public_key.hex()
    assert qr["keywrap_suite"] == KEM_SUITE_X25519
    assert qr["proto_version"] == OURS
    session = await a.repo.get_pairing(qr["token"])
    assert session.relay_via == a.conn_id and session.inbox_url == ""


async def test_initiate_refusals(sides):
    a = await sides("a")
    with pytest.raises(PairingReachInvalidError):
        await a.coord.initiate("https://a.example/inbox", reach="mesh")
    with pytest.raises(ValueError, match="federation base"):
        await a.coord.initiate(None, reach="url_gfs")
    with pytest.raises(ValueError, match="federation base"):
        await a.coord.initiate("")
    bare = PairingCoordinator(a.repo, KeyManager(os.urandom(32)), a.ident.public_key)
    with pytest.raises(GfsNotConnectedError):
        await bare.initiate(None, reach="gfs")
    nowhere = await sides("n", on_g=False)
    with pytest.raises(GfsNotConnectedError):
        await nowhere.coord.initiate(None, reach="gfs")


# ── accept (scanner) ──


async def test_a_scanner_with_a_url_but_not_on_the_gfs_still_pairs_url_gfs(sides):
    a = await sides("a")
    b = await sides("b", on_g=False)
    qr = await a.coord.initiate("https://a.example/inbox", reach="url_gfs")

    await b.coord.accept(qr, "https://b.example/inbox")

    assert b.http.posts == [qr["inbox_url"]]
    row = await b.repo.get_instance(a.iid)
    assert row.gfs_relay is True
    assert row.remote_keywrap_pk == a.keywrap.public_key.hex()
    assert await b.repo.list_gfs_routes(a.iid) == []  # not on G: nothing seeded


async def test_a_gfs_code_with_an_inbox_url_is_refused(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    with pytest.raises(ValueError, match="must not carry an inbox_url"):
        await b.coord.accept({**qr, "inbox_url": "https://x.example/i"}, None)
    assert await b.repo.get_instance(a.iid) is None


async def test_an_unbound_keywrap_key_in_the_code_fails_closed(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    forged = {**qr, "keywrap_pk": generate_x25519_keypair().public_key.hex()}
    with pytest.raises(PairingKeywrapInvalidError):
        await b.coord.accept(forged, None)
    assert await b.repo.get_instance(a.iid) is None
    assert b.relay.sent == [] and b.http.posts == []


async def test_an_unknown_reach_in_the_code_fails_closed(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    with pytest.raises(PairingReachInvalidError):
        await b.coord.accept({**qr, "reach": "carrier-pigeon"}, None)


async def test_a_failed_inbox_post_falls_back_to_the_bootstrap_gfs(sides):
    a = await sides("a")
    b = await sides("b", http_status=503)
    qr = await a.coord.initiate("https://a.example/inbox", reach="url_gfs")

    await b.coord.accept(qr, "https://b.example/inbox")

    assert b.http.posts == [qr["inbox_url"]]
    [(to, envelope, url)] = b.relay.sent
    assert (to, url) == (a.iid, URL_G)
    inner = _open(a, envelope)
    assert inner["event_type"] == "pairing_peer_accept"
    assert inner["keywrap_suite"] == KEM_SUITE_X25519
    assert inner["inbox_url"].startswith("https://b.example/inbox/")


@pytest.mark.parametrize("bogus", [True, 0, OURS + 1, "53", None])
async def test_a_bogus_stated_proto_version_is_ignored(sides, bogus):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    await b.coord.accept({**qr, "proto_version": bogus}, None)
    assert (await b.repo.get_instance(a.iid)).proto_version == 1


# ── handle_peer_accept (code owner) ──


def _accept_body(side: _Side, token: str, *, inbox_url="", keywrap=True) -> dict:
    body = {
        "event_type": "pairing_peer_accept",
        "token": token,
        "verification_code": "123456",
        "identity_pk": side.ident.public_key.hex(),
        "instance_id": side.iid,
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": inbox_url,
        "display_name": "B",
        "sig_suite": "ed25519",
    }
    if keywrap:
        body.update(
            keywrap_pk=side.keywrap.public_key.hex(),
            keywrap_sig=b64url_encode(
                sign_ed25519(side.ident.private_key, side.keywrap.public_key)
            ),
            keywrap_suite=KEM_SUITE_X25519,
        )
    return sign_peer_body(body, own_identity_seed=side.ident.private_key)


async def test_a_relayed_accept_for_a_classic_code_is_refused(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate("https://a.example/inbox")
    with pytest.raises(ValueError, match="pairing's GFS"):
        await a.coord.handle_peer_accept(
            _accept_body(b, qr["token"], inbox_url="https://b.example/i/x"),
            relayed_via=a.conn_id,
        )
    assert await a.repo.get_instance(b.iid) is None


async def test_an_accept_without_url_or_keywrap_key_is_refused(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    with pytest.raises(ValueError, match="no inbox_url and no"):
        await a.coord.handle_peer_accept(
            _accept_body(b, qr["token"], keywrap=False),
            relayed_via=a.conn_id,
        )
    assert await a.repo.get_instance(b.iid) is None


async def test_an_accept_with_an_unbound_keywrap_key_fails_closed(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    body = _accept_body(b, qr["token"], keywrap=False)
    body = sign_peer_body(
        {
            **{k: v for k, v in body.items() if k != "signature"},
            "keywrap_pk": generate_x25519_keypair().public_key.hex(),
            "keywrap_sig": "AAAA",
            "keywrap_suite": KEM_SUITE_X25519,
        },
        own_identity_seed=b.ident.private_key,
    )
    with pytest.raises(PairingKeywrapInvalidError):
        await a.coord.handle_peer_accept(body, relayed_via=a.conn_id)
    assert await a.repo.get_instance(b.iid) is None


async def test_a_relayed_accept_seats_the_relay_and_seeds_the_route(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    body = _accept_body(b, qr["token"])
    body = sign_peer_body(
        {**{k: v for k, v in body.items() if k != "signature"}, "proto_version": OURS},
        own_identity_seed=b.ident.private_key,
    )
    result = await a.coord.handle_peer_accept(body, relayed_via=a.conn_id)
    assert result["ok"] is True and result["replay"] is False

    row = await a.repo.get_instance(b.iid)
    assert row.status is PairingStatus.PENDING_RECEIVED
    assert row.remote_inbox_url == ""
    assert row.gfs_relay is True and row.proto_version == OURS
    assert [r.gfs_connection_id for r in await a.repo.list_gfs_routes(b.iid)] == [
        a.conn_id
    ]
    # A retried relayed accept is the idempotent replay, not a rebuild.
    again = await a.coord.handle_peer_accept(body, relayed_via=a.conn_id)
    assert again["replay"] is True


# ── confirm / handle_peer_confirm ──


async def test_a_failed_confirm_post_falls_back_to_the_relay_and_probes(sides):
    a = await sides("a", http_status=503)
    b = await sides("b")
    qr = await a.coord.initiate("https://a.example/inbox", reach="url_gfs")
    await a.coord.handle_peer_accept(
        _accept_body(b, qr["token"], inbox_url="https://b.example/inbox/x"),
    )

    confirmed = await a.coord.confirm(qr["token"], "123456")

    assert confirmed.gfs_relay is True
    assert confirmed.remote_keywrap_pk == b.keywrap.public_key.hex()
    assert a.http.posts == ["https://b.example/inbox/x"]
    [(to, envelope, url)] = a.relay.sent
    assert (to, url) == (b.iid, URL_G)
    assert _open(b, envelope)["event_type"] == "pairing_peer_confirm"
    assert a.probed == [b.iid]


async def test_a_relayed_confirm_through_another_connection_is_refused(sides):
    a = await sides("a")
    b = await sides("b")
    qr = await a.coord.initiate(None, reach="gfs")
    await b.coord.accept(qr, None)
    body = sign_peer_body(
        {
            "event_type": "pairing_peer_confirm",
            "token": qr["token"],
            "instance_id": a.iid,
        },
        own_identity_seed=a.ident.private_key,
    )
    with pytest.raises(ValueError, match="pairing's GFS"):
        await b.coord.handle_peer_confirm(body, relayed_via="some-other-connection")
    assert (await b.repo.get_instance(a.iid)).status is PairingStatus.PENDING_RECEIVED
    assert b.probed == []

    await b.coord.handle_peer_confirm(body, relayed_via=b.conn_id)
    row = await b.repo.get_instance(a.iid)
    assert row.status is PairingStatus.CONFIRMED
    assert row.gfs_relay is True and row.remote_keywrap_pk is not None
    assert b.probed == [a.iid]
