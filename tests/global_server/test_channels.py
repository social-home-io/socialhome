"""Integration tests for opaque private-space channels (v_51).

Real GFS app, real SQLite, real aiohttp client + WebSocket. A channel is a
random id + a channel key the server pins on first registration; nothing the
server stores or relays names a space, a space key or an owner household.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_channel import (
    CHANNEL_FRAME_KEYS,
    CHANNEL_ROUTES,
    ChannelPublishAnonRequest,
    ChannelPublishRequest,
    ChannelSubscribeRequest,
    ChannelUnsubscribeRequest,
)
from socialhome.global_server.app_keys import (
    gfs_channel_repo_key,
    gfs_channel_service_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_member_publish_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.channels import (
    CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP,
    _is_channel_path,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.server import create_gfs_app
from socialhome.gfs_channel import (
    channel_pk_of,
    derive_channel_seed,
    issue_channel_cert,
    issue_channel_pass,
    issue_channel_writer_key,
    new_channel_id,
    sign_notice,
    sign_publish_anon,
    sign_register,
    sign_unregister,
)

GFS_ID = "gfs-node-a"
SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)
SPACE_ID = "private-space-0f3c"
CT = "bm9uY2U:Y2hhbm5lbC1pdGVtLWNpcGhlcnRleHQ"


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)

    def instance(self) -> ClientInstance:
        return ClientInstance(
            instance_id=self.instance_id,
            display_name="H",
            public_key=self.pk.hex(),
            status="active",
        )

    def hello(self) -> dict:
        ts = int(datetime.now(timezone.utc).timestamp())
        sig = sign_ed25519(self.seed, f"{self.instance_id}|{ts}".encode())
        return {
            "type": "hello",
            "instance_id": self.instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }


def _now(delta_s: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


class _Channel:
    def __init__(self) -> None:
        self.id = new_channel_id()
        self.seed = derive_channel_seed(SPACE_SEED, SPACE_ID, self.id)
        self.pk = channel_pk_of(self.seed)

    def register(self, **kw) -> dict:
        return sign_register(
            channel_seed=self.seed,
            channel_id=self.id,
            gfs_instance_id=kw.pop("gfs_instance_id", GFS_ID),
            ts=kw.pop("ts", _now()),
            **kw,
        ).to_wire()

    def notice(self, epoch: int, mode: str = "trusted", wkc=None, **kw) -> dict:
        return sign_notice(
            channel_seed=kw.pop("seed", self.seed),
            channel_id=self.id,
            gfs_instance_id=GFS_ID,
            ts=kw.pop("ts", _now()),
            epoch=epoch,
            publish_mode=mode,
            writer_key_cert=wkc,
        ).to_wire()

    def passport(self, h: _Household, epoch: int):
        return issue_channel_pass(
            channel_seed=self.seed, channel_id=self.id, epoch=epoch, instance_pk=h.pk
        )

    def cert(self, h: _Household, epoch: int, *, seed=None, channel_id=None):
        return issue_channel_cert(
            channel_seed=seed or self.seed,
            channel_id=channel_id or self.id,
            epoch=epoch,
            instance_pk=h.pk,
            scope="write",
        )

    def writer(self, epoch: int):
        return issue_channel_writer_key(
            space_seed=SPACE_SEED, space_id=SPACE_ID, channel_id=self.id, epoch=epoch
        )


def _subscribe_body(ch: _Channel, h: _Household, epoch: int, *, passport=None) -> dict:
    req = ChannelSubscribeRequest(
        instance_id=h.instance_id,
        gfs_instance_id=GFS_ID,
        channel_id=ch.id,
        ts=_now(),
        signature="",
        channel_pass=passport or ch.passport(h, epoch),
    )
    canonical = json.dumps(
        req.signing_payload(), separators=(",", ":"), sort_keys=True
    ).encode()
    return replace(
        req, signature=b64url_encode(sign_ed25519(h.seed, canonical))
    ).to_wire()


def _publish_body(ch: _Channel, h: _Household, epoch: int, *, cert=None) -> dict:
    req = ChannelPublishRequest(
        instance_id=h.instance_id,
        gfs_instance_id=GFS_ID,
        channel_id=ch.id,
        ts=_now(),
        signature="",
        epoch=epoch,
        channel_cert=cert or ch.cert(h, epoch),
        payload=CT,
    )
    sig = sign_ed25519(h.seed, req.signing_bytes())
    return replace(req, signature=b64url_encode(sig)).to_wire()


def _anon_body(ch: _Channel, epoch: int, *, writer_seed: bytes | None = None) -> dict:
    grant = ch.writer(epoch)
    seed = writer_seed or b64url_decode(grant.writer_seed)
    req = ChannelPublishAnonRequest(
        gfs_instance_id=GFS_ID,
        channel_id=ch.id,
        ts=_now(),
        nonce=b64url_encode(os.urandom(16)),
        epoch=epoch,
        payload=CT,
        writer_sig="",
        writer_sig_suite="ed25519",
    )
    return sign_publish_anon(req, writer_seed=seed).to_wire()


@pytest.fixture
async def gfs(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id=GFS_ID,
        cluster_enabled=False,
        cluster_node_id=GFS_ID,
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        fed = app[gfs_fed_repo_key]
        tc.app_ = app
        tc.member = _Household()
        tc.other = _Household()
        tc.outsider = _Household()
        for h in (tc.member, tc.other, tc.outsider):
            await fed.upsert_instance(h.instance())
            await fed.mark_relay_seen(h.instance_id, at=int(time.time()))
        tc.ch = _Channel()
        yield tc


async def _ready(gfs, epoch: int = 3, mode: str = "trusted", wkc=None) -> None:
    resp = await gfs.post("/gfs/channels/register", json=gfs.ch.register())
    assert resp.status == 201, await resp.text()
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(epoch, mode, wkc))
    assert resp.status == 200, await resp.text()


async def _seat(gfs, h: _Household, epoch: int = 3) -> None:
    resp = await gfs.post(
        "/gfs/channels/subscribe", json=_subscribe_body(gfs.ch, h, epoch)
    )
    assert resp.status == 200, await resp.text()


async def _wait_connected(app, instance_id: str) -> None:
    registry = app[gfs_ws_registry_key]
    for _ in range(200):
        if registry.is_connected(instance_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never connected")


async def _queued(gfs, h: _Household) -> list:
    await gfs.app_[gfs_member_publish_key].wait_idle()
    return await gfs.app_[gfs_envelope_queue_repo_key].list_for(h.instance_id, now=0)


def _advance(gfs, seconds: int) -> None:
    svc = gfs.app_[gfs_channel_service_key]
    base = time.time()
    svc._clock = lambda: base + seconds


async def _refused(resp) -> None:
    assert resp.status == 403
    assert await resp.json() == {"error": "not authorized for this channel"}


# ── Routes carry no identifier in the path ───────────────────────────────


def test_every_channel_route_keeps_ids_out_of_the_url() -> None:
    for route in CHANNEL_ROUTES:
        assert "{" not in route
        assert _is_channel_path(route)
    assert not _is_channel_path("/gfs/member-publish")


async def test_gfs_info_advertises_private_channels(gfs) -> None:
    resp = await gfs.get("/gfs/info")
    body = await resp.json()
    assert body["capabilities"]["private_channels"] is True


# ── Registration and pins ────────────────────────────────────────────────


async def test_register_pins_on_first_use_and_refreshes(gfs) -> None:
    resp = await gfs.post("/gfs/channels/register", json=gfs.ch.register())
    assert resp.status == 201
    assert await resp.json() == {"status": "registered"}
    resp = await gfs.post("/gfs/channels/register", json=gfs.ch.register())
    assert resp.status == 200
    assert await resp.json() == {"status": "refreshed"}
    row = await gfs.app_[gfs_channel_repo_key].get(gfs.ch.id)
    assert row.channel_pk == gfs.ch.pk


@pytest.mark.security
async def test_another_key_for_a_pinned_id_is_refused_there_is_no_repin(gfs) -> None:
    await _ready(gfs)
    squatter = os.urandom(32)
    body = sign_register(
        channel_seed=squatter, channel_id=gfs.ch.id, gfs_instance_id=GFS_ID, ts=_now()
    ).to_wire()
    resp = await gfs.post("/gfs/channels/register", json=body)
    assert resp.status == 409
    row = await gfs.app_[gfs_channel_repo_key].get(gfs.ch.id)
    assert row.channel_pk == gfs.ch.pk
    # A re-pin field is not a feature: the body is malformed.
    body = {**gfs.ch.register(), "repin_cert": {"anything": 1}}
    resp = await gfs.post("/gfs/channels/register", json=body)
    assert resp.status == 400


async def test_registration_stops_at_the_server_wide_cap(gfs) -> None:
    """C2: a per-address budget alone lets many addresses grow the table
    without bound — a server-wide cap answers 503."""
    svc = gfs.app_[gfs_channel_service_key]
    svc._max_channels = 3
    for _ in range(3):
        resp = await gfs.post("/gfs/channels/register", json=_Channel().register())
        assert resp.status == 201
    resp = await gfs.post("/gfs/channels/register", json=_Channel().register())
    assert resp.status == 503
    # Refreshing a channel that exists still works at the cap.
    first = _Channel()
    svc._max_channels = 4
    assert (
        await gfs.post("/gfs/channels/register", json=first.register())
    ).status == 201
    assert (
        await gfs.post("/gfs/channels/register", json=first.register())
    ).status == 200


@pytest.mark.security
async def test_register_needs_proof_of_possession(gfs) -> None:
    body = gfs.ch.register()
    body["channel_pk"] = channel_pk_of(os.urandom(32))
    await _refused(await gfs.post("/gfs/channels/register", json=body))
    assert await gfs.app_[gfs_channel_repo_key].get(gfs.ch.id) is None


@pytest.mark.security
async def test_register_is_bound_to_this_server_and_fresh(gfs) -> None:
    await _refused(
        await gfs.post(
            "/gfs/channels/register", json=gfs.ch.register(gfs_instance_id="other")
        )
    )
    await _refused(
        await gfs.post("/gfs/channels/register", json=gfs.ch.register(ts=_now(-900)))
    )


async def test_register_is_rate_limited_per_address(gfs) -> None:
    for _ in range(CHANNEL_REGISTER_MAX_PER_MINUTE_PER_IP):
        resp = await gfs.post("/gfs/channels/register", json=_Channel().register())
        assert resp.status == 201
    resp = await gfs.post("/gfs/channels/register", json=_Channel().register())
    assert resp.status == 429


async def test_a_body_naming_a_space_is_malformed(gfs) -> None:
    body = gfs.ch.register()
    body["space_id"] = SPACE_ID
    resp = await gfs.post("/gfs/channels/register", json=body)
    assert resp.status == 400


# ── Epoch notices ────────────────────────────────────────────────────────


async def test_epoch_rises_by_one_per_minute_without_any_owner(gfs) -> None:
    await _ready(gfs, epoch=10)
    repo = gfs.app_[gfs_channel_repo_key]
    # Too soon: 429 with when to retry.
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(11))
    assert resp.status == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert (await repo.get(gfs.ch.id)).epoch == 10
    _advance(gfs, 61)
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(11, ts=_now(61)))
    assert resp.status == 200
    row = await repo.get(gfs.ch.id)
    assert (row.epoch, row.epoch_prev) == (11, 10)
    # A jump of 5 needs 5 minutes; a replay of an older notice changes nothing.
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(16, ts=_now(61)))
    assert resp.status == 429
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(9, ts=_now(61)))
    assert resp.status == 200
    assert (await repo.get(gfs.ch.id)).epoch == 11


@pytest.mark.security
async def test_a_forged_or_misaddressed_notice_is_refused(gfs) -> None:
    await _ready(gfs)
    await _refused(
        await gfs.post(
            "/gfs/channels/epoch", json=gfs.ch.notice(4, seed=os.urandom(32))
        )
    )
    other = _Channel()
    await _refused(await gfs.post("/gfs/channels/epoch", json=other.notice(1)))


@pytest.mark.security
async def test_first_notice_is_bounded_by_the_ceiling(gfs) -> None:
    resp = await gfs.post("/gfs/channels/register", json=gfs.ch.register())
    assert resp.status == 201
    # Channel epochs carry a secret 40-bit offset, so a large first epoch is
    # normal — only one near the 64-bit ceiling is refused.
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(2**41))
    assert resp.status == 200
    other = _Channel()
    resp = await gfs.post("/gfs/channels/register", json=other.register())
    assert resp.status == 201
    await _refused(await gfs.post("/gfs/channels/epoch", json=other.notice(2**62 + 1)))


async def test_mode_moves_only_with_a_higher_epoch(gfs) -> None:
    """A mode switch rotates: only a notice that raises the epoch moves the
    mode — a replay or a lagging seed holder at the current epoch never
    flips it."""
    await _ready(gfs)
    repo = gfs.app_[gfs_channel_repo_key]
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(3, "strict"))
    assert resp.status == 200
    assert not (await repo.get(gfs.ch.id)).strict
    _advance(gfs, 61)
    resp = await gfs.post(
        "/gfs/channels/epoch", json=gfs.ch.notice(4, "strict", ts=_now(61))
    )
    assert resp.status == 200
    assert (await repo.get(gfs.ch.id)).strict
    resp = await gfs.post(
        "/gfs/channels/epoch", json=gfs.ch.notice(4, "trusted", ts=_now(61))
    )
    assert (await repo.get(gfs.ch.id)).strict


@pytest.mark.security
async def test_a_notice_reports_what_the_server_holds(gfs) -> None:
    """C1: another key holder stepping the epoch locks writers out; the
    answer to the owner's next notice shows it the server is past it."""
    await _ready(gfs, epoch=3)
    _advance(gfs, 61)
    assert (
        await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(4, ts=_now(61)))
    ).status == 200
    _advance(gfs, 122)
    assert (
        await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(5, ts=_now(122)))
    ).status == 200
    # The owner, still at 3, announces: told the server holds 5.
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(3, ts=_now(122)))
    assert resp.status == 200
    body = await resp.json()
    assert body["epoch"] == 5 and body["writer_pk"] is None
    # Members' certs at 3 are refused now — the lock-out the owner heals.
    await _refused(
        await gfs.post(
            "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 3)
        )
    )


async def test_writer_key_is_pinned_only_at_the_current_epoch(gfs) -> None:
    await _ready(gfs, epoch=3)
    repo = gfs.app_[gfs_channel_repo_key]
    ahead = gfs.ch.writer(5).writer_key_cert
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(5, "strict", ahead))
    assert resp.status == 429
    assert (await repo.get(gfs.ch.id)).writer_pk_for(5) is None
    now = gfs.ch.writer(3).writer_key_cert
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(3, "strict", now))
    assert resp.status == 200
    assert (await repo.get(gfs.ch.id)).writer_pk_for(3) == now.writer_pk


# ── Seats ────────────────────────────────────────────────────────────────


@pytest.mark.security
async def test_subscribe_needs_a_pass_for_this_household_and_an_open_epoch(gfs) -> None:
    await _ready(gfs, epoch=3)
    # Someone else's pass.
    body = _subscribe_body(
        gfs.ch, gfs.outsider, 3, passport=gfs.ch.passport(gfs.member, 3)
    )
    await _refused(await gfs.post("/gfs/channels/subscribe", json=body))
    # A pass from a key that is not the pinned one.
    forged = issue_channel_pass(
        channel_seed=os.urandom(32),
        channel_id=gfs.ch.id,
        epoch=3,
        instance_pk=gfs.outsider.pk,
    )
    body = _subscribe_body(gfs.ch, gfs.outsider, 3, passport=forged)
    await _refused(await gfs.post("/gfs/channels/subscribe", json=body))
    # A closed epoch.
    await _refused(
        await gfs.post(
            "/gfs/channels/subscribe", json=_subscribe_body(gfs.ch, gfs.member, 1)
        )
    )
    await _seat(gfs, gfs.member, 3)


async def test_unsubscribe_drops_the_seat(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    req = ChannelUnsubscribeRequest(
        instance_id=gfs.member.instance_id,
        gfs_instance_id=GFS_ID,
        channel_id=gfs.ch.id,
        ts=_now(),
        signature="",
    )
    canonical = json.dumps(
        req.signing_payload(), separators=(",", ":"), sort_keys=True
    ).encode()
    body = replace(
        req, signature=b64url_encode(sign_ed25519(gfs.member.seed, canonical))
    ).to_wire()
    resp = await gfs.post("/gfs/channels/unsubscribe", json=body)
    assert resp.status == 200
    repo = gfs.app_[gfs_channel_repo_key]
    assert not await repo.has_subscription(gfs.ch.id, gfs.member.instance_id)


# ── Trusted publish ──────────────────────────────────────────────────────


async def test_trusted_publish_fans_out_an_identity_free_frame(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    await _seat(gfs, gfs.other)
    async with (
        gfs.ws_connect("/gfs/ws") as other_ws,
        gfs.ws_connect("/gfs/ws") as pub_ws,
    ):
        await other_ws.send_json(gfs.other.hello())
        await pub_ws.send_json(gfs.member.hello())
        await _wait_connected(gfs.app_, gfs.other.instance_id)
        await _wait_connected(gfs.app_, gfs.member.instance_id)
        resp = await gfs.post(
            "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 3)
        )
        assert resp.status == 200
        frame = await asyncio.wait_for(other_ws.receive_json(), timeout=5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pub_ws.receive_json(), timeout=0.3)
    assert frame == {
        "type": "relay",
        "channel_id": gfs.ch.id,
        "event_type": "space_item",
        "epoch": 3,
        "payload": CT,
    }
    assert set(frame) - {"type"} == CHANNEL_FRAME_KEYS
    for leak in (gfs.member.instance_id, SPACE_ID, SPACE_PK.hex()):
        assert leak not in json.dumps(frame)


async def test_an_offline_seat_gets_the_item_queued(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.other)
    resp = await gfs.post(
        "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 3)
    )
    assert resp.status == 200
    queued = await _queued(gfs, gfs.other)
    assert [q.frame_type for q in queued] == ["relay"]
    assert queued[0].sealed["channel_id"] == gfs.ch.id
    # A non-member (no seat) gets nothing.
    assert await _queued(gfs, gfs.outsider) == []


@pytest.mark.security
@pytest.mark.parametrize(
    "case", ["forged", "other_channel", "stale_epoch", "other_household"]
)
async def test_trusted_publish_refusals(gfs, case: str) -> None:
    await _ready(gfs, epoch=3)
    await _seat(gfs, gfs.other)
    if case == "forged":
        body = _publish_body(
            gfs.ch, gfs.member, 3, cert=gfs.ch.cert(gfs.member, 3, seed=os.urandom(32))
        )
    elif case == "other_channel":
        body = _publish_body(
            gfs.ch,
            gfs.member,
            3,
            cert=gfs.ch.cert(gfs.member, 3, channel_id=new_channel_id()),
        )
    elif case == "stale_epoch":
        body = _publish_body(gfs.ch, gfs.member, 1)
    else:
        body = _publish_body(gfs.ch, gfs.member, 3, cert=gfs.ch.cert(gfs.other, 3))
    await _refused(await gfs.post("/gfs/channels/publish", json=body))
    assert await _queued(gfs, gfs.other) == []


@pytest.mark.security
async def test_the_previous_epoch_closes_after_the_grace(gfs) -> None:
    await _ready(gfs, epoch=3)
    _advance(gfs, 61)
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(4, ts=_now(61)))
    assert resp.status == 200
    resp = await gfs.post(
        "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 3)
    )
    assert resp.status == 200  # within the grace
    _advance(gfs, 61 + 601)
    await _refused(
        await gfs.post(
            "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.other, 3)
        )
    )


@pytest.mark.security
async def test_a_seat_whose_pass_epoch_closed_receives_nothing(gfs) -> None:
    """A household removed at a rotation gets no new pass: once the grace is
    over its old seat is dead, even though the row still exists."""
    await _ready(gfs, epoch=3)
    await _seat(gfs, gfs.outsider, 3)  # will be "removed"
    _advance(gfs, 61)
    resp = await gfs.post("/gfs/channels/epoch", json=gfs.ch.notice(4, ts=_now(61)))
    assert resp.status == 200
    _advance(gfs, 61 + 601)
    resp = await gfs.post(
        "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 4)
    )
    assert resp.status == 200
    assert await _queued(gfs, gfs.outsider) == []


@pytest.mark.security
async def test_identified_publish_into_a_strict_channel_is_refused(gfs) -> None:
    await _ready(gfs, epoch=3, mode="strict")
    await _refused(
        await gfs.post(
            "/gfs/channels/publish", json=_publish_body(gfs.ch, gfs.member, 3)
        )
    )


# ── Strict publish ───────────────────────────────────────────────────────


async def test_anonymous_publish_under_the_pinned_writer_key(gfs) -> None:
    await _ready(gfs, epoch=3, mode="strict", wkc=gfs.ch.writer(3).writer_key_cert)
    await _seat(gfs, gfs.other)
    body = _anon_body(gfs.ch, 3)
    resp = await gfs.post("/gfs/channels/publish-anon", json=body)
    assert resp.status == 200, await resp.text()
    queued = await _queued(gfs, gfs.other)
    assert len(queued) == 1
    assert set(queued[0].sealed) == CHANNEL_FRAME_KEYS
    # An exact replay is refused.
    await _refused(await gfs.post("/gfs/channels/publish-anon", json=body))


@pytest.mark.security
async def test_anonymous_publish_refusals(gfs) -> None:
    await _ready(gfs, epoch=3, mode="strict", wkc=gfs.ch.writer(3).writer_key_cert)
    # Another key than the pinned one.
    await _refused(
        await gfs.post(
            "/gfs/channels/publish-anon",
            json=_anon_body(gfs.ch, 3, writer_seed=os.urandom(32)),
        )
    )
    # No pin for that epoch.
    await _refused(
        await gfs.post("/gfs/channels/publish-anon", json=_anon_body(gfs.ch, 4))
    )
    # Identity fields are malformed, not ignored.
    body = _anon_body(gfs.ch, 3)
    body["instance_id"] = gfs.member.instance_id
    resp = await gfs.post("/gfs/channels/publish-anon", json=body)
    assert resp.status == 400


# ── Unregister ───────────────────────────────────────────────────────────


async def test_unregister_drops_channel_and_seats(gfs) -> None:
    await _ready(gfs)
    await _seat(gfs, gfs.member)
    forged = sign_unregister(
        channel_seed=os.urandom(32),
        channel_id=gfs.ch.id,
        gfs_instance_id=GFS_ID,
        ts=_now(),
    ).to_wire()
    await _refused(await gfs.post("/gfs/channels/unregister", json=forged))
    body = sign_unregister(
        channel_seed=gfs.ch.seed,
        channel_id=gfs.ch.id,
        gfs_instance_id=GFS_ID,
        ts=_now(),
    ).to_wire()
    resp = await gfs.post("/gfs/channels/unregister", json=body)
    assert resp.status == 200
    repo = gfs.app_[gfs_channel_repo_key]
    assert await repo.get(gfs.ch.id) is None
    assert not await repo.has_subscription(gfs.ch.id, gfs.member.instance_id)
    # Idempotent.
    resp = await gfs.post("/gfs/channels/unregister", json=body)
    assert resp.status == 200


# ── Blind ────────────────────────────────────────────────────────────────


@pytest.mark.security
async def test_the_server_stores_nothing_that_names_the_space(gfs, tmp_dir) -> None:
    await _ready(gfs, epoch=3, mode="strict", wkc=gfs.ch.writer(3).writer_key_cert)
    await _seat(gfs, gfs.other)
    resp = await gfs.post("/gfs/channels/publish-anon", json=_anon_body(gfs.ch, 3))
    assert resp.status == 200
    await gfs.app_[gfs_member_publish_key].wait_idle()
    dump = (tmp_dir / "gfs.db").read_bytes()
    for wal in tmp_dir.glob("gfs.db*"):
        dump += wal.read_bytes()
    for needle in (SPACE_ID, SPACE_PK.hex(), b64url_encode(SPACE_PK)):
        assert needle.encode() not in dump
    assert gfs.ch.id.encode() in dump
