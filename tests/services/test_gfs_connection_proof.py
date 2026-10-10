"""Household side of GFS proof of possession (:mod:`socialhome.gfs_proof`).

At pairing the household pins the GFS's id + key from the unauthenticated
``GET /gfs/info``. A GFS whose SIGNED capability block offers
``proof_of_possession`` must also sign a fresh nonce with that KEY, bound to
the URL the household used — otherwise the pairing is refused before the
household registers (no token spent, no identity sent). The first successful
proof sets a persisted ratchet (``proof_required_at``): from then on the
connection's reconnect reconcile runs only after a fresh proof. An older GFS
without the capability still pairs, logged at INFO, unproven.

Runs against the REAL GFS app (real SQLite, real aiohttp) listening on a port
chosen up front, so its configured ``base_url`` is the address the household
actually uses — and, for the cluster cases, behind a round-robin balancer in
front of several nodes that share the seed and base_url.
"""

from __future__ import annotations

import itertools
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
from socialhome.services import gfs_connection_service as svc_mod
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


def _signed_info(kp, gfs_id: str, caps: dict) -> dict:
    sig, suite = sign_capabilities(kp.private_key, gfs_id, caps)
    return {
        "gfs_instance_id": gfs_id,
        "public_key": kp.public_key.hex(),
        "capabilities": caps,
        "capabilities_sig": sig,
        "capabilities_sig_suite": suite,
    }


async def _stub(aiohttp_server, handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    return await aiohttp_server(app)


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
            sig, suite = sign_possession_proof(own_kp.private_key, body["nonce"], "")
            return web.json_response({"proof_sig": sig, "proof_sig_suite": suite})
        return web.json_response({"status": "registered"})

    return await _stub(aiohttp_server, handler), seen


# ── Pairing ─────────────────────────────────────────────────────────────


async def test_pairing_with_a_real_gfs_is_proven_and_ratchets(gfs, repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    assert conn.status == "active"
    assert svc.possession_proven(conn) is True
    stored = await repo.get(conn.id)
    assert stored is not None and stored.proof_required_at is not None
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

    server = await _stub(aiohttp_server, handler)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unproven"


@pytest.mark.parametrize(
    ("status", "body", "reason"),
    [
        (503, {"error": "unavailable"}, "unreachable"),
        # A rate-limited challenge is "busy", never an impostor verdict.
        (429, {"error": "rate_limited"}, "busy"),
        # A node that does not know /gfs/proof yet (rolling deploy behind a
        # balancer): retryable, not "unproven".
        (404, {"error": "not_found"}, "unreachable"),
        (405, {"error": "method"}, "unreachable"),
        (403, {"error": "nope"}, "unproven"),
        (200, ["not", "an", "object"], "unproven"),
        (200, {"proof_sig": 42, "proof_sig_suite": "ed25519"}, "unproven"),
        (200, {"proof_sig": "abc"}, "unproven"),
    ],
)
async def test_a_gfs_that_offers_proof_but_cannot_give_one_is_refused(
    repo, session, aiohttp_server, status, body, reason
):
    kp = generate_identity_keypair()
    info = _signed_info(kp, "gfs-x", {PROOF_CAPABILITY: True})

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(info)
        return web.json_response(body, status=status)

    server = await _stub(aiohttp_server, handler)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == reason
    assert await repo.list_all() == []


async def test_unreachable_proof_endpoint_is_unreachable(repo, session, aiohttp_server):
    """The connection drops mid-challenge: retryable, not an identity verdict."""
    kp = generate_identity_keypair()
    info = _signed_info(kp, "gfs-x", {PROOF_CAPABILITY: True})

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/gfs/info":
            return web.json_response(info)
        request.transport.close()  # type: ignore[union-attr]
        return web.Response()

    server = await _stub(aiohttp_server, handler)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unreachable"


async def test_older_gfs_without_the_capability_pairs_unproven(
    repo, session, aiohttp_server, caplog
):
    kp = generate_identity_keypair()
    info = _signed_info(kp, "gfs-old", {"anonymous_publish": True})
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append(request.path)
        if request.path == "/gfs/info":
            return web.json_response(info)
        return web.json_response({"status": "registered"})

    server = await _stub(aiohttp_server, handler)
    svc = GfsConnectionService(repo, http_client=session)
    with caplog.at_level(logging.INFO, logger=svc_mod.__name__):
        conn = await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert conn.status == "active"
    assert svc.possession_proven(conn) is False
    assert (await repo.get(conn.id)).proof_required_at is None
    # Never challenged a server that doesn't offer the endpoint.
    assert PROOF_ROUTE not in seen
    assert any(
        "proof_of_possession" in r.getMessage() and r.levelno == logging.INFO
        for r in caplog.records
    )


async def test_pairing_under_a_ratcheted_key_requires_the_proof(
    repo, session, aiohttp_server
):
    """A key this household already holds a proven connection for can't be
    paired again from a descriptor without the proof (a replayed pre-proof
    block at another address)."""
    kp = generate_identity_keypair()
    await repo.save(
        GfsConnection(
            id="c-old",
            gfs_instance_id="gfs-k",
            display_name="GFS",
            public_key=kp.public_key.hex(),
            inbox_url="https://gfs.example",
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
            proof_required_at="2026-01-01T00:00:00+00:00",
        )
    )
    info = _signed_info(kp, "gfs-k2", {"anonymous_publish": True})

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(info)
        return web.json_response({"status": "registered"})

    server = await _stub(aiohttp_server, handler)
    svc = GfsConnectionService(repo, http_client=session)
    with pytest.raises(GfsSignupError) as exc:
        await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert exc.value.reason == "unproven"
    assert [c.id for c in await repo.list_all()] == ["c-old"]


# ── A load-balanced cluster ─────────────────────────────────────────────


async def _cluster(tmp_dir, aiohttp_server, ids: tuple[str, ...]):
    """Nodes sharing the seed and base_url behind a round-robin balancer that
    sends every request to the NEXT node — so ``/gfs/info``, ``/gfs/proof``
    and ``/gfs/register`` of one pairing land on different nodes."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    nodes: list[TestClient] = []
    for n, iid in enumerate(ids):
        (tmp_dir / f"node{n}").mkdir()
        cfg = GfsConfig(
            host="127.0.0.1",
            port=0,
            base_url=base_url,
            data_dir=str(tmp_dir / f"node{n}"),
            instance_id=iid,
            signing_seed_hex="42" * 32,
        )
        tc = TestClient(TestServer(create_gfs_app(cfg)))
        await tc.start_server()
        nodes.append(tc)
    turn = itertools.cycle(range(len(nodes)))

    async def balance(request: web.Request) -> web.Response:
        node = nodes[next(turn)]
        body = await request.read()
        headers = {}
        if "Content-Type" in request.headers:
            headers["Content-Type"] = request.headers["Content-Type"]
        resp = await node.request(
            request.method, request.path_qs, data=body or None, headers=headers
        )
        return web.Response(
            body=await resp.read(),
            status=resp.status,
            content_type=resp.content_type,
        )

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", balance)
    lb = await aiohttp_server(app, port=port)
    return lb, nodes, turn


@pytest.mark.parametrize(
    "ids", [("gfs-node-a", "gfs-node-b"), ("gfs-shared", "gfs-shared")]
)
@pytest.mark.parametrize("offset", [0, 1])
async def test_pairing_succeeds_on_any_node_pair_of_a_cluster(
    tmp_dir, repo, session, aiohttp_server, ids, offset
):
    lb, nodes, turn = await _cluster(tmp_dir, aiohttp_server, ids)
    try:
        for _ in range(offset):
            next(turn)
        # A token every node accepts would need a shared DB; register lands
        # on whichever node is next, so mint one there.
        svc = GfsConnectionService(repo, http_client=session)
        register_node = nodes[(offset + 2) % len(nodes)]
        conn = await svc.pair(
            {"gfs_url": _url(lb), "token": await _token(register_node)}, **OWN
        )
        assert svc.possession_proven(conn) is True
        assert conn.gfs_instance_id == ids[offset % len(nodes)]
        # And the reconnect gate passes whichever node answers.
        for _ in range(3):
            svc._proven_at.clear()
            assert await svc.possession_gate(conn.id) is True
    finally:
        for tc in nodes:
            await tc.close()


# ── Reconnect gate + ratchet ────────────────────────────────────────────


async def _saved_conn(repo, url: str, **kw) -> GfsConnection:
    fields = {
        "id": "c1",
        "gfs_instance_id": "gfs-real",
        "display_name": "GFS",
        "public_key": "aa" * 32,
        "inbox_url": url,
        "status": "active",
        "paired_at": "2026-01-01T00:00:00+00:00",
    }
    fields.update(kw)
    conn = GfsConnection(**fields)
    await repo.save(conn)
    return conn


async def test_possession_unknown_before_any_check(repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await _saved_conn(repo, "http://127.0.0.1:9")
    assert svc.possession_proven(conn) is None


async def test_verify_possession_after_a_move_to_a_new_url(gfs, repo, session):
    """The re-bind hook: a GFS that moved keeps its key, and proving it at
    the NEW address is what lets a seat re-bind there."""
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    # Same key, a URL the real server is not configured for → refused.
    moved = replace(conn, inbox_url="http://localhost:" + str(gfs.port))
    assert await svc.verify_gfs_possession(moved) is False
    # The pinned key, not whatever the server claims, decides.
    wrong_key = replace(conn, public_key=generate_identity_keypair().public_key.hex())
    assert await svc.verify_gfs_possession(wrong_key) is False
    # The id is a label: a node serving another id still proves the key.
    other_id = replace(conn, gfs_instance_id="gfs-other")
    assert await svc.verify_gfs_possession(other_id) is True
    assert await svc.verify_gfs_possession(conn) is True


async def test_verify_possession_never_raises_without_a_session(repo):
    svc = GfsConnectionService(repo)
    conn = await _saved_conn(repo, "http://127.0.0.1:9")
    assert await svc.verify_gfs_possession(conn) is False


async def test_gate_passes_and_reuses_a_fresh_proof(gfs, repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    calls: list[str] = []
    real_post = session.post

    def counting_post(url, **kw):
        calls.append(str(url))
        return real_post(url, **kw)

    session.post = counting_post  # type: ignore[method-assign]
    try:
        await svc.refresh_connection_metadata(conn.id)
        assert await svc.possession_gate(conn.id) is True
        assert await svc.possession_gate(conn.id) is True
    finally:
        session.post = real_post  # type: ignore[method-assign]
    # The pairing proof is still fresh: no further challenge.
    assert not [c for c in calls if c.endswith(PROOF_ROUTE)]


async def test_gate_refuses_a_ratcheted_connection_that_fails(
    gfs, repo, session, caplog
):
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(gfs), "token": await _token(gfs)}, **OWN)
    # Re-key the stored pin: the server at the URL no longer proves the key
    # this household pinned.
    await repo.save(
        replace(conn, public_key=generate_identity_keypair().public_key.hex())
    )
    svc._proven_at.clear()
    with caplog.at_level(logging.WARNING):
        assert await svc.possession_gate(conn.id) is False
    assert svc.possession_proven(conn) is False
    assert any("possession" in r.getMessage() for r in caplog.records)


async def test_ratchet_survives_restart_and_refuses_a_downgrade(
    repo, session, aiohttp_server, caplog
):
    """Proven once, the connection requires the proof — also in a NEW
    process (fresh service, same DB) — and a server that then serves a
    descriptor without the capability and no proof (a replayed pre-proof
    block) is refused."""
    kp = generate_identity_keypair()
    state = {"caps": {PROOF_CAPABILITY: True}, "proof": True}
    url_box: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(_signed_info(kp, "gfs-dg", state["caps"]))
        if request.path == PROOF_ROUTE:
            if not state["proof"]:
                return web.json_response({"error": "not_found"}, status=404)
            nonce = (await request.json())["nonce"]
            sig, suite = sign_possession_proof(kp.private_key, nonce, url_box["url"])
            return web.json_response({"proof_sig": sig, "proof_sig_suite": suite})
        return web.json_response({"status": "registered"})

    server = await _stub(aiohttp_server, handler)
    url_box["url"] = _url(server)
    svc = GfsConnectionService(repo, http_client=session)
    conn = await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    assert (await repo.get(conn.id)).proof_required_at is not None

    state.update(caps={"anonymous_publish": True}, proof=False)
    restarted = GfsConnectionService(repo, http_client=session)
    await restarted.refresh_connection_metadata(conn.id)
    with caplog.at_level(logging.WARNING):
        assert await restarted.possession_gate(conn.id) is False
    assert any(PROOF_CAPABILITY in r.getMessage() for r in caplog.records)
    # The ratchet never clears on its own.
    assert (await repo.get(conn.id)).proof_required_at is not None


async def test_gate_sets_the_ratchet_when_an_upgraded_gfs_first_proves(
    gfs, repo, session
):
    """Paired before the GFS learnt the proof (no ratchet): the first
    successful proof on a reconnect sets it."""
    info = await (await gfs.get("/gfs/info")).json()
    conn = await _saved_conn(
        repo,
        _url(gfs),
        gfs_instance_id=info["gfs_instance_id"],
        public_key=info["public_key"],
    )
    svc = GfsConnectionService(repo, http_client=session)
    await svc.refresh_connection_metadata(conn.id)
    assert await svc.possession_gate(conn.id) is True
    assert (await repo.get(conn.id)).proof_required_at is not None


async def test_gate_passes_an_older_gfs_without_a_ratchet(
    repo, session, aiohttp_server
):
    kp = generate_identity_keypair()

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(
                _signed_info(kp, "gfs-real", {"anonymous_publish": True})
            )
        return web.json_response({}, status=404)

    server = await _stub(aiohttp_server, handler)
    conn = await _saved_conn(repo, _url(server), public_key=kp.public_key.hex())
    svc = GfsConnectionService(repo, http_client=session)
    await svc.refresh_connection_metadata(conn.id)
    assert await svc.possession_gate(conn.id) is True
    assert svc.possession_proven(conn) is False


async def test_gate_without_ratchet_tolerates_a_transient_failure(
    repo, session, aiohttp_server
):
    """Offered but busy / rolling (429 / 404) on a never-proven connection:
    nothing to enforce yet, so the reconcile is not blocked."""
    kp = generate_identity_keypair()

    async def handler(request: web.Request) -> web.Response:
        if request.path == "/gfs/info":
            return web.json_response(
                _signed_info(kp, "gfs-real", {PROOF_CAPABILITY: True})
            )
        return web.json_response({"error": "rate_limited"}, status=429)

    server = await _stub(aiohttp_server, handler)
    conn = await _saved_conn(repo, _url(server), public_key=kp.public_key.hex())
    svc = GfsConnectionService(repo, http_client=session)
    await svc.refresh_connection_metadata(conn.id)
    assert await svc.possession_gate(conn.id) is True
    assert (await repo.get(conn.id)).proof_required_at is None


async def test_gate_for_unknown_connection_is_true(repo, session):
    svc = GfsConnectionService(repo, http_client=session)
    assert await svc.possession_gate("nope") is True


async def test_failed_proof_warning_names_what_the_server_reported(
    gfs, repo, session, aiohttp_server, caplog
):
    server, _ = await _impostor(aiohttp_server, gfs, forward_proof=True)
    svc = GfsConnectionService(repo, http_client=session)
    with caplog.at_level(logging.WARNING, logger=svc_mod.__name__):
        with pytest.raises(GfsSignupError):
            await svc.pair({"gfs_url": _url(server), "token": "t"}, **OWN)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "gfs-real" in text  # the reported instance id
    assert _url(gfs) in text  # the reported base_url
