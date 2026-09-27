"""The household pairing handshake is a signed-in admin action.

``/api/pairing/initiate``, ``/accept`` and ``/confirm`` are the admin's own
SPA steps for pairing with another household (a trust decision for the whole
household). The peer's side of the handshake never uses them — it arrives
through the envelope-signed federation inbox. So none of them may sit on the
auth middleware's public-path list, and an unauthenticated call must be
refused before any pairing state exists or any outbound request is made.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from socialhome.app import create_app
from socialhome.app_keys import federation_repo_key, federation_service_key
from socialhome.auth import _DEFAULT_PUBLIC_PATHS, _DEFAULT_PUBLIC_PATH_PATTERNS
from socialhome.config import Config
from socialhome.crypto import (
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
)

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
