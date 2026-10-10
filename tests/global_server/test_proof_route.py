"""``POST /gfs/proof`` — the GFS proves it holds its identity key.

The household sends a fresh nonce; the GFS signs it, bound to its own
configured ``base_url``, with the identity key ``/gfs/info`` publishes. The
endpoint is unauthenticated, so it is body-capped and rate-limited, and it
takes nothing from the requester but the nonce.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode
from socialhome.gfs_proof import (
    PROOF_CAPABILITY,
    PROOF_ROUTE,
    PROOF_SIG_SUITE_ED25519,
    new_proof_nonce,
    verify_possession_proof,
)
from socialhome.global_server import public
from socialhome.global_server.app_keys import gfs_cluster_key
from socialhome.global_server.config import GfsConfig
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
        "base_url",
        "nonce",
        "proof_sig",
        "proof_sig_suite",
    }
    assert body["gfs_instance_id"] == "gfs-proof"
    assert body["base_url"] == BASE_URL
    assert body["nonce"] == nonce
    assert body["proof_sig_suite"] == PROOF_SIG_SUITE_ED25519
    assert verify_possession_proof(
        info["public_key"],
        info["gfs_instance_id"],
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
        info["gfs_instance_id"],
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
    sig, suite = cluster.sign_possession_proof("gfs-proof", new_proof_nonce(), BASE_URL)
    assert sig and suite == PROOF_SIG_SUITE_ED25519
    monkeypatch.setattr(cluster, "_signing_key", b"")
    assert cluster.sign_possession_proof("gfs-proof", new_proof_nonce(), BASE_URL) == (
        "",
        "",
    )
