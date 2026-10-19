"""Regression tests for the §11 pairing handshake.

* Both sides of a pair derive *the same* AES-256-GCM session keys for
  each direction so encrypt-on-A round-trips through decrypt-on-B (and
  vice-versa).
* The QR-scanner side (``accept``) tells the QR-issuer side
  (``initiate``) where to route subsequent envelopes back to it. Without
  the scanner's own inbox URL in the peer-accept body, A's
  ``RemoteInstance.remote_inbox_url`` would echo back to A itself and
  every outbound envelope would 404 / time out.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.federation.peer_pairing_client import (
    sign_peer_body,
)
from socialhome.infrastructure.key_manager import KeyManager


class _FakeRepo:
    """In-memory ``AbstractFederationRepo`` good enough for handshake tests."""

    def __init__(self, local_identity: dict | None = None) -> None:
        self.instances: dict = {}
        self.pairings: dict = {}
        self._local_identity = local_identity

    async def save_instance(self, inst):
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid):
        return self.instances.get(iid)

    async def create_pairing(self, session):
        self.pairings[session.token] = session

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def update_pairing(self, session):
        self.pairings[session.token] = session

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)

    async def get_local_identity(self) -> dict | None:
        return self._local_identity


def _kek() -> KeyManager:
    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


async def test_directional_keys_match_between_initiator_and_acceptor():
    """A's outbound key must equal B's inbound key (and vice versa).

    Pre-fix, both sides derived two keys with the same HKDF info strings,
    so every outbound envelope failed to decrypt on the receiving end.
    """
    kek_a = _kek()
    kek_b = _kek()
    coord_a = PairingCoordinator(
        _FakeRepo(), kek_a, generate_identity_keypair().public_key
    )
    coord_b = PairingCoordinator(
        _FakeRepo(), kek_b, generate_identity_keypair().public_key
    )

    a_dh = generate_x25519_keypair()
    b_dh = generate_x25519_keypair()

    a_self_enc, a_remote_enc = coord_a._derive_directional_keys(
        own_dh_sk=a_dh.private_key,
        peer_dh_pk=b_dh.public_key,
        is_initiator=True,
    )
    b_self_enc, b_remote_enc = coord_b._derive_directional_keys(
        own_dh_sk=b_dh.private_key,
        peer_dh_pk=a_dh.public_key,
        is_initiator=False,
    )

    # Each side stores keys encrypted under its own KEK; round-trip the
    # plaintext for the comparison.
    a_self = kek_a.decrypt(a_self_enc)
    a_remote = kek_a.decrypt(a_remote_enc)
    b_self = kek_b.decrypt(b_self_enc)
    b_remote = kek_b.decrypt(b_remote_enc)

    # A→B: encrypt with A.self_to_remote, decrypt with B.remote_to_self.
    assert a_self == b_remote
    # B→A: encrypt with B.self_to_remote, decrypt with A.remote_to_self.
    assert b_self == a_remote
    # The two directions use distinct keys.
    assert a_self != a_remote


async def test_accept_carries_acceptor_inbox_url_in_peer_accept_body():
    """B's peer-accept response must advertise *B's* inbox URL, not A's.

    Without the ``own_inbox_base_url`` argument the body echoes back the
    QR's inbox URL (A's) and A would route every subsequent envelope to
    itself.
    """
    captured: list[dict] = []

    class _RecordingPeerClient:
        async def send_peer_accept(self, *, peer_inbox_url, body):
            captured.append({"peer_inbox_url": peer_inbox_url, "body": body})

            class _R:
                ok = True
                status_code = 200
                error = None

            return _R()

    kp = generate_identity_keypair()
    coord = PairingCoordinator(_FakeRepo(), _kek(), kp.public_key)
    coord.attach_peer_pairing_client(_RecordingPeerClient())

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    a_inbox_url = "https://alpha.example/federation/inbox/A-INBOX-ID"
    qr = {
        "token": "tok-acc-1",
        "instance_id": derive_instance_id(peer_kp.public_key),
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "inbox_url": a_inbox_url,
    }
    own_base = "https://beta.example/federation/inbox"

    await coord.accept(qr, own_inbox_base_url=own_base)

    assert len(captured) == 1
    body = captured[0]["body"]
    assert body["inbox_url"].startswith(own_base + "/")
    # And that URL is not the QR's URL.
    assert body["inbox_url"] != a_inbox_url
    # Peer-accept itself is POSTed *to* A — the destination URL is the QR's.
    assert captured[0]["peer_inbox_url"] == a_inbox_url


async def test_accept_peer_accept_body_carries_home_coords_when_set():
    """accept() (B-side) includes home_lat/home_lon in the peer-accept
    body when the local identity has coordinates set, so A's RemoteInstance
    row learns B's home location immediately on pairing."""
    captured: list[dict] = []

    class _RecordingPeerClient:
        async def send_peer_accept(self, *, peer_inbox_url, body):
            captured.append({"peer_inbox_url": peer_inbox_url, "body": body})

            class _R:
                ok = True
                status_code = 200
                error = None

            return _R()

    kp = generate_identity_keypair()
    repo = _FakeRepo(local_identity={"home_lat": 52.52, "home_lon": 13.40})
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    coord.attach_peer_pairing_client(_RecordingPeerClient())

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr = {
        "token": "tok-coords-1",
        "instance_id": derive_instance_id(peer_kp.public_key),
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "inbox_url": "https://alpha.example/federation/inbox/A-INBOX",
    }

    await coord.accept(
        qr,
        own_inbox_base_url="https://beta.example/federation/inbox",
        share_home=True,
    )

    assert len(captured) == 1
    body = captured[0]["body"]
    assert body.get("home_lat") == 52.52
    assert body.get("home_lon") == 13.40


async def test_accept_peer_accept_body_omits_home_coords_when_none():
    """accept() omits home_lat/home_lon from the body when the local
    identity has NULL coords (not yet configured)."""
    captured: list[dict] = []

    class _RecordingPeerClient:
        async def send_peer_accept(self, *, peer_inbox_url, body):
            captured.append(body)

            class _R:
                ok = True
                status_code = 200
                error = None

            return _R()

    kp = generate_identity_keypair()
    repo = _FakeRepo(local_identity={"home_lat": None, "home_lon": None})
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    coord.attach_peer_pairing_client(_RecordingPeerClient())

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr = {
        "token": "tok-no-coords",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "inbox_url": "https://alpha.example/federation/inbox/A-INBOX2",
    }

    await coord.accept(qr)

    assert len(captured) == 1
    assert "home_lat" not in captured[0]
    assert "home_lon" not in captured[0]


async def test_handle_peer_accept_populates_home_coords_on_remote_instance():
    """handle_peer_accept() (A-side) reads home_lat/home_lon from the
    incoming body and stores them on the new RemoteInstance row."""
    kp_a = generate_identity_keypair()
    kp_b = generate_identity_keypair()
    dh_b = generate_x25519_keypair()

    repo_a = _FakeRepo()
    coord_a = PairingCoordinator(repo_a, _kek(), kp_a.public_key)

    # A initiates — this creates a PENDING_SENT PairingSession.
    qr = await coord_a.initiate(inbox_base_url="https://a.example/federation/inbox")
    token = qr["token"]

    # Build the peer-accept body as B would send it, including home coords.
    peer_accept: dict = {
        "token": token,
        "verification_code": "123456",
        "identity_pk": kp_b.public_key.hex(),
        "instance_id": derive_instance_id(kp_b.public_key),
        "dh_pk": dh_b.public_key.hex(),
        "inbox_url": "https://b.example/federation/inbox/B-LOCAL",
        "display_name": "Household B",
        "sig_suite": "ed25519",
        "home_lat": 52.52,
        "home_lon": 13.40,
    }
    signed_body = sign_peer_body(peer_accept, own_identity_seed=kp_b.private_key)

    await coord_a.handle_peer_accept(signed_body)

    b_iid = derive_instance_id(kp_b.public_key)
    stored = repo_a.instances.get(b_iid)
    assert stored is not None
    assert stored.home_lat == 52.52
    assert stored.home_lon == 13.40


async def test_handle_peer_accept_home_coords_none_when_absent_from_body():
    """handle_peer_accept() stores None for home_lat/home_lon when the
    body doesn't include those fields (older peers / coords not set)."""
    kp_a = generate_identity_keypair()
    kp_b = generate_identity_keypair()
    dh_b = generate_x25519_keypair()

    repo_a = _FakeRepo()
    coord_a = PairingCoordinator(repo_a, _kek(), kp_a.public_key)

    qr = await coord_a.initiate(inbox_base_url="https://a.example/federation/inbox")
    token = qr["token"]

    peer_accept: dict = {
        "token": token,
        "verification_code": "654321",
        "identity_pk": kp_b.public_key.hex(),
        "instance_id": derive_instance_id(kp_b.public_key),
        "dh_pk": dh_b.public_key.hex(),
        "inbox_url": "https://b.example/federation/inbox/B-LOCAL2",
        "display_name": "Household B",
        "sig_suite": "ed25519",
    }
    signed_body = sign_peer_body(peer_accept, own_identity_seed=kp_b.private_key)

    await coord_a.handle_peer_accept(signed_body)

    b_iid = derive_instance_id(kp_b.public_key)
    stored = repo_a.instances.get(b_iid)
    assert stored is not None
    assert stored.home_lat is None
    assert stored.home_lon is None
