"""Space media on demand over HTTP (v_83) — real GFS app, real SQLite, real
client; the holder household is a fake socket that answers the way a
household does (a signed ``PUT /gfs/media/serve/{ticket}`` or a decline)."""

from __future__ import annotations

import asyncio
import os
import time

import aiohttp
import orjson
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.canonical_json import canonical_bytes
from socialhome.crypto import b64url_encode, ed25519_public_key, sign_ed25519
from socialhome.domain.gfs_space_media import (
    AUTH_AUTHORITY,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SPACE_MEDIA_AUTH_HEADER,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    space_media_address,
)
from socialhome.domain.gfs_space_media_on_demand import (
    MemberPullRequest,
    member_serve_signing_bytes,
    serve_signing_body,
)
from socialhome.gfs_channel import channel_pk_of
from socialhome.global_server import space_media_on_demand as od_mod
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_db_key,
    gfs_fed_repo_key,
    gfs_space_epoch_repo_key,
    gfs_space_media_on_demand_key,
    gfs_space_media_store_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.blob_store import BLOB_OVERHEAD
from socialhome.global_server.config import GfsConfig, SpaceMediaOnDemandConfig
from socialhome.global_server.domain import ClientInstance, GfsSpaceMedia, GlobalSpace
from socialhome.global_server.server import create_gfs_app
from socialhome.moment_public_media import PAD_BUCKETS
from socialhome.space_media_hint import SpaceMediaLocator, seal_space_media_hint
from socialhome.writer_cert import sign_writer_cert
from socialhome.writer_key import derive_writer_seed, sign_with_writer_key

SPACE = "sp-od"
SEED = os.urandom(32)
PK = ed25519_public_key(SEED)
HOST_SEED = os.urandom(32)
SEAT_SEED = os.urandom(32)
HOST = "host"
SEAT = "seat"
OTHER = "other"
CHANNEL = "1e" * 16
CHANNEL_SEED = os.urandom(32)
SMALL = PAD_BUCKETS[1] + BLOB_OVERHEAD


class _HolderWS:
    """A household's socket as the registry sees it: frames go to a queue."""

    def __init__(self) -> None:
        self.closed = False
        self.frames: asyncio.Queue[dict] = asyncio.Queue()
        self.count = 0

    async def send_str(self, data: str) -> None:
        self.count += 1
        self.frames.put_nowait(orjson.loads(data))

    async def send_bytes(self, data: bytes) -> None:  # pragma: no cover
        raise AssertionError("a media request is never a binary frame")

    async def close(self, **_kw) -> None:
        self.closed = True


def _cfg(**over) -> SpaceMediaOnDemandConfig:
    base = {
        "stream_wait_seconds": 5,
        "coalesce_seconds": 30,
        # Below the config bound on purpose: an idle pull ends fast in tests.
        "member_pull_hold_seconds": 1,
    }
    base.update(over)
    return SpaceMediaOnDemandConfig(**base)


@pytest.fixture
async def gfs(tmp_dir, request):
    params = getattr(request, "param", None) or {}
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-test",
        space_media_on_demand=_cfg(**params.get("od", {})),
        **params.get("cfg", {}),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        fed = app[gfs_fed_repo_key]
        for iid, seed in (
            (HOST, HOST_SEED),
            (SEAT, SEAT_SEED),
            (OTHER, os.urandom(32)),
        ):
            await fed.upsert_instance(
                ClientInstance(
                    instance_id=iid,
                    display_name=iid,
                    public_key=ed25519_public_key(seed).hex(),
                    status="active",
                )
            )
        await fed.upsert_space(
            GlobalSpace(
                space_id=SPACE,
                owning_instance=HOST,
                status="active",
                identity_public_key=PK.hex(),
            )
        )
        channels = app[gfs_channel_repo_key]
        now = int(time.time())
        await channels.register(
            CHANNEL,
            channel_suite="ed25519",
            channel_pk=channel_pk_of(CHANNEL_SEED),
            now=now,
        )
        await channels.set_epoch(CHANNEL, 3, expected=None, now=now)
        await channels.add_subscriber(CHANNEL, SEAT, pass_epoch=3, now=now)
        tc.app_ = app
        yield tc


async def _connect(tc, iid: str, *, serves: bool = True) -> _HolderWS:
    ws = _HolderWS()
    await tc.app_[gfs_ws_registry_key].register(iid, ws, media_serve=serves)
    return ws


def _hint(blob_id: str, scope=SCOPE_SPACE, scope_id=SPACE) -> dict:
    return seal_space_media_hint(
        media_key=os.urandom(32),
        scope=scope,
        scope_id=scope_id,
        blob_id=blob_id,
        epoch=1,
        locator=SpaceMediaLocator(item_id="post", origin="author", filename="p.webp"),
    )


async def _want(tc, blob_id: str, scope=SCOPE_SPACE, scope_id=SPACE, *, reader=0):
    """One reader's request; *reader* picks its own network (10.<n>.0.1)
    — readers are separate households, each under its own budgets."""
    return await tc.post(
        f"/gfs/media/{blob_id}/wanted",
        json={
            "scope": scope,
            "scope_id": scope_id,
            "hint": _hint(blob_id, scope, scope_id),
        },
        headers={"X-Forwarded-For": f"10.{reader}.0.1"},
    )


def _serve_headers(iid: str, seed: bytes, ticket: str, op: str = "serve") -> dict:
    ts = int(time.time())
    sig = sign_ed25519(seed, canonical_bytes(serve_signing_body(iid, ticket, ts, op)))
    return {
        "X-SH-Instance": iid,
        "X-SH-Timestamp": str(ts),
        "X-SH-Signature": b64url_encode(sig),
        "Content-Type": "application/octet-stream",
    }


def _auth(blob: bytes, blob_id: str) -> str:
    now = int(time.time())
    claim = SpaceMediaClaim(
        scope=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=blob_id,
        size=len(blob),
        expires_at=now + 86_400,
        ts=now,
    )
    sig = sign_ed25519(SEED, claim.authority_signing_bytes())
    return SpaceMediaUploadAuth(
        claim=claim, kind=AUTH_AUTHORITY, sig=b64url_encode(sig), sig_suite="ed25519"
    ).to_header()


async def _answer(tc, ws: _HolderWS, iid: str, seed: bytes, blob: bytes, *, auth=True):
    """Wait for one request and serve *blob* (what a household does)."""
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    headers = _serve_headers(iid, seed, frame["ticket"])
    if auth and frame["mode"] == "cache":
        headers[SPACE_MEDIA_AUTH_HEADER] = _auth(blob, frame["blob_id"])
    resp = await tc.put(
        f"/gfs/media/serve/{frame['ticket']}", data=blob, headers=headers
    )
    return frame, resp.status, await resp.json()


def _blob(scope=SCOPE_SPACE, scope_id=SPACE) -> tuple[bytes, str]:
    data = os.urandom(SMALL)
    return data, space_media_address(scope, scope_id, data)


async def _mark_stored(tc, blob_id: str, *, ago: int, scope=SCOPE_SPACE, sid=SPACE):
    """The cache stored *blob_id* *ago* seconds ago and dropped it since."""
    now = int(time.time())
    await tc.app_[gfs_space_media_on_demand_key]._demand.note_dropped(
        [
            GfsSpaceMedia(
                blob_id=blob_id,
                size=SMALL,
                created_at=now - ago,
                expires_at=now - ago + 10,
                last_fetched_at=now - ago,
                scope_kind=scope,
                scope_id=sid,
            )
        ],
        now=now,
    )


# ── Cache or stream ──────────────────────────────────────────────────────


async def test_a_cached_blob_is_answered_from_the_cache(gfs):
    data, bid = _blob()
    store = gfs.app_[gfs_space_media_store_key]
    await store.put(
        scope_kind=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=bid,
        data=data,
        expires_at=int(time.time()) + 3600,
    )
    ws = await _connect(gfs, HOST)
    resp = await _want(gfs, bid)
    assert resp.status == 200
    assert await resp.read() == data
    assert resp.headers["Cache-Control"] == "no-store"
    assert ws.count == 0


async def test_old_content_streams_and_nothing_is_stored(gfs):
    """Never stored here (or long ago): the holder streams it, the reader
    gets the exact bytes, the cache keeps nothing, nor may a CDN."""
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame, status, body = await _answer(gfs, ws, HOST, HOST_SEED, data)
    resp = await reader
    assert frame["mode"] == "stream"
    assert (status, body) == (200, {"stored": False})
    assert resp.status == 200 and await resp.read() == data
    assert resp.headers["Cache-Control"] == "no-store"
    assert await gfs.app_[gfs_space_media_store_key].read(bid) is None


async def test_the_holder_learns_no_reader(gfs):
    """The request names the blob, its scope, the sealed hint and a ticket
    — no reader address, bucket or identity."""
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame, _, _ = await _answer(gfs, ws, HOST, HOST_SEED, data)
    await reader
    assert set(frame) == {
        "type",
        "ticket",
        "scope",
        "scope_id",
        "blob_id",
        "mode",
        "hint",
    }
    assert set(frame["hint"]) == {"suite", "epoch", "ct"}
    assert "127.0.0.1" not in orjson.dumps(frame).decode()


async def test_fresh_content_is_re_uploaded_into_the_cache(gfs):
    data, bid = _blob()
    await _mark_stored(gfs, bid, ago=3600)
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame, status, body = await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert frame["mode"] == "cache"
    assert (status, body) == (200, {"stored": True})
    assert await (await reader).read() == data
    # Every later reader is served from the cache — the plain GET too.
    resp = await gfs.get(f"/gfs/media/{bid}")
    assert resp.status == 200 and await resp.read() == data
    stats = await gfs.app_[gfs_space_media_on_demand_key].stats()
    assert stats["reuploads"] == 1 and stats["streams_served"] == 1


@pytest.mark.parametrize(
    "gfs", [{"cfg": {"space_media_max_bytes": SMALL}}], indirect=True
)
async def test_a_full_cache_streams_fresh_content_and_stores_nothing(gfs):
    store = gfs.app_[gfs_space_media_store_key]
    filler, fid = _blob()
    await store.put(
        scope_kind=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=fid,
        data=filler,
        expires_at=int(time.time()) + 3600,
    )
    data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame, _status, body = await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert frame["mode"] == "stream"
    assert body == {"stored": False}
    assert await (await reader).read() == data
    assert await store.read(bid) is None
    assert await store.read(fid) == filler


@pytest.mark.parametrize(
    "gfs", [{"od": {"cache_after_requests": 3, "coalesce_seconds": 1}}], indirect=True
)
async def test_content_requested_n_times_gets_cached(gfs, monkeypatch):
    """Old content streams — until it is asked for the Nth time within the
    window: then it is re-uploaded into the cache."""
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    svc = gfs.app_[gfs_space_media_on_demand_key]
    modes = []
    for _ in range(3):
        reader = asyncio.create_task(_want(gfs, bid))
        frame, _, _ = await _answer(gfs, ws, HOST, HOST_SEED, data)
        modes.append(frame["mode"])
        assert await (await reader).read() == data
        # The session lingers coalesce_seconds; let it go.
        for s in list(svc._sessions.values()):
            svc._close(s)
    assert modes == ["stream", "stream", "cache"]
    assert await gfs.app_[gfs_space_media_store_key].read(bid) == data


# ── Coalescing ───────────────────────────────────────────────────────────


async def test_fifty_readers_share_one_stream(gfs):
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    readers = [asyncio.create_task(_want(gfs, bid, reader=n)) for n in range(50)]
    await asyncio.sleep(0.2)
    _, status, _ = await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert status == 200
    bodies = [await (await r).read() for r in readers]
    assert all(b == data for b in bodies)
    assert ws.count == 1


async def test_fifty_readers_share_one_re_upload(gfs):
    data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)
    ws = await _connect(gfs, HOST)
    readers = [asyncio.create_task(_want(gfs, bid, reader=n)) for n in range(50)]
    await asyncio.sleep(0.2)
    frame, _, body = await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert (frame["mode"], body) == ("cache", {"stored": True})
    assert all([await (await r).read() == data for r in readers])
    assert ws.count == 1


async def test_a_reader_right_after_a_stream_joins_it_without_a_new_request(gfs):
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    first = asyncio.create_task(_want(gfs, bid))
    await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert await (await first).read() == data
    again = await _want(gfs, bid)
    assert await again.read() == data
    assert ws.count == 1


# ── Clean misses, bounded ────────────────────────────────────────────────


async def test_an_offline_holder_is_a_clean_miss_and_a_repeat_asks_nobody(gfs):
    _, bid = _blob()
    started = time.monotonic()
    resp = await _want(gfs, bid)
    assert resp.status == 404
    assert resp.headers["Cache-Control"] == "no-store"
    assert time.monotonic() - started < 5
    # The holder comes online, but a repeat inside the window asks nobody.
    ws = await _connect(gfs, HOST)
    assert (await _want(gfs, bid)).status == 404
    assert ws.count == 0


@pytest.mark.parametrize("gfs", [{"od": {"stream_wait_seconds": 2}}], indirect=True)
async def test_a_holder_that_never_answers_is_a_bounded_miss(gfs, monkeypatch):
    monkeypatch.setattr(od_mod, "HOLDER_START_TIMEOUT_S", 1.0)
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    started = time.monotonic()
    resp = await _want(gfs, bid)
    assert resp.status == 404
    assert time.monotonic() - started < 4
    assert ws.count == 1


async def test_a_decline_is_a_miss_at_once(gfs):
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    started = time.monotonic()
    resp = await gfs.post(
        f"/gfs/media/serve/{frame['ticket']}/decline",
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"], "decline"),
    )
    assert resp.status == 200
    assert (await reader).status == 404
    assert time.monotonic() - started < 2


async def test_a_holder_without_the_label_is_never_asked(gfs):
    _, bid = _blob()
    ws = await _connect(gfs, HOST, serves=False)
    assert (await _want(gfs, bid)).status == 404
    assert ws.count == 0


async def test_other_bytes_never_reach_a_reader_whole_and_are_not_stored(gfs):
    _data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    _, status, _ = await _answer(gfs, ws, HOST, HOST_SEED, os.urandom(SMALL))
    assert status == 422
    resp = await reader
    # Cut short of Content-Length: the client never sees a whole body.
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()
    assert await gfs.app_[gfs_space_media_store_key].read(bid) is None


async def test_only_the_household_asked_may_serve(gfs):
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    resp = await gfs.put(
        f"/gfs/media/serve/{frame['ticket']}",
        data=data,
        headers=_serve_headers(SEAT, SEAT_SEED, frame["ticket"]),
    )
    assert resp.status == 403
    bad = _serve_headers(HOST, HOST_SEED, frame["ticket"])
    bad["X-SH-Signature"] = b64url_encode(b"x" * 64)
    assert (
        await gfs.put(f"/gfs/media/serve/{frame['ticket']}", data=data, headers=bad)
    ).status == 401
    ok = await gfs.put(
        f"/gfs/media/serve/{frame['ticket']}",
        data=data,
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"]),
    )
    assert ok.status == 200
    assert await (await reader).read() == data
    assert (
        await gfs.put(
            "/gfs/media/serve/" + "0" * 32,
            data=data,
            headers=_serve_headers(HOST, HOST_SEED, "0" * 32),
        )
    ).status == 404


# ── Scopes ───────────────────────────────────────────────────────────────


async def test_a_private_channel_is_served_by_its_seated_key_holders_only(gfs):
    data, bid = _blob(SCOPE_CHANNEL, CHANNEL)
    host_ws = await _connect(gfs, HOST)
    other_ws = await _connect(gfs, OTHER)
    seat_ws = await _connect(gfs, SEAT)
    reader = asyncio.create_task(_want(gfs, bid, SCOPE_CHANNEL, CHANNEL))
    frame, status, _ = await _answer(gfs, seat_ws, SEAT, SEAT_SEED, data)
    assert (frame["scope"], frame["scope_id"], status) == (SCOPE_CHANNEL, CHANNEL, 200)
    assert await (await reader).read() == data
    assert host_ws.count == 0 and other_ws.count == 0


async def test_an_unlisted_scope_or_bad_body_asks_nobody(gfs):
    ws = await _connect(gfs, HOST)
    _, bid = _blob(scope_id="sp-unknown")
    assert (await _want(gfs, bid, SCOPE_SPACE, "sp-unknown")).status == 404
    resp = await gfs.post(f"/gfs/media/{bid}/wanted", json={"scope": "space"})
    assert resp.status == 422
    assert ws.count == 0


@pytest.mark.parametrize("gfs", [{"od": {"prefix_wanted_per_hour": 2}}], indirect=True)
async def test_one_reader_prefix_is_bounded(gfs):
    for _ in range(2):
        _, bid = _blob()
        assert (await _want(gfs, bid)).status == 404
    _, bid = _blob()
    resp = await _want(gfs, bid)
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "600"


@pytest.mark.parametrize("gfs", [{"od": {"enabled": False}}], indirect=True)
async def test_off_is_a_miss_and_not_advertised(gfs):
    _, bid = _blob()
    assert (await _want(gfs, bid)).status == 404
    info = await (await gfs.get("/gfs/info")).json()
    assert "on_demand" not in info["capabilities"]["space_media"]


# ── Review regressions (adversarial round 1) ─────────────────────────────


async def test_concurrent_readers_never_open_a_second_session(gfs):
    """Review H1: readers arriving while the first one is being decided
    share its session — one holder request, everybody served (repeated:
    the race was intermittent)."""
    ws = await _connect(gfs, HOST)
    for round_ in range(5):
        data, bid = _blob()
        readers = [
            asyncio.create_task(_want(gfs, bid, reader=n + 10 * round_))
            for n in range(10)
        ]
        _, status, _ = await _answer(gfs, ws, HOST, HOST_SEED, data)
        assert status == 200
        assert all([await (await r).read() == data for r in readers])
        assert ws.frames.empty()
    assert ws.count == 5


async def test_a_junk_hint_never_makes_a_blob_dark(gfs):
    """Review M1: a reader without the key sends a junk hint first; the
    holder declines it as ``bad_hint`` and the GFS asks again with the next
    reader's hint — the real reader is served, nothing is marked missing."""
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    junk = asyncio.create_task(_want(gfs, bid, reader=1))
    first = await asyncio.wait_for(ws.frames.get(), 5)
    real = asyncio.create_task(_want(gfs, bid, reader=2))
    await asyncio.sleep(0.2)
    resp = await gfs.post(
        f"/gfs/media/serve/{first['ticket']}/decline",
        json={"reason": "bad_hint"},
        headers=_serve_headers(HOST, HOST_SEED, first["ticket"], "decline"),
    )
    assert resp.status == 200
    second = await asyncio.wait_for(ws.frames.get(), 5)
    assert second["ticket"] == first["ticket"]
    assert second["hint"] != first["hint"]
    ok = await gfs.put(
        f"/gfs/media/serve/{second['ticket']}",
        data=data,
        headers=_serve_headers(HOST, HOST_SEED, second["ticket"]),
    )
    assert ok.status == 200
    assert await (await real).read() == data
    assert await (await junk).read() == data


async def test_only_junk_hints_leave_the_blob_askable(gfs):
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    await gfs.post(
        f"/gfs/media/serve/{frame['ticket']}/decline",
        json={"reason": "bad_hint"},
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"], "decline"),
    )
    assert (await reader).status == 404
    # Not marked missing: the next reader asks the holder again.
    again = asyncio.create_task(_want(gfs, bid))
    await asyncio.wait_for(ws.frames.get(), 5)
    again.cancel()


async def test_a_serve_signature_is_not_a_decline(gfs):
    """Review L4: the signed body names the operation."""
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    resp = await gfs.post(
        f"/gfs/media/serve/{frame['ticket']}/decline",
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"], "serve"),
    )
    assert resp.status == 401
    reader.cancel()


async def test_a_second_serve_of_one_ticket_is_refused(gfs):
    """Review L1: the first PUT claims the ticket before any await."""
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    url = f"/gfs/media/serve/{frame['ticket']}"
    puts = await asyncio.gather(
        gfs.put(
            url, data=data, headers=_serve_headers(HOST, HOST_SEED, frame["ticket"])
        ),
        gfs.put(
            url, data=data, headers=_serve_headers(HOST, HOST_SEED, frame["ticket"])
        ),
    )
    assert sorted(p.status for p in puts) == [200, 409]
    assert await (await reader).read() == data


# ── Review regressions (adversarial round 2) ─────────────────────────────


@pytest.mark.parametrize(
    "gfs", [{"od": {"scope_stream_bytes_per_hour": SMALL * 8}}], indirect=True
)
async def test_a_reader_past_its_stream_share_never_makes_a_holder_send(gfs):
    """Review N2: the opener's prefix share (1/8 of the scope's budget) is
    checked BEFORE any holder is asked — no holder seals and sends bytes
    that would be refused, and the blob is not marked missing."""
    ws = await _connect(gfs, HOST)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid, reader=7))
    await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert await (await reader).read() == data
    _, other = _blob()
    resp = await _want(gfs, other, reader=7)
    assert resp.status == 429
    assert ws.count == 1
    # Another reader is not limited by that one's share, and the blob was
    # not marked missing.
    other_reader = asyncio.create_task(_want(gfs, other, reader=8))
    await asyncio.wait_for(ws.frames.get(), 5)
    other_reader.cancel()


async def test_a_quota_refusal_ends_the_session_at_once(gfs, monkeypatch):
    """Review N1: a holder's bytes refused by a budget race end the session
    (never a session hanging forever, its slot leaked)."""
    svc = gfs.app_[gfs_space_media_on_demand_key]

    async def refuse(_self, _session, _size):
        return False

    monkeypatch.setattr(od_mod.GfsSpaceMediaOnDemand, "charge_stream", refuse)
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    resp = await gfs.put(
        f"/gfs/media/serve/{frame['ticket']}",
        data=data,
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"]),
    )
    assert resp.status == 429
    assert (await reader).status == 404
    for _ in range(50):
        if not svc._sessions:
            break
        await asyncio.sleep(0.05)
    assert svc._sessions == {}
    assert svc._recent == {}


async def test_one_reader_prefix_has_one_hint_in_a_session(gfs):
    """Review N3: joiners from one prefix can't fill the hint slots."""
    ws = await _connect(gfs, HOST)
    _, bid = _blob()
    readers = [asyncio.create_task(_want(gfs, bid, reader=3)) for _ in range(4)]
    await asyncio.wait_for(ws.frames.get(), 5)
    svc = gfs.app_[gfs_space_media_on_demand_key]
    (session,) = svc._sessions.values()
    assert len(session.hints) == 1
    for r in readers:
        r.cancel()


# ── #1317 independent review ──────────────────────────────────────────────


async def test_a_cached_answer_is_bounded_by_a_reader_deadline(gfs, monkeypatch):
    """#1317 review 7: a cached blob is written under a deadline too — a
    reader that stops reading can't hold it."""
    data, bid = _blob()
    await gfs.app_[gfs_space_media_store_key].put(
        scope_kind=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=bid,
        data=data,
        expires_at=int(time.time()) + 3600,
    )
    monkeypatch.setattr(
        od_mod.GfsSpaceMediaOnDemand, "cached_deadline", lambda _self, _size: 0.0
    )
    resp = await _want(gfs, bid)
    with pytest.raises(aiohttp.ClientPayloadError):
        await resp.read()


@pytest.mark.parametrize("gfs", [{"od": {"stream_wait_seconds": 8}}], indirect=True)
async def test_readers_on_a_node_without_the_claim_poll_once_and_stop_with_it(
    gfs, monkeypatch
):
    """#1317 review 8: readers waiting for another node's re-upload share
    ONE poll of the shared cache, which stops as soon as that claim ends
    (the other node streamed, or nobody answered) — not 0.5 s per reader
    for the whole wait."""
    _data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)
    svc = gfs.app_[gfs_space_media_on_demand_key]
    now = int(time.time())
    await svc._demand.note_request(bid, SCOPE_SPACE, SPACE, now=now, window_s=3600)
    # Another node holds the one re-upload.
    assert await svc._demand.claim(bid, SCOPE_SPACE, SPACE, now=now, hold_s=60)
    reads = {"n": 0}
    store_cls = type(gfs.app_[gfs_space_media_store_key])
    real_read = store_cls.read

    async def counting_read(self, blob_id, **kw):
        if blob_id == bid:
            reads["n"] += 1
        return await real_read(self, blob_id, **kw)

    monkeypatch.setattr(store_cls, "read", counting_read)
    started = time.monotonic()
    readers = [asyncio.create_task(_want(gfs, bid, reader=n)) for n in range(3)]
    await asyncio.sleep(2.0)
    await svc._demand.release_claim(bid, SCOPE_SPACE, SPACE, until=now + 60)
    statuses = [(await r).status for r in readers]
    assert statuses == [404, 404, 404]
    assert time.monotonic() - started < 4.5
    # One poller (≈ every 0.5 s) plus each reader's first look.
    assert reads["n"] <= 10


# ── Member fallback (listed spaces, writer-key holders) ──────────────────

EPOCH = 3
WRITER_SEED = derive_writer_seed(SEED, SPACE, EPOCH)
#: A member household of the space. It never registers here and never
#: names itself: the GFS must not learn (or keep) this id or key.
MEMBER = "member-household"
MEMBER_SEED = os.urandom(32)
#: The network the member pulls from (never stored either).
MEMBER_NET = 77


async def _pin_writer(tc, epoch: int = EPOCH) -> None:
    repo = tc.app_[gfs_space_epoch_repo_key]
    await repo.confirm(SPACE, epoch, now=int(time.time()))
    await repo.pin_writer_key(
        SPACE,
        epoch,
        b64url_encode(ed25519_public_key(derive_writer_seed(SEED, SPACE, epoch))),
        replace=True,
    )


def _pull_body(
    *,
    seed: bytes = WRITER_SEED,
    epoch: int = EPOCH,
    gfs_id: str = "gfs-test",
    ts: int | None = None,
    scope_id: str = SPACE,
) -> dict:
    unsigned = MemberPullRequest(
        scope="space",
        scope_id=scope_id,
        epoch=epoch,
        gfs_instance_id=gfs_id,
        ts=int(time.time()) if ts is None else ts,
        nonce=os.urandom(16).hex(),
        sig="-",
        sig_suite="ed25519",
    )
    sig = sign_with_writer_key(seed, unsigned.signing_bytes())
    return {**unsigned.to_wire(), "sig": sig}


async def _pull(tc, body: dict | None = None, *, net: int = MEMBER_NET):
    resp = await tc.post(
        "/gfs/media/member-pull",
        json=body if body is not None else _pull_body(),
        headers={"X-Forwarded-For": f"10.{net}.0.1"},
    )
    data = await resp.json() if resp.status in (200, 403, 422, 429, 503) else None
    return resp, data


def _member_headers(
    ticket: str, op: str = "serve", *, seed: bytes = WRITER_SEED, epoch: int = EPOCH
) -> dict:
    ts = int(time.time())
    sig = sign_with_writer_key(
        seed, member_serve_signing_bytes(ticket, op, "space", SPACE, epoch, ts)
    )
    return {
        "X-SH-Writer-Epoch": str(epoch),
        "X-SH-Timestamp": str(ts),
        "X-SH-Writer-Sig": sig,
        "X-SH-Writer-Sig-Suite": "ed25519",
        "Content-Type": "application/octet-stream",
        "X-Forwarded-For": f"10.{MEMBER_NET}.0.1",
    }


async def _wants(tc) -> list:
    return await tc.app_[gfs_db_key].fetchall(
        "SELECT * FROM gfs_space_media_member_wants"
    )


async def _until_wanted(tc, n: int = 1) -> None:
    for _ in range(100):
        if len(await _wants(tc)) >= n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("no member want was offered")


def _writer_key_auth(blob: bytes, blob_id: str) -> str:
    now = int(time.time())
    claim = SpaceMediaClaim(
        scope=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=blob_id,
        size=len(blob),
        expires_at=now + 86_400,
        ts=now,
    )
    sig = sign_with_writer_key(
        WRITER_SEED, claim.writer_signing_bytes(kind="writer_key", epoch=EPOCH)
    )
    return SpaceMediaUploadAuth(
        claim=claim, kind="writer_key", sig=sig, sig_suite="ed25519", epoch=EPOCH
    ).to_header()


def _writer_cert_auth(blob: bytes, blob_id: str) -> str:
    """The IDENTIFIED upload authorization (it names the member's household
    key) — never taken on a member serve."""
    now = int(time.time())
    claim = SpaceMediaClaim(
        scope=SCOPE_SPACE,
        scope_id=SPACE,
        blob_id=blob_id,
        size=len(blob),
        expires_at=now + 86_400,
        ts=now,
    )
    cert = sign_writer_cert(
        space_seed=SEED,
        space_id=SPACE,
        epoch=EPOCH,
        instance_pk=ed25519_public_key(MEMBER_SEED),
        scope="comment",
    )
    sig = sign_ed25519(
        MEMBER_SEED, claim.writer_signing_bytes(kind="writer_cert", epoch=EPOCH)
    )
    return SpaceMediaUploadAuth(
        claim=claim,
        kind="writer_cert",
        sig=b64url_encode(sig),
        sig_suite="ed25519",
        epoch=EPOCH,
        writer_cert=cert,
    ).to_header()


async def test_owner_offline_a_member_pulls_serves_and_the_reader_gets_it(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    resp, body = await _pull(gfs)
    assert resp.status == 200
    (want,) = body["wants"]
    assert want["blob_id"] == bid and want["mode"] == "stream"
    assert len(want["hints"]) == 1
    # Nothing in it names a reader, the owner or a household.
    assert set(want) == {"ticket", "blob_id", "mode", "hints"}
    served = await gfs.put(
        f"/gfs/media/serve/{want['ticket']}",
        data=data,
        headers=_member_headers(want["ticket"]),
    )
    assert served.status == 200 and await served.json() == {"stored": False}
    got = await reader
    assert got.status == 200 and await got.read() == data
    stats = await gfs.app_[gfs_space_media_on_demand_key].stats()
    assert stats["member_wants"] == 1 and stats["member_serves"] == 1
    # The session ended: its want is gone.
    for _ in range(50):
        if not await _wants(gfs):
            break
        await asyncio.sleep(0.05)
    assert await _wants(gfs) == []


async def test_nothing_about_the_member_is_stored(gfs):
    """After a pull and a member serve no table holds the member household's
    id, its key, or the address it came from."""
    await _pin_writer(gfs)
    data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)  # a re-upload: the cache keeps it
    reader = asyncio.create_task(_want(gfs, bid))
    _, body = await _pull(gfs)
    ticket = body["wants"][0]["ticket"]
    headers = _member_headers(ticket)
    headers[SPACE_MEDIA_AUTH_HEADER] = _writer_key_auth(data, bid)
    served = await gfs.put(f"/gfs/media/serve/{ticket}", data=data, headers=headers)
    assert await served.json() == {"stored": True}
    assert await (await reader).read() == data
    db = gfs.app_[gfs_db_key]
    pk = ed25519_public_key(MEMBER_SEED)
    needles = {
        MEMBER,
        pk.hex(),
        b64url_encode(pk),
        f"10.{MEMBER_NET}.0.1",
        f"10.{MEMBER_NET}.0.",
    }
    tables = [
        r[0]
        for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    assert "gfs_space_media_member_wants" in tables
    for table in tables:
        for row in await db.fetchall(f"SELECT * FROM {table}"):
            for cell in tuple(row):
                text = (
                    cell.decode(errors="ignore")
                    if isinstance(cell, bytes)
                    else str(cell)
                )
                assert not any(n in text for n in needles), (table, text)


async def test_owner_online_serves_and_no_member_want_exists(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    _, status, _ = await _answer(gfs, ws, HOST, HOST_SEED, data)
    assert status == 200
    assert await (await reader).read() == data
    assert await _wants(gfs) == []
    resp, _ = await _pull(gfs)
    assert resp.status == 204


async def test_owner_not_held_is_authoritative_members_not_asked(gfs):
    await _pin_writer(gfs)
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    await gfs.post(
        f"/gfs/media/serve/{frame['ticket']}/decline",
        json={"reason": "not_held"},
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"], "decline"),
    )
    assert (await reader).status == 404
    assert await _wants(gfs) == []


async def test_owner_busy_hands_over_to_the_members(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    frame = await asyncio.wait_for(ws.frames.get(), 5)
    resp = await gfs.post(
        f"/gfs/media/serve/{frame['ticket']}/decline",
        json={"reason": "busy"},
        headers=_serve_headers(HOST, HOST_SEED, frame["ticket"], "decline"),
    )
    assert resp.status == 200
    _, body = await _pull(gfs)
    ticket = body["wants"][0]["ticket"]
    assert ticket == frame["ticket"]
    ok = await gfs.put(
        f"/gfs/media/serve/{ticket}", data=data, headers=_member_headers(ticket)
    )
    assert ok.status == 200
    assert await (await reader).read() == data


async def test_a_silent_owner_gets_a_shorter_window_then_members(gfs, monkeypatch):
    monkeypatch.setattr(od_mod, "MEMBER_FALLBACK_OWNER_WINDOW_S", 0.3)
    await _pin_writer(gfs)
    _, bid = _blob()
    ws = await _connect(gfs, HOST)
    reader = asyncio.create_task(_want(gfs, bid))
    await asyncio.wait_for(ws.frames.get(), 5)
    started = time.monotonic()
    await _until_wanted(gfs)
    assert time.monotonic() - started < 2
    # The owner is out of the session now: its late serve is refused.
    (row,) = await _wants(gfs)
    late = await gfs.put(
        f"/gfs/media/serve/{row['ticket']}",
        data=b"x" * SMALL,
        headers=_serve_headers(HOST, HOST_SEED, row["ticket"]),
    )
    assert late.status == 403
    reader.cancel()


async def test_no_pinned_writer_key_means_no_member_fallback(gfs):
    """A listed space nobody holds a writer key for (pinned here): owner
    only, exactly as before."""
    _, bid = _blob()
    assert (await _want(gfs, bid)).status == 404
    assert await _wants(gfs) == []


@pytest.mark.parametrize(
    "case", ["bad_sig", "no_pinned_key", "wrong_gfs", "stale_ts", "unlisted"]
)
async def test_a_non_member_is_refused_uniformly_and_sees_no_want(gfs, case):
    await _pin_writer(gfs)
    _, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    await _until_wanted(gfs)
    body = {
        "bad_sig": lambda: _pull_body(seed=os.urandom(32)),
        "no_pinned_key": lambda: _pull_body(epoch=EPOCH + 1),
        "wrong_gfs": lambda: _pull_body(gfs_id="gfs-other"),
        "stale_ts": lambda: _pull_body(ts=int(time.time()) - 301),
        "unlisted": lambda: _pull_body(scope_id="sp-unknown"),
    }[case]()
    resp, data = await _pull(gfs, body)
    assert (resp.status, data) == (403, {"error": "forbidden"})
    # The want is still there, unclaimed.
    (row,) = await _wants(gfs)
    assert row["claimed_until"] == 0
    reader.cancel()


async def test_a_replayed_pull_is_refused(gfs):
    await _pin_writer(gfs)
    body = _pull_body()
    first, _ = await _pull(gfs, body)
    assert first.status == 204
    again, data = await _pull(gfs, body)
    assert (again.status, data) == (403, {"error": "forbidden"})


async def test_two_pullers_one_want_only_one_claims_it(gfs):
    await _pin_writer(gfs)
    _, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    await _until_wanted(gfs)
    (a, a_body), (b, b_body) = await asyncio.gather(
        _pull(gfs, net=MEMBER_NET), _pull(gfs, net=MEMBER_NET + 1)
    )
    statuses = sorted([a.status, b.status])
    assert statuses == [200, 204]
    winner = a_body if a.status == 200 else b_body
    assert len(winner["wants"]) == 1
    reader.cancel()


async def test_an_expired_claim_is_offered_again(gfs, monkeypatch):
    monkeypatch.setattr(od_mod, "MEMBER_START_TIMEOUT_S", 1)
    await _pin_writer(gfs)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    _, first = await _pull(gfs)
    ticket = first["wants"][0]["ticket"]
    # The first member never serves; its claim runs out and another pull
    # gets the same want.
    await asyncio.sleep(1.1)
    _, second = await _pull(gfs, net=MEMBER_NET + 1)
    assert second["wants"][0]["ticket"] == ticket
    ok = await gfs.put(
        f"/gfs/media/serve/{ticket}", data=data, headers=_member_headers(ticket)
    )
    assert ok.status == 200
    assert await (await reader).read() == data


async def test_at_most_three_members_are_asked(gfs):
    await _pin_writer(gfs)
    _, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    for n in range(od_mod.MAX_MEMBER_ATTEMPTS):
        resp, body = await _pull(gfs, net=MEMBER_NET + n)
        assert resp.status == 200, n
        ticket = body["wants"][0]["ticket"]
        declined = await gfs.post(
            f"/gfs/media/serve/{ticket}/decline",
            json={"reason": "not_held"},
            headers=_member_headers(ticket, "decline"),
        )
        assert declined.status == 200
    assert (await reader).status == 404
    resp, _ = await _pull(gfs, net=MEMBER_NET + 9)
    assert resp.status == 204


@pytest.mark.parametrize(
    "gfs", [{"od": {"member_pullers_per_scope": 1}}], indirect=True
)
async def test_pullers_per_scope_per_node_are_capped(gfs):
    await _pin_writer(gfs)
    held = asyncio.create_task(_pull(gfs, net=MEMBER_NET))
    await asyncio.sleep(0.2)
    resp, _ = await _pull(gfs, net=MEMBER_NET + 1)
    assert resp.status == 503 and resp.headers["Retry-After"]
    assert (await held)[0].status == 204


@pytest.mark.parametrize(
    "gfs",
    [{"od": {"member_pullers_per_scope": 4, "member_pullers_max": 1}}],
    indirect=True,
)
async def test_pullers_per_node_are_capped(gfs):
    await _pin_writer(gfs)
    held = asyncio.create_task(_pull(gfs, net=MEMBER_NET))
    await asyncio.sleep(0.2)
    resp, _ = await _pull(gfs, net=MEMBER_NET + 1)
    assert resp.status == 503
    await held


@pytest.mark.parametrize(
    "gfs", [{"od": {"member_pullers_per_scope": 4}}], indirect=True
)
async def test_pullers_per_prefix_are_capped(gfs, monkeypatch):
    monkeypatch.setattr(od_mod, "MAX_PULLS_PER_PREFIX", 1)
    await _pin_writer(gfs)
    held = asyncio.create_task(_pull(gfs, net=MEMBER_NET))
    await asyncio.sleep(0.2)
    same, _ = await _pull(gfs, net=MEMBER_NET)
    other, _ = await _pull(gfs, net=MEMBER_NET + 1)
    assert same.status == 503
    assert other.status == 204
    await held


async def test_pulls_are_rate_limited_per_address(gfs):
    codes = []
    for _ in range(od_mod.MEMBER_PULL_MAX_PER_MINUTE_PER_IP + 1):
        resp, _ = await _pull(gfs, {"scope": "space"})
        codes.append(resp.status)
    assert codes[:-1] == [422] * od_mod.MEMBER_PULL_MAX_PER_MINUTE_PER_IP
    assert codes[-1] == 429


async def test_a_member_serve_without_a_claim_is_refused(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    await _until_wanted(gfs)
    (row,) = await _wants(gfs)
    ticket = row["ticket"]
    resp = await gfs.put(
        f"/gfs/media/serve/{ticket}", data=data, headers=_member_headers(ticket)
    )
    assert (resp.status, await resp.json()) == (403, {"error": "forbidden"})
    # A decline without a claim, or a bad writer signature, alike.
    resp = await gfs.post(
        f"/gfs/media/serve/{ticket}/decline",
        headers=_member_headers(ticket, "decline"),
    )
    assert resp.status == 403
    reader.cancel()


async def test_a_member_serve_with_a_bad_signature_is_refused(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    _, body = await _pull(gfs)
    ticket = body["wants"][0]["ticket"]
    for headers in (
        _member_headers(ticket, seed=os.urandom(32)),
        _member_headers(ticket, "decline"),  # a decline's signature
        _member_headers(ticket, epoch=EPOCH + 1),
    ):
        resp = await gfs.put(f"/gfs/media/serve/{ticket}", data=data, headers=headers)
        assert resp.status == 403
    ok = await gfs.put(
        f"/gfs/media/serve/{ticket}", data=data, headers=_member_headers(ticket)
    )
    assert ok.status == 200
    assert await (await reader).read() == data


async def test_an_owner_style_serve_on_a_member_phase_session_is_refused(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    reader = asyncio.create_task(_want(gfs, bid))
    _, body = await _pull(gfs)
    ticket = body["wants"][0]["ticket"]
    for iid, seed in ((HOST, HOST_SEED), (SEAT, SEAT_SEED)):
        resp = await gfs.put(
            f"/gfs/media/serve/{ticket}",
            data=data,
            headers=_serve_headers(iid, seed, ticket),
        )
        assert resp.status == 403
    reader.cancel()


async def test_a_member_serve_with_an_identified_auth_streams_but_is_not_stored(gfs):
    await _pin_writer(gfs)
    data, bid = _blob()
    await _mark_stored(gfs, bid, ago=60)
    reader = asyncio.create_task(_want(gfs, bid))
    _, body = await _pull(gfs)
    want = body["wants"][0]
    assert want["mode"] == "cache"
    headers = _member_headers(want["ticket"])
    headers[SPACE_MEDIA_AUTH_HEADER] = _writer_cert_auth(data, bid)
    resp = await gfs.put(
        f"/gfs/media/serve/{want['ticket']}", data=data, headers=headers
    )
    assert (resp.status, await resp.json()) == (200, {"stored": False})
    assert await (await reader).read() == data
    assert await gfs.app_[gfs_space_media_store_key].read(bid) is None


async def test_the_capability_carries_member_pull(gfs):
    info = await (await gfs.get("/gfs/info")).json()
    on_demand = info["capabilities"]["space_media"]["on_demand"]
    assert on_demand["member_pull"] == {"hold_s": 1}


@pytest.mark.parametrize("gfs", [{"od": {"member_fallback": False}}], indirect=True)
async def test_member_fallback_off_is_not_advertised_and_pulls_404(gfs):
    await _pin_writer(gfs)
    info = await (await gfs.get("/gfs/info")).json()
    assert "member_pull" not in info["capabilities"]["space_media"]["on_demand"]
    resp, _ = await _pull(gfs)
    assert resp.status == 404
    _, bid = _blob()
    assert (await _want(gfs, bid)).status == 404
    assert await _wants(gfs) == []
