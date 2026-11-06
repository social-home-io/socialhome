"""The space media cache over HTTP — real GFS app, real SQLite, real client.

``PUT /gfs/spaces/{space_id}/media/{blob_id}`` (header-authorized, identity
free), ``GET /gfs/spaces/media/{blob_id}`` (public, CDN-cacheable for an hour)
and ``POST /gfs/spaces/{space_id}/media/{blob_id}/delete`` (space authority).
"""

from __future__ import annotations

import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
    sign_authority_event,
)
from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.gfs_space_media import (
    AUTH_AUTHORITY,
    AUTH_WRITER_CERT,
    SPACE_MEDIA_AUTH_HEADER,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    space_media_delete_payload,
)
from socialhome.global_server.app_keys import (
    gfs_fed_repo_key,
    gfs_space_media_store_key,
)
from socialhome.global_server.blob_store import BLOB_OVERHEAD
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.space_media_auth import (
    SPACE_MEDIA_MAX_PER_MINUTE_PER_IP,
    is_space_media_write_path,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, blob_id_of
from socialhome.writer_cert import sign_writer_cert

SPACE = "sp-media"
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
SMALL = PAD_BUCKETS[1] + BLOB_OVERHEAD
_REFUSED = {"error": "not authorized for this space"}


async def _app(tmp_dir, **cfg_over):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
        **cfg_over,
    )
    app = create_gfs_app(cfg)
    return app


@pytest.fixture
async def gfs(tmp_dir, request):
    app = await _app(tmp_dir, **(getattr(request, "param", None) or {}))
    async with TestClient(TestServer(app)) as tc:
        fed = app[gfs_fed_repo_key]
        await fed.upsert_instance(
            ClientInstance(instance_id="owner", display_name="O", public_key="aa" * 32)
        )
        for sid in (SPACE, "sp-banned"):
            await fed.upsert_space(
                GlobalSpace(
                    space_id=sid,
                    owning_instance="owner",
                    status="active",
                    identity_public_key=PK.hex(),
                )
            )
        await fed.set_space_status("sp-banned", "banned")
        tc.app_ = app
        yield tc


def _claim(data: bytes, *, space=SPACE, ts=None, expires=None, size=None):
    now = int(time.time())
    return SpaceMediaClaim(
        space_id=space,
        blob_id=blob_id_of(data),
        size=len(data) if size is None else size,
        expires_at=expires if expires is not None else now + 86_400,
        ts=now if ts is None else ts,
    )


def _authority_header(claim: SpaceMediaClaim, seed: bytes = SEED) -> dict:
    sig = sign_ed25519(seed, claim.authority_signing_bytes())
    auth = SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_AUTHORITY, sig=b64url_encode(sig), sig_suite="ed25519"
    )
    return {
        SPACE_MEDIA_AUTH_HEADER: auth.to_header(),
        "Content-Type": "application/octet-stream",
    }


def _writer_header(claim: SpaceMediaClaim, *, epoch: int = 3) -> dict:
    holder = os.urandom(32)
    cert = sign_writer_cert(
        space_seed=SEED,
        space_id=claim.space_id,
        epoch=epoch,
        instance_pk=ed25519_public_key(holder),
        scope="comment",
    )
    sig = sign_ed25519(
        holder, claim.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=epoch)
    )
    auth = SpaceMediaUploadAuth(
        claim=claim,
        kind=AUTH_WRITER_CERT,
        sig=b64url_encode(sig),
        sig_suite="ed25519",
        epoch=epoch,
        writer_cert=cert,
    )
    return {SPACE_MEDIA_AUTH_HEADER: auth.to_header()}


def _put_url(data: bytes, space: str = SPACE) -> str:
    return f"/gfs/spaces/{space}/media/{blob_id_of(data)}"


def _get_url(data: bytes) -> str:
    return f"/gfs/spaces/media/{blob_id_of(data)}"


# ── Upload + fetch ───────────────────────────────────────────────────────


async def test_authority_upload_then_anyone_fetches(gfs):
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 201
    assert await r.json() == {"stored": True}
    g = await gfs.get(_get_url(data))
    assert g.status == 200
    assert await g.read() == data
    assert g.headers["Cache-Control"] == "public, max-age=3600, immutable"
    assert g.headers["Access-Control-Allow-Origin"] == "*"
    assert g.headers["Cross-Origin-Resource-Policy"] == "cross-origin"
    assert g.headers["Content-Type"] == "application/octet-stream"
    assert g.headers["X-Content-Type-Options"] == "nosniff"


async def test_a_re_upload_is_200_already_stored(gfs):
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 200
    assert await r.json() == {"stored": False, "status": "already_stored"}


async def test_a_writer_cert_holder_uploads(gfs):
    data = os.urandom(SMALL)
    r = await gfs.put(_put_url(data), data=data, headers=_writer_header(_claim(data)))
    assert r.status == 201


async def test_a_miss_is_404_no_store(gfs):
    g = await gfs.get(f"/gfs/spaces/media/{'a' * 64}")
    assert g.status == 404
    assert g.headers["Cache-Control"] == "no-store"
    assert g.headers["Access-Control-Allow-Origin"] == "*"


async def test_a_non_address_is_not_routed_to_the_cache(gfs):
    g = await gfs.get("/gfs/spaces/media/not-a-blob")
    assert g.headers.get("Cache-Control") != "public, max-age=3600, immutable"


# ── Uniform refusals ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case", ["wrong_key", "unknown_space", "banned", "stale_ts", "future_ts", "path"]
)
async def test_every_auth_failure_is_the_same_403(gfs, case):
    data = os.urandom(SMALL)
    space = SPACE
    if case == "wrong_key":
        headers = _authority_header(_claim(data), seed=os.urandom(32))
    elif case == "unknown_space":
        space = "sp-nope"
        headers = _authority_header(_claim(data, space=space))
    elif case == "banned":
        space = "sp-banned"
        headers = _authority_header(_claim(data, space=space))
    elif case == "stale_ts":
        headers = _authority_header(_claim(data, ts=int(time.time()) - 301))
    elif case == "future_ts":
        headers = _authority_header(_claim(data, ts=int(time.time()) + 301))
    else:  # signed for another space than the path names
        headers = _authority_header(_claim(data))
        space = "sp-banned"
    r = await gfs.put(_put_url(data, space), data=data, headers=headers)
    assert r.status == 403
    assert await r.json() == _REFUSED
    assert (await gfs.get(_get_url(data))).status == 404


@pytest.mark.parametrize("header", [None, "", "%%%", "a" * 5000])
async def test_a_missing_or_malformed_header_is_400(gfs, header):
    data = os.urandom(SMALL)
    headers = {} if header is None else {SPACE_MEDIA_AUTH_HEADER: header}
    r = await gfs.put(_put_url(data), data=data, headers=headers)
    assert r.status == 400


# ── Body bounds ──────────────────────────────────────────────────────────


async def test_a_body_other_than_the_signed_size_is_422(gfs):
    data = os.urandom(SMALL)
    claim = _claim(data, size=SMALL + 1)
    r = await gfs.put(_put_url(data), data=data, headers=_authority_header(claim))
    assert r.status == 422


async def test_a_body_past_the_signed_size_is_413_before_buffering(gfs):
    data = os.urandom(SMALL)
    claim = _claim(data, size=SMALL - 1)
    r = await gfs.put(_put_url(data), data=data, headers=_authority_header(claim))
    assert r.status == 413


@pytest.mark.parametrize("gfs", [{"space_media_max_bytes": SMALL}], indirect=True)
async def test_a_blob_larger_than_the_cache_is_413_unread(gfs):
    data = os.urandom(PAD_BUCKETS[2] + BLOB_OVERHEAD)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 413


async def test_a_padded_bucket_and_sha_are_required(gfs):
    data = os.urandom(1000)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 422
    assert (await r.json())["error"] == "not_a_padded_blob"


@pytest.mark.parametrize(
    "gfs", [{"space_media_space_uploads_per_hour": 1}], indirect=True
)
async def test_the_space_quota_is_429(gfs):
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    await gfs.put(_put_url(first), data=first, headers=_authority_header(_claim(first)))
    r = await gfs.put(
        _put_url(second), data=second, headers=_authority_header(_claim(second))
    )
    assert r.status == 429
    assert (await r.json())["error"] == "space_media_quota"
    assert "Retry-After" in r.headers


# ── Delete ───────────────────────────────────────────────────────────────


def _delete_body(data: bytes, *, seed: bytes = SEED, ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
        space_id=SPACE,
        payload=space_media_delete_payload(SPACE, blob_id_of(data), ts),
        space_seed=seed,
    )
    return {"ts": ts, **sig}


async def test_the_space_authority_deletes_a_blob(gfs):
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    bad = await gfs.post(
        _put_url(data) + "/delete", json=_delete_body(data, seed=os.urandom(32))
    )
    assert bad.status == 403
    assert await bad.json() == _REFUSED
    r = await gfs.post(_put_url(data) + "/delete", json=_delete_body(data))
    assert r.status == 200
    assert await r.json() == {"deleted": True}
    assert (await gfs.get(_get_url(data))).status == 404


async def test_a_malformed_delete_body_is_400(gfs):
    data = os.urandom(SMALL)
    r = await gfs.post(_put_url(data) + "/delete", data=b"[]")
    assert r.status == 400


# ── Capability, rate limit ───────────────────────────────────────────────


async def test_info_advertises_the_cache_in_the_signed_block(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    assert body["capabilities"]["space_media"] == {
        "max_blob_bytes": MAX_BLOB_BYTES,
        "ttl_s": 3 * 86_400,
    }
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


@pytest.mark.parametrize("gfs", [{"space_media_max_bytes": 0}], indirect=True)
async def test_a_turned_off_cache_is_not_advertised(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    assert "space_media" not in body["capabilities"]


def test_only_the_write_paths_are_rate_limited_per_ip():
    assert SPACE_MEDIA_MAX_PER_MINUTE_PER_IP >= 60
    assert is_space_media_write_path(f"/gfs/spaces/s/media/{'a' * 64}")
    assert is_space_media_write_path(f"/gfs/spaces/s/media/{'a' * 64}/delete")
    assert not is_space_media_write_path(f"/gfs/spaces/media/{'a' * 64}")
    assert not is_space_media_write_path("/gfs/spaces/s/epoch")
    assert not is_space_media_write_path("/gfs/spaces/s/media")


async def test_the_store_is_wired(gfs):
    store = gfs.app_[gfs_space_media_store_key]
    assert store.limits.ttl_s == 3 * 86_400
