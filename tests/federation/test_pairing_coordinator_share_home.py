"""The pairing handshake honours the per-pair home-sharing switch.

Owner decision 2026-10-19: home sharing with a paired household is a
per-pair switch (``remote_instances.share_home``), on by default — and
EVERY sender of this household's home location honours it, the pairing
code (QR) and the scanner's ``PAIRING_PEER_ACCEPT`` included. No pair row
exists when a code is minted or scanned, so the admin's choice is passed in
(the route pre-sets it from the household's ``share_home_default``) and:

* no coordinates are in the QR / peer-accept body when it is off;
* the pair row the handshake creates carries the same choice, so the later
  ``LOCAL_HOME_LOCATION_CHANGED`` fan-out keeps honouring it;
* the coordinator itself fails closed: a caller that does not say
  ``share_home`` sends nothing.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.domain.federation import (
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.federation.peer_pairing_client import sign_peer_body
from socialhome.infrastructure.key_manager import KeyManager

_HOME = {
    "instance_id": "self",
    "display_name": "Alpha",
    "home_lat": 52.52,
    "home_lon": 13.405,
}


class _FakeRepo:
    def __init__(self, local_identity: dict | None = None) -> None:
        self.instances: dict = {}
        self.pairings: dict = {}
        self.share_home_writes: list[tuple[str, bool]] = []
        self._local_identity = local_identity

    async def save_instance(self, inst):
        # Mirrors the SQLite upsert: ``share_home`` is a local opt-in a
        # rebuild-and-save never overwrites on an existing row.
        old = self.instances.get(inst.id)
        if old is not None:
            inst = replace(inst, share_home=old.share_home)
        self.instances[inst.id] = inst
        return inst

    async def get_instance(self, iid):
        return self.instances.get(iid)

    async def set_share_home(self, iid, *, value):
        self.share_home_writes.append((iid, value))
        self.instances[iid] = replace(self.instances[iid], share_home=value)

    async def create_pairing(self, session):
        self.pairings[session.token] = session

    async def get_pairing(self, token):
        return self.pairings.get(token)

    async def update_pairing(self, session):
        self.pairings[session.token] = session

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)

    async def get_local_identity(self):
        return self._local_identity


class _RecordingClient:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def send_peer_accept(self, *, peer_inbox_url, body):
        self.bodies.append(body)

        class _R:
            ok = True
            status_code = 200
            error = None

        return _R()

    async def send_peer_confirm(self, *, peer_inbox_url, body):
        return await self.send_peer_accept(peer_inbox_url=peer_inbox_url, body=body)


def _kek() -> KeyManager:
    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


def _coordinator(repo: _FakeRepo) -> tuple[PairingCoordinator, _RecordingClient]:
    coord = PairingCoordinator(repo, _kek(), generate_identity_keypair().public_key)
    client = _RecordingClient()
    coord.attach_peer_pairing_client(client)
    return coord, client


def _qr(token: str) -> dict:
    peer = generate_identity_keypair()
    return {
        "token": token,
        "instance_id": derive_instance_id(peer.public_key),
        "identity_pk": peer.public_key.hex(),
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": "https://alpha.example/federation/inbox/A-INBOX",
    }


# ── The pairing code (QR) ─────────────────────────────────────────────


async def test_qr_has_no_home_when_sharing_is_off():
    coord, _ = _coordinator(_FakeRepo(_HOME))
    payload = await coord.initiate("https://us.example/inbox", share_home=False)
    assert "home_lat" not in payload
    assert "home_lon" not in payload


async def test_qr_carries_home_when_sharing_is_on():
    coord, _ = _coordinator(_FakeRepo(_HOME))
    payload = await coord.initiate("https://us.example/inbox", share_home=True)
    assert payload["home_lat"] == 52.52
    assert payload["home_lon"] == 13.405


async def test_qr_fails_closed_when_the_caller_does_not_say():
    """The route always decides (household default); a caller that does
    not must not leak the home."""
    coord, _ = _coordinator(_FakeRepo(_HOME))
    payload = await coord.initiate("https://us.example/inbox")
    assert "home_lat" not in payload


async def test_qr_never_names_the_choice():
    """The switch is this household's business: nothing about it is on
    the code, so the scanner can't tell 'off' from 'no home set'."""
    coord, _ = _coordinator(_FakeRepo(_HOME))
    payload = await coord.initiate("https://us.example/inbox", share_home=False)
    assert "share_home" not in payload


async def test_the_initiators_choice_is_kept_on_the_session():
    repo = _FakeRepo(_HOME)
    coord, _ = _coordinator(repo)
    payload = await coord.initiate("https://us.example/inbox", share_home=False)
    assert repo.pairings[payload["token"]].share_home is False
    payload = await coord.initiate("https://us.example/inbox", share_home=True)
    assert repo.pairings[payload["token"]].share_home is True


# ── The scanner's peer-accept ─────────────────────────────────────────


async def test_peer_accept_has_no_home_when_sharing_is_off():
    repo = _FakeRepo(_HOME)
    coord, client = _coordinator(repo)
    qr = _qr("tok-off")
    await coord.accept(qr, "https://beta.example/inbox", share_home=False)
    assert len(client.bodies) == 1
    assert "home_lat" not in client.bodies[0]
    assert "home_lon" not in client.bodies[0]
    assert "share_home" not in client.bodies[0]
    assert repo.instances[qr["instance_id"]].share_home is False


async def test_peer_accept_carries_home_when_sharing_is_on():
    repo = _FakeRepo(_HOME)
    coord, client = _coordinator(repo)
    qr = _qr("tok-on")
    await coord.accept(qr, "https://beta.example/inbox", share_home=True)
    assert client.bodies[0]["home_lat"] == 52.52
    assert repo.instances[qr["instance_id"]].share_home is True


async def test_peer_accept_fails_closed_when_the_caller_does_not_say():
    repo = _FakeRepo(_HOME)
    coord, client = _coordinator(repo)
    qr = _qr("tok-default")
    await coord.accept(qr, "https://beta.example/inbox")
    assert "home_lat" not in client.bodies[0]
    assert repo.instances[qr["instance_id"]].share_home is False


async def test_a_share_home_key_planted_in_the_code_is_ignored():
    """The code comes from the other household: it can't switch our
    sharing on."""
    repo = _FakeRepo(_HOME)
    coord, client = _coordinator(repo)
    qr = {**_qr("tok-planted"), "share_home": True}
    await coord.accept(qr, "https://beta.example/inbox", share_home=False)
    assert "home_lat" not in client.bodies[0]
    assert repo.instances[qr["instance_id"]].share_home is False


async def test_scanning_over_an_existing_row_applies_the_choice():
    """A household met through an invite link already has a row
    (``share_home`` off); pairing for real applies the admin's choice
    although the upsert keeps local opt-ins."""
    repo = _FakeRepo(_HOME)
    coord, _ = _coordinator(repo)
    qr = _qr("tok-existing")
    iid = qr["instance_id"]
    repo.instances[iid] = RemoteInstance(
        id=iid,
        display_name="Beta",
        remote_identity_pk=qr["identity_pk"],
        key_self_to_remote="",
        key_remote_to_self="",
        remote_inbox_url="",
        local_inbox_id="x",
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.SPACE_SESSION,
        share_home=False,
    )
    await coord.accept(qr, "https://beta.example/inbox", share_home=True)
    assert repo.instances[iid].share_home is True
    assert repo.share_home_writes == [(iid, True)]


# ── The initiator's row (peer-accept received) ────────────────────────


async def _peer_accept_for(coord_a: PairingCoordinator, token: str) -> str:
    """Sign a scanner's peer-accept for *token* and hand it to A."""
    b = generate_identity_keypair()
    body = sign_peer_body(
        {
            "token": token,
            "verification_code": "123456",
            "identity_pk": b.public_key.hex(),
            "dh_pk": generate_x25519_keypair().public_key.hex(),
            "inbox_url": "https://beta.example/federation/inbox/B",
            "home_lat": 48.1,
            "home_lon": 11.5,
        },
        own_identity_seed=b.private_key,
    )
    await coord_a.handle_peer_accept(body)
    return derive_instance_id(b.public_key)


async def test_initiator_row_keeps_the_choice_made_for_the_code():
    for choice in (False, True):
        repo = _FakeRepo(_HOME)
        coord, _ = _coordinator(repo)
        payload = await coord.initiate("https://us.example/inbox", share_home=choice)
        iid = await _peer_accept_for(coord, payload["token"])
        assert repo.instances[iid].share_home is choice
        # The scanner's own home is stored as it shared it — its switch,
        # not ours.
        assert repo.instances[iid].home_lat == 48.1


async def test_confirm_keeps_the_choice():
    repo = _FakeRepo(_HOME)
    coord, _ = _coordinator(repo)

    async def _promote(inst, *, token=None):
        repo.pairings.pop(token, None)
        repo.instances[inst.id] = inst
        return True

    repo.promote_pending_instance = _promote  # type: ignore[attr-defined]
    payload = await coord.initiate("https://us.example/inbox", share_home=False)
    await _peer_accept_for(coord, payload["token"])
    confirmed = await coord.confirm(payload["token"], "123456")
    assert confirmed.share_home is False
