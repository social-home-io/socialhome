"""The household pairing handshake is a signed-in admin action.

``/api/pairing/initiate``, ``/accept`` and ``/confirm`` are the admin's own
SPA steps for pairing with another household (a trust decision for the whole
household). The peer's side of the handshake never uses them — it arrives
through the envelope-signed federation inbox. So none of them may sit on the
auth middleware's public-path list, and an unauthenticated call must be
refused before any pairing state exists or any outbound request is made.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    event_bus_key,
    federation_repo_key,
    federation_service_key,
)
from socialhome.auth import (
    _DEFAULT_PUBLIC_PATH_PATTERNS,
    _DEFAULT_PUBLIC_PATHS,
    sha256_token_hash,
)
from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.domain.federation import (
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.peer_pairing_client import sign_peer_body

pytestmark = pytest.mark.security


_HANDSHAKE_PATHS = (
    "/api/pairing/initiate",
    "/api/pairing/accept",
    "/api/pairing/confirm",
)


def test_pairing_handshake_is_not_a_public_path():
    for path in _HANDSHAKE_PATHS:
        assert not any(path.startswith(p) for p in _DEFAULT_PUBLIC_PATHS), path
    assert not any("pairing" in p for p in _DEFAULT_PUBLIC_PATH_PATTERNS)


class _RecordingPeerClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_peer_accept(self, *, peer_inbox_url, body):
        self.calls.append(peer_inbox_url)
        raise AssertionError("unauthenticated accept reached the network")

    async def send_peer_confirm(self, *, peer_inbox_url, body):
        self.calls.append(peer_inbox_url)
        raise AssertionError("unauthenticated confirm reached the network")


@pytest.fixture
async def anon_client(aiohttp_client, tmp_dir):
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        apps_path=str(tmp_dir / "apps"),
        mode="standalone",
        log_level="WARNING",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {
                "standalone": MappingProxyType(
                    {"external_url": "https://test.example"},
                ),
            },
        ),
    )
    return await aiohttp_client(create_app(cfg))


@pytest.mark.parametrize("path", _HANDSHAKE_PATHS)
async def test_unauthenticated_handshake_step_is_refused(anon_client, path):
    rec = _RecordingPeerClient()
    app = anon_client.server.app
    app[federation_service_key]._pairing.attach_peer_pairing_client(rec)
    peer = generate_identity_keypair()
    peer_id = derive_instance_id(peer.public_key)
    body = {
        "token": "tok-anon",
        "instance_id": peer_id,
        "identity_pk": peer.public_key.hex(),
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": "https://peer.example/federation/inbox/abc",
        "verification_code": "000000",
    }
    r = await anon_client.post(path, json=body)
    assert r.status == 401
    repo = app[federation_repo_key]
    assert await repo.get_instance(peer_id) is None
    assert await repo.get_pairing("tok-anon") is None
    assert rec.calls == []


# ─── Every household-trust mutation is admin-only ──────────────────────────
#
# Pairing, introductions, transitive auto-pair, unpairing and per-peer
# settings all change which households this one trusts, so each is a
# decision for a household admin. Walk every write method mounted under the
# pairing / connections prefixes (a route added later is covered without
# editing this test) and prove an anonymous caller gets 401, a signed-in
# non-admin gets 403, and neither touches a peer row or the network.

_TRUST_PREFIXES = ("/api/pairing", "/api/connections")
_WRITE_METHODS = ("post", "put", "patch", "delete")
_SEEDED_PEER = "peer-trust-boundary"


def _trust_mutations(app) -> list[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.router.routes():
        info = route.resource.get_info() if route.resource is not None else {}
        path = info.get("path") or info.get("formatter") or ""
        if not path.startswith(_TRUST_PREFIXES):
            continue
        for meth in _WRITE_METHODS:
            if callable(getattr(route.handler, meth, None)):
                out.add((meth, path))
    return sorted(out)


def _concrete(path: str) -> str:
    return (
        path.replace("{instance_id}", _SEEDED_PEER)
        .replace("{request_id}", "req-x")
        .replace("{id}", "req-x")
    )


async def _seed_non_admin(db, n: int) -> str:
    # A fresh user per call: the pairing prefix has a tight per-user rate
    # limit, and a 429 would mask the status under test.
    username = f"member{n}"
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name, is_admin) VALUES(?,?,?,0)",
        (username, f"{username}-id", username),
    )
    raw = f"{username}-raw-tok"
    await db.enqueue(
        "INSERT INTO api_tokens(token_id, user_id, label, token_hash) VALUES(?,?,?,?)",
        (f"tok-{username}", f"{username}-id", "t", sha256_token_hash(raw)),
    )
    return raw


def _seeded_peer() -> RemoteInstance:
    return RemoteInstance(
        id=_SEEDED_PEER,
        display_name="Peer",
        remote_identity_pk="aa" * 32,
        key_self_to_remote="enc",
        key_remote_to_self="enc",
        remote_inbox_url="https://peer.example/federation/inbox/x",
        local_inbox_id="wh-x",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
    )


def test_trust_mutation_walk_finds_the_known_routes(anon_client):
    found = _trust_mutations(anon_client.server.app)
    for expected in (
        ("post", "/api/pairing/initiate"),
        ("post", "/api/pairing/introduce"),
        ("post", "/api/pairing/auto-pair-via"),
        ("delete", "/api/pairing/connections/{instance_id}"),
        ("patch", "/api/pairing/connections/{instance_id}"),
        ("patch", "/api/pairing/connections/{instance_id}/alias"),
    ):
        assert expected in found, expected


async def test_every_trust_mutation_refuses_anonymous_and_non_admin(
    anon_client, monkeypatch
):
    app = anon_client.server.app
    repo = app[federation_repo_key]
    await repo.save_instance(_seeded_peer())
    fed = app[federation_service_key]
    sent: list[object] = []

    async def _no_send(self, **kwargs):
        sent.append(kwargs)
        raise AssertionError("refused call reached the federation layer")

    # ``FederationService`` is slotted — patch the class (undone on teardown).
    monkeypatch.setattr(type(fed), "send_event", _no_send)
    body = {
        "target_instance_id": "target-x",
        "via_instance_id": _SEEDED_PEER,
        "share_home": True,
        "alias": "renamed",
    }

    for n, (meth, path) in enumerate(_trust_mutations(app)):
        url = _concrete(path)
        anon = await anon_client.request(meth.upper(), url, json=body)
        assert anon.status == 401, (meth, path, anon.status)

        tok = await _seed_non_admin(app[db_key], n)
        r = await anon_client.request(
            meth.upper(),
            url,
            json=body,
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status == 403, (meth, path, r.status)

    inst = await repo.get_instance(_SEEDED_PEER)
    assert inst is not None
    assert inst.status is PairingStatus.CONFIRMED
    assert inst.local_alias is None
    assert sent == []


# ─── Only this household's own verification step confirms a pairing ────────
#
# After the scanner's signed peer-accept lands, the initiator stores the
# scanner's row as PENDING_RECEIVED *with* keys — so the scanner already
# passes the inbound signature check before the local admin has compared
# the SAS. A federation-inbox ``PAIRING_CONFIRM`` from that pending peer
# must never flip the row: the pairing is confirmed by the admin entering
# the code, nothing else. ``PAIRING_ABORT`` likewise only cancels the
# sender's own pending session.


async def _dispatch(app, event_type, payload, *, from_instance: str) -> None:
    handlers = app[federation_service_key]._event_registry.handlers_for(event_type)
    assert handlers
    for handler in handlers:
        await handler(
            FederationEvent(
                msg_id=f"m-{event_type.value}",
                event_type=event_type,
                from_instance=from_instance,
                to_instance="us",
                timestamp=datetime.now(timezone.utc).isoformat(),
                payload=payload,
            )
        )


async def _peer_accepted(app) -> tuple[str, str, str]:
    """Walk initiate + the scanner's signed peer-accept on the initiator.

    Returns ``(token, verification_code, scanner_instance_id)``.
    """
    coord = app[federation_service_key]._pairing
    qr = await coord.initiate("https://test.example/federation/inbox")
    scanner = generate_identity_keypair()
    scanner_id = derive_instance_id(scanner.public_key)
    body = sign_peer_body(
        {
            "token": qr["token"],
            "verification_code": "424242",
            "identity_pk": scanner.public_key.hex(),
            "instance_id": scanner_id,
            "dh_pk": generate_x25519_keypair().public_key.hex(),
            "inbox_url": "https://scanner.example/federation/inbox/s1",
            "display_name": "Scanner",
        },
        own_identity_seed=scanner.private_key,
    )
    await coord.handle_peer_accept(body)
    inst = await app[federation_repo_key].get_instance(scanner_id)
    assert inst is not None and inst.status is PairingStatus.PENDING_RECEIVED
    assert inst.remote_identity_pk  # keyed — would pass the §24.11 lookup
    return qr["token"], "424242", scanner_id


@pytest.mark.parametrize(
    "payload",
    [{}, {"token": "ignored"}, {"verification_code": "424242"}],
    ids=["bare", "with-token", "with-code"],
)
async def test_pending_peer_cannot_confirm_itself_over_the_inbox(anon_client, payload):
    app = anon_client.server.app
    token, _code, scanner_id = await _peer_accepted(app)
    confirmed: list[PairingConfirmed] = []
    app[event_bus_key].subscribe(PairingConfirmed, confirmed.append)

    await _dispatch(
        app, FederationEventType.PAIRING_CONFIRM, payload, from_instance=scanner_id
    )

    repo = app[federation_repo_key]
    inst = await repo.get_instance(scanner_id)
    assert inst is not None
    assert inst.status is PairingStatus.PENDING_RECEIVED
    assert await repo.get_pairing(token) is not None
    assert confirmed == []


async def test_admin_entering_the_code_still_confirms(anon_client):
    """Control: the local verification step is what confirms the pair."""
    app = anon_client.server.app
    token, code, scanner_id = await _peer_accepted(app)
    await app[federation_service_key]._pairing.confirm(token, code)
    inst = await app[federation_repo_key].get_instance(scanner_id)
    assert inst is not None
    assert inst.status is PairingStatus.CONFIRMED


async def test_abort_cannot_cancel_another_peers_pending_session(anon_client):
    app = anon_client.server.app
    token, _code, scanner_id = await _peer_accepted(app)
    repo = app[federation_repo_key]
    await repo.save_instance(_seeded_peer())  # a different, confirmed peer

    await _dispatch(
        app,
        FederationEventType.PAIRING_ABORT,
        {"token": token, "reason": "x"},
        from_instance=_SEEDED_PEER,
    )

    assert await repo.get_pairing(token) is not None
    inst = await repo.get_instance(scanner_id)
    assert inst is not None and inst.status is PairingStatus.PENDING_RECEIVED
    other = await repo.get_instance(_SEEDED_PEER)
    assert other is not None and other.status is PairingStatus.CONFIRMED


async def test_abort_from_the_sessions_own_peer_still_cancels(anon_client):
    """Control: the peer the session is with can still cancel it."""
    app = anon_client.server.app
    token, _code, scanner_id = await _peer_accepted(app)
    await _dispatch(
        app,
        FederationEventType.PAIRING_ABORT,
        {"token": token, "reason": "declined"},
        from_instance=scanner_id,
    )
    repo = app[federation_repo_key]
    assert await repo.get_pairing(token) is None
    assert await repo.get_instance(scanner_id) is None
