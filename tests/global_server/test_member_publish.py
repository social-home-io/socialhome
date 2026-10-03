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
from dataclasses import replace
from datetime import datetime, timedelta, timezone

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
    MEMBER_PUBLISH_EPOCH_GRACE_S,
    SPACE_ITEM_EVENT_TYPE,
    MemberPublishRequest,
)
from socialhome.global_server import member_publish as mp_mod
from socialhome.global_server.app_keys import (
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
from socialhome.global_server.server import create_gfs_app
from socialhome.writer_cert import sign_writer_cert

SPACE_ID = "sp-public"
OTHER_SPACE = "sp-other"
CIPHERTEXT = "bm9uY2U:c3BhY2UtaXRlbS1jaXBoZXJ0ZXh0"


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
            inbox_url=f"http://{self.instance_id}.home/wh",
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
) -> dict:
    req = MemberPublishRequest(
        instance_id=publisher.instance_id,
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
        yield tc


async def _wait_connected(app, instance_id: str) -> None:
    registry = app[gfs_ws_registry_key]
    for _ in range(200):
        if registry.is_connected(instance_id):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{instance_id} never connected")


async def _queued(gfs, household: _Household) -> list:
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


async def test_a_cert_learns_the_space_epoch(gfs):
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=4))
    assert resp.status == 200
    state = await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID)
    assert state.current == 4


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


# ── Refusals: epoch freshness ────────────────────────────────────────────


@pytest.mark.security
async def test_a_stale_epoch_is_refused_after_the_grace(gfs):
    epochs = gfs.app_[gfs_space_epoch_repo_key]
    old = int(datetime.now(timezone.utc).timestamp()) - MEMBER_PUBLISH_EPOCH_GRACE_S - 5
    await epochs.advance(SPACE_ID, 3, seen_at=old - 10)
    await epochs.advance(SPACE_ID, 4, seen_at=old)

    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
    await _assert_refused(resp)


async def test_the_previous_epoch_is_accepted_within_the_grace(gfs):
    epochs = gfs.app_[gfs_space_epoch_repo_key]
    now = int(datetime.now(timezone.utc).timestamp())
    await epochs.advance(SPACE_ID, 3, seen_at=now - 100)
    await epochs.advance(SPACE_ID, 4, seen_at=now - 10)

    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
    assert resp.status == 200
    assert (await epochs.get(SPACE_ID)).current == 4


@pytest.mark.security
async def test_an_epoch_older_than_the_previous_is_refused(gfs):
    epochs = gfs.app_[gfs_space_epoch_repo_key]
    now = int(datetime.now(timezone.utc).timestamp())
    await epochs.advance(SPACE_ID, 4, seen_at=now)
    await epochs.advance(SPACE_ID, 5, seen_at=now)

    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=3))
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


async def test_an_epoch_notice_advances_the_epoch_and_shuts_out_old_certs(gfs):
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(9))
    assert resp.status == 200
    assert await resp.json() == {"status": "ok"}
    state = await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID)
    assert state.current == 9
    # A cert two epochs behind the notice is older than ``previous`` (None) —
    # refused at once.
    resp = await gfs.post("/gfs/member-publish", json=_body(gfs.publisher, epoch=7))
    await _assert_refused(resp)


async def test_a_replayed_older_notice_never_rolls_the_epoch_back(gfs):
    await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(9))
    resp = await gfs.post(f"/gfs/spaces/{SPACE_ID}/epoch", json=_notice(2))
    assert resp.status == 200
    assert (await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID)).current == 9


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


async def test_an_authority_post_relay_teaches_the_epoch(gfs):
    envelope = {"space_id": SPACE_ID, "epoch": 6, "encrypted_payload": CIPHERTEXT}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=SPACE_SEED,
        )
    )
    await gfs.app_[gfs_federation_key].publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, envelope
    )
    assert (await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID)).current == 6


async def test_a_malformed_relay_epoch_teaches_nothing(gfs):
    envelope = {"space_id": SPACE_ID, "epoch": "6", "encrypted_payload": CIPHERTEXT}
    envelope.update(
        sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=SPACE_ID,
            payload=strip_authority_sig_fields(envelope),
            space_seed=SPACE_SEED,
        )
    )
    await gfs.app_[gfs_federation_key].publish_event(
        SPACE_ID, AUTHORITY_EVENT_SPACE_POST_PUBLIC, envelope
    )
    assert await gfs.app_[gfs_space_epoch_repo_key].get(SPACE_ID) is None


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
