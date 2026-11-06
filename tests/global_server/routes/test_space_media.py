"""The space media cache over HTTP — real GFS app, real SQLite, real client.

One scope-free path: ``PUT /gfs/media/{blob_id}`` (header-authorized,
identity free — the scope only inside the signed claim), ``GET
/gfs/media/{blob_id}`` (public, CDN-cacheable for an hour) and ``POST
/gfs/media/{blob_id}/delete`` (the scope's seed holder; the scope only inside
the signed body).
"""

from __future__ import annotations

import asyncio
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
    AUTH_CHANNEL,
    AUTH_CHANNEL_CERT,
    AUTH_WRITER_CERT,
    CHANNEL_MEDIA_DELETE_PREFIX,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SPACE_MEDIA_AUTH_HEADER,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    media_delete_payload,
)
from socialhome.gfs_channel import channel_pk_of, issue_channel_cert, signing_bytes
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_fed_repo_key,
    gfs_space_media_store_key,
)
from socialhome.global_server.blob_store import BLOB_OVERHEAD
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.routes import space_media as space_media_routes
from socialhome.global_server.routes.space_media import GfsMediaView
from socialhome.global_server.server import create_gfs_app
from socialhome.global_server.space_media_auth import (
    SPACE_MEDIA_MAX_PER_MINUTE_PER_IP,
    is_space_media_write,
)
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS, blob_id_of
from socialhome.writer_cert import sign_writer_cert

SPACE = "sp-media"
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
SMALL = PAD_BUCKETS[1] + BLOB_OVERHEAD
CHANNEL = "0f" * 16
CHANNEL_SEED = os.urandom(32)
_REFUSED = {"error": "not authorized for this scope"}


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
        channels = app[gfs_channel_repo_key]
        now = int(time.time())
        await channels.register(
            CHANNEL,
            channel_suite="ed25519",
            channel_pk=channel_pk_of(CHANNEL_SEED),
            now=now,
        )
        await channels.set_epoch(CHANNEL, 3, expected=None, now=now)
        tc.app_ = app
        yield tc


def _claim(
    data: bytes, *, space=SPACE, ts=None, expires=None, size=None, scope=SCOPE_SPACE
):
    now = int(time.time())
    return SpaceMediaClaim(
        scope=scope,
        scope_id=space,
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
        space_id=claim.scope_id,
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


def _put_url(data: bytes) -> str:
    """Every write names only the blob — never a space or a channel."""
    return f"/gfs/media/{blob_id_of(data)}"


_ch_url = _put_url


def _get_url(data: bytes) -> str:
    return f"/gfs/media/{blob_id_of(data)}"


def _ch_claim(data: bytes, **over) -> SpaceMediaClaim:
    return _claim(data, space=CHANNEL, scope=SCOPE_CHANNEL, **over)


def _channel_header(claim: SpaceMediaClaim, seed: bytes = CHANNEL_SEED) -> dict:
    sig = sign_ed25519(seed, claim.channel_signing_bytes())
    auth = SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig=b64url_encode(sig), sig_suite="ed25519"
    )
    return {SPACE_MEDIA_AUTH_HEADER: auth.to_header()}


def _channel_cert_header(claim: SpaceMediaClaim, *, epoch: int = 3) -> dict:
    holder = os.urandom(32)
    cert = issue_channel_cert(
        channel_seed=CHANNEL_SEED,
        channel_id=claim.scope_id,
        epoch=epoch,
        instance_pk=ed25519_public_key(holder),
        scope="comment",
    )
    sig = sign_ed25519(
        holder, claim.writer_signing_bytes(kind=AUTH_CHANNEL_CERT, epoch=epoch)
    )
    auth = SpaceMediaUploadAuth(
        claim=claim,
        kind=AUTH_CHANNEL_CERT,
        sig=b64url_encode(sig),
        sig_suite="ed25519",
        epoch=epoch,
        channel_cert=cert,
    )
    return {SPACE_MEDIA_AUTH_HEADER: auth.to_header()}


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
    g = await gfs.get(f"/gfs/media/{'a' * 64}")
    assert g.status == 404
    assert g.headers["Cache-Control"] == "no-store"
    assert g.headers["Access-Control-Allow-Origin"] == "*"


async def test_a_non_address_is_not_routed_to_the_cache(gfs):
    g = await gfs.get("/gfs/media/not-a-blob")
    assert g.headers.get("Cache-Control") != "public, max-age=3600, immutable"


# ── Uniform refusals ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case", ["wrong_key", "unknown_space", "banned", "stale_ts", "future_ts", "path"]
)
async def test_every_auth_failure_is_the_same_403(gfs, case):
    data = os.urandom(SMALL)
    if case == "wrong_key":
        headers = _authority_header(_claim(data), seed=os.urandom(32))
    elif case == "unknown_space":
        headers = _authority_header(_claim(data, space="sp-nope"))
    elif case == "banned":
        headers = _authority_header(_claim(data, space="sp-banned"))
    elif case == "stale_ts":
        headers = _authority_header(_claim(data, ts=int(time.time()) - 301))
    elif case == "future_ts":
        headers = _authority_header(_claim(data, ts=int(time.time()) + 301))
    else:  # signed for another blob than the path names
        headers = _authority_header(_claim(os.urandom(SMALL)))
    r = await gfs.put(_put_url(data), data=data, headers=headers)
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
    # A real (smaller) bucket signed, a larger body sent.
    claim = _claim(data, size=PAD_BUCKETS[0] + BLOB_OVERHEAD)
    r = await gfs.put(_put_url(data), data=data, headers=_authority_header(claim))
    assert r.status == 413
    # Its reserved upload slot is given back.
    store = gfs.app_[gfs_space_media_store_key]
    bucket = int(time.time()) // 3600
    assert await store._repo.upload_counts("space", SPACE, bucket=bucket) == (0, 0)


@pytest.mark.parametrize("gfs", [{"space_media_max_bytes": SMALL}], indirect=True)
async def test_a_blob_larger_than_the_cache_is_413_unread(gfs):
    data = os.urandom(PAD_BUCKETS[2] + BLOB_OVERHEAD)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 413
    assert (await r.json())["error"] == "too_large_for_cache"
    store = gfs.app_[gfs_space_media_store_key]
    assert (await store.stats())["refused_too_large"] == 1


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
async def test_the_upload_rate_is_429(gfs):
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    await gfs.put(_put_url(first), data=first, headers=_authority_header(_claim(first)))
    r = await gfs.put(
        _put_url(second), data=second, headers=_authority_header(_claim(second))
    )
    assert r.status == 429
    assert (await r.json())["error"] == "space_media_quota"
    assert "Retry-After" in r.headers


# ── Security review F7: nothing is read before the cheap checks ──────────


@pytest.fixture
def body_reads(monkeypatch):
    """Count (and optionally stall) the upload view's body reads."""
    reads = {"n": 0, "stall": 0.0}
    real = GfsMediaView.bounded_raw

    async def spy(self, max_bytes):
        reads["n"] += 1
        if reads["stall"]:
            await asyncio.sleep(reads["stall"])
        return await real(self, max_bytes)

    monkeypatch.setattr(GfsMediaView, "bounded_raw", spy)
    return reads


@pytest.mark.parametrize(
    "gfs", [{"space_media_space_uploads_per_hour": 1}], indirect=True
)
async def test_the_rate_is_checked_before_the_body_is_read(gfs, body_reads):
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    await gfs.put(_put_url(first), data=first, headers=_authority_header(_claim(first)))
    assert body_reads["n"] == 1
    r = await gfs.put(
        _put_url(second), data=second, headers=_authority_header(_claim(second))
    )
    assert r.status == 429
    assert body_reads["n"] == 1


async def test_a_size_that_is_no_padded_bucket_is_refused_unread(gfs, body_reads):
    data = os.urandom(SMALL + 1)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 422
    assert (await r.json())["error"] == "not_a_padded_blob"
    assert body_reads["n"] == 0


async def test_a_retry_of_a_held_blob_is_answered_unread(gfs, body_reads):
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert await r.json() == {"stored": False, "status": "already_stored"}
    assert body_reads["n"] == 1


async def test_a_body_that_stalls_times_out_and_refunds_its_slot(
    gfs, body_reads, monkeypatch
):
    monkeypatch.setattr(space_media_routes, "BODY_READ_TIMEOUT_S", 0.05)
    body_reads["stall"] = 0.5
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 408
    store = gfs.app_[gfs_space_media_store_key]
    bucket = int(time.time()) // 3600
    assert await store._repo.upload_counts("space", SPACE, bucket=bucket) == (0, 0)
    assert (await gfs.get(_get_url(data))).status == 404


# ── Delete ───────────────────────────────────────────────────────────────


def _delete_body(data: bytes, *, seed: bytes = SEED, ts: int | None = None) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_MEDIA_DELETE,
        space_id=SPACE,
        payload=media_delete_payload(SCOPE_SPACE, SPACE, blob_id_of(data), ts),
        space_seed=seed,
    )
    return {
        "scope": SCOPE_SPACE,
        "scope_id": SPACE,
        "ts": ts,
        "sig": sig["authority_sig"],
        "sig_suite": sig["authority_sig_suite"],
    }


def _channel_delete_body(data: bytes, *, seed: bytes = CHANNEL_SEED) -> dict:
    ts = int(time.time())
    body = media_delete_payload(SCOPE_CHANNEL, CHANNEL, blob_id_of(data), ts)
    sig = sign_ed25519(seed, signing_bytes(CHANNEL_MEDIA_DELETE_PREFIX, body))
    return {
        "scope": SCOPE_CHANNEL,
        "scope_id": CHANNEL,
        "ts": ts,
        "sig": b64url_encode(sig),
        "sig_suite": "ed25519",
    }


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
        "space_max_bytes": 256 * 1024**2,
        "space_uploads_per_hour": 300,
        "scopes": ["channel", "space"],
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


async def test_the_writes_are_shed_per_ip_by_route_not_by_raw_path(gfs):
    """Security review F6: the limiter matches the routed write (PUT and
    the delete), so no spelling of the path slips past it; the public GET
    of the same path is not limited (a CDN serves most of those)."""
    assert SPACE_MEDIA_MAX_PER_MINUTE_PER_IP >= 60
    data = os.urandom(SMALL)
    statuses: dict[int, int] = {}
    for _ in range(SPACE_MEDIA_MAX_PER_MINUTE_PER_IP + 5):
        r = await gfs.put(
            _put_url(data), data=data, headers={SPACE_MEDIA_AUTH_HEADER: "x"}
        )
        statuses[r.status] = statuses.get(r.status, 0) + 1
    assert statuses.get(429, 0) == 5
    assert (await gfs.post(_put_url(data) + "/delete", json={})).status == 429
    assert (await gfs.get(_get_url(data))).status == 404


def test_the_write_limiter_is_cluster_wide():
    """Each node allows its share of the per-IP budget over the live nodes."""
    assert is_space_media_write("PUT", "/gfs/media/{blob_id}")
    assert is_space_media_write("POST", "/gfs/media/{blob_id}/delete")
    assert not is_space_media_write("GET", "/gfs/media/{blob_id}")
    assert not is_space_media_write("PUT", "/gfs/moments/media/{blob_id}")


async def test_the_store_is_wired(gfs):
    store = gfs.app_[gfs_space_media_store_key]
    assert store.limits.ttl_s == 3 * 86_400


# ── Channel scope: a private space's opaque channel ──────────────────────


async def test_a_channel_key_upload_is_fetched_by_address(gfs):
    """A private space's invite-link members get its pictures too: the
    channel key uploads under the opaque channel id, anyone fetches by
    address — the GFS never learns the space."""
    data = os.urandom(SMALL)
    r = await gfs.put(
        _ch_url(data), data=data, headers=_channel_header(_ch_claim(data))
    )
    assert r.status == 201
    g = await gfs.get(_get_url(data))
    assert g.status == 200
    assert await g.read() == data
    claims = await gfs.app_[gfs_space_media_store_key]._repo.claims_of(blob_id_of(data))
    assert [(c.scope_kind, c.scope_id) for c in claims] == [("channel", CHANNEL)]


async def test_a_channel_cert_holder_uploads(gfs):
    data = os.urandom(SMALL)
    r = await gfs.put(
        _ch_url(data), data=data, headers=_channel_cert_header(_ch_claim(data))
    )
    assert r.status == 201


@pytest.mark.parametrize("case", ["wrong_key", "unknown_channel"])
async def test_every_channel_auth_failure_is_the_same_403(gfs, case):
    data = os.urandom(SMALL)
    url = _ch_url(data)
    if case == "wrong_key":
        headers = _channel_header(_ch_claim(data), seed=os.urandom(32))
    else:
        headers = _channel_header(_claim(data, space="1e" * 16, scope=SCOPE_CHANNEL))
    r = await gfs.put(url, data=data, headers=headers)
    assert r.status == 403
    assert await r.json() == _REFUSED


async def test_the_scoped_write_paths_are_gone(gfs):
    """Security review F3: a channel id never rides in a URL path (access
    logs, proxies) — the old scoped upload / delete routes do not exist."""
    data = os.urandom(SMALL)
    bid = blob_id_of(data)
    for url, headers in (
        (f"/gfs/spaces/{SPACE}/media/{bid}", _authority_header(_claim(data))),
        (f"/gfs/channels/{CHANNEL}/media/{bid}", _channel_header(_ch_claim(data))),
    ):
        r = await gfs.put(url, data=data, headers=headers)
        assert r.status in (404, 405), url
        r = await gfs.post(url + "/delete", json={})
        assert r.status in (404, 405), url
    assert (await gfs.get(_get_url(data))).status == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scope": SCOPE_SPACE, "scope_id": 5},
        {"scope": "room", "scope_id": SPACE},
    ],
)
async def test_a_delete_names_its_scope_in_the_signed_body(gfs, body):
    """The scope rides in the body the signature covers; without a usable
    one the delete is the uniform 403."""
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    signed = _delete_body(data)
    r = await gfs.post(
        _put_url(data) + "/delete",
        json={**signed, **body}
        if body
        else {k: v for k, v in signed.items() if k not in ("scope", "scope_id")},
    )
    assert r.status == 403
    assert (await gfs.get(_get_url(data))).status == 200


@pytest.mark.parametrize("gfs", [{"space_media_space_max_bytes": SMALL}], indirect=True)
async def test_a_full_channel_evicts_its_oldest_not_refuses(gfs):
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    await gfs.put(_ch_url(first), data=first, headers=_channel_header(_ch_claim(first)))
    r = await gfs.put(
        _ch_url(second), data=second, headers=_channel_header(_ch_claim(second))
    )
    assert r.status == 201
    assert (await gfs.get(_get_url(second))).status == 200
    assert (await gfs.get(_get_url(first))).status == 404


@pytest.mark.parametrize(
    "gfs", [{"space_media_space_uploads_per_hour": 1}], indirect=True
)
async def test_the_upload_rate_is_per_channel(gfs):
    first, second, third = (os.urandom(SMALL) for _ in range(3))
    await gfs.put(_ch_url(first), data=first, headers=_channel_header(_ch_claim(first)))
    r = await gfs.put(
        _ch_url(second), data=second, headers=_channel_header(_ch_claim(second))
    )
    assert r.status == 429
    # A space's budget is its own.
    r = await gfs.put(
        _put_url(third), data=third, headers=_authority_header(_claim(third))
    )
    assert r.status == 201


async def test_the_channel_key_deletes_a_blob(gfs):
    data = os.urandom(SMALL)
    await gfs.put(_ch_url(data), data=data, headers=_channel_header(_ch_claim(data)))
    bad = await gfs.post(
        _ch_url(data) + "/delete",
        json=_channel_delete_body(data, seed=os.urandom(32)),
    )
    assert bad.status == 403
    assert await bad.json() == _REFUSED
    r = await gfs.post(_ch_url(data) + "/delete", json=_channel_delete_body(data))
    assert await r.json() == {"deleted": True}
    assert (await gfs.get(_get_url(data))).status == 404


# ── Review fixes: disabled cache, claims across scopes ───────────────────


@pytest.mark.parametrize("gfs", [{"space_media_max_bytes": 0}], indirect=True)
async def test_a_turned_off_cache_answers_503_before_anything(gfs):
    data = os.urandom(SMALL)
    # Even an unauthenticated upload gets the clear answer — not a 403/413.
    r = await gfs.put(_put_url(data), data=data, headers={})
    assert r.status == 503
    assert await r.json() == {"error": "space_media_disabled"}
    r = await gfs.put(
        _ch_url(data), data=data, headers=_channel_header(_ch_claim(data))
    )
    assert r.status == 503
    stats = await gfs.app_[gfs_space_media_store_key].stats()
    assert (stats["refused_too_large"], stats["refused_quota"]) == (0, 0)


async def test_identical_bytes_from_another_scope_coalesce_and_survive_its_delete(gfs):
    """No address squatting: a scope that uploaded a blob first can't take
    it away from another scope that uploads the same bytes."""
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 201
    r = await gfs.put(
        _ch_url(data), data=data, headers=_channel_header(_ch_claim(data))
    )
    assert r.status == 200
    assert await r.json() == {"stored": False, "status": "already_stored"}
    r = await gfs.post(_put_url(data) + "/delete", json=_delete_body(data))
    assert await r.json() == {"deleted": True}
    g = await gfs.get(_get_url(data))
    assert g.status == 200
    assert await g.read() == data
    r = await gfs.post(_ch_url(data) + "/delete", json=_channel_delete_body(data))
    assert await r.json() == {"deleted": True}
    assert (await gfs.get(_get_url(data))).status == 404


# ── Security review F2: no blob behind the public static mount ───────────


async def test_space_blobs_are_not_served_by_the_public_media_mount(gfs, tmp_dir):
    """A space blob is served only by ``GET /gfs/media/{id}`` (ban, expiry,
    tombstone and headers apply) — never as a static file, and it isn't
    stored under the public ``media/`` dir at all."""
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 201
    assert (await gfs.get(f"/media/spaces/{blob_id_of(data)}")).status == 404
    assert not (tmp_dir / "media" / "spaces" / blob_id_of(data)).exists()
    assert (tmp_dir / "space_media" / blob_id_of(data)).exists()


# ── Security review F5: a captured delete can't be replayed ──────────────


async def test_a_replayed_delete_is_the_uniform_403(gfs):
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    body = _delete_body(data)
    assert (await gfs.post(_put_url(data) + "/delete", json=body)).status == 200
    r = await gfs.post(_put_url(data) + "/delete", json=body)
    assert r.status == 403
    assert await r.json() == _REFUSED
