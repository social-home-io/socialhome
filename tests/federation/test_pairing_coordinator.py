"""Tests for :class:`PairingCoordinator` — household address checks.

The scanned QR and the inbound peer-accept body both carry the other
household's federation inbox URL. That URL is where this household later
POSTs signed pairing bodies (and, once paired, every envelope), so it is
validated where it enters — before any pairing state is written.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)
from socialhome.federation.pairing_coordinator import PairingCoordinator
from socialhome.federation.peer_pairing_client import sign_peer_body
from socialhome.federation.peer_url import InvalidPeerUrlError
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

    async def update_pairing(self, session):
        self.pairings[session.token] = session

    async def delete_pairing(self, token):
        self.pairings.pop(token, None)

    async def get_local_identity(self):
        return None


class _RecordingPeerClient:
    def __init__(self) -> None:
        self.accepts: list[str] = []

    async def send_peer_accept(self, *, peer_inbox_url, body):
        self.accepts.append(peer_inbox_url)

        class _R:
            ok = True
            status_code = 200
            error = None

        return _R()


def _kek() -> KeyManager:
    return KeyManager.from_data_dir(Path(tempfile.mkdtemp()))


def _qr(inbox_url: object) -> dict:
    peer_kp = generate_identity_keypair()
    return {
        "token": "tok-url",
        "instance_id": derive_instance_id(peer_kp.public_key),
        "identity_pk": peer_kp.public_key.hex(),
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": inbox_url,
    }


BAD_URLS = [
    "file:///etc/passwd",
    "ftp://alpha.example/federation/inbox/A",
    "javascript:alert(1)",
    "https:///federation/inbox/A",
    "https://user:pw@alpha.example/federation/inbox/A",
    "https://user@alpha.example/federation/inbox/A",
    "",
]


@pytest.mark.parametrize("bad_url", BAD_URLS)
async def test_accept_rejects_invalid_qr_inbox_url_before_any_state(bad_url):
    repo = _FakeRepo()
    peer_client = _RecordingPeerClient()
    coord = PairingCoordinator(repo, _kek(), generate_identity_keypair().public_key)
    coord.attach_peer_pairing_client(peer_client)

    with pytest.raises(InvalidPeerUrlError):
        await coord.accept(
            _qr(bad_url),
            own_inbox_base_url="https://beta.example/federation/inbox",
        )

    # Fail early: no session, no provisional instance, no outbound POST.
    assert repo.pairings == {}
    assert repo.instances == {}
    assert peer_client.accepts == []


@pytest.mark.parametrize(
    "good_url",
    [
        "https://alpha.example/federation/inbox/A",
        "http://127.0.0.1:18001/federation/inbox/A",
        "http://192.168.1.20:8123/api/socialhome/inbox/A",
    ],
)
async def test_accept_allows_valid_qr_inbox_url(good_url):
    repo = _FakeRepo()
    peer_client = _RecordingPeerClient()
    coord = PairingCoordinator(repo, _kek(), generate_identity_keypair().public_key)
    coord.attach_peer_pairing_client(peer_client)

    await coord.accept(
        _qr(good_url),
        own_inbox_base_url="http://127.0.0.1:18002/federation/inbox",
    )
    assert peer_client.accepts == [good_url]
    assert len(repo.instances) == 1


async def _initiated() -> tuple[PairingCoordinator, _FakeRepo, str]:
    repo = _FakeRepo()
    coord = PairingCoordinator(repo, _kek(), generate_identity_keypair().public_key)
    qr = await coord.initiate(inbox_base_url="https://a.example/federation/inbox")
    return coord, repo, qr["token"]


def _peer_accept(token: str, inbox_url: str) -> dict:
    kp_b = generate_identity_keypair()
    body = {
        "token": token,
        "verification_code": "123456",
        "identity_pk": kp_b.public_key.hex(),
        "instance_id": derive_instance_id(kp_b.public_key),
        "dh_pk": generate_x25519_keypair().public_key.hex(),
        "inbox_url": inbox_url,
        "display_name": "Household B",
        "sig_suite": "ed25519",
    }
    return sign_peer_body(body, own_identity_seed=kp_b.private_key)


@pytest.mark.parametrize("bad_url", BAD_URLS)
async def test_handle_peer_accept_rejects_invalid_inbox_url(bad_url):
    coord, repo, token = await _initiated()
    before = dict(repo.pairings)

    with pytest.raises(InvalidPeerUrlError):
        await coord.handle_peer_accept(_peer_accept(token, bad_url))

    # Nothing materialised: no RemoteInstance, session untouched.
    assert repo.instances == {}
    assert repo.pairings == before


async def test_handle_peer_accept_allows_valid_inbox_url():
    coord, repo, token = await _initiated()
    result = await coord.handle_peer_accept(
        _peer_accept(token, "http://127.0.0.1:18002/federation/inbox/B"),
    )
    assert result["ok"] is True
    assert len(repo.instances) == 1
