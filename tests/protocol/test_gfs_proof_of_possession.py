"""§27.9 release blocker: a host replaying a real GFS's ``/gfs/info`` is not
that GFS.

``GET /gfs/info`` is unauthenticated and static — its signed capability block
proves what the GFS's key once signed, not that the server answering holds
the key. A household pinning id + key from it must not be fooled by a host at
another address that serves the real descriptor, even one that forwards the
``POST /gfs/proof`` challenge to the real GFS and returns its genuine
signature: the proof is bound to the GFS's OWN configured ``base_url``, so it
fails at the impostor's address. The impostor learns neither the pairing token
nor the household's identity — the household refuses before registering.
"""

from __future__ import annotations

import socket
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.gfs_proof import PROOF_ROUTE
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionService,
    GfsSignupError,
)

pytestmark = pytest.mark.security

OWN = {
    "own_instance_id": "victim.home",
    "own_public_key_hex": "aa" * 32,
    "own_display_name": "Victim House",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def real_gfs(tmp_path):
    port = _free_port()
    cfg = GfsConfig(
        host="127.0.0.1",
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        data_dir=str(tmp_path),
        instance_id="gfs-genuine",
    )
    async with TestClient(
        TestServer(create_gfs_app(cfg), host="127.0.0.1", port=port)
    ) as tc:
        yield tc


async def test_relaying_impostor_cannot_pass_for_the_real_gfs(
    real_gfs, aiohttp_server, tmp_path
):
    info = await (await real_gfs.get("/gfs/info")).json()
    received: list[tuple[str, object]] = []

    async def impostor(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        received.append((request.path, body))
        if request.path == "/gfs/info":
            return web.json_response(info)  # byte-for-byte the real descriptor
        if request.path == PROOF_ROUTE:
            # Forward the challenge to the real GFS: a GENUINE signature.
            resp = await real_gfs.post(PROOF_ROUTE, json=body)
            return web.json_response(await resp.json(), status=resp.status)
        return web.json_response({"status": "registered"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", impostor)
    server = await aiohttp_server(app)
    impostor_url = str(server.make_url("")).rstrip("/")

    db = AsyncDatabase(tmp_path / "household.db", batch_timeout_ms=10)
    await db.startup()
    try:
        repo = SqliteGfsConnectionRepo(db)
        async with aiohttp.ClientSession() as session:
            svc = GfsConnectionService(repo, http_client=session)
            with pytest.raises(GfsSignupError) as exc:
                await svc.pair(
                    {"gfs_url": impostor_url, "token": "secret-token"}, **OWN
                )
        assert exc.value.reason == "unproven"
        # Nothing pinned, nothing registered anywhere.
        assert await repo.list_all() == []
        assert (
            await real_gfs.server.app[gfs_fed_repo_key].get_instance("victim.home")
        ) is None
        # The impostor saw the descriptor fetch and a random nonce — never the
        # token, never the household's identity.
        assert [p for p, _ in received] == ["/gfs/info", PROOF_ROUTE]
        assert set(received[1][1]) == {"nonce"}  # type: ignore[arg-type]
        assert "secret-token" not in repr(received)
        assert "victim.home" not in repr(received)
    finally:
        await db.shutdown()


async def test_a_seat_rebind_needs_a_proof_at_the_new_address(
    real_gfs, aiohttp_server, tmp_path
):
    """The follow-seat rebind hook (``verify_gfs_possession`` with the
    candidate address): the real server at its own address proves the pinned
    key; a host at another address replaying the descriptor and relaying the
    challenge to the real server does not — so a seat never follows an
    impostor's "move"."""
    info = await (await real_gfs.get("/gfs/info")).json()

    async def relay(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(info)
        resp = await real_gfs.post(request.path, json=await request.json())
        return web.json_response(await resp.json(), status=resp.status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", relay)
    impostor = await aiohttp_server(app)

    pinned = GfsConnection(
        id="c1",
        gfs_instance_id=info["gfs_instance_id"],
        display_name="GFS",
        public_key=info["public_key"],
        inbox_url="https://old-address.example",
        status="active",
        paired_at="2026-01-01T00:00:00+00:00",
    )
    db = AsyncDatabase(tmp_path / "household2.db", batch_timeout_ms=10)
    await db.startup()
    try:
        async with aiohttp.ClientSession() as session:
            svc = GfsConnectionService(SqliteGfsConnectionRepo(db), http_client=session)
            genuine = replace(pinned, inbox_url=str(real_gfs.make_url("")).rstrip("/"))
            fake = replace(pinned, inbox_url=str(impostor.make_url("")).rstrip("/"))
            assert await svc.verify_gfs_possession(genuine) is True
            assert await svc.verify_gfs_possession(fake) is False
    finally:
        await db.shutdown()
