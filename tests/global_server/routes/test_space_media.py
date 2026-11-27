"""The space media cache over HTTP — real GFS app, real SQLite, real client.

One scope-free path: ``PUT /gfs/media/{blob_id}`` (header-authorized,
identity free — the scope only inside the signed claim), ``GET
/gfs/media/{blob_id}`` (public, CDN-cacheable for an hour) and ``POST
/gfs/media/{blob_id}/delete`` (the scope's seed holder; the scope only inside
the signed body).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
from datetime import UTC, datetime

import orjson
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.gfs_space_media import (
    ADDR_SUITE_SM1P,
    AUTH_CHANNEL,
    CHANNEL_MEDIA_DELETE_PREFIX,
    PIECE_PROOF_HEADER,
    SCOPE_CHANNEL,
    SPACE_MEDIA_AUTH_HEADER,
    PieceProof,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    media_delete_payload,
    piece_address,
    pieced_file_address,
    space_media_address,
)
from socialhome.gfs_channel import (
    channel_pk_of,
    new_channel_id,
    sign_register,
    signing_bytes,
)
from socialhome.global_server import space_media as space_media_mod
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
from socialhome.moment_public_media import MAX_BLOB_BYTES, PAD_BUCKETS

SMALL = PAD_BUCKETS[1] + BLOB_OVERHEAD
CHANNEL = "0f" * 16
CHANNEL_SEED = os.urandom(32)
#: Since v_96 the cache takes only the channel scope (owner decision Q5):
#: the tests' default scope is the channel.
SPACE = CHANNEL
#: The listed space's authority seed — it authorizes nothing here any more.
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
SCOPE_SPACE = "space"
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
            ClientInstance(
                instance_id="owner",
                display_name="O",
                public_key="aa" * 32,
                status="active",
            )
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
        # The channel is IN USE: two member households hold a seat (a pass
        # for an open epoch) — media uploads need that (security review F1,
        # re-attack N2).
        await fed.upsert_instance(
            ClientInstance(
                instance_id="member2",
                display_name="M",
                public_key="cc" * 32,
                status="active",
            )
        )
        await channels.add_subscriber(CHANNEL, "owner", pass_epoch=3, now=now)
        await channels.add_subscriber(CHANNEL, "member2", pass_epoch=3, now=now)
        tc.app_ = app
        yield tc


#: The scope-bound address each test blob was last named under.
_ADDR: dict[bytes, str] = {}


def _addr(data: bytes, space: str = SPACE, scope: str = SCOPE_CHANNEL) -> str:
    """The blob's scope-bound address (security review v2, N1)."""
    _ADDR[data] = space_media_address(scope, space, data)
    return _ADDR[data]


def _bid(data: bytes) -> str:
    return _ADDR.get(data) or space_media_address(SCOPE_CHANNEL, SPACE, data)


def _claim(
    data: bytes, *, space=SPACE, ts=None, expires=None, size=None, scope=SCOPE_CHANNEL
):
    now = int(time.time())
    return SpaceMediaClaim(
        scope=scope,
        scope_id=space,
        blob_id=_addr(data, space, scope),
        size=len(data) if size is None else size,
        expires_at=expires if expires is not None else now + 86_400,
        ts=now if ts is None else ts,
    )


def _authority_header(claim: SpaceMediaClaim, seed: bytes = CHANNEL_SEED) -> dict:
    """The seed holder's upload header — since v_96 the channel key's."""
    sig = sign_ed25519(seed, claim.channel_signing_bytes())
    auth = SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig=b64url_encode(sig), sig_suite="ed25519"
    )
    return {
        SPACE_MEDIA_AUTH_HEADER: auth.to_header(),
        "Content-Type": "application/octet-stream",
    }


def _identified_header(claim: SpaceMediaClaim, kind: str) -> dict:
    """A header naming one of the identified kinds that left with the
    identified member publish (v_90): ``writer_cert`` / ``channel_cert``."""
    body = {
        **claim.signing_body(),
        "auth": kind,
        "sig": "x" * 86,
        "sig_suite": "ed25519",
        "epoch": 3,
        kind: {"cert_suite": "ed25519"},
    }
    raw = base64.urlsafe_b64encode(orjson.dumps(body)).rstrip(b"=").decode()
    return {SPACE_MEDIA_AUTH_HEADER: raw}


def _put_url(data: bytes, space: str = SPACE) -> str:
    """Every write names only the blob — never a space or a channel (the
    scope is bound into the address and named in the signed claim)."""
    return f"/gfs/media/{_addr(data, space)}"


def _ch_url(data: bytes, channel: str = CHANNEL) -> str:
    return f"/gfs/media/{_addr(data, channel, SCOPE_CHANNEL)}"


def _get_url(data: bytes) -> str:
    return f"/gfs/media/{_bid(data)}"


def _ch_claim(data: bytes, **over) -> SpaceMediaClaim:
    return _claim(data, space=CHANNEL, scope=SCOPE_CHANNEL, **over)


def _channel_header(claim: SpaceMediaClaim, seed: bytes = CHANNEL_SEED) -> dict:
    sig = sign_ed25519(seed, claim.channel_signing_bytes())
    auth = SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_CHANNEL, sig=b64url_encode(sig), sig_suite="ed25519"
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


async def test_an_identified_writer_cert_upload_is_refused(gfs):
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data),
        data=data,
        headers=_identified_header(_claim(data), "writer_cert"),
    )
    assert r.status == 400
    assert (await gfs.get(_get_url(data))).status == 404


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
    "case", ["wrong_key", "unknown_channel", "stale_ts", "future_ts", "path"]
)
async def test_every_auth_failure_is_the_same_403(gfs, case):
    data = os.urandom(SMALL)
    if case == "wrong_key":
        headers = _authority_header(_claim(data), seed=os.urandom(32))
    elif case == "unknown_channel":
        headers = _authority_header(_claim(data, space="1f" * 16))
    elif case == "stale_ts":
        headers = _authority_header(_claim(data, ts=int(time.time()) - 301))
    elif case == "future_ts":
        # Well past the window: ``now + 301`` became in-window as soon as
        # one second passed before the GFS checked it (a loaded test run).
        headers = _authority_header(_claim(data, ts=int(time.time()) + 360))
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
    real = GfsMediaView.read_body

    async def spy(self, size):
        reads["n"] += 1
        if reads["stall"]:
            await asyncio.sleep(reads["stall"])
        return await real(self, size)

    monkeypatch.setattr(GfsMediaView, "read_body", spy)
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


async def _stalled_upload(gfs, data: bytes, *, xff: str):
    """An upload whose headers arrive and whose body never does."""
    headers = _authority_header(_claim(data))
    rd, wr = await asyncio.open_connection(gfs.server.host, gfs.server.port)
    wr.write(
        (
            f"PUT {_put_url(data)} HTTP/1.1\r\nHost: gfs.test\r\n"
            f"Content-Length: {len(data)}\r\nX-Forwarded-For: {xff}\r\n"
            f"{SPACE_MEDIA_AUTH_HEADER}: {headers[SPACE_MEDIA_AUTH_HEADER]}\r\n\r\n"
        ).encode()
    )
    await wr.drain()
    return rd, wr


async def _status(rd) -> int:
    line = await asyncio.wait_for(rd.readline(), 5)
    return int(line.split()[1])


async def test_a_body_that_makes_no_progress_is_cut_and_refunds_its_slot(
    gfs, monkeypatch
):
    """Third review A5: progress, not one long deadline — a body that sends
    nothing for a progress window is 408 and its slot given back."""
    monkeypatch.setattr(space_media_routes, "PROGRESS_WINDOW_S", 0.2)
    data = os.urandom(SMALL)
    rd, wr = await _stalled_upload(gfs, data, xff="203.0.113.5")
    try:
        assert await _status(rd) == 408
    finally:
        wr.close()
    store = gfs.app_[gfs_space_media_store_key]
    bucket = int(time.time()) // 3600
    assert await store._repo.upload_counts("space", SPACE, bucket=bucket) == (0, 0)
    assert (await gfs.get(_get_url(data))).status == 404


# ── Delete ───────────────────────────────────────────────────────────────


def _channel_delete_body(
    data: bytes, *, seed: bytes = CHANNEL_SEED, ts: int | None = None
) -> dict:
    ts = int(time.time()) if ts is None else ts
    body = media_delete_payload(SCOPE_CHANNEL, CHANNEL, _bid(data), ts)
    sig = sign_ed25519(seed, signing_bytes(CHANNEL_MEDIA_DELETE_PREFIX, body))
    return {
        "scope": SCOPE_CHANNEL,
        "scope_id": CHANNEL,
        "ts": ts,
        "sig": b64url_encode(sig),
        "sig_suite": "ed25519",
    }


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
        "scopes": ["channel"],
        # v_83 — a reader may ask for a blob it missed, waiting this long.
        "on_demand": {"wait_s": 25},
        # v_97 — it keeps the 8 MiB pieces of a larger file (sm1p).
        "pieces": True,
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
    held = await gfs.app_[gfs_space_media_store_key]._repo.get_blob(_bid(data))
    assert (held.scope_kind, held.scope_id) == ("channel", CHANNEL)


async def test_an_identified_channel_cert_upload_is_refused(gfs):
    data = os.urandom(SMALL)
    r = await gfs.put(
        _ch_url(data),
        data=data,
        headers=_identified_header(_ch_claim(data), "channel_cert"),
    )
    assert r.status == 400


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
    bid = _bid(data)
    for url, headers in (
        (f"/gfs/spaces/{SPACE}/media/{bid}", _authority_header(_claim(data))),
        (f"/gfs/channels/{CHANNEL}/media/{bid}", _channel_header(_ch_claim(data))),
    ):
        r = await gfs.put(url, data=data, headers=headers)
        assert r.status in (404, 405), url
        r = await gfs.post(url + "/delete", json={})
        assert r.status in (404, 405), url
    assert (await gfs.get(_get_url(data))).status == 404


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
    first, second = (os.urandom(SMALL) for _ in range(2))
    await gfs.put(_ch_url(first), data=first, headers=_channel_header(_ch_claim(first)))
    r = await gfs.put(
        _ch_url(second), data=second, headers=_channel_header(_ch_claim(second))
    )
    assert r.status == 429


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


# ── Review fixes: disabled cache ─────────────────────────────────────────


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
    assert (await gfs.get(f"/media/spaces/{_bid(data)}")).status == 404
    assert not (tmp_dir / "media" / "spaces" / _bid(data)).exists()
    assert (tmp_dir / "space_media" / _bid(data)).exists()


# ── Security review F5: a captured delete can't be replayed ──────────────


async def test_a_replayed_delete_is_the_uniform_403(gfs):
    data = os.urandom(SMALL)
    await gfs.put(_put_url(data), data=data, headers=_authority_header(_claim(data)))
    body = _channel_delete_body(data)
    assert (await gfs.post(_put_url(data) + "/delete", json=body)).status == 200
    r = await gfs.post(_put_url(data) + "/delete", json=body)
    assert r.status == 403
    assert await r.json() == _REFUSED


async def test_the_space_scope_is_refused(gfs):
    """v_96 (P7, owner decision Q5) — the retired ``space`` scope takes no
    upload (the codec refuses it: 400) and no delete (403)."""
    data = os.urandom(SMALL)
    body = {
        "scope": "space",
        "scope_id": "sp-1",
        "blob_id": "ab" * 32,
        "addr_suite": "sm1",
        "size": len(data),
        "expires_at": int(time.time()) + 3600,
        "ts": int(time.time()),
        "auth": "authority",
        "sig": "x" * 86,
        "sig_suite": "ed25519",
    }
    raw = base64.urlsafe_b64encode(orjson.dumps(body)).rstrip(b"=").decode()
    r = await gfs.put(
        "/gfs/media/" + "ab" * 32, data=data, headers={SPACE_MEDIA_AUTH_HEADER: raw}
    )
    assert r.status == 400
    r = await gfs.post(
        "/gfs/media/" + "ab" * 32 + "/delete",
        json={
            "scope": "space",
            "scope_id": "sp-1",
            "ts": int(time.time()),
            "sig": "x" * 86,
            "sig_suite": "ed25519",
        },
    )
    assert r.status == 403


# ── Security review F1: an anonymous channel can't flush the cache ───────


async def _anon_channel(gfs) -> tuple[str, bytes]:
    """Register a fresh channel over HTTP, as anyone can — no seat."""
    seed = os.urandom(32)
    cid = new_channel_id()
    req = sign_register(
        channel_seed=seed,
        channel_id=cid,
        gfs_instance_id="gfs-test",
        ts=datetime.now(UTC).isoformat(),
    )
    r = await gfs.post("/gfs/channels/register", json=req.to_wire())
    assert r.status in (200, 201)
    return cid, seed


@pytest.mark.parametrize("gfs", [{"space_media_max_bytes": 3 * SMALL}], indirect=True)
async def test_an_anonymous_unused_channel_cannot_upload_or_flush(gfs):
    """Probe P1: a channel nobody holds a seat in (registered anonymously,
    10 / min / address) is refused media — uniformly — so junk channels
    can't evict a real space's blobs."""
    victim = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(victim), data=victim, headers=_authority_header(_claim(victim))
    )
    assert r.status == 201
    cid, seed = await _anon_channel(gfs)
    for _ in range(3):
        junk = os.urandom(SMALL)
        claim = _claim(junk, space=cid, scope=SCOPE_CHANNEL)
        r = await gfs.put(
            _ch_url(junk, cid), data=junk, headers=_channel_header(claim, seed=seed)
        )
        assert r.status == 403
        assert await r.json() == _REFUSED
    assert (await gfs.get(_get_url(victim))).status == 200


@pytest.mark.parametrize(
    "gfs", [{"space_media_ip_bytes_per_hour": SMALL}], indirect=True
)
async def test_an_address_over_its_byte_budget_is_429(gfs):
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    r = await gfs.put(
        _put_url(first), data=first, headers=_authority_header(_claim(first))
    )
    assert r.status == 201
    r = await gfs.put(
        _put_url(second), data=second, headers=_authority_header(_claim(second))
    )
    assert r.status == 429
    assert (await r.json())["error"] == "ip_media_quota"
    assert "Retry-After" in r.headers
    # The scope's own slot was given back with it.
    store = gfs.app_[gfs_space_media_store_key]
    bucket = int(time.time()) // 3600
    assert await store._repo.upload_counts("channel", SPACE, bucket=bucket) == (1, 0)


# ── Security review v2, N1: the address is bound to the scope ────────────


# ── Security re-attack N2: cheap channels can't game fair eviction ───────


async def _seated_channel(
    gfs, households: list[str], *, joined: int
) -> tuple[str, bytes]:
    """A channel with an epoch and seats for *households* (joined at
    *joined*) — what one household can set up for itself via the landing
    page, an anonymous epoch notice and passes under its own channel key."""
    cid, seed = await _anon_channel(gfs)
    channels = gfs.app_[gfs_channel_repo_key]
    await channels.set_epoch(cid, 3, expected=None, now=joined)
    for h in households:
        await channels.add_subscriber(cid, h, pass_epoch=3, now=joined)
    return cid, seed


async def _active(gfs, iid: str) -> None:
    await gfs.app_[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=iid,
            display_name=iid,
            public_key=os.urandom(32).hex(),
            status="active",
        )
    )


async def test_a_private_space_with_one_seated_member_household_gets_media(gfs):
    """Third review A4: the typical invite-link private space — the owner
    household never subscribes to its own channel, one link-joined member
    household holds the only seat. Its media must work (the owner rule:
    every member equal however reached)."""
    await _active(gfs, "member")
    cid, seed = await _seated_channel(gfs, ["member"], joined=int(time.time()))
    data = os.urandom(SMALL)
    claim = _claim(data, space=cid, scope=SCOPE_CHANNEL)
    r = await gfs.put(
        _ch_url(data, cid), data=data, headers=_channel_header(claim, seed=seed)
    )
    assert r.status == 201
    assert (await gfs.get(_get_url(data))).status == 200


async def test_a_member_in_many_private_spaces_gets_media_in_each(gfs):
    """No per-household channel count refuses a legit member's 6th+
    private space (fairness is per household instead, A3)."""
    await _active(gfs, "member")
    now = int(time.time())
    for i in range(7):
        cid, seed = await _seated_channel(gfs, ["member"], joined=now + i)
        data = os.urandom(SMALL)
        claim = _claim(data, space=cid, scope=SCOPE_CHANNEL)
        r = await gfs.put(
            _ch_url(data, cid), data=data, headers=_channel_header(claim, seed=seed)
        )
        assert r.status == 201, i


# ── Security re-attack N5: slow bodies can't pile up ─────────────────────


async def test_stalled_uploads_of_one_client_do_not_lock_out_another(gfs):
    """Third review A5 probe: 16 header-only uploads from ONE client bucket
    hold at most its per-client share of body reads (the rest are 503 busy
    at once) — another client's upload still goes through."""
    stalled = [
        await _stalled_upload(gfs, os.urandom(SMALL), xff="203.0.113.5")
        for _ in range(16)
    ]
    try:
        await asyncio.sleep(0.3)
        data = os.urandom(SMALL)
        r = await gfs.put(
            _put_url(data),
            data=data,
            headers={
                **_authority_header(_claim(data)),
                "X-Forwarded-For": "198.51.100.9",
            },
        )
        assert r.status == 201
        # Past its share, the stalling client's uploads were answered at once.
        busy = 0
        for rd, _wr in stalled:
            try:
                busy += await asyncio.wait_for(_status(rd), 0.5) == 503
            except TimeoutError:
                pass
        assert busy == 16 - space_media_mod.PER_CLIENT_BODY_READS
    finally:
        for _rd, wr in stalled:
            wr.close()


def test_the_body_read_gate_bounds_bodies_and_committed_bytes():
    """Bodies in flight are bounded per client (/64), per prefix (/48 or
    /24), in COUNT per node, and by the signed bytes of every admitted body
    (what it will buffer — a stalled body holds its slot whatever it has
    sent so far; final check N2)."""
    gate = space_media_mod.BodyReadGate(
        per_client=2, per_prefix=3, max_bodies=4, max_bytes=100
    )
    assert gate.enter("a", "p", 10) and gate.enter("a", "p", 10)
    assert not gate.enter("a", "p", 10)  # a's share
    assert gate.enter("b", "p", 10)
    assert not gate.enter("c", "p", 10)  # the prefix's share
    assert gate.enter("d", "q", 60)
    assert not gate.enter("e", "r", 1)  # 4 bodies: the node's count
    gate.leave("a", "p", 10)
    assert not gate.enter("e", "r", 50)  # 30 + 60 + 50 > 100 committed
    assert gate.enter("e", "r", 10)
    for client, prefix, size in (
        ("a", "p", 10),
        ("b", "p", 10),
        ("d", "q", 60),
        ("e", "r", 10),
    ):
        gate.leave(client, prefix, size)
    assert gate.in_flight == 0
    assert gate.committed == 0


async def test_stalled_bodies_from_many_48s_stay_bounded_per_node(gfs):
    """N2 probe: 16 /48s × 6 stalled header-only uploads (96 bodies) — the
    node admits at most MAX_BODIES_IN_FLIGHT of them, the rest are 503 at
    once."""
    stalled = []
    for net in range(16):
        for sub64 in range(2):
            for _ in range(3):
                stalled.append(
                    await _stalled_upload(
                        gfs, os.urandom(SMALL), xff=f"2001:db8:{net + 1:x}:{sub64:x}::1"
                    )
                )
    try:
        await asyncio.sleep(0.5)
        busy = 0
        for rd, _wr in stalled:
            try:
                busy += await asyncio.wait_for(_status(rd), 0.3) == 503
            except TimeoutError:
                pass
        assert len(stalled) - busy <= space_media_mod.MAX_BODIES_IN_FLIGHT
        store = gfs.app_[gfs_space_media_store_key]
        assert store.body_gate.in_flight <= space_media_mod.MAX_BODIES_IN_FLIGHT
    finally:
        for _rd, wr in stalled:
            wr.close()


async def test_a_body_stays_counted_until_it_is_stored(gfs, monkeypatch):
    """N2: a finished body still waits for the node's lock and the hash —
    it holds its gate slot (and bytes) until ``store.put`` returns."""
    store = gfs.app_[gfs_space_media_store_key]
    seen = {}
    real_put = type(store).put

    async def watching_put(self, **kw):
        seen["in_flight"] = self.body_gate.in_flight
        seen["committed"] = self.body_gate.committed
        return await real_put(self, **kw)

    monkeypatch.setattr(type(store), "put", watching_put)
    data = os.urandom(SMALL)
    r = await gfs.put(
        _put_url(data), data=data, headers=_authority_header(_claim(data))
    )
    assert r.status == 201
    assert seen == {"in_flight": 1, "committed": SMALL}
    assert store.body_gate.in_flight == 0


async def test_one_ipv6_48_cannot_close_the_node(gfs):
    """A5-residual probe: 12 /64s of ONE /48, 3 stalled uploads each, hold at
    most the /48's share of body reads — another network's upload still goes
    through."""
    stalled = []
    for n in range(12):
        for _ in range(3):
            stalled.append(
                await _stalled_upload(
                    gfs, os.urandom(SMALL), xff=f"2001:db8:aa:{n:x}::1"
                )
            )
    try:
        await asyncio.sleep(0.3)
        data = os.urandom(SMALL)
        r = await gfs.put(
            _put_url(data),
            data=data,
            headers={
                **_authority_header(_claim(data)),
                "X-Forwarded-For": "2001:db8:bb::9",
            },
        )
        assert r.status == 201
        busy = 0
        for rd, _wr in stalled:
            try:
                busy += await asyncio.wait_for(_status(rd), 0.5) == 503
            except TimeoutError:
                pass
        assert busy == len(stalled) - space_media_mod.PER_PREFIX_BODY_READS
    finally:
        for _rd, wr in stalled:
            wr.close()


# ── Third review A3: fairness and caps are per HOUSEHOLD ─────────────────


async def _own_space(gfs, sid: str, *, household: str, status="active") -> bytes:
    """A space *household* publishes for itself (status as given)."""
    seed = os.urandom(32)
    await gfs.app_[gfs_fed_repo_key].upsert_space(
        GlobalSpace(
            space_id=sid,
            owning_instance=household,
            status=status,
            identity_public_key=ed25519_public_key(seed).hex(),
        )
    )
    return seed


async def _space_put(gfs, data: bytes, sid: str, seed: bytes) -> int:
    claim = _claim(data, space=sid)
    r = await gfs.put(
        _put_url(data, sid), data=data, headers=_authority_header(claim, seed)
    )
    return r.status


# ── Fourth review G1: channel blobs count against the uploader ───────────


# ── Final check N4: one network prefix is one owner and one budget ───────


@pytest.mark.parametrize(
    "gfs",
    [{"space_media_max_bytes": 6 * SMALL, "space_media_space_max_bytes": 64 * SMALL}],
    indirect=True,
)
async def test_many_64s_of_one_48_are_one_channel_owner(gfs):
    """N4 probe: channel uploads from 6 different /64s of ONE IPv6 /48 are
    one fairness owner (the /48), not 6 — the real space keeps its share."""
    victim = [os.urandom(SMALL) for _ in range(4)]
    for v in victim:
        r = await gfs.put(
            _put_url(v),
            data=v,
            headers={**_authority_header(_claim(v)), "X-Forwarded-For": "192.0.2.50"},
        )
        assert r.status == 201
    victim_urls = [f"/gfs/media/{_bid(v)}" for v in victim]
    await _active(gfs, "member")
    cid, seed = await _seated_channel(gfs, ["member"], joined=int(time.time()))
    for n in range(6):
        junk = os.urandom(SMALL)
        claim = _claim(junk, space=cid, scope=SCOPE_CHANNEL)
        r = await gfs.put(
            _ch_url(junk, cid),
            data=junk,
            headers={
                **_channel_header(claim, seed=seed),
                "X-Forwarded-For": f"2001:db8:cc:{n:x}::1",
            },
        )
        assert r.status == 201
    alive = [(await gfs.get(u)).status for u in victim_urls]
    assert alive.count(200) >= 3, alive


@pytest.mark.parametrize(
    "gfs", [{"space_media_ip_bytes_per_hour": SMALL}], indirect=True
)
async def test_the_byte_budget_is_per_network_prefix(gfs):
    """N4: the per-address byte budget keys on the /48 (IPv4 /24), so
    hopping /64s of one /48 doesn't reset it."""
    first, second = os.urandom(SMALL), os.urandom(SMALL)
    r = await gfs.put(
        _put_url(first),
        data=first,
        headers={
            **_authority_header(_claim(first)),
            "X-Forwarded-For": "2001:db8:dd:1::1",
        },
    )
    assert r.status == 201
    r = await gfs.put(
        _put_url(second),
        data=second,
        headers={
            **_authority_header(_claim(second)),
            "X-Forwarded-For": "2001:db8:dd:2::1",
        },
    )
    assert r.status == 429
    assert (await r.json())["error"] == "ip_media_quota"


# ── v_97 pieces (sm1p): a derived address, proved by the manifest ─────────


def _pieces(n: int = 3) -> list[bytes]:
    return [os.urandom(SMALL) for _ in range(n)]


def _proof(pieces: list[bytes], index: int) -> PieceProof:
    return PieceProof(
        manifest=tuple(hashlib.sha256(p).hexdigest() for p in pieces), index=index
    )


def _piece_addr(proof: PieceProof, channel: str = CHANNEL) -> str:
    return piece_address(
        pieced_file_address(SCOPE_CHANNEL, channel, proof.manifest), proof.index
    )


def _piece_headers(blob_id: str, size: int, proof: PieceProof | None) -> dict:
    now = int(time.time())
    claim = SpaceMediaClaim(
        scope=SCOPE_CHANNEL,
        scope_id=CHANNEL,
        blob_id=blob_id,
        size=size,
        expires_at=now + 86_400,
        ts=now,
        addr_suite=ADDR_SUITE_SM1P,
    )
    headers = _channel_header(claim)
    if proof is not None:
        headers[PIECE_PROOF_HEADER] = proof.to_header()
    return headers


async def test_a_piece_with_its_proof_is_stored_at_its_derived_address(gfs):
    pieces = _pieces()
    proof = _proof(pieces, 1)
    addr = _piece_addr(proof)
    r = await gfs.put(
        f"/gfs/media/{addr}",
        data=pieces[1],
        headers=_piece_headers(addr, len(pieces[1]), proof),
    )
    assert r.status == 201
    g = await gfs.get(f"/gfs/media/{addr}")
    assert g.status == 200
    assert await g.read() == pieces[1]


async def test_a_wrong_piece_at_a_real_piece_address_is_refused(gfs):
    """A member holding the key seals other bytes and tries to put them
    where the author's piece belongs: the manifest names the real piece."""
    pieces = _pieces()
    proof = _proof(pieces, 1)
    addr = _piece_addr(proof)
    evil = os.urandom(SMALL)
    r = await gfs.put(
        f"/gfs/media/{addr}", data=evil, headers=_piece_headers(addr, SMALL, proof)
    )
    assert r.status == 422
    # A manifest forged to name the evil bytes derives another address.
    forged = _proof([pieces[0], evil, pieces[2]], 1)
    r = await gfs.put(
        f"/gfs/media/{addr}", data=evil, headers=_piece_headers(addr, SMALL, forged)
    )
    assert r.status == 422
    assert (await gfs.get(f"/gfs/media/{addr}")).status == 404
    # The real piece still lands afterwards: nobody squatted the address.
    r = await gfs.put(
        f"/gfs/media/{addr}",
        data=pieces[1],
        headers=_piece_headers(addr, SMALL, proof),
    )
    assert r.status == 201


async def test_a_piece_under_another_index_is_refused(gfs):
    pieces = _pieces()
    proof = _proof(pieces, 1)
    addr = _piece_addr(proof)
    r = await gfs.put(
        f"/gfs/media/{addr}",
        data=pieces[0],
        headers=_piece_headers(addr, SMALL, _proof(pieces, 0)),
    )
    assert r.status == 422


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "garbage",
        # index out of range
        base64.urlsafe_b64encode(
            orjson.dumps({"manifest": ["a" * 64, "b" * 64], "index": 5})
        )
        .decode()
        .rstrip("="),
        # past the 200 MiB / 8 MiB bound
        base64.urlsafe_b64encode(
            orjson.dumps({"manifest": ["a" * 64] * 26, "index": 0})
        )
        .decode()
        .rstrip("="),
    ],
)
async def test_an_sm1p_upload_needs_a_well_formed_proof(gfs, body_reads, raw):
    pieces = _pieces(2)
    proof = _proof(pieces, 0)
    addr = _piece_addr(proof)
    headers = _piece_headers(addr, SMALL, None)
    if raw is not None:
        headers[PIECE_PROOF_HEADER] = raw
    r = await gfs.put(f"/gfs/media/{addr}", data=pieces[0], headers=headers)
    assert r.status == 400
    assert body_reads["n"] == 0


async def test_a_content_addressed_upload_never_takes_a_piece_proof_path(gfs):
    """An sm1 claim is checked as a content address even when a proof rides
    along: a proof can't turn a piece address into an sm1 upload."""
    pieces = _pieces(2)
    proof = _proof(pieces, 0)
    addr = _piece_addr(proof)
    now = int(time.time())
    claim = SpaceMediaClaim(
        scope=SCOPE_CHANNEL,
        scope_id=CHANNEL,
        blob_id=addr,
        size=SMALL,
        expires_at=now + 86_400,
        ts=now,
    )
    headers = {**_channel_header(claim), PIECE_PROOF_HEADER: proof.to_header()}
    r = await gfs.put(f"/gfs/media/{addr}", data=pieces[0], headers=headers)
    assert r.status == 422


@pytest.mark.parametrize(
    "gfs", [{"space_media_max_bytes": 4 * 1024 * 1024}], indirect=True
)
async def test_a_cache_too_small_for_a_piece_does_not_advertise_pieces(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    assert "pieces" not in body["capabilities"]["space_media"]


@pytest.mark.parametrize(
    "gfs", [{"space_media_space_max_bytes": 3 * SMALL}], indirect=True
)
async def test_one_file_holds_at_most_half_the_scopes_share(gfs):
    """v_97 — one big file can't evict most of a space's pictures: its
    pieces (known from the proof's manifest) take at most half the scope's
    share; a file that can't fit is refused at its FIRST piece (never half
    kept), ``413 file_over_share``."""
    pieces = _pieces(3)
    for index, want in ((0, 413),):
        proof = _proof(pieces, index)
        addr = _piece_addr(proof)
        r = await gfs.put(
            f"/gfs/media/{addr}",
            data=pieces[index],
            headers=_piece_headers(addr, SMALL, proof),
        )
        assert r.status == want, await r.text()
    assert (await r.json())["error"] == "file_over_share"
