"""§27.9 release blocker: a household adopts a GFS's new public id only
under the key it pinned.

A household pins ``(public_key, gfs_instance_id)`` at pairing. The id is a
label the operator may change (a cluster moving from per-node ids to one
shared id); the KEY is the trust anchor. So the household rebinds to a new
id served by ``GET /gfs/info`` only when the served key IS the pinned key and
the signed capability block verifies under (pinned key, new id). A server
with any other key — an impostor at the same address, or a real re-key —
can never move the pinned id, and nothing from its block is trusted.

End to end: real GFS apps on real ports, a real household
:class:`GfsConnectionService` over a real SQLite repo.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from socialhome.crypto import ed25519_public_key
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.global_server.app_keys import gfs_cluster_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import GfsConnectionService

pytestmark = pytest.mark.security

PINNED_SEED = "a7" * 32
IMPOSTOR_SEED = "3c" * 32


async def _gfs(data_dir: Path, *, seed_hex: str, instance_id: str) -> TestServer:
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url="http://127.0.0.1:1",
            data_dir=str(data_dir),
            instance_id=instance_id,
            signing_seed_hex=seed_hex,
        )
    )
    server = TestServer(app)
    await server.start_server()
    return server


@pytest.fixture
async def household(tmp_path: Path) -> AsyncIterator[SqliteGfsConnectionRepo]:
    db = AsyncDatabase(tmp_path / "household.db", batch_timeout_ms=10)
    await db.startup()
    try:
        yield SqliteGfsConnectionRepo(db)
    finally:
        await db.shutdown()


async def _pin(repo: SqliteGfsConnectionRepo, server: TestServer) -> None:
    """The connection the household made at pairing: the ORIGINAL server's
    key under its old per-node id, at the address *server* now answers."""
    await repo.save(
        GfsConnection(
            id="conn-1",
            gfs_instance_id="gfs-0",
            display_name="GFS",
            public_key=ed25519_public_key(bytes.fromhex(PINNED_SEED)).hex(),
            inbox_url=str(server.make_url("")).rstrip("/"),
            status="active",
            paired_at="2026-10-01T00:00:00+00:00",
        )
    )


async def _refresh(
    repo: SqliteGfsConnectionRepo,
) -> tuple[GfsConnectionService, bool]:
    """A reconnect's metadata refresh, then a capability check on the row as
    it stands. Returns the service and whether ``private_channels`` was
    trusted."""
    async with aiohttp.ClientSession() as http:
        svc = GfsConnectionService(repo, http_client=http, publish_client=http)
        await svc.refresh_connection_metadata("conn-1")
        conn = await repo.get("conn-1")
        assert conn is not None
        trusted = await svc.private_channels_supported(conn)
    return svc, trusted


async def test_a_server_with_another_key_cannot_move_the_pinned_id(
    tmp_path, household, caplog
):
    impostor = await _gfs(
        tmp_path / "impostor", seed_hex=IMPOSTOR_SEED, instance_id="gfs-shared"
    )
    try:
        await _pin(household, impostor)
        with caplog.at_level(logging.WARNING):
            svc, trusted = await _refresh(household)
        conn = await household.get("conn-1")
        assert conn is not None and conn.gfs_instance_id == "gfs-0"
        assert svc.known_instance_ids(conn) == frozenset({"gfs-0"})
        # Nothing the impostor signed is trusted.
        assert trusted is False
        assert svc._anon_publish.get("conn-1") is False
        assert any(
            "pinned key" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )
    finally:
        await impostor.close()


async def test_an_impostor_claiming_the_pinned_key_cannot_move_the_pinned_id(
    tmp_path, household, monkeypatch
):
    """It serves the PINNED public key on /gfs/info but can only sign the
    block with its own: the block does not verify, nothing is adopted."""
    impostor = await _gfs(
        tmp_path / "impostor", seed_hex=IMPOSTOR_SEED, instance_id="gfs-shared"
    )
    # It advertises the pinned public key; its signing key stays its own.
    monkeypatch.setattr(
        impostor.app[gfs_cluster_key],
        "_own_pk_hex",
        ed25519_public_key(bytes.fromhex(PINNED_SEED)).hex(),
    )
    try:
        await _pin(household, impostor)
        svc, trusted = await _refresh(household)
        conn = await household.get("conn-1")
        assert conn is not None and conn.gfs_instance_id == "gfs-0"
        assert trusted is False
        assert svc._anon_publish.get("conn-1") is False
    finally:
        await impostor.close()


async def test_the_same_server_under_the_pinned_key_is_followed_to_its_new_id(
    tmp_path, household
):
    """The positive control: the original server (same seed) now serving a
    shared id is adopted, and its capabilities verify."""
    moved = await _gfs(
        tmp_path / "moved", seed_hex=PINNED_SEED, instance_id="gfs-shared"
    )
    try:
        await _pin(household, moved)
        svc, trusted = await _refresh(household)
        conn = await household.get("conn-1")
        assert conn is not None and conn.gfs_instance_id == "gfs-shared"
        assert trusted is True
        assert svc._anon_publish.get("conn-1") is True
    finally:
        await moved.close()
