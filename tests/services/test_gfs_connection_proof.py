"""Household side of GFS proof of possession (:mod:`socialhome.gfs_proof`).

At pairing the household pins the GFS's id + key from the unauthenticated
``GET /gfs/info``. A GFS whose SIGNED capability block offers
``proof_of_possession`` must also sign a fresh nonce, bound to the URL the
household used — otherwise the pairing is refused before the household
registers (no token spent, no identity sent). An older GFS without the
capability still pairs, logged at INFO and recorded as unproven.

Runs against the REAL GFS app (real SQLite, real aiohttp) listening on a port
chosen up front, so its configured ``base_url`` is the address the household
actually uses.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import replace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import sign_capabilities
from socialhome.crypto import generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import GfsConnection
from socialhome.gfs_proof import PROOF_CAPABILITY, PROOF_ROUTE, sign_possession_proof
from socialhome.global_server.app_keys import gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.services.gfs_connection_service import (
    GfsConnectionService,
    GfsSignupError,
)

OWN = {
    "own_instance_id": "alpha.home",
    "own_public_key_hex": "aa" * 32,
    "own_display_name": "Alpha House",
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def repo(tmp_dir):
    db = AsyncDatabase(tmp_dir / "household.db", batch_timeout_ms=10)
    await db.startup()
    yield SqliteGfsConnectionRepo(db)
    await db.shutdown()


@pytest.fixture
async def gfs(tmp_dir):
    """The real GFS, served at exactly its configured ``base_url``."""
    port = _free_port()
    (tmp_dir / "gfs").mkdir()
    cfg = GfsConfig(
        host="127.0.0.1",
        port=port,
        base_url=f"http://127.0.0.1:{port}",
        data_dir=str(tmp_dir / "gfs"),
        instance_id="gfs-real",
    )
    async with TestClient(
        TestServer(create_gfs_app(cfg), host="127.0.0.1", port=port)
    ) as tc:
        yield tc


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


def _url(server) -> str:
    return str(server.make_url("")).rstrip("/")


async def _token(tc: TestClient) -> str:
    token_svc = tc.server.app["gfs_token_service"]
    token, _wait = await token_svc.generate("203.0.113.1")
    assert token
    return token


async def _impostor(aiohttp_server, real: TestClient, *, forward_proof: bool):
    """A host at ANOTHER address replaying the real GFS's ``/gfs/info``.

    With *forward_proof* it also relays ``POST /gfs/proof`` to the real GFS
    (the strongest impostor: it gets a genuine signature back); without, it
    answers the challenge with a signature from its own key.
    """
    info = await (await real.get("/gfs/info")).json()
    seen: list[str] = []
    own_kp = generate_identity_keypair()

    async def handler(request: web.Request) -> web.Response:
        seen.append(f"{request.method} {request.path}")
        if request.path == "/gfs/info":
            return web.json_response(info)
        if request.path == PROOF_ROUTE:
            body = await request.json()
            if forward_proof:
                resp = await real.post(PROOF_ROUTE, json=body)
                return web.json_response(await resp.json(), status=resp.status)
            sig, suite = sign_possession_proof(
                own_kp.private_key, info["gfs_instance_id"], body["nonce"], ""
            )
            return web.json_response(
                {
                    "gfs_instance_id": info["gfs_instance_id"],
                    "base_url": "",
                    "nonce": body["nonce"],
                    "proof_sig": sig,
                    "proof_sig_suite": suite,
                }
            )
        return web.json_response({"status": "registered"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return await aiohttp_server(app), seen


async def test_pairing_with_a_real_gfs_is_proven(gfs, repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    assert conn.status == "active"
    assert svc.possession_proven(conn) is True
    assert await svc.verify_gfs_possession(conn) is True


@pytest.mark.parametrize("forward_proof", [True, False])
async def test_impostor_replaying_info_at_another_url_is_refused(
    gfs, repo, session, aiohttp_server, forward_proof
):
    server, seen = await _impostor(aiohttp_server, gfs, forward_proof=forward_proof)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unproven"
    # Refused BEFORE registering: no token spent, no household identity sent.
    assert seen == ["GET /gfs/info", f"POST {PROOF_ROUTE}"]
    assert await repo.list_all() == []
    assert await gfs.server.app[gfs_fed_repo_key].get_instance("alpha.home") is None


async def test_unknown_proof_suite_is_refused(gfs, repo, session, aiohttp_server):
    info = await (await gfs.get("/gfs/info")).json()

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(info)
        body = await request.json()
        resp = await (await gfs.post(PROOF_ROUTE, json=body)).json()
        return web.json_response({**resp, "proof_sig_suite": "rsa-pss"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unproven"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (503, {"error": "unavailable"}, "unreachable"),
        (404, {"error": "not_found"}, "unproven"),
        (200, ["not", "an", "object"], "unproven"),
        (200, {"proof_sig": 42, "proof_sig_suite": "ed25519"}, "unproven"),
        (200, {"proof_sig": "abc"}, "unproven"),
    ],
)
async def test_a_gfs_that_offers_proof_but_cannot_give_one_is_refused(
    repo, session, aiohttp_server, status, body, reason
):
    kp = generate_identity_keypair()
    caps = {PROOF_CAPABILITY: True}
    sig, suite = sign_capabilities(kp.private_key, "gfs-x", caps)
    info = {
        "gfs_instance_id": "gfs-x",
        "public_key": kp.public_key.hex(),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(info)
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == reason
    assert await repo.list_all() == []


async def test_unreachable_proof_endpoint_is_unreachable(repo, session, aiohttp_server):
    """The connection drops mid-challenge: retryable, not an identity verdict."""
    kp = generate_identity_keypair()
    caps = {PROOF_CAPABILITY: True}
    sig, suite = sign_capabilities(kp.private_key, "gfs-x", caps)
    info = {
        "gfs_instance_id": "gfs-x",
        "public_key": kp.public_key.hex(),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/gfs/info":
            return web.json_response(info)
        request.transport.close()  # type: ignore[union-attr]
        return web.Response()

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unreachable"


async def test_older_gfs_without_the_capability_pairs_unproven(
    repo, session, aiohttp_server, caplog
):
    kp = generate_identity_keypair()
    caps = {"anonymous_publish": True}
    sig, suite = sign_capabilities(kp.private_key, "gfs-old", caps)
    info = {
        "gfs_instance_id": "gfs-old",
        "public_key": kp.public_key.hex(),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.path)
        if request.path == "/gfs/info":
            return web.json_response(info)
        return web.json_response({"status": "registered"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    svc = GfsConnectionService(repo, http_client=session)
    with caplog.at_level(
        logging.INFO, logger="socialhome.services.gfs_connection_service"
    ):
        conn = await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert conn.status == "active"
    assert svc.possession_proven(conn) is False
    # Never challenged a server that doesn't offer the endpoint.
    assert PROOF_ROUTE not in seen
    assert any(
        "proof_of_possession" in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    )


async def test_possession_unknown_before_any_check(repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await _saved_conn(repo, "http://127.0.0.1:9")
    assert svc.possession_proven(conn) is None


async def _saved_conn(repo, url: str, **kw):
    from_kw = {
        "id": "c1",
        "gfs_instance_id": "gfs-real",
        "display_name": "GFS",
        "public_key": "aa" * 32,
        "inbox_url": url,
        "status": "active",
        "paired_at": "2026-01-01T00:00:00+00:00",
    }
    from_kw.update(kw)
    conn = GfsConnection(**from_kw)
    await repo.save(conn)
    return conn


async def test_verify_possession_after_a_move_to_a_new_url(gfs, repo, session):
    """The re-bind hook: a GFS that moved to a new URL keeps its id + key, and
    proving it at the NEW address is what lets a seat re-bind there."""
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    # Same id + key, a URL the real server is not configured for → refused.
    moved = replace(conn, inbox_url="http://localhost:" + str(gfs.port))
    assert await svc.verify_gfs_possession(moved) is False
    # The pinned key, not whatever the server claims, decides.
    wrong_key = replace(conn, public_key=generate_identity_keypair().public_key.hex())
    assert await svc.verify_gfs_possession(wrong_key) is False
    wrong_id = replace(conn, gfs_instance_id="gfs-other")
    assert await svc.verify_gfs_possession(wrong_id) is False
    assert await svc.verify_gfs_possession(conn) is True


async def test_verify_possession_never_raises_without_a_session(repo):
    svc = GfsConnectionService(repo)
    conn = await _saved_conn(repo, "http://127.0.0.1:9")
    assert await svc.verify_gfs_possession(conn) is False


async def test_reconnect_reverifies_and_records_failure(gfs, repo, session, caplog):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    await svc.refresh_connection_metadata(conn.id)
    assert svc.possession_proven(conn) is True

    # Re-key the stored pin: the server at the URL no longer proves the key
    # this household pinned — a reconnect must notice and say so.
    other = generate_identity_keypair().public_key.hex()
    await repo.save(replace(conn, public_key=other))
    with caplog.at_level(logging.WARNING):
        await svc.refresh_connection_metadata(conn.id)
    assert svc.possession_proven(conn) is False
    assert any("possession" in r.getMessage() for r in caplog.records)


async def test_reconnect_downgrade_is_unproven(repo, session, aiohttp_server, caplog):
    """A server that proved possession and later serves a descriptor without
    the capability (a replayed OLD block) is no longer proven."""
    kp = generate_identity_keypair()
    state = {"caps": {PROOF_CAPABILITY: True}}

    def _info() -> dict:
        caps = state["caps"]
        sig, suite = sign_capabilities(kp.private_key, "gfs-dg", caps)
        return {
            "gfs_instance_id": "gfs-dg",
            "public_key": kp.public_key.hex(),
            "capabilities": caps,
            "capabilities_sig": sig,
            "capabilities_sig_suite": suite,
        }

    url_box: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(_info())
        if request.path == PROOF_ROUTE:
            nonce = (await request.json())["nonce"]
            sig, suite = sign_possession_proof(
                kp.private_key, "gfs-dg", nonce, url_box["url"]
            )
            return web.json_response({"proof_sig": sig, "proof_sig_suite": suite})
        return web.json_response({"status": "registered"})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = await aiohttp_server(app)
    url_box["url"] = _url(server)
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert svc.possession_proven(conn) is True

    state["caps"] = {"anonymous_publish": True}
    with caplog.at_level(logging.WARNING):
        await svc.refresh_connection_metadata(conn.id)
    assert svc.possession_proven(conn) is False
    assert any("proof_of_possession" in r.getMessage() for r in caplog.records)
