"""``PairingCoordinator.confirm()`` (initiator side of the §11 QR
handshake) must publish :class:`PairingConfirmed` on the local bus so
subscribers — capabilities announcement, peer-directory mirror, DM
history scheduler, space sync scheduler — see the new confirmed peer
without waiting for the next restart.

This historically only fired on the responder side (via
``peer_confirm``), causing the asymmetric "a sees b at proto_v2 but
b sees a at proto_v1" failure the federation-demo's capability
assertion surfaced.
"""

from __future__ import annotations

import asyncio

from socialhome.crypto import (
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.events import PairingConfirmed
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager


class _FakeRepo:
    def __init__(self, *, local_identity: dict | None = None) -> None:
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

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)

    async def promote_pending_instance(self, inst, *, token=None):
        """Mirrors the SQLite repo: needs the session (when named) AND a
        still-pending row, else changes nothing."""
        row = self.instances.get(inst.id)
        if token is not None and token not in self.pairings:
            return False
        if row is None or row.status.value not in ("pending_sent", "pending_received"):
            return False
        if token is not None:
            self.pairings.pop(token)
        self.instances[inst.id] = inst
        return True

    async def get_local_identity(self):
        return self._local_identity


def _kek() -> KeyManager:
    import tempfile
    from pathlib import Path

    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


async def test_confirm_publishes_pairing_confirmed_on_local_bus():
    """``confirm()`` runs through the accept + confirm flow and the
    resulting bus event lets the local subscriber (capabilities
    announcement, peer directory, …) wake up immediately."""
    captured: list[PairingConfirmed] = []
    bus = EventBus()
    bus.subscribe(PairingConfirmed, captured.append)

    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), kp.public_key, bus=bus)

    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-confirm-publish",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
        }
    )
    confirmed = await coord.confirm(
        "tok-confirm-publish",
        accept_result["verification_code"],
    )

    # The bus is async, so we have to flush queued events before
    # asserting. Both sides of the publish are inside ``confirm()``;
    # one ``await`` cycle is enough to drain the in-process delivery.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(captured) == 1, (
        f"expected one PairingConfirmed publish, got {len(captured)}"
    )
    assert captured[0].instance_id == confirmed.id


async def test_initiate_carries_home_coords_in_qr_payload():
    """The QR payload has to ship the initiator's own coords so the
    scanner can seed their RemoteInstance for us with coords on the
    spot. The peer-accept body covers the reverse direction (scanner
    → initiator), so a missing carry here meant only one direction
    of the pair ever picked up coords during the handshake — the
    other side had to wait for a subsequent
    ``LOCAL_HOME_LOCATION_CHANGED`` envelope (which only fires on
    share_home flips, not on idle pairings)."""
    kp = generate_identity_keypair()
    repo = _FakeRepo(
        local_identity={
            "instance_id": "self",
            "display_name": "Alpha House",
            "home_lat": 52.52,
            "home_lon": 13.405,
        }
    )
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    # Only when the admin shares the home with this household (the route
    # pre-sets the switch from ``share_home_default``).
    payload = await coord.initiate(
        "https://us.example/federation/inbox", share_home=True
    )
    assert payload["home_lat"] == 52.52
    assert payload["home_lon"] == 13.405


async def test_initiate_omits_home_coords_when_unset():
    """Operators who haven't set a home location must still be able
    to pair — the carry is conditional on coords being set, not
    mandatory."""
    kp = generate_identity_keypair()
    repo = _FakeRepo(
        local_identity={
            "instance_id": "self",
            "display_name": "Alpha House",
            # No home coords set.
        }
    )
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    payload = await coord.initiate("https://us.example/federation/inbox")
    assert "home_lat" not in payload
    assert "home_lon" not in payload


async def test_accept_persists_home_coords_from_qr_payload():
    """The scanner side has to write the initiator's coords into the
    new RemoteInstance row at ``accept`` time — otherwise the
    coords from the QR are read off the payload and silently
    discarded."""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    await coord.accept(
        {
            "token": "tok-accept-coords",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
            "home_lat": 52.52,
            "home_lon": 13.405,
        }
    )
    assert len(repo.instances) == 1
    saved = next(iter(repo.instances.values()))
    assert saved.home_lat == 52.52
    assert saved.home_lon == 13.405


async def test_confirm_preserves_home_coords():
    """Regression: ``confirm()`` rebuilds the RemoteInstance to flip
    its status to CONFIRMED. The rebuild used to drop ``home_lat`` /
    ``home_lon`` so every cross-household viewer saw the peer with
    NULL coords until a subsequent ``LOCAL_HOME_LOCATION_CHANGED``
    event refilled them. ``peer_confirm()`` already preserves the
    pair (see line ~649); ``confirm()`` now follows the same shape."""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-confirm-coords",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
            "home_lat": 52.52,
            "home_lon": 13.405,
        }
    )
    confirmed = await coord.confirm(
        "tok-confirm-coords",
        accept_result["verification_code"],
    )
    assert confirmed.status.value == "confirmed"
    assert confirmed.home_lat == 52.52, "confirm dropped home_lat"
    assert confirmed.home_lon == 13.405, "confirm dropped home_lon"


async def test_confirm_skips_publish_when_no_bus_wired():
    """In-test or standalone constructions that don't pass a bus
    must not raise — confirm() should fall through gracefully."""
    kp = generate_identity_keypair()
    repo = _FakeRepo()
    # No bus= kwarg.
    coord = PairingCoordinator(repo, _kek(), kp.public_key)
    peer_kp = generate_identity_keypair()
    peer_dh = generate_x25519_keypair()
    accept_result = await coord.accept(
        {
            "token": "tok-no-bus",
            "identity_pk": peer_kp.public_key.hex(),
            "dh_pk": peer_dh.public_key.hex(),
            "inbox_url": "https://peer/wh",
        }
    )
    confirmed = await coord.confirm(
        "tok-no-bus",
        accept_result["verification_code"],
    )
    assert confirmed.status.value == "confirmed"
