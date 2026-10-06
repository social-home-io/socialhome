"""Integration tests for trusted-mode member publish (v_49).

Real GFS app, real SQLite, real aiohttp client + WebSocket: ``POST
/gfs/member-publish`` authorizes a member household by its registered
identity + a space-authority-signed writer cert, and fans the opaque item out
to the space's subscribers (live or queued) without naming the publisher.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import orjson
import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    ed25519_public_key,
    sign_ed25519,
)
from socialhome.domain.gfs_member_publish import (
    MEMBER_PUBLISH_ANON_FRAME_KEYS,
    MEMBER_PUBLISH_EPOCH_GRACE_S,
    MemberPublishAnonRequest,
    owner_epoch_notice_signing_payload,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishRequest,
)
from socialhome.global_server import member_publish as mp_mod
from socialhome.global_server import envelope_relay as envelope_relay_mod
from socialhome.global_server import federation as federation_mod
from socialhome.global_server.routes import ws as ws_mod
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_envelope_queue_repo_key,
    gfs_fed_repo_key,
    gfs_federation_key,
    gfs_member_publish_key,
    gfs_space_epoch_repo_key,
    gfs_ws_registry_key,
)
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.domain import ClientInstance, GlobalSpace
from socialhome.global_server.member_publish import (
    MEMBER_PUBLISH_MAX_PER_MINUTE,
    _is_member_publish_path,
)
from socialhome.domain.writer_cert import MAX_WRITER_CERT_EPOCH
from socialhome.global_server.server import create_gfs_app
from socialhome.writer_cert import bind_writer_users, sign_writer_cert
from socialhome.writer_key import (
    derive_writer_seed,
    sign_with_writer_key,
    sign_writer_key_cert,
)

SPACE_ID = "sp-public"
OTHER_SPACE = "sp-other"
CIPHERTEXT = "bm9uY2U:c3BhY2UtaXRlbS1jaXBoZXJ0ZXh0"
GFS_ID = "gfs-node-a"


class _Household:
    def __init__(self) -> None:
        self.seed = os.urandom(32)
        self.pk = ed25519_public_key(self.seed)
        self.instance_id = derive_instance_id(self.pk)

    def instance(self, status: str = "active") -> ClientInstance:
        return ClientInstance(
            instance_id=self.instance_id,
            display_name="H",
            public_key=self.pk.hex(),
            # A closed loopback port: an HTTPS-inbox fallback is refused at
            # once, never a real DNS lookup of ``<id>.home``.
            inbox_url=f"http://127.0.0.1:1/{self.instance_id}/wh",
            status=status,
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


SPACE_SEED = os.urandom(32)
SPACE_PK = ed25519_public_key(SPACE_SEED)


def _now_iso(delta_s: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat()


def _cert(holder: _Household, *, epoch=3, space_id=SPACE_ID, seed=SPACE_SEED):
    return sign_writer_cert(
        space_seed=seed,
        space_id=space_id,
        epoch=epoch,
        instance_pk=holder.pk,
        scope="comment",
    )


def _body(
    publisher: _Household,
    *,
    cert=None,
    epoch: int = 3,
    target: str = SPACE_ID,
    payload: str = CIPHERTEXT,
    ts: str | None = None,
    signer: _Household | None = None,
    gfs_instance_id: str = GFS_ID,
) -> dict:
    req = MemberPublishRequest(
        instance_id=publisher.instance_id,
        gfs_instance_id=gfs_instance_id,
        ts=ts or _now_iso(),
        signature="",
        target=target,
        epoch=epoch,
        writer_cert=cert if cert is not None else _cert(publisher, epoch=epoch),
        payload=payload,
    )
    sig = sign_ed25519((signer or publisher).seed, req.signing_bytes())
    return replace(req, signature=b64url_encode(sig)).to_wire()


@pytest.fixture
async def gfs(tmp_dir):
    cfg = GfsConfig(
        host="127.0.0.1",
        port=0,
        base_url="http://gfs.test",
        data_dir=str(tmp_dir),
        instance_id="gfs-node-a",
        cluster_enabled=False,
        cluster_node_id="gfs-node-a",
        cluster_peers=(),
    )
    app = create_gfs_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        fed = app[gfs_fed_repo_key]
        tc.app_ = app
        tc.publisher = _Household()
        tc.subscriber = _Household()
        tc.owner = _Household()
        for h in (tc.publisher, tc.subscriber, tc.owner):
            await fed.upsert_instance(h.instance())
        for sid in (SPACE_ID, OTHER_SPACE):
            await fed.upsert_space(
                GlobalSpace(
                    space_id=sid,
                    owning_instance=tc.owner.instance_id,
                    name="Public",
                    allow_subscribers=True,
                    status="active",
                    identity_public_key=SPACE_PK.hex(),
                )
            )
        # The publisher is subscribed too — it must never get its own echo.
        for h in (tc.publisher, tc.subscriber):
            await fed.add_subscriber(space_id=SPACE_ID, instance_id=h.instance_id)
        # Seen recently (a WS session held long enough): offline items
        # queue for it.
        await fed.mark_relay_seen(tc.subscriber.instance_id, at=int(time.time()))
        yield tc


async def _wait_connected(app, instance_id: str) -> None:
    registry = app[gfs_ws_registry_key]
    for _ in range(200):
        if registry.is_connected(instance_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{instance_id} never connected")


async def _settle(gfs) -> None:
    """Wait until the background fan-out has handled every accepted item."""
    await gfs.app_[gfs_member_publish_key].wait_idle()


async def _queued(gfs, household: _Household) -> list:
    await _settle(gfs)
    return await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        household.instance_id, now=0
    )


async def _assert_refused(resp) -> None:
    assert resp.status == 403
    assert await resp.json() == {"error": "not authorized to publish to this space"}


# ── Accept + fan-out ─────────────────────────────────────────────────────


async def test_a_valid_cert_is_relayed_live_without_naming_the_publisher(gfs):
    async with (
        gfs.ws_connect("/gfs/ws") as sub_ws,
        gfs.ws_connect("/gfs/ws") as pub_ws,
    ):
        await sub_ws.send_json(gfs.subscriber.hello())
        await pub_ws.send_json(gfs.publisher.hello())
        await _wait_connected(gfs.app_, gfs.subscriber.instance_id)
        await _wait_connected(gfs.app_, gfs.publisher.instance_id)

        body = _body(gfs.publisher)
        resp = await gfs.post("/gfs/member-publish", json=body)
        assert resp.status == 200
        assert await resp.json() == {"status": "published"}

        frame = await asyncio.wait_for(sub_ws.receive_json(), timeout=5)
        # The publisher's own socket stays silent (no self-echo).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(pub_ws.receive_json(), timeout=0.3)

    assert frame == {
        "type": "relay",
        "space_id": SPACE_ID,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": 3,
        "writer_cert": body["writer_cert"],
        "payload": CIPHERTEXT,
    }
    assert "from_instance" not in frame
    assert gfs.publisher.instance_id not in json.dumps(frame)


async def test_an_offline_subscriber_gets_the_item_queued_then_drained(gfs):
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    assert resp.status == 200

    queued = await _queued(gfs, gfs.subscriber)
    assert [q.frame_type for q in queued] == ["relay"]
    assert await _queued(gfs, gfs.publisher) == []

    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(gfs.subscriber.hello())
        frame = await asyncio.wait_for(ws.receive_json(), timeout=5)
    assert frame["type"] == "relay"
    assert frame["event_type"] == SPACE_ITEM_EVENT_TYPE
    assert frame["payload"] == CIPHERTEXT
    assert "from_instance" not in frame


# ── Refusals: certs ──────────────────────────────────────────────────────


@pytest.mark.security
async def test_a_forged_cert_is_refused(gfs):
    forged = _cert(gfs.publisher, seed=os.urandom(32))
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, cert=forged))
    await _assert_refused(resp)
    assert await _queued(gfs, gfs.subscriber) == []


@pytest.mark.security
async def test_someone_elses_cert_is_refused(gfs):
    """A household can only publish with a cert issued to its own key."""
    resp = await gfs.post(
        "/gfs/member-publish",
        json=_body(gfs.publisher, cert=_cert(gfs.subscriber)),
    )
    await _assert_refused(resp)


@pytest.mark.security
async def test_a_cert_for_another_space_is_refused(gfs):
    resp = await gfs.post(
        "/gfs/member-publish",
        json=_body(gfs.publisher, cert=_cert(gfs.publisher, space_id=OTHER_SPACE)),
    )
    await _assert_refused(resp)


@pytest.mark.security
async def test_a_cert_for_another_epoch_than_the_request_is_refused(gfs):
    resp = await gfs.post(
        "/gfs/member-publish",
        json=_body(gfs.publisher, epoch=3, cert=_cert(gfs.publisher, epoch=2)),
    )
    await _assert_refused(resp)


@pytest.mark.security
async def test_an_unknown_cert_suite_is_refused(gfs):
    cert = replace(_cert(gfs.publisher), cert_suite="ed25519+mldsa65")
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, cert=cert))
    await _assert_refused(resp)


# ── Refusals: household identity ─────────────────────────────────────────


@pytest.mark.security
async def test_a_bad_household_signature_is_refused(gfs):
    body = _body(gfs.publisher, signer=gfs.subscriber)
    resp = await gfs.post("/gfs/member-publish", json=body)
    await _assert_refused(resp)


@pytest.mark.security
async def test_a_tampered_field_breaks_the_household_signature(gfs):
    body = _body(gfs.publisher)
    body["payload"] = "dGFtcGVyZWQ"
    resp = await gfs.post("/gfs/member-publish", json=body)
    await _assert_refused(resp)


@pytest.mark.security
async def test_a_stale_ts_is_refused(gfs):
    body = _body(gfs.publisher, ts=_now_iso(-400))
    resp = await gfs.post("/gfs/member-publish", json=body)
    await _assert_refused(resp)


@pytest.mark.security
async def test_an_unregistered_household_is_refused(gfs):
    stranger = _Household()
    resp = await gfs.post("/gfs/member-publish", json=_body(stranger))
    await _assert_refused(resp)


@pytest.mark.security
@pytest.mark.parametrize("status", ["banned", "pending"])
async def test_a_non_active_household_is_refused(gfs, status):
    await gfs.app_[gfs_fed_repo_key].upsert_instance(gfs.publisher.instance(status))
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    await _assert_refused(resp)


# ── Refusals: space ──────────────────────────────────────────────────────


@pytest.mark.security
async def test_an_unknown_space_is_refused(gfs):
    resp = await gfs.post(
        "/gfs/member-publish",
        json=_body(
            gfs.publisher,
            target="sp-nope",
            cert=_cert(gfs.publisher, space_id="sp-nope"),
        ),
    )
    await _assert_refused(resp)


@pytest.mark.security
@pytest.mark.parametrize(
    "change",
    [
        {"status": "banned"},
        {"allow_subscribers": False},
        {"identity_public_key": ""},
    ],
)
async def test_a_banned_unreadable_or_unpinned_space_is_refused(gfs, change):
    fed = gfs.app_[gfs_fed_repo_key]
    space = await fed.get_space(SPACE_ID)
    if "identity_public_key" in change:
        # The upsert never clears a set pin — build the row unpinned.
        await fed.delete_space(SPACE_ID)
    await fed.upsert_space(replace(space, **change))
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    await _assert_refused(resp)


async def test_a_withdrawn_space_still_relays_to_existing_subscribers(gfs):
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.set_space_withdrawn(SPACE_ID, True)
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    assert resp.status == 200


# ── Replay + rate limit ──────────────────────────────────────────────────


@pytest.mark.security
async def test_a_replayed_request_is_not_fanned_out_twice(gfs):
    body = _body(gfs.publisher)
    for _ in range(2):
        resp = await gfs.post("/gfs/member-publish", json=body)
        assert resp.status == 200
    assert len(await _queued(gfs, gfs.subscriber)) == 1


@pytest.mark.security
async def test_the_per_household_space_rate_limit_answers_429(gfs):
    for i in range(MEMBER_PUBLISH_MAX_PER_MINUTE):
        resp = await gfs.post(
            "/gfs/member-publish", json=_body(gfs.publisher, payload=f"ct-{i}")
        )
        assert resp.status == 200, i
    resp = await gfs.post(
        "/gfs/member-publish", json=_body(gfs.publisher, payload="ct-over")
    )
    assert resp.status == 429
    assert resp.headers["Retry-After"] == "60"
    assert len(await _queued(gfs, gfs.subscriber)) == MEMBER_PUBLISH_MAX_PER_MINUTE


async def test_the_rate_limit_is_per_space(gfs, monkeypatch):
    svc_limiter = gfs.app_[gfs_member_publish_key]._limiter
    monkeypatch.setattr(svc_limiter, "_limit", 1, raising=False)
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.add_subscriber(
        space_id=OTHER_SPACE, instance_id=gfs.subscriber.instance_id
    )
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    ).status == 200
    other = _body(
        gfs.publisher,
        target=OTHER_SPACE,
        cert=_cert(gfs.publisher, space_id=OTHER_SPACE),
    )
    assert (await gfs.post("/gfs/member-publish", json=other)).status == 200


# ── Malformed bodies ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(event_type="space_post_created"),
        lambda b: b.update(from_instance="x"),
        lambda b: b.pop("writer_cert"),
        lambda b: b.update(payload={"content": "plaintext"}),
    ],
)
async def test_a_malformed_body_is_a_400(gfs, mutate):
    body = _body(gfs.publisher)
    mutate(body)
    resp = await gfs.post("/gfs/member-publish", json=body)
    assert resp.status == 400


async def test_a_non_object_body_is_a_400(gfs):
    resp = await gfs.post("/gfs/member-publish", data=b"[1, 2]")
    assert resp.status == 400


# ── Epoch notice ─────────────────────────────────────────────────────────


def _notice(epoch, *, seed=SPACE_SEED, space_id=SPACE_ID) -> dict:
    payload = {"space_id": space_id, "epoch": epoch}
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
        space_id=space_id,
        payload=payload,
        space_seed=seed,
    )
    return {"epoch": epoch, **sig}


@pytest.mark.security
@pytest.mark.parametrize(
    "body",
    [
        _notice(5, seed=os.urandom(32)),
        {**_notice(5), "epoch": 6},
        {**_notice(5), "authority_sig_suite": "ed25519+mldsa65"},
        _notice(5, space_id=OTHER_SPACE),
        {**_notice(5), "epoch": "5"},
        {**_notice(5), "epoch": -1},
    ],
)
async def test_a_bad_epoch_notice_is_refused(gfs, body):
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)
    assert resp.status == 403
    assert await resp.json() == {"error": "not authorized for this space"}
    assert await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID) is None


@pytest.mark.security
async def test_a_relay_payload_signature_is_not_an_epoch_notice(gfs):
    """The signing bytes bind the event type."""
    payload = {"space_id": SPACE_ID, "epoch": 5}
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        space_id=SPACE_ID,
        payload=payload,
        space_seed=SPACE_SEED,
    )
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json={"epoch": 5, **sig})
    assert resp.status == 403


async def test_an_epoch_notice_for_an_unknown_space_is_refused(gfs):
    resp = await gfs.post(
        "/gfs/spaces/sp-nope/epoch", json=_notice(5, space_id="sp-nope")
    )
    assert resp.status == 403


async def test_an_epoch_notice_for_an_unpinned_space_is_refused(gfs):
    fed = gfs.app_[gfs_fed_repo_key]
    space = await fed.get_space(SPACE_ID)
    await fed.delete_space(SPACE_ID)
    await fed.upsert_space(replace(space, identity_public_key=""))
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(5))
    assert resp.status == 403


async def test_an_epoch_notice_missing_a_field_is_a_400(gfs):
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json={"epoch": 5})
    assert resp.status == 400


# ── Capability + limiter wiring ──────────────────────────────────────────


async def test_info_advertises_member_publish_trusted_in_the_signed_block(gfs):
    resp = await gfs.get("/gfs/info")
    body = await resp.json()
    assert body["capabilities"]["member_publish_trusted"] is True
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )


@pytest.mark.parametrize(
    ("path", "limited"),
    [
        ("/gfs/member-publish", True),
        ("/gfs/spaces/abc/epoch", True),
        ("/gfs/publish", False),
        ("/gfs/spaces/abc/publish", False),
    ],
)
def test_the_per_ip_limiter_covers_exactly_the_member_publish_routes(path, limited):
    assert _is_member_publish_path(path) is limited


@pytest.mark.security
async def test_no_log_line_carries_the_ciphertext(gfs, caplog):
    with caplog.at_level(logging.DEBUG, logger="socialhome"):
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
        await gfs.post(
            "/gfs/member-publish",
            json=_body(gfs.publisher, signer=gfs.subscriber, payload="x" + CIPHERTEXT),
        )
    assert CIPHERTEXT not in caplog.text


def test_module_constants():
    assert mp_mod.MEMBER_PUBLISH_MAX_PER_MINUTE == 30
    assert mp_mod.EPOCH_NOTICE_ROUTE == "/gfs/spaces/{space_id}/epoch"


async def test_a_non_object_epoch_notice_is_a_400(gfs):
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", data=b"[1]")
    assert resp.status == 400


async def test_a_tail_dropped_subscriber_does_not_fail_the_publish(gfs, monkeypatch):
    monkeypatch.setattr(
        "socialhome.global_server.envelope_relay.RELAY_QUEUE_MAX_PER_RECIPIENT", 0
    )
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    assert resp.status == 200
    assert await _queued(gfs, gfs.subscriber) == []


# ── Review fixes: sybil amplification (I2) ───────────────────────────────


async def _add_sybils(gfs, n: int, *, seen: bool = False) -> list[_Household]:
    fed = gfs.app_[gfs_fed_repo_key]
    out = []
    for _ in range(n):
        h = _Household()
        await fed.upsert_instance(h.instance())
        await fed.add_subscriber(space_id=SPACE_ID, instance_id=h.instance_id)
        # Every sybil sends a bare hello — that alone must not count as seen.
        await fed.upsert_rtc_connection(h.instance_id, transport="websocket")
        if seen:
            await fed.mark_relay_seen(h.instance_id, at=int(time.time()))
        out.append(h)
    return out


@pytest.mark.security
async def test_never_seen_offline_subscribers_get_no_queue_rows(gfs):
    sybils = await _add_sybils(gfs, 200)
    big = "A" * (200 * 1024)
    started = asyncio.get_running_loop().time()
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, payload=big))
    elapsed = asyncio.get_running_loop().time() - started
    assert resp.status == 200
    assert elapsed < 2.0
    for h in sybils:
        assert await _queued(gfs, h) == []
    # The genuine, recently-seen subscriber still gets its copy.
    assert len(await _queued(gfs, gfs.subscriber)) == 1


@pytest.mark.security
async def test_the_per_space_aggregate_rate_limit_answers_429(gfs, monkeypatch):
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setattr(svc._space_limiter, "_limit", 1)
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    ).status == 200
    # A different household, same space: the space budget is spent.
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.subscriber))
    assert resp.status == 429


async def test_stop_drains_the_workers_and_refuses_new_jobs(gfs):
    svc = gfs.app_[gfs_member_publish_key]
    await svc.stop()
    assert svc._tasks == []
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    assert resp.status == 503
    await svc.start()


# ── Review fixes: audience binding (M4) ──────────────────────────────────


@pytest.mark.security
async def test_a_request_signed_for_another_gfs_is_refused(gfs):
    resp = await gfs.post(
        "/gfs/member-publish",
        json=_body(gfs.publisher, gfs_instance_id="gfs-node-b"),
    )
    await _assert_refused(resp)


# ── Round 2: epoch lockout (I1) ───────────────────────────────────────────


async def _state(gfs):
    return await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID)


def _owner_notice(
    gfs,
    epoch,
    *,
    ts: str | None = None,
    signer: _Household | None = None,
    owning: _Household | None = None,
    gfs_instance_id: str = GFS_ID,
) -> dict:
    owner = owning or gfs.owner
    ts = ts or _now_iso()
    payload = owner_epoch_notice_signing_payload(
        owning_instance=owner.instance_id,
        gfs_instance_id=gfs_instance_id,
        space_id=SPACE_ID,
        epoch=epoch,
        ts=ts,
    )
    sig = sign_ed25519(
        (signer or owner).seed,
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )
    return {
        "owning_instance": owner.instance_id,
        "gfs_instance_id": gfs_instance_id,
        "epoch": epoch,
        "ts": ts,
        "signature": b64url_encode(sig),
    }


async def _confirm(gfs, epoch) -> None:
    resp = await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_owner_notice(gfs, epoch)
    )
    assert resp.status == 200


async def test_the_owner_confirms_an_epoch_and_retires_the_old_one(gfs):
    await _confirm(gfs, 3)
    await _confirm(gfs, 4)
    state = await _state(gfs)
    assert (state.confirmed, state.previous, state.current) == (4, 3, 4)
    # The old epoch rides the grace …
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
    ).status == 200
    # … and is retired once it runs out; older ones never pass.
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + MEMBER_PUBLISH_EPOCH_GRACE_S + 5
    await _assert_refused(
        await gfs.post(
            "/gfs/member-publish", json=_body(gfs.publisher, epoch=3, payload="x1")
        )
    )
    await _assert_refused(
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=2))
    )


async def test_the_owner_may_jump_to_wall_clock_after_a_restore(gfs):
    await _confirm(gfs, 5)
    now = int(time.time())
    await _confirm(gfs, now)
    assert (await _state(gfs)).confirmed == now


@pytest.mark.security
@pytest.mark.parametrize("epoch", [MAX_WRITER_CERT_EPOCH, 10**12])
async def test_an_owner_notice_beyond_the_ceiling_is_refused(gfs, epoch):
    resp = await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_owner_notice(gfs, epoch)
    )
    assert resp.status == 403
    assert await _state(gfs) is None


@pytest.mark.security
@pytest.mark.parametrize(
    "kwargs",
    [
        # A registered household that is not the owner, signing for itself.
        {"owning": "publisher"},
        # The owner's id, someone else's signature.
        {"signer": "publisher"},
        # Addressed to another connection server.
        {"gfs_instance_id": "gfs-node-b"},
        # Stale.
        {"ts": "stale"},
    ],
)
async def test_only_a_fresh_owner_signature_confirms(gfs, kwargs):
    kw = dict(kwargs)
    if kw.get("owning") == "publisher":
        kw["owning"] = gfs.publisher
    if kw.get("signer") == "publisher":
        kw["signer"] = gfs.publisher
    if kw.get("ts") == "stale":
        kw["ts"] = _now_iso(-400)
    resp = await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_owner_notice(gfs, 7, **kw)
    )
    assert resp.status == 403
    assert await _state(gfs) is None


@pytest.mark.security
async def test_a_seed_only_notice_cannot_jump_to_wall_clock(gfs):
    """R1: one seed-holder notice used to move a small epoch to ~1.7e9."""
    await _confirm(gfs, 3)
    jump = int(time.time()) + 3600
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(jump))
    assert resp.status == 200  # verified, but changes nothing
    assert (await _state(gfs)).current == 3
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + 601
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.subscriber, epoch=4))
    ).status == 200


@pytest.mark.security
async def test_seed_only_notices_step_by_one_at_most_once_a_minute(gfs):
    """R2: repeated seed-only notices used to ratchet without bound."""
    await _confirm(gfs, 10)
    svc = gfs.app_[gfs_member_publish_key]
    base = time.time() + 120
    svc._clock = lambda: base
    for epoch in (10_000, 11, 12):
        await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(epoch))
    state = await _state(gfs)
    assert (state.current, state.confirmed) == (11, 10)


async def test_a_seed_only_notice_never_initialises_the_state(gfs):
    await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(4))
    assert await _state(gfs) is None


@pytest.mark.security
async def test_seed_only_raises_never_strand_writers_on_the_confirmed_epoch(gfs):
    await _confirm(gfs, 3)
    svc = gfs.app_[gfs_member_publish_key]
    t = time.time()
    for step in range(1, 4):
        svc._clock = lambda step=step: t + 61 * step
        await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(3 + step))
    assert (await _state(gfs)).current == 6
    svc._clock = lambda: t + 10_000
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
    ).status == 200


@pytest.mark.security
async def test_certs_never_raise_the_epoch(gfs):
    """Two +1 certs used to raise ``previous`` past the real writers."""
    await _confirm(gfs, 3)
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=4))
    ).status == 200
    state = await _state(gfs)
    assert (state.current, state.confirmed) == (3, 3)
    await _assert_refused(
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=5))
    )
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + 10_000
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.subscriber, epoch=3))
    ).status == 200


@pytest.mark.security
async def test_a_cert_far_ahead_is_refused_once_an_epoch_is_confirmed(gfs):
    await _confirm(gfs, 3)
    await _assert_refused(
        await gfs.post(
            "/gfs/member-publish",
            json=_body(gfs.publisher, epoch=MAX_WRITER_CERT_EPOCH),
        )
    )


def _relay_envelope(epoch) -> dict:
    envelope = {"space_id": SPACE_ID, "epoch": epoch, "encrypted_payload": CIPHERTEXT}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=SPACE_SEED,
        )
    )
    return envelope


async def test_an_authority_relay_steps_the_current_epoch_by_one(gfs):
    await _confirm(gfs, 3)
    # The owner's confirm counts as the last raise; let a minute pass.
    await gfs.app_[gfs_db_key].enqueue(
        "UPDATE global_spaces SET content_epoch_raised_at=0 WHERE space_id=?",
        (SPACE_ID,),
    )
    fed_svc = gfs.app_[gfs_federation_key]
    await fed_svc.publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, _relay_envelope(4)
    )
    assert (await _state(gfs)).current == 4


@pytest.mark.security
async def test_an_authority_relay_at_an_absurd_epoch_teaches_nothing(gfs):
    await _confirm(gfs, 3)
    await gfs.app_[gfs_federation_key].publish_event(
        SPACE_ID,
        AUTHORITY_EVENT_SPACE_POST_PUBLIC,
        _relay_envelope(MAX_WRITER_CERT_EPOCH),
    )
    assert (await _state(gfs)).current == 3


async def test_a_malformed_relay_epoch_teaches_nothing(gfs):
    await _confirm(gfs, 3)
    await gfs.app_[gfs_federation_key].publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, _relay_envelope("4")
    )
    assert (await _state(gfs)).current == 3


async def test_the_first_confirmed_epoch_gives_its_predecessor_the_grace(gfs):
    await _confirm(gfs, 4)
    assert (await _state(gfs)).previous == 3
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
    ).status == 200


async def test_a_missing_owner_notice_field_is_a_400(gfs):
    body = _owner_notice(gfs, 4)
    del body["ts"]
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)
    assert resp.status == 400


# ── Round 2: sybils and the relay queue (I2) ──────────────────────────────


@pytest.mark.security
async def test_bare_hello_sybils_get_no_rows_and_cannot_starve_the_seen(
    gfs, monkeypatch
):
    """R3: a bare hello used to count as seen, so sybils filled the cap."""
    monkeypatch.setattr(envelope_relay_mod, "RELAY_QUEUE_MAX_TOTAL_BYTES", 4000)
    sybils = await _add_sybils(gfs, 5)
    for i in range(3):
        resp = await gfs.post(
            "/gfs/member-publish",
            json=_body(gfs.publisher, payload="B" * 900 + str(i)),
        )
        assert resp.status == 200
    for h in sybils:
        assert await _queued(gfs, h) == []
    assert len(await _queued(gfs, gfs.subscriber)) == 3


@pytest.mark.security
async def test_the_global_cap_evicts_from_the_largest_holder(gfs, monkeypatch):
    one = len(
        orjson.dumps(
            MemberPublishRequest.from_wire(
                _body(gfs.publisher, payload="L" * 900 + "0")
            ).fan_out_frame()
        )
    )
    cap = 3 * one + 50
    monkeypatch.setattr(envelope_relay_mod, "RELAY_QUEUE_MAX_TOTAL_BYTES", cap)
    hog = (await _add_sybils(gfs, 1, seen=True))[0]
    # The hog alone subscribes to a second space and soaks up the cap there.
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.add_subscriber(space_id=OTHER_SPACE, instance_id=hog.instance_id)
    await fed.remove_subscriber(space_id=SPACE_ID, instance_id=hog.instance_id)
    for i in range(4):
        await gfs.post(
            "/gfs/member-publish",
            json=_body(
                gfs.publisher,
                target=OTHER_SPACE,
                cert=_cert(gfs.publisher, space_id=OTHER_SPACE),
                payload="H" * 900 + str(i),
            ),
        )
    await _settle(gfs)
    for i in range(2):
        await gfs.post(
            "/gfs/member-publish",
            json=_body(gfs.publisher, payload="L" * 900 + str(i)),
        )
    legit = await _queued(gfs, gfs.subscriber)
    assert len(legit) == 2
    # The hog paid for the room, down to its fair share.
    assert len(await _queued(gfs, hog)) == 1
    total = await gfs.app_[gfs_envelope_queue_repo_key].relay_bytes(0)
    assert total <= cap


@pytest.mark.security
async def test_a_subscriber_seen_beyond_the_queue_ttl_gets_no_row(gfs):
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.mark_relay_seen(
        gfs.subscriber.instance_id, at=int(time.time()) - 2 * 86400
    )
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    ).status == 200
    assert await _queued(gfs, gfs.subscriber) == []


async def test_a_long_enough_ws_session_marks_the_household_seen(gfs, monkeypatch):
    stranger = _Household()
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.upsert_instance(stranger.instance())
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(stranger.hello())
        await _wait_connected(gfs.app_, stranger.instance_id)
    await asyncio.sleep(0.05)
    assert (await fed.get_instance(stranger.instance_id)) is not None
    db = gfs.app_[gfs_db_key]
    row = await db.fetchone(
        "SELECT relay_seen_at FROM client_instances WHERE instance_id=?",
        (stranger.instance_id,),
    )
    assert row["relay_seen_at"] is None  # a bare hello earns nothing
    monkeypatch.setattr(ws_mod, "RELAY_SEEN_MIN_SESSION_S", 0.05)
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(stranger.hello())
        await _wait_connected(gfs.app_, stranger.instance_id)
        await asyncio.sleep(0.3)  # held past the (shortened) minimum
    for _ in range(100):
        row = await db.fetchone(
            "SELECT relay_seen_at FROM client_instances WHERE instance_id=?",
            (stranger.instance_id,),
        )
        if row["relay_seen_at"] is not None:
            break
        await asyncio.sleep(0.02)
    assert row["relay_seen_at"] is not None


def _subscribe_sig(h: _Household, space_id: str, ts: str) -> str:
    canonical = json.dumps(
        {
            "action": "subscribe",
            "instance_id": h.instance_id,
            "space_id": space_id,
            "ts": ts,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return b64url_encode(sign_ed25519(h.seed, canonical))


@pytest.mark.security
async def test_one_household_holds_a_bounded_number_of_subscriptions(gfs, monkeypatch):
    monkeypatch.setattr(federation_mod, "MAX_SUBSCRIPTIONS_PER_INSTANCE", 1)
    fed_svc = gfs.app_[gfs_federation_key]
    ts = _now_iso()
    # The subscriber already holds SPACE_ID: re-subscribing it is fine …
    await fed_svc.subscribe(
        gfs.subscriber.instance_id,
        SPACE_ID,
        ts,
        _subscribe_sig(gfs.subscriber, SPACE_ID, ts),
    )
    # … a second space is over the limit.
    with pytest.raises(PermissionError):
        await fed_svc.subscribe(
            gfs.subscriber.instance_id,
            OTHER_SPACE,
            ts,
            _subscribe_sig(gfs.subscriber, OTHER_SPACE, ts),
        )


# ── Round 2: fan-out fairness, ordering, shutdown ─────────────────────────


async def test_one_space_is_pinned_to_one_worker(gfs):
    svc = gfs.app_[gfs_member_publish_key]
    assert svc._worker_index(SPACE_ID) == svc._worker_index(SPACE_ID)
    assert 0 <= svc._worker_index(OTHER_SPACE) < mp_mod.FAN_OUT_WORKERS


async def test_a_space_s_items_arrive_in_publish_order(gfs):
    async with gfs.ws_connect("/gfs/ws") as ws:
        await ws.send_json(gfs.subscriber.hello())
        await _wait_connected(gfs.app_, gfs.subscriber.instance_id)
        for i in range(6):
            resp = await gfs.post(
                "/gfs/member-publish", json=_body(gfs.publisher, payload=f"ct-{i}")
            )
            assert resp.status == 200
        got = [
            (await asyncio.wait_for(ws.receive_json(), timeout=5))["payload"]
            for _ in range(6)
        ]
    assert got == [f"ct-{i}" for i in range(6)]


async def test_one_busy_space_cannot_refuse_another(gfs, monkeypatch):
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setitem(svc._pending, SPACE_ID, mp_mod.FAN_OUT_BACKLOG_PER_SPACE)
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    assert resp.status == 503
    assert resp.headers["Retry-After"] == "5"
    other = _body(
        gfs.publisher,
        target=OTHER_SPACE,
        cert=_cert(gfs.publisher, space_id=OTHER_SPACE),
    )
    assert (await gfs.post("/gfs/member-publish", json=other)).status == 200
    monkeypatch.delitem(svc._pending, SPACE_ID)


async def test_stop_drains_accepted_items_before_returning(gfs):
    svc = gfs.app_[gfs_member_publish_key]
    for i in range(4):
        assert (
            await gfs.post(
                "/gfs/member-publish", json=_body(gfs.publisher, payload=f"s-{i}")
            )
        ).status == 200
    await svc.stop()
    rows = await gfs.app_[gfs_envelope_queue_repo_key].list_for(
        gfs.subscriber.instance_id, now=0
    )
    assert len(rows) == 4
    await svc.start()


async def test_stop_warns_with_the_count_it_could_not_drain(gfs, monkeypatch, caplog):
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setattr(mp_mod, "FAN_OUT_STOP_DRAIN_S", 0.05)
    gate = asyncio.Event()

    async def _stuck_relay(self, targets, *, queue_ok, frame):
        await gate.wait()
        return 0

    monkeypatch.setattr(type(svc._relay), "fan_out_relay", _stuck_relay)
    for i in range(3):
        await gfs.post(
            "/gfs/member-publish", json=_body(gfs.publisher, payload=f"w-{i}")
        )
    with caplog.at_level(logging.WARNING, logger="socialhome.global_server"):
        stopping = asyncio.create_task(svc.stop())
        await asyncio.sleep(0.2)
        gate.set()
        await stopping
    assert "not fanned out before shutdown" in caplog.text
    await svc.start()


@pytest.mark.security
async def test_a_writer_cert_carrying_a_user_binding_is_a_400(gfs):
    """The v2 binding names the household's users: it must never reach the
    connection server, so a request carrying it is refused outright (and
    nothing of it can reach a fan-out frame or the queue)."""
    bound = bind_writer_users(
        _cert(gfs.publisher), space_seed=SPACE_SEED, user_ids=["u-1", "u-2"]
    )
    body = _body(gfs.publisher)
    body["writer_cert"] = bound.to_wire()
    resp = await gfs.post("/gfs/member-publish", json=body)
    assert resp.status == 400
    assert await _queued(gfs, gfs.subscriber) == []


# ── Strict mode (v_50): anonymous publish under the writer group key ─────


def _wkc(epoch: int, *, seed: bytes = SPACE_SEED, space_id: str = SPACE_ID) -> dict:
    return sign_writer_key_cert(
        space_seed=seed,
        space_id=space_id,
        epoch=epoch,
        writer_pk=ed25519_public_key(derive_writer_seed(seed, space_id, epoch)),
    ).to_wire()


def _strict_notice(
    gfs,
    epoch: int,
    *,
    mode: str | None = "strict",
    wkc: dict | None = None,
    ts: str | None = None,
    tamper_mode: str | None = None,
) -> dict:
    ts = ts or _now_iso()
    wkc = wkc if wkc is not None else _wkc(epoch)
    payload = owner_epoch_notice_signing_payload(
        owning_instance=gfs.owner.instance_id,
        gfs_instance_id=GFS_ID,
        space_id=SPACE_ID,
        epoch=epoch,
        ts=ts,
        publish_mode=mode,
        writer_key_cert=wkc,
    )
    sig = sign_ed25519(
        gfs.owner.seed,
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
    )
    body = {
        "owning_instance": gfs.owner.instance_id,
        "gfs_instance_id": GFS_ID,
        "epoch": epoch,
        "ts": ts,
        "signature": b64url_encode(sig),
        "writer_key_cert": wkc,
    }
    if mode is not None:
        body["publish_mode"] = tamper_mode or mode
    return body


async def _go_strict(gfs, epoch: int = 3, mode: str = "strict") -> None:
    resp = await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_strict_notice(gfs, epoch, mode=mode)
    )
    assert resp.status == 200


def _anon(
    *,
    epoch: int = 3,
    key_epoch: int | None = None,
    payload: str = CIPHERTEXT,
    ts: str | None = None,
    nonce: str | None = None,
    gfs_instance_id: str = GFS_ID,
    suite: str = "ed25519",
    seed: bytes = SPACE_SEED,
) -> dict:
    req = MemberPublishAnonRequest(
        gfs_instance_id=gfs_instance_id,
        ts=ts or _now_iso(),
        nonce=nonce or b64url_encode(os.urandom(16)),
        target=SPACE_ID,
        epoch=epoch,
        payload=payload,
        writer_sig="",
        writer_sig_suite=suite,
    )
    writer_seed = derive_writer_seed(
        seed, SPACE_ID, epoch if key_epoch is None else key_epoch
    )
    return replace(
        req, writer_sig=sign_with_writer_key(writer_seed, req.signing_bytes())
    ).to_wire()


async def _strict_state(gfs):
    return await gfs.app_[gfs_space_epoch_repo_key].get_strict(SPACE_ID)


@pytest.mark.security
async def test_strict_a_valid_writer_sig_is_relayed_without_any_identity(gfs):
    await _go_strict(gfs)
    async with gfs.ws_connect("/gfs/ws") as sub_ws:
        await sub_ws.send_json(gfs.subscriber.hello())
        await _wait_connected(gfs.app_, gfs.subscriber.instance_id)
        resp = await gfs.post("/gfs/member-publish-anon", json=_anon())
        assert resp.status == 200
        assert await resp.json() == {"status": "published"}
        frame = await asyncio.wait_for(sub_ws.receive_json(), timeout=5)
    assert frame == {
        "type": "relay",
        "space_id": SPACE_ID,
        "event_type": SPACE_ITEM_EVENT_TYPE,
        "epoch": 3,
        "payload": CIPHERTEXT,
    }
    assert set(frame) == {"type"} | MEMBER_PUBLISH_ANON_FRAME_KEYS


async def test_strict_the_owner_notice_sets_mode_and_pins_the_key(gfs):
    await _go_strict(gfs)
    state = await _strict_state(gfs)
    assert state.strict
    assert state.writer_pk_for(3) == _wkc(3)["writer_pk"]


@pytest.mark.security
async def test_strict_a_wrong_epoch_key_is_refused(gfs):
    await _go_strict(gfs, 3)
    await _assert_refused(
        await gfs.post("/gfs/member-publish-anon", json=_anon(epoch=3, key_epoch=4))
    )


@pytest.mark.security
async def test_strict_a_missing_pin_is_refused(gfs):
    await _confirm(gfs, 3)  # v_49-shaped owner notice: no writer key pinned
    await _assert_refused(await gfs.post("/gfs/member-publish-anon", json=_anon()))


@pytest.mark.security
async def test_strict_a_key_from_another_space_seed_is_refused(gfs):
    await _go_strict(gfs, 3)
    await _assert_refused(
        await gfs.post("/gfs/member-publish-anon", json=_anon(seed=os.urandom(32)))
    )


@pytest.mark.security
async def test_strict_a_replay_is_refused(gfs):
    await _go_strict(gfs)
    body = _anon()
    assert (await gfs.post("/gfs/member-publish-anon", json=body)).status == 200
    await _assert_refused(await gfs.post("/gfs/member-publish-anon", json=body))
    assert len(await _queued(gfs, gfs.subscriber)) == 1


@pytest.mark.security
@pytest.mark.parametrize(
    "over",
    [
        {"gfs_instance_id": "gfs-node-b"},
        {"ts": "2020-01-01T00:00:00+00:00"},
        {"ts": "2026-10-03T10:00:00"},
        {"ts": "not-a-time"},
        {"suite": "ed25519+mldsa65"},
    ],
)
async def test_strict_stale_foreign_or_unknown_suite_is_refused(gfs, over):
    await _go_strict(gfs)
    await _assert_refused(
        await gfs.post("/gfs/member-publish-anon", json=_anon(**over))
    )


@pytest.mark.security
async def test_strict_a_tampered_signed_body_is_refused(gfs):
    await _go_strict(gfs)
    body = _anon()
    body["payload"] = "dGFtcGVyZWQ:Y3Q"
    await _assert_refused(await gfs.post("/gfs/member-publish-anon", json=body))


@pytest.mark.security
@pytest.mark.parametrize("field", ["instance_id", "signature", "writer_cert"])
async def test_strict_anything_identifying_is_a_400(gfs, field):
    await _go_strict(gfs)
    body = _anon()
    body[field] = "x"
    assert (await gfs.post("/gfs/member-publish-anon", json=body)).status == 400


@pytest.mark.security
async def test_strict_an_identified_publish_into_a_strict_space_is_refused(gfs):
    await _go_strict(gfs)
    await _assert_refused(
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    )
    assert await _queued(gfs, gfs.subscriber) == []


async def test_trusted_mode_takes_both_paths(gfs):
    await _go_strict(gfs, mode="trusted")
    assert not (await _strict_state(gfs)).strict
    assert (
        await gfs.post("/gfs/member-publish", json=_body(gfs.publisher))
    ).status == 200
    assert (await gfs.post("/gfs/member-publish-anon", json=_anon())).status == 200


async def test_strict_an_older_notice_never_moves_the_mode_back(gfs):
    await _go_strict(gfs, 3)
    older = _strict_notice(gfs, 3, mode="trusted", ts=_now_iso(-60))
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=older)).status == 200
    assert (await _strict_state(gfs)).strict


@pytest.mark.security
async def test_strict_a_mode_outside_the_owner_signature_is_refused(gfs):
    body = _strict_notice(gfs, 3, mode="strict", tamper_mode="trusted")
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)
    assert resp.status == 403
    # Nothing was written.
    assert await _state(gfs) is None


@pytest.mark.security
async def test_strict_an_unknown_mode_is_refused(gfs):
    body = _strict_notice(gfs, 3, mode="open")
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)).status == 403


@pytest.mark.security
async def test_strict_a_writer_key_cert_by_another_authority_is_refused(gfs):
    body = _strict_notice(gfs, 3, wkc=_wkc(3, seed=os.urandom(32)))
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)).status == 403
    assert await _state(gfs) is None


@pytest.mark.security
async def test_strict_a_writer_key_cert_for_another_epoch_is_refused(gfs):
    body = _strict_notice(gfs, 3, wkc=_wkc(4))
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)).status == 403


async def test_strict_a_stale_owner_notice_pins_nothing(gfs):
    await _go_strict(gfs, 5)
    stale = _strict_notice(gfs, 4)
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=stale)).status == 200
    state = await _strict_state(gfs)
    assert state.writer_pk_for(4) is None
    assert state.writer_key_epoch == 5


def _delegated_notice(epoch: int, *, wkc: dict | None = None) -> dict:
    return {
        **_notice(epoch),
        "writer_key_cert": wkc if wkc is not None else _wkc(epoch),
    }


@pytest.mark.security
async def test_strict_a_delegated_admin_pins_only_at_plus_one(gfs):
    await _go_strict(gfs, 3)
    # Beyond +1: neither the epoch nor the key moves.
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_delegated_notice(5))
    assert resp.status == 200
    assert (await _strict_state(gfs)).writer_pk_for(5) is None
    await _assert_refused(
        await gfs.post("/gfs/member-publish-anon", json=_anon(epoch=5))
    )
    # Exactly +1 under the step rule: pinned, and publishable.
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + 120
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_delegated_notice(4))
    assert resp.status == 200
    state = await _strict_state(gfs)
    assert state.writer_pk_for(4) == _wkc(4)["writer_pk"]
    assert state.writer_pk_for(3) == _wkc(3)["writer_pk"]


@pytest.mark.security
async def test_strict_a_delegated_admin_cannot_replace_the_owners_pin(gfs):
    await _go_strict(gfs, 3)
    other = sign_writer_key_cert(
        space_seed=SPACE_SEED,
        space_id=SPACE_ID,
        epoch=3,
        writer_pk=ed25519_public_key(os.urandom(32)),
    ).to_wire()
    resp = await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_delegated_notice(3, wkc=other)
    )
    assert resp.status == 200
    assert (await _strict_state(gfs)).writer_pk_for(3) == _wkc(3)["writer_pk"]


@pytest.mark.security
async def test_strict_a_delegated_pin_needs_an_owner_confirmed_epoch(gfs):
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_delegated_notice(1))
    assert resp.status == 200
    assert (await _strict_state(gfs)).writer_pk_for(1) is None


@pytest.mark.security
async def test_strict_a_delegated_writer_key_cert_by_another_authority_is_refused(gfs):
    await _go_strict(gfs, 3)
    body = _delegated_notice(4, wkc=_wkc(4, seed=os.urandom(32)))
    assert (await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=body)).status == 403


async def test_strict_the_owner_replaces_a_delegated_pin(gfs):
    await _go_strict(gfs, 3)
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + 120
    rogue = sign_writer_key_cert(
        space_seed=SPACE_SEED,
        space_id=SPACE_ID,
        epoch=4,
        writer_pk=ed25519_public_key(os.urandom(32)),
    ).to_wire()
    await gfs.post(
        f"/gfs/spaces/{SPACE_ID}/epoch", json=_delegated_notice(4, wkc=rogue)
    )
    assert (await _strict_state(gfs)).writer_pk_for(4) == rogue["writer_pk"]
    await _go_strict(gfs, 4)
    assert (await _strict_state(gfs)).writer_pk_for(4) == _wkc(4)["writer_pk"]


async def test_strict_the_previous_key_rides_the_grace_then_retires(gfs):
    await _go_strict(gfs, 3)
    await _go_strict(gfs, 4)
    assert (
        await gfs.post("/gfs/member-publish-anon", json=_anon(epoch=3))
    ).status == 200
    svc = gfs.app_[gfs_member_publish_key]
    svc._clock = lambda: time.time() + MEMBER_PUBLISH_EPOCH_GRACE_S + 5
    await _assert_refused(
        await gfs.post(
            "/gfs/member-publish-anon",
            json=_anon(epoch=3, ts=_now_iso(MEMBER_PUBLISH_EPOCH_GRACE_S + 5)),
        )
    )


async def test_strict_keys_are_forgotten_on_an_authority_repin(gfs):
    await _go_strict(gfs, 3)
    fed = gfs.app_[gfs_fed_repo_key]
    assert await fed.set_space_authority(
        SPACE_ID,
        expected_pk=SPACE_PK.hex(),
        expected_cert=None,
        new_pk="bb" * 32,
        cert={"key_epoch": 1},
    )
    await _assert_refused(await gfs.post("/gfs/member-publish-anon", json=_anon()))


@pytest.mark.security
async def test_strict_the_per_writer_key_limit_answers_429(gfs, monkeypatch):
    await _go_strict(gfs)
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setattr(svc._writer_key_limiter, "_limit", 1, raising=False)
    assert (await gfs.post("/gfs/member-publish-anon", json=_anon())).status == 200
    resp = await gfs.post("/gfs/member-publish-anon", json=_anon(payload="b3RoZXI:Y3Q"))
    assert resp.status == 429


async def test_strict_a_banned_or_unreadable_space_is_refused(gfs):
    await _go_strict(gfs)
    fed = gfs.app_[gfs_fed_repo_key]
    await fed.set_space_status(SPACE_ID, "banned")
    await _assert_refused(await gfs.post("/gfs/member-publish-anon", json=_anon()))


def test_the_anon_route_is_ip_limited():
    assert _is_member_publish_path("/gfs/member-publish-anon")


async def test_the_info_block_advertises_member_publish_strict(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    assert body["capabilities"]["member_publish_strict"] is True


# ── X1: one writer-key holder must not starve anonymous publishing ───────


@pytest.mark.security
async def test_strict_one_key_holder_cannot_starve_the_space(gfs):
    """Every publish-capable household shares the writer key, so the per-key
    and per-space limits can't tell an abuser from the rest. A per-(space,
    client IP) limit stops one household's flood long before the space-wide
    budget, so writers from other addresses keep publishing."""
    await _go_strict(gfs)
    svc = gfs.app_[gfs_member_publish_key]
    for i in range(mp_mod.MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP):
        await svc.publish_anon(
            MemberPublishAnonRequest.from_wire(_anon(payload=f"Z2FyYmFnZS0{i}")),
            client_ip="203.0.113.7",
        )
    with pytest.raises(mp_mod.MemberPublishRateLimited):
        await svc.publish_anon(
            MemberPublishAnonRequest.from_wire(_anon(payload="b25lLW1vcmU")),
            client_ip="203.0.113.7",
        )
    # A legitimate writer elsewhere is unaffected.
    await svc.publish_anon(
        MemberPublishAnonRequest.from_wire(_anon(payload="bGVnaXQtcG9zdA")),
        client_ip="198.51.100.9",
    )
    assert (
        mp_mod.MEMBER_PUBLISH_ANON_MAX_PER_MINUTE_PER_SPACE_IP
        < mp_mod.MEMBER_PUBLISH_MAX_PER_MINUTE_PER_SPACE
    )


async def test_strict_the_route_applies_the_per_address_limit(gfs, monkeypatch):
    await _go_strict(gfs)
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setattr(svc._anon_ip_limiter, "_limit", 1, raising=False)
    assert (await gfs.post("/gfs/member-publish-anon", json=_anon())).status == 200
    resp = await gfs.post("/gfs/member-publish-anon", json=_anon(payload="b3RoZXI:Y3Q"))
    assert resp.status == 429


async def test_strict_a_refused_signature_costs_no_address_budget(gfs, monkeypatch):
    await _go_strict(gfs)
    svc = gfs.app_[gfs_member_publish_key]
    monkeypatch.setattr(svc._anon_ip_limiter, "_limit", 1, raising=False)
    await _assert_refused(
        await gfs.post("/gfs/member-publish-anon", json=_anon(key_epoch=9))
    )
    assert (await gfs.post("/gfs/member-publish-anon", json=_anon())).status == 200


async def test_the_public_listing_carries_the_publish_mode(gfs):
    """v_50: households read the mode off the directory they already fetch
    (cookie-less) and never send an identified publish into a strict space."""
    listing = await (await gfs.get("/gfs/spaces")).json()
    row = next(sp for sp in listing["spaces"] if sp["space_id"] == SPACE_ID)
    assert row["member_publish_mode"] == "trusted"
    await _go_strict(gfs)
    listing = await (await gfs.get("/gfs/spaces")).json()
    row = next(sp for sp in listing["spaces"] if sp["space_id"] == SPACE_ID)
    assert row["member_publish_mode"] == "strict"
    detail = await (await gfs.get(f"/gfs/spaces/{SPACE_ID}")).json()
    assert detail["member_publish_mode"] == "strict"
    # The writer keys never reach the public directory.
    assert "writer_key" not in json.dumps(listing)
