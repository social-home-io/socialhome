"""``POST /gfs/envelope/multi`` (v_90): ONE sealed frame for many mailboxes,
its body stored once (GFS migration 0031) and pushed byte-identical to every
registered mailbox's claim socket. Real GFS app, real SQLite, real sockets."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import ed25519_public_key
from socialhome.domain.gfs_mailbox import (
    ENVELOPE_MULTI_CAPABILITY,
    ENVELOPE_MULTI_PATH,
    MULTI_MAX_RECIPIENTS,
    MULTI_SEALED_KEYS,
)
from socialhome.federation.gfs_mailbox import (
    MailboxClaim,
    derive_mailbox_keys,
    register_mailbox,
)
from socialhome.global_server.app_keys import (
    gfs_db_key,
    gfs_envelope_queue_repo_key,
    gfs_envelope_relay_key,
    gfs_fed_repo_key,
    gfs_mailbox_repo_key,
)
from socialhome.global_server.config import EnvelopeMailboxConfig
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import (
    ENVELOPE_MULTI_MAX_BODY_BYTES,
    ENVELOPE_MULTI_MAX_CIPHERTEXT_CHARS,
    ENVELOPE_MULTI_MAX_PER_MINUTE,
    InvalidEnvelope,
    validate_multi_envelope,
)
from socialhome.global_server.repositories import (
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsMailboxRepo,
)
from socialhome.global_server.server import create_gfs_app
from tests.federation.test_gfs_mailbox import _ticket_cache
from tests.global_server.test_mailbox_cluster import GFS_ID as CLUSTER_GFS_ID
from tests.global_server.test_mailbox_cluster import _url as _cluster_url
from tests.global_server.test_mailbox_cluster import _wait as _cluster_wait
from tests.global_server.test_mailbox_cluster import nodes  # noqa: F401 (fixture)
from tests.global_server.test_mailboxes import (
    GFS_ID,
    INSTANCE,
    Box,
    _cfg,
    _claim,
    _register,
    _settle,
)

MR = {"mr_suite": "mr-aesgcm-256", "kid": "a2lkLWtpZC1raWQta2lk", "ciphertext": "Y3Q"}
MR_JSON_LEN = len(json.dumps(MR, separators=(",", ":")))


@pytest.fixture
async def gfs(tmp_dir):
    app = create_gfs_app(_cfg(tmp_dir))
    async with TestClient(TestServer(app)) as tc:
        await app[gfs_fed_repo_key].upsert_instance(
            ClientInstance(
                instance_id=INSTANCE,
                display_name="Recipient",
                public_key=ed25519_public_key(b"\x07" * 32).hex(),
                status="active",
            )
        )
        tc._app = app
        yield tc


async def _bodies(app) -> int:
    row = await app[gfs_db_key].fetchone(
        "SELECT COUNT(*) AS n FROM gfs_envelope_bodies"
    )
    return int(dict(row)["n"])


async def _multi(gfs, ids, sealed=None):
    return await gfs.post(
        ENVELOPE_MULTI_PATH,
        json={"to_mailboxes": list(ids), "sealed": sealed or MR},
    )


# ── Shape ────────────────────────────────────────────────────────────────


def test_constants():
    assert ENVELOPE_MULTI_CAPABILITY == "envelope_multi"
    assert ENVELOPE_MULTI_PATH == "/gfs/envelope/multi"
    assert MULTI_MAX_RECIPIENTS == 500
    assert MULTI_SEALED_KEYS == frozenset({"mr_suite", "kid", "ciphertext"})
    assert ENVELOPE_MULTI_MAX_PER_MINUTE == 60


def test_validate_multi_envelope_accepts_the_wire_shape():
    ids, sealed = validate_multi_envelope(
        {"to_mailboxes": ["a" * 32, "b" * 32], "sealed": MR}
    )
    assert ids == ["a" * 32, "b" * 32]
    assert sealed == MR


_BAD = [
    [],
    {"to_mailboxes": ["a" * 32]},
    {"sealed": MR},
    {"to_mailboxes": [], "sealed": MR},
    {"to_mailboxes": "a" * 32, "sealed": MR},
    {"to_mailboxes": ["a" * 31], "sealed": MR},
    {"to_mailboxes": ["A" * 32], "sealed": MR},
    {"to_mailboxes": ["a" * 32 + "\n"], "sealed": MR},
    {"to_mailboxes": [7], "sealed": MR},
    {"to_mailboxes": ["a" * 32] * (MULTI_MAX_RECIPIENTS + 1), "sealed": MR},
    {"to_mailboxes": ["a" * 32], "sealed": MR, "extra": 1},
    {"to_mailboxes": ["a" * 32], "sealed": {**MR, "to": "x"}},
    {"to_mailboxes": ["a" * 32], "sealed": {"kid": "k", "ciphertext": "c"}},
    {"to_mailboxes": ["a" * 32], "sealed": {**MR, "kid": ""}},
    {"to_mailboxes": ["a" * 32], "sealed": {**MR, "mr_suite": 1}},
    {"to_mailboxes": ["a" * 32], "sealed": "opaque"},
    {
        "to_mailboxes": ["a" * 32],
        "sealed": {**MR, "ciphertext": "c" * (ENVELOPE_MULTI_MAX_CIPHERTEXT_CHARS + 1)},
    },
]


@pytest.mark.parametrize("body", _BAD)
def test_validate_multi_envelope_refuses(body):
    with pytest.raises(InvalidEnvelope):
        validate_multi_envelope(body)


@pytest.mark.parametrize("body", _BAD)
async def test_route_answers_400_for_a_malformed_body(gfs, body):
    resp = await gfs.post(ENVELOPE_MULTI_PATH, json=body)
    assert resp.status == 400


async def test_route_answers_400_for_bad_json(gfs):
    resp = await gfs.post(ENVELOPE_MULTI_PATH, data=b"{nope")
    assert resp.status == 400


async def test_route_answers_413_over_the_body_cap(gfs):
    resp = await gfs.post(
        ENVELOPE_MULTI_PATH, data=b"x" * (ENVELOPE_MULTI_MAX_BODY_BYTES + 1)
    )
    assert resp.status == 413


async def test_mailboxes_off_means_no_route_and_no_capability(tmp_dir):
    app = create_gfs_app(
        _cfg(tmp_dir, envelope_mailbox=EnvelopeMailboxConfig(enabled=False))
    )
    async with TestClient(TestServer(app)) as tc:
        assert (await _multi(tc, ["a" * 32])).status == 404
        info = await (await tc.get("/gfs/info")).json()
        assert ENVELOPE_MULTI_CAPABILITY not in info["capabilities"]


# ── Uniform answer, delivery only to registered mailboxes ────────────────


@pytest.mark.security
async def test_uniform_202_and_only_registered_mailboxes_get_a_row(gfs):
    box = Box()
    assert (await _register(gfs, box)).status == 200
    answers = []
    for ids in ([box.id], ["q" * 32], [INSTANCE], [box.id, "q" * 32, INSTANCE]):
        resp = await _multi(gfs, ids)
        answers.append((resp.status, await resp.read()))
    assert len(set(answers)) == 1
    assert answers[0][0] == 202
    assert json.loads(answers[0][1]) == {"status": "accepted"}
    await _settle(gfs._app)
    queue = gfs._app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(box.id) == 2
    assert await queue.count_for(INSTANCE) == 0
    assert await queue.count_for("q" * 32) == 0
    # One body per accepted upload that reached a mailbox, not per recipient.
    assert await _bodies(gfs._app) == 2


@pytest.mark.security
async def test_an_id_held_by_a_mailbox_and_an_instance_is_skipped(gfs):
    box = Box()
    await _register(gfs, box)
    await gfs._app[gfs_fed_repo_key].upsert_instance(
        ClientInstance(
            instance_id=box.id,
            display_name="Shadow",
            public_key=ed25519_public_key(b"\x09" * 32).hex(),
            status="active",
        )
    )
    assert (await _multi(gfs, [box.id])).status == 202
    await _settle(gfs._app)
    assert await gfs._app[gfs_envelope_queue_repo_key].count_for(box.id) == 0
    assert await _bodies(gfs._app) == 0


async def test_one_body_many_refs_identical_bytes_and_freed_after_delivery(gfs):
    boxes = [Box() for _ in range(3)]
    for box in boxes:
        await _register(gfs, box)
    assert (await _multi(gfs, [b.id for b in boxes] + [boxes[0].id])).status == 202
    await _settle(gfs._app)
    app = gfs._app
    queue = app[gfs_envelope_queue_repo_key]
    assert await _bodies(app) == 1
    for box in boxes:
        assert await queue.count_for(box.id) == 1  # deduplicated
    assert await queue.pool_bytes("envelope", "mailbox") == MR_JSON_LEN

    received = []
    for box in boxes:
        ws = await _claim(gfs, box)
        assert (await ws.receive_json(timeout=2)) == {"type": "mailbox_claimed"}
        msg = await ws.receive(timeout=2)
        received.append(msg.data)
        await ws.close()
        # Freed only after the LAST reference went.
        expected = 0 if box is boxes[-1] else 1
        for _ in range(200):
            if await _bodies(app) == expected:
                break
            await asyncio.sleep(0.01)
        assert await _bodies(app) == expected
    assert len(set(received)) == 1
    assert json.loads(received[0]) == {"type": "envelope", "sealed": MR}
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_live_claim_sockets_receive_the_frame_at_once(gfs):
    boxes = [Box() for _ in range(2)]
    sockets = []
    for box in boxes:
        await _register(gfs, box)
        ws = await _claim(gfs, box)
        assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
        sockets.append(ws)
    assert (await _multi(gfs, [b.id for b in boxes])).status == 202
    frames = [(await ws.receive(timeout=2)).data for ws in sockets]
    assert frames[0] == frames[1]
    assert json.loads(frames[0]) == {"type": "envelope", "sealed": MR}
    await _settle(gfs._app)
    for _ in range(200):
        if await _bodies(gfs._app) == 0:
            break
        await asyncio.sleep(0.01)
    assert await _bodies(gfs._app) == 0
    for ws in sockets:
        await ws.close()


async def test_no_log_record_names_a_mailbox(gfs, caplog):
    boxes = [Box() for _ in range(2)]
    for box in boxes:
        await _register(gfs, box)
    caplog.set_level(logging.DEBUG)
    assert (await _multi(gfs, [b.id for b in boxes] + ["q" * 32])).status == 202
    await _settle(gfs._app)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "multi frame queued for 2 of 3 mailboxes" in text
    for box in boxes:
        assert box.id not in text
    assert "q" * 32 not in text


async def test_a_multi_upload_takes_one_inflight_slot(gfs):
    relay = gfs._app[gfs_envelope_relay_key]
    relay.submit_multi([Box().id for _ in range(5)], MR)
    assert relay.in_flight == 1
    await _settle(gfs._app)


async def test_rate_limit_429_after_the_per_minute_budget(gfs):
    statuses = []
    for _ in range(ENVELOPE_MULTI_MAX_PER_MINUTE + 1):
        resp = await _multi(gfs, ["q" * 32])
        statuses.append(resp.status)
    assert statuses[:ENVELOPE_MULTI_MAX_PER_MINUTE] == [202] * (
        ENVELOPE_MULTI_MAX_PER_MINUTE
    )
    assert statuses[-1] == 429
    # The single-recipient route keeps its own budget.
    one = await gfs.post(
        "/gfs/envelope",
        json={
            "to_instance": "q" * 32,
            "sealed": {"kem_suite": "x", "eph_pk": "y", "ciphertext": "z"},
        },
    )
    assert one.status == 202
    await _settle(gfs._app)


async def test_gfs_info_signs_envelope_multi(gfs):
    body = await (await gfs.get("/gfs/info")).json()
    assert body["capabilities"][ENVELOPE_MULTI_CAPABILITY] == {
        "max_recipients": MULTI_MAX_RECIPIENTS
    }
    assert verify_capabilities(
        body["public_key"],
        body["gfs_instance_id"],
        body["capabilities"],
        body["capabilities_sig"],
        body["capabilities_sig_suite"],
    )
    assert GFS_ID == body["gfs_instance_id"]


# ── Repository: body stored once, caps charge the full body ──────────────

A, B, C = "a" * 32, "b" * 32, "c" * 32
SEALED_JSON = '{"mr_suite":"s","kid":"k","ciphertext":"' + "x" * 960 + '"}'
SIZE = len(SEALED_JSON)


def _caps(**kw) -> dict:
    caps = {
        "created_at": 100,
        "expires_at": 1000,
        "max_per_recipient": 10,
        "max_bytes_per_recipient": 10**6,
        "max_total_bytes": 10**7,
        "ceiling_bytes": 2 * 10**7,
    }
    caps.update(kw)
    return caps


@pytest.fixture
async def queue(gfs_db):
    return SqliteGfsEnvelopeQueueRepo(gfs_db)


async def _count(db, table: str) -> int:
    row = await db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
    return int(dict(row)["n"])


async def test_enqueue_multi_stores_one_body_and_a_ref_per_recipient(queue, gfs_db):
    admitted = await queue.enqueue_multi([A, B, C], SEALED_JSON, **_caps())
    assert admitted == [A, B, C]
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    assert await _count(gfs_db, "gfs_envelope_queue") == 3
    row = await gfs_db.fetchone(
        "SELECT COALESCE(SUM(LENGTH(sealed_json)), 0) AS n FROM gfs_envelope_queue"
    )
    assert dict(row)["n"] == 0  # the refs hold no copy
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE
    for to in (A, B, C):
        (item,) = await queue.list_for(to, now=100)
        assert item.sealed == json.loads(SEALED_JSON)
        assert item.frame_type == "envelope"
    for to in (A, B):
        (item,) = await queue.list_for(to, now=100)
        await queue.delete(item.id)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    (item,) = await queue.list_for(C, now=100)
    await queue.delete(item.id)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_a_capped_recipient_is_skipped_and_charged_the_full_body(queue, gfs_db):
    caps = _caps(max_per_recipient=3, max_bytes_per_recipient=2 * SIZE + 10)
    # B is at its row cap; C lacks the room for one more body.
    for _ in range(3):
        assert await queue.enqueue(B, "{}", mailbox_pool=True, **caps)
    assert await queue.enqueue(C, "x" * (SIZE + 11), mailbox_pool=True, **caps)
    assert await queue.enqueue_multi([A, B, C], SEALED_JSON, **caps) == [A]
    assert await queue.enqueue_multi([A], SEALED_JSON, **caps) == [A]
    # A's two references charge two full bodies: 11 more bytes do not fit,
    # although the reference rows hold no copy of the body.
    assert not await queue.enqueue(A, "y" * 11, mailbox_pool=True, **caps)
    assert await queue.enqueue(A, "y" * 10, mailbox_pool=True, **caps)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 2


async def test_nothing_admitted_stores_no_body(queue, gfs_db):
    caps = _caps(max_bytes_per_recipient=SIZE - 1)
    assert await queue.enqueue_multi([A, B], SEALED_JSON, **caps) == []
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_a_full_pool_admits_only_recipients_under_their_reserve(queue, gfs_db):
    roomy = _caps(max_per_recipient=100, max_total_bytes=10**9)
    # B holds more than the reserve's 20 rows, filling the pool.
    for _ in range(21):
        assert await queue.enqueue(B, SEALED_JSON, mailbox_pool=True, **roomy)
    full = _caps(
        max_per_recipient=100, max_total_bytes=SIZE * 21, ceiling_bytes=SIZE * 40
    )
    assert await queue.enqueue_multi([A, B], SEALED_JSON, **full) == [A]
    # Past the ceiling nobody gets in.
    tight = _caps(max_total_bytes=SIZE, ceiling_bytes=SIZE)
    assert await queue.enqueue_multi([C], SEALED_JSON, **tight) == []


async def test_pool_admits_everyone_under_the_total(queue):
    caps = _caps(max_total_bytes=SIZE * 2)
    assert await queue.enqueue_multi([A, B, C], SEALED_JSON, **caps) == [A, B, C]
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE


async def test_prune_expired_frees_refs_and_bodies(queue, gfs_db):
    await queue.enqueue_multi([A, B], SEALED_JSON, **_caps())
    assert await queue.list_for(A, now=1000) == []
    await queue.prune_expired(1000)
    assert await _count(gfs_db, "gfs_envelope_queue") == 0
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_unregister_and_idle_sweep_free_the_body(queue, gfs_db):
    mailboxes = SqliteGfsMailboxRepo(gfs_db)
    ids = []
    for n in range(2):
        seed = bytes([n + 1]) * 32
        from_box = Box(seed)
        await mailboxes.register(
            from_box.id,
            from_box.pk,
            now=100,
            ticket_nonce=(n + 1).to_bytes(32, "big"),
            ticket_expires_at=10**10,
        )
        ids.append(from_box.id)
    await queue.enqueue_multi(ids, SEALED_JSON, **_caps(expires_at=10**9))
    assert await mailboxes.unregister(ids[0])
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    assert await mailboxes.prune_idle(older_than=10**8) == 1
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_recompute_pool_totals_agrees_with_the_triggers(queue, gfs_db):
    await queue.enqueue(A, "{}", mailbox_pool=True, **_caps())
    await queue.enqueue(A, '{"h":1}', **_caps())
    await queue.enqueue_multi([A, B, C], SEALED_JSON, **_caps())
    before = (
        await queue.pool_bytes("envelope", "mailbox"),
        await queue.pool_bytes("envelope", "household"),
    )
    assert before == (2 + SIZE, 7)
    await gfs_db.enqueue("UPDATE gfs_queue_totals SET bytes = 12345")
    await queue.recompute_pool_totals()
    after = (
        await queue.pool_bytes("envelope", "mailbox"),
        await queue.pool_bytes("envelope", "household"),
    )
    assert after == before


async def test_mailbox_repo_filters_to_registered_mailboxes(gfs):
    repo = gfs._app[gfs_mailbox_repo_key]
    box = Box()
    await _register(gfs, box)
    got = await repo.mailboxes_among([box.id, "q" * 32, INSTANCE])
    assert got == {box.id}
    assert await repo.mailboxes_among([]) == set()


# ── Cluster: queued on node A, drained by a claim on node B ──────────────


async def test_multi_posted_on_a_reaches_claims_on_b(nodes):  # noqa: F811
    a, b, session = nodes
    live = derive_mailbox_keys(b"\x0a" * 32, CLUSTER_GFS_ID, 900001)
    later = derive_mailbox_keys(b"\x0b" * 32, CLUSTER_GFS_ID, 900002)
    tickets = await _ticket_cache(a.app, session, _cluster_url(a), batch=2)
    for keys in (live, later):
        await register_mailbox(
            session, _cluster_url(a), keys, CLUSTER_GFS_ID, tickets=tickets
        )
    body = {"to_mailboxes": [live.mailbox_id, later.mailbox_id], "sealed": MR}
    async with MailboxClaim(session, _cluster_url(b), live, CLUSTER_GFS_ID) as claim:
        async with session.post(
            _cluster_url(a) + ENVELOPE_MULTI_PATH, json=body
        ) as resp:
            assert resp.status == 202
        assert await claim.receive(timeout=5) == {"type": "envelope", "sealed": MR}
    queue = a.app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(later.mailbox_id) == 1
    assert await _bodies(a.app) == 1
    async with MailboxClaim(session, _cluster_url(b), later, CLUSTER_GFS_ID) as claim:
        assert await claim.receive(timeout=5) == {"type": "envelope", "sealed": MR}

    async def _freed() -> bool:
        return await _bodies(a.app) == 0

    await _cluster_wait(_freed)
    assert await queue.pool_bytes("envelope", "mailbox") == 0
