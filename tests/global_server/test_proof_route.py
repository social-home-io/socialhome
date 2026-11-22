"""``POST /gfs/proof`` — the GFS proves it holds its identity key.

The household sends a fresh nonce; the GFS signs it, bound to its own
configured ``base_url``, with the identity key ``/gfs/info`` publishes. The
endpoint is unauthenticated, so it is body-capped and rate-limited, and it
takes nothing from the requester but the nonce.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode, generate_identity_keypair
from socialhome.gfs_client_status import (
    CLIENT_STATUS_REQUEST_MAX_AGE_S,
    CLIENT_STATUS_REQUEST_MAX_FUTURE_S,
    sign_status_request,
    status_request_ts,
    verify_client_status,
)
from socialhome.gfs_proof import (
    PROOF_CAPABILITY,
    PROOF_ROUTE,
    PROOF_SIG_SUITE_ED25519,
    new_proof_nonce,
    verify_possession_proof,
)
from socialhome.global_server import public
from socialhome.global_server.app_keys import gfs_cluster_key, gfs_fed_repo_key
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.routes import relay as relay_routes
from socialhome.global_server.server import create_gfs_app

BASE_URL = "https://gfs.proof.test"


def _cfg(tmp_path, **kw) -> GfsConfig:
    return GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url=kw.pop("base_url", BASE_URL),
        data_dir=str(tmp_path),
        instance_id="gfs-proof",
        **kw,
    )


@pytest.fixture
async def client(tmp_path):
    app = create_gfs_app(_cfg(tmp_path))
    async with TestClient(TestServer(app)) as tc:
        yield tc


def _ip(n: int) -> dict[str, str]:
    """A distinct client address per call (loopback is a trusted proxy)."""
    return {"X-Forwarded-For": f"198.51.100.{n}"}


async def test_info_advertises_proof_of_possession_signed(client):
    body = await (await client.get("/gfs/info")).json()
    assert body["capabilities"][PROOF_CAPABILITY] is True
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


async def test_proof_verifies_against_the_info_key(client):
    info = await (await client.get("/gfs/info")).json()
    nonce = new_proof_nonce()
    resp = await client.post(PROOF_ROUTE, json={"nonce": nonce}, headers=_ip(1))
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {
        "gfs_instance_id",
        "gfs_key",
        "base_url",
        "nonce",
        "proof_sig",
        "proof_sig_suite",
    }
    assert body["gfs_instance_id"] == "gfs-proof"
    assert body["gfs_key"] == info["public_key"]
    assert body["base_url"] == BASE_URL
    assert body["nonce"] == nonce
    assert body["proof_sig_suite"] == PROOF_SIG_SUITE_ED25519
    assert verify_possession_proof(
        info["public_key"],
        nonce,
        BASE_URL,
        body["proof_sig"],
        body["proof_sig_suite"],
    )


async def test_proof_binds_the_configured_base_url_not_the_host_header(client):
    """A relay forwarding the challenge cannot make the GFS sign the relay's
    address: the URL in the proof is the GFS's own config, whatever Host the
    request carried."""
    info = await (await client.get("/gfs/info")).json()
    nonce = new_proof_nonce()
    resp = await client.post(
        PROOF_ROUTE,
        json={"nonce": nonce, "base_url": "https://impostor.example"},
        headers={**_ip(2), "Host": "impostor.example"},
    )
    body = await resp.json()
    assert body["base_url"] == BASE_URL
    assert not verify_possession_proof(
        info["public_key"],
        nonce,
        "https://impostor.example",
        body["proof_sig"],
        body["proof_sig_suite"],
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"nonce": ""},
        {"nonce": 42},
        {"nonce": ["x"]},
        {"nonce": b64url_encode(b"\x01" * 16)},
        {"nonce": "!!!"},
    ],
)
async def test_bad_nonce_is_400(client, payload):
    resp = await client.post(PROOF_ROUTE, json=payload, headers=_ip(3))
    assert resp.status == 400


async def test_non_json_body_is_400(client):
    resp = await client.post(PROOF_ROUTE, data=b"not json", headers=_ip(4))
    assert resp.status == 400


async def test_oversized_body_is_413(client):
    resp = await client.post(
        PROOF_ROUTE,
        data=b'{"nonce":"' + b"A" * 8192 + b'"}',
        headers={**_ip(5), "Content-Type": "application/json"},
    )
    assert resp.status == 413


async def test_proof_is_rate_limited_per_ip(client):
    for _ in range(public.PROOF_MAX_PER_MINUTE):
        resp = await client.post(
            PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(6)
        )
        assert resp.status == 200
    shed = await client.post(
        PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(6)
    )
    assert shed.status == 429
    assert shed.headers["Retry-After"] == "60"
    # Another address still gets through.
    ok = await client.post(
        PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(7)
    )
    assert ok.status == 200


async def test_get_is_not_allowed(client):
    assert (await client.get(PROOF_ROUTE)).status == 405


async def test_without_a_signing_key_the_capability_is_off_and_proof_503(
    client, monkeypatch
):
    """A GFS that cannot sign never advertises a proof it cannot give."""
    cluster = client.server.app[gfs_cluster_key]
    monkeypatch.setattr(cluster, "_signing_key", b"")
    info = await (await client.get("/gfs/info")).json()
    assert "capabilities_sig" not in info
    assert info["capabilities"][PROOF_CAPABILITY] is False
    resp = await client.post(
        PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(8)
    )
    assert resp.status == 503
    assert await resp.json() == {"error": "unavailable"}


async def test_wildcard_inferred_base_url_does_not_offer_the_proof(tmp_path):
    """The env fallback infers ``http://0.0.0.0:8765`` from the bind address —
    nobody's address, so the GFS offers no proof rather than one that fails
    for every household (which then pairs it as an older GFS)."""
    app = create_gfs_app(_cfg(tmp_path, base_url="http://0.0.0.0:8765"))
    async with TestClient(TestServer(app)) as tc:
        info = await (await tc.get("/gfs/info")).json()
        assert info["capabilities"][PROOF_CAPABILITY] is False
        assert "capabilities_sig" in info
        resp = await tc.post(
            PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(9)
        )
        assert resp.status == 503


async def test_cluster_signs_nothing_without_a_key(client, monkeypatch):
    cluster = client.server.app[gfs_cluster_key]
    sig, suite = cluster.sign_possession_proof(new_proof_nonce(), BASE_URL)
    assert sig and suite == PROOF_SIG_SUITE_ED25519
    monkeypatch.setattr(cluster, "_signing_key", b"")
    assert cluster.sign_possession_proof(new_proof_nonce(), BASE_URL) == ("", "")


async def test_nodes_with_a_shared_seed_and_different_ids_prove_the_same_key(
    tmp_path,
):
    """A load-balanced cluster: every node shares the seed and base_url, ids
    may differ per node. A proof from ANY node verifies against the key any
    node's /gfs/info published — the id is not part of what is proven."""
    seed = "11" * 32
    clients = []
    for n, iid in enumerate(("gfs-node-a", "gfs-node-b")):
        (tmp_path / str(n)).mkdir()
        app = create_gfs_app(
            GfsConfig(
                host="127.0.0.1",
                port=0,
                base_url=BASE_URL,
                data_dir=str(tmp_path / str(n)),
                instance_id=iid,
                signing_seed_hex=seed,
            )
        )
        tc = TestClient(TestServer(app))
        await tc.start_server()
        clients.append(tc)
    try:
        info_a = await (await clients[0].get("/gfs/info")).json()
        nonce = new_proof_nonce()
        resp = await clients[1].post(
            PROOF_ROUTE, json={"nonce": nonce}, headers=_ip(10)
        )
        body = await resp.json()
        assert body["gfs_instance_id"] == "gfs-node-b"
        assert verify_possession_proof(
            info_a["public_key"],
            nonce,
            BASE_URL,
            body["proof_sig"],
            body["proof_sig_suite"],
        )
    finally:
        for tc in clients:
            await tc.close()


# ── A pending household asks for its approval status ──────────────────────


async def _register(client, kp, instance_id: str, status: str) -> None:
    await client.server.app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=instance_id,
            display_name="Home",
            public_key=kp.public_key.hex(),
            status=status,
        )
    )


async def _gfs_key(client) -> str:
    return (await (await client.get("/gfs/info")).json())["public_key"]


async def _ask(
    client,
    kp,
    instance_id: str,
    n: int = 50,
    *,
    gfs_key: str = "",
    ts: int | None = None,
    edit=None,
):
    """POST a signed question → ``(nonce, question, response)``. Asks the
    GFS at *client* unless *gfs_key* names another one; *edit* rewrites the
    wire ``client`` block after signing (an on-path edit)."""
    if not gfs_key:
        gfs_key = await _gfs_key(client)
    nonce = new_proof_nonce()
    question = sign_status_request(
        kp.private_key,
        gfs_key,
        instance_id,
        nonce,
        ts=status_request_ts(time.time()) if ts is None else ts,
    )
    block = question.to_wire()
    if edit is not None:
        block = edit(block)
    body = {"nonce": nonce, "client": block}
    resp = await client.post(PROOF_ROUTE, json=body, headers=_ip(n))
    return nonce, question, resp


@pytest.mark.parametrize("status", ["pending", "active", "banned"])
async def test_a_registered_household_learns_its_status_signed(client, status):
    home = generate_identity_keypair()
    await _register(client, home, "home-a", status)
    gfs_key = await _gfs_key(client)
    nonce, question, resp = await _ask(client, home, "home-a")
    assert resp.status == 200
    body = await resp.json()
    assert body["client_status"] == status
    assert verify_client_status(
        gfs_key,
        nonce,
        question,
        status,
        body["client_status_sig"],
        body["client_status_sig_suite"],
    )
    # The possession proof rides along unchanged.
    assert verify_possession_proof(
        gfs_key, nonce, BASE_URL, body["proof_sig"], body["proof_sig_suite"]
    )


async def test_an_unknown_instance_and_a_bad_signature_answer_the_same(client):
    """No registration oracle: only the key holder learns a real status."""
    home = generate_identity_keypair()
    impostor = generate_identity_keypair()
    await _register(client, home, "home-a", "active")
    gfs_key = await _gfs_key(client)
    for n, (kp, iid) in enumerate(((impostor, "home-a"), (home, "home-gone"))):
        nonce, question, resp = await _ask(client, kp, iid, n=60 + n)
        assert resp.status == 200
        body = await resp.json()
        assert body["client_status"] == "unknown"
        assert verify_client_status(
            gfs_key,
            nonce,
            question,
            "unknown",
            body["client_status_sig"],
            body["client_status_sig_suite"],
        )


async def test_a_rejected_household_reads_unknown(client):
    """The operator's reject deletes the row: the household reads unknown."""
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "pending")
    await client.server.app[gfs_fed_repo_key].delete_instance("home-a")
    _, _, resp = await _ask(client, home, "home-a")
    assert (await resp.json())["client_status"] == "unknown"


async def test_a_question_meant_for_another_gfs_reads_unknown(client):
    """Another GFS that saw the household's question can't replay it here
    to learn whether — or how — the household is registered."""
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "banned")
    other_gfs = generate_identity_keypair().public_key.hex()
    _, _, resp = await _ask(client, home, "home-a", n=73, gfs_key=other_gfs)
    assert (await resp.json())["client_status"] == "unknown"


@pytest.mark.parametrize("suite", ["ed448", "ED25519", ""])
async def test_a_tampered_question_suite_gets_no_signed_status(client, suite):
    """Finding 1: an on-path edit of ``sig_suite`` used to make the GFS sign
    ``unknown`` over the household's real id, nonce and signature — which
    the household accepted and suspended on. Now it is an unsigned ``400``
    (the household asks again later) and nothing is signed."""
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "active")
    _, _, resp = await _ask(
        client, home, "home-a", n=70, edit=lambda b: {**b, "sig_suite": suite}
    )
    assert resp.status == 400
    assert "client_status_sig" not in await resp.text()


async def test_a_replayed_question_past_its_window_gets_no_signed_status(client):
    """Finding 2: a captured question is no lasting status oracle. Inside
    its window it is answered; replayed after it, the GFS refuses it with
    an unsigned ``400`` and signs nothing."""
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "pending")
    gfs_key = await _gfs_key(client)
    nonce = new_proof_nonce()
    asked_at = status_request_ts(time.time())
    question = sign_status_request(
        home.private_key, gfs_key, "home-a", nonce, ts=asked_at
    )
    body = {"nonce": nonce, "client": question.to_wire()}
    fresh = await client.post(PROOF_ROUTE, json=body, headers=_ip(74))
    assert (await fresh.json())["client_status"] == "pending"
    later = time.time() + CLIENT_STATUS_REQUEST_MAX_AGE_S + 120
    with patch.object(relay_routes.time, "time", return_value=later):
        replay = await client.post(PROOF_ROUTE, json=body, headers=_ip(75))
    assert replay.status == 400
    assert "client_status" not in await replay.text()


async def test_a_question_from_the_future_gets_no_signed_status(client):
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "active")
    ahead = status_request_ts(time.time()) + CLIENT_STATUS_REQUEST_MAX_FUTURE_S + 120
    _, _, resp = await _ask(client, home, "home-a", n=76, ts=ahead)
    assert resp.status == 400


@pytest.mark.parametrize("size", [0, 32, 63, 65])
async def test_a_question_sig_of_the_wrong_length_gets_no_signed_status(client, size):
    """Finding 5: ``sig`` must decode to exactly 64 bytes — refused before
    the GFS signs anything."""
    home = generate_identity_keypair()
    await _register(client, home, "home-a", "active")
    _, _, resp = await _ask(
        client,
        home,
        "home-a",
        n=77,
        edit=lambda b: {**b, "sig": b64url_encode(b"x" * size)},
    )
    assert resp.status == 400
    assert "client_status" not in await resp.text()


@pytest.mark.parametrize(
    "block",
    [
        "x",
        [],
        {"instance_id": "home-a"},
        {"instance_id": 1, "sig": "s", "sig_suite": "ed25519", "ts": 0},
    ],
)
async def test_a_malformed_client_block_is_400(client, block):
    resp = await client.post(
        PROOF_ROUTE,
        json={"nonce": new_proof_nonce(), "client": block},
        headers=_ip(71),
    )
    assert resp.status == 400


async def test_without_a_client_block_the_answer_carries_no_status(client):
    resp = await client.post(
        PROOF_ROUTE, json={"nonce": new_proof_nonce()}, headers=_ip(72)
    )
    assert "client_status" not in await resp.json()
