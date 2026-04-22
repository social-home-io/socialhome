"""§25.8 migration: hybrid-suite pairing hand-shake tests.

Verifies the QR payload carries ``pq_identity_pk`` + ``sig_suite``,
that the intersection negotiation picks the right suite for every
classical / hybrid peer combination, and that the stored
:class:`RemoteInstance` preserves the PQ material across confirm.
"""

from __future__ import annotations

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.infrastructure.key_manager import KeyManager


class _FakeRepo:
    def __init__(self) -> None:
        self.instances: dict = {}
        self.pairings: dict = {}

    async def save_instance(self, inst):
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid):
        return self.instances.get(iid)

    async def create_pairing(self, session):
        self.pairings[session.token] = session

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)


def _kek() -> KeyManager:
    import tempfile
    from pathlib import Path

    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


async def test_initiate_hybrid_carries_pq_pk_in_qr_payload():
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(
        repo,
        _kek(),
        kp.public_key,
        own_pq_pk=b"FAKE-PQ-PK",
        own_sig_suite="ed25519+mldsa65",
    )
    payload = await coord.initiate(webhook_url="https://x/wh")
    assert payload["sig_suite"] == "ed25519+mldsa65"
    assert payload["pq_algorithm"] == "mldsa65"
    assert payload["pq_identity_pk"] == b"FAKE-PQ-PK".hex()


async def test_initiate_classical_omits_pq_fields():
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    payload = await coord.initiate(webhook_url="https://x/wh")
    assert payload["sig_suite"] == "ed25519"
    assert "pq_algorithm" not in payload
    assert "pq_identity_pk" not in payload


async def test_accept_negotiates_hybrid_when_both_sides_support_it():
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(
        repo,
        _kek(),
        kp.public_key,
        own_pq_pk=b"LOCAL-PQ",
        own_sig_suite="ed25519+mldsa65",
    )
    # Craft a QR scan payload from a hybrid peer.
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr = {
        "token": "tok-1",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "https://peer/wh",
        "sig_suite": "ed25519+mldsa65",
        "pq_algorithm": "mldsa65",
        "pq_identity_pk": "aa" * 32,
    }
    result = await coord.accept(qr)
    peer_iid = derive_instance_id(peer_kp.public_key)
    stored = repo.instances[peer_iid]
    assert stored.sig_suite == "ed25519+mldsa65"
    assert stored.remote_pq_algorithm == "mldsa65"
    assert stored.remote_pq_identity_pk == "aa" * 32
    assert result["verification_code"]


async def test_accept_falls_back_to_classical_when_peer_is_classical():
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(
        repo,
        _kek(),
        kp.public_key,
        own_pq_pk=b"LOCAL-PQ",
        own_sig_suite="ed25519+mldsa65",
    )
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr = {
        "token": "tok-2",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "https://peer/wh",
        "sig_suite": "ed25519",
    }
    await coord.accept(qr)
    peer_iid = derive_instance_id(peer_kp.public_key)
    stored = repo.instances[peer_iid]
    assert stored.sig_suite == "ed25519"
    assert stored.remote_pq_algorithm is None
    assert stored.remote_pq_identity_pk is None


async def test_accept_falls_back_when_local_is_classical_peer_hybrid():
    """Local classical + peer hybrid → pair runs classical."""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(
        repo,
        _kek(),
        kp.public_key,
        own_sig_suite="ed25519",
    )
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    qr = {
        "token": "tok-3",
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": peer_dh.public_key.hex(),
        "webhook_url": "https://peer/wh",
        "sig_suite": "ed25519+mldsa65",
        "pq_algorithm": "mldsa65",
        "pq_identity_pk": "bb" * 32,
    }
    await coord.accept(qr)
    peer_iid = derive_instance_id(peer_kp.public_key)
    stored = repo.instances[peer_iid]
    # PQ material from the peer is persisted for future bookkeeping,
    # but the negotiated suite is the floor (classical).
    assert stored.sig_suite == "ed25519"
    assert stored.remote_pq_identity_pk == "bb" * 32


async def test_confirm_preserves_pq_material():
    """confirm() rebuilds the instance with CONFIRMED status + keeps pq_*"""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(
        repo,
        _kek(),
        kp.public_key,
        own_pq_pk=b"LOCAL-PQ",
        own_sig_suite="ed25519+mldsa65",
    )
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-4",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "webhook_url": "https://peer/wh",
            "sig_suite": "ed25519+mldsa65",
            "pq_algorithm": "mldsa65",
            "pq_identity_pk": "cc" * 32,
        }
    )
    confirmed = await coord.confirm(
        "tok-4",
        accept_result["verification_code"],
    )
    assert confirmed.sig_suite == "ed25519+mldsa65"
    assert confirmed.remote_pq_algorithm == "mldsa65"
    assert confirmed.remote_pq_identity_pk == "cc" * 32
