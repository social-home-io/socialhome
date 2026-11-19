"""``POST /gfs/envelope/multi`` (v_91): ONE sealed body for many mailboxes,
each with its own ``wrap``. The body is stored once (GFS migration 0031),
each wrap in its mailbox's reference row, and every registered mailbox's
claim socket gets the SAME body with ITS OWN wrap. Real GFS app, real
SQLite, real sockets."""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import pytest
from aiohttp.test_utils import TestClient, TestServer

from socialhome.capabilities_sig import verify_capabilities
from socialhome.crypto import ed25519_public_key
from socialhome.domain.gfs_mailbox import (
    ENVELOPE_MULTI_CAPABILITY,
    ENVELOPE_MULTI_PATH,
    MULTI_BODY_KEYS,
    MULTI_ENTRY_KEYS,
    MULTI_MAX_RECIPIENTS,
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
    ENVELOPE_MAX_PER_MINUTE,
    ENVELOPE_MULTI_MAX_BODY_BYTES,
    ENVELOPE_MULTI_MAX_CIPHERTEXT_CHARS,
    ENVELOPE_MULTI_MAX_FIELD_CHARS,
    ENVELOPE_MULTI_MAX_PER_MINUTE,
    ENVELOPE_MULTI_MAX_WRAP_CIPHERTEXT_CHARS,
    ENVELOPE_MULTI_MAX_WRAP_EPH_PK_CHARS,
    ENVELOPE_MULTI_RECIPIENTS_PER_MINUTE,
    InvalidEnvelope,
    validate_multi_envelope,
)
from socialhome.global_server.repositories import (
    REF_ROW_BYTES,
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

BODY = {"mr_suite": "mr-wrap-aesgcm-256", "ciphertext": "Ym9keS1jdA"}
BODY_JSON_LEN = len(json.dumps(BODY, separators=(",", ":")))


def _wrap(mailbox_id: str) -> dict[str, str]:
    """A distinct, well-formed wrap per mailbox (opaque to the GFS)."""
    return {
        "kem_suite": "x25519-hkdf-sha256-aesgcm",
        "eph_pk": "ZXBo" + mailbox_id[:8],
        "ciphertext": "d3JhcA" + mailbox_id,
    }


def _wrap_len(mailbox_id: str) -> int:
    return len(json.dumps(_wrap(mailbox_id), separators=(",", ":")))


def _entries(ids) -> list[dict]:
    return [{"mailbox": i, "wrap": _wrap(i)} for i in ids]


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


async def _multi(gfs, ids, body=None):
    return await gfs.post(
        ENVELOPE_MULTI_PATH,
        json={"to": _entries(ids), "body": body or BODY},
    )


def _expected(mailbox_id: str) -> dict:
    return {"type": "envelope", "sealed": {**BODY, "wrap": _wrap(mailbox_id)}}


# ── Shape ────────────────────────────────────────────────────────────────


def test_constants():
    assert ENVELOPE_MULTI_CAPABILITY == "envelope_multi"
    assert ENVELOPE_MULTI_PATH == "/gfs/envelope/multi"
    assert MULTI_MAX_RECIPIENTS == 500
    assert MULTI_ENTRY_KEYS == frozenset({"mailbox", "wrap"})
    assert MULTI_BODY_KEYS == frozenset({"mr_suite", "ciphertext"})
    assert ENVELOPE_MULTI_MAX_PER_MINUTE == 60
    assert ENVELOPE_MULTI_MAX_WRAP_CIPHERTEXT_CHARS == 256


def test_validate_multi_envelope_accepts_the_wire_shape():
    entries, body = validate_multi_envelope(
        {"to": _entries(["a" * 32, "b" * 32]), "body": BODY}
    )
    assert entries == [("a" * 32, _wrap("a" * 32)), ("b" * 32, _wrap("b" * 32))]
    assert body == BODY


def test_a_wrap_at_its_bounds_is_taken():
    """A wrap's suite tag and ``eph_pk`` are bounded, with room for a
    hybrid post-quantum suite (an X25519 + ML-KEM-768 ``eph_pk`` is ~1.5 k
    b64url chars)."""
    wrap = {
        **_wrap("a" * 32),
        "kem_suite": "k" * ENVELOPE_MULTI_MAX_FIELD_CHARS,
        "eph_pk": "e" * ENVELOPE_MULTI_MAX_WRAP_EPH_PK_CHARS,
    }
    [(_mailbox, got)], _body = validate_multi_envelope(
        {"to": [{"mailbox": "a" * 32, "wrap": wrap}], "body": BODY}
    )
    assert got == wrap


def test_a_repeated_mailbox_keeps_its_first_entry():
    other = {**_wrap("a" * 32), "ciphertext": "second"}
    entries, _body = validate_multi_envelope(
        {
            "to": [
                {"mailbox": "a" * 32, "wrap": _wrap("a" * 32)},
                {"mailbox": "b" * 32, "wrap": _wrap("b" * 32)},
                {"mailbox": "a" * 32, "wrap": other},
            ],
            "body": BODY,
        }
    )
    assert entries == [("a" * 32, _wrap("a" * 32)), ("b" * 32, _wrap("b" * 32))]


A_ENTRY = {"mailbox": "a" * 32, "wrap": _wrap("a" * 32)}
W = _wrap("a" * 32)

_BAD = [
    [],
    {"to": [A_ENTRY]},
    {"body": BODY},
    {"to": [], "body": BODY},
    {"to": A_ENTRY, "body": BODY},
    {"to": [A_ENTRY], "body": BODY, "extra": 1},
    # The previous v_91 shape is gone.
    {"to_mailboxes": ["a" * 32], "sealed": BODY},
    # Entries.
    {"to": ["a" * 32], "body": BODY},
    {"to": [{"mailbox": "a" * 32}], "body": BODY},
    {"to": [{**A_ENTRY, "extra": 1}], "body": BODY},
    {"to": [{"mailbox": "a" * 31, "wrap": W}], "body": BODY},
    {"to": [{"mailbox": "A" * 32, "wrap": W}], "body": BODY},
    {"to": [{"mailbox": "a" * 32 + "\n", "wrap": W}], "body": BODY},
    {"to": [{"mailbox": 7, "wrap": W}], "body": BODY},
    {"to": [A_ENTRY] * (MULTI_MAX_RECIPIENTS + 1), "body": BODY},
    # Wraps: exactly the single envelope's sealed dict, bounded ciphertext.
    {"to": [{"mailbox": "a" * 32, "wrap": "opaque"}], "body": BODY},
    {"to": [{"mailbox": "a" * 32, "wrap": {**W, "to": "x"}}], "body": BODY},
    {
        "to": [{"mailbox": "a" * 32, "wrap": {"eph_pk": "e", "ciphertext": "c"}}],
        "body": BODY,
    },
    {"to": [{"mailbox": "a" * 32, "wrap": {**W, "eph_pk": ""}}], "body": BODY},
    {"to": [{"mailbox": "a" * 32, "wrap": {**W, "kem_suite": 1}}], "body": BODY},
    {
        "to": [
            {
                "mailbox": "a" * 32,
                "wrap": {**W, "kem_suite": "k" * (ENVELOPE_MULTI_MAX_FIELD_CHARS + 1)},
            }
        ],
        "body": BODY,
    },
    {
        "to": [
            {
                "mailbox": "a" * 32,
                "wrap": {
                    **W,
                    "eph_pk": "e" * (ENVELOPE_MULTI_MAX_WRAP_EPH_PK_CHARS + 1),
                },
            }
        ],
        "body": BODY,
    },
    {
        "to": [
            {
                "mailbox": "a" * 32,
                "wrap": {
                    **W,
                    "ciphertext": "c" * (ENVELOPE_MULTI_MAX_WRAP_CIPHERTEXT_CHARS + 1),
                },
            }
        ],
        "body": BODY,
    },
    # Body: exactly {mr_suite, ciphertext}.
    {"to": [A_ENTRY], "body": {**BODY, "kid": "k"}},
    {"to": [A_ENTRY], "body": {"ciphertext": "c"}},
    {"to": [A_ENTRY], "body": {**BODY, "mr_suite": ""}},
    {"to": [A_ENTRY], "body": {**BODY, "mr_suite": 1}},
    {"to": [A_ENTRY], "body": {**BODY, "mr_suite": "s" * 65}},
    {"to": [A_ENTRY], "body": "opaque"},
    {
        "to": [A_ENTRY],
        "body": {**BODY, "ciphertext": "c" * (ENVELOPE_MULTI_MAX_CIPHERTEXT_CHARS + 1)},
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


async def test_a_full_upload_fits_the_body_cap(gfs):
    # 500 entries with full-size wraps and a full-size body are accepted.
    ids = _ids(MULTI_MAX_RECIPIENTS)
    wrap = {
        "kem_suite": "x25519-hkdf-sha256-aesgcm",
        "eph_pk": "e" * 44,
        "ciphertext": "c" * ENVELOPE_MULTI_MAX_WRAP_CIPHERTEXT_CHARS,
    }
    body = {
        "mr_suite": "mr-wrap-aesgcm-256",
        "ciphertext": "c" * ENVELOPE_MULTI_MAX_CIPHERTEXT_CHARS,
    }
    payload = json.dumps(
        {"to": [{"mailbox": i, "wrap": wrap} for i in ids], "body": body}
    ).encode()
    assert len(payload) <= ENVELOPE_MULTI_MAX_BODY_BYTES
    resp = await gfs.post(ENVELOPE_MULTI_PATH, data=payload)
    assert resp.status == 202
    await _settle(gfs._app)


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


@pytest.mark.security
async def test_each_mailbox_gets_the_same_body_with_its_own_wrap(gfs):
    boxes = [Box() for _ in range(3)]
    for box in boxes:
        await _register(gfs, box)
    # boxes[0] is named twice: it keeps its FIRST wrap, one reference.
    to = _entries([b.id for b in boxes]) + [
        {"mailbox": boxes[0].id, "wrap": {**_wrap(boxes[0].id), "ciphertext": "x"}}
    ]
    resp = await gfs.post(ENVELOPE_MULTI_PATH, json={"to": to, "body": BODY})
    assert resp.status == 202
    await _settle(gfs._app)
    app = gfs._app
    queue = app[gfs_envelope_queue_repo_key]
    assert await _bodies(app) == 1
    for box in boxes:
        assert await queue.count_for(box.id) == 1  # deduplicated
    assert await queue.pool_bytes("envelope", "mailbox") == BODY_JSON_LEN + sum(
        REF_ROW_BYTES + _wrap_len(b.id) for b in boxes
    )

    for box in boxes:
        ws = await _claim(gfs, box)
        assert (await ws.receive_json(timeout=2)) == {"type": "mailbox_claimed"}
        assert await ws.receive_json(timeout=2) == _expected(box.id)
        await ws.close()
        # Freed only after the LAST reference went.
        expected = 0 if box is boxes[-1] else 1
        for _ in range(200):
            if await _bodies(app) == expected:
                break
            await asyncio.sleep(0.01)
        assert await _bodies(app) == expected
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
    for box, ws in zip(boxes, sockets, strict=True):
        assert await ws.receive_json(timeout=2) == _expected(box.id)
    await _settle(gfs._app)
    for _ in range(200):
        if await _bodies(gfs._app) == 0:
            break
        await asyncio.sleep(0.01)
    assert await _bodies(gfs._app) == 0
    for ws in sockets:
        await ws.close()


@pytest.mark.security
async def test_no_log_record_names_a_mailbox(gfs, caplog):
    boxes = [Box() for _ in range(2)]
    for box in boxes:
        await _register(gfs, box)
    caplog.set_level(logging.DEBUG)
    assert (await _multi(gfs, [b.id for b in boxes] + ["q" * 32])).status == 202
    await _settle(gfs._app)
    for box in boxes:
        ws = await _claim(gfs, box)
        assert (await ws.receive_json(timeout=2))["type"] == "mailbox_claimed"
        assert await ws.receive_json(timeout=2) == _expected(box.id)
        await ws.close()
    await _settle(gfs._app)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "multi frame queued for 2 of 3 mailboxes" in text
    for box in boxes:
        assert box.id not in text
        assert _wrap(box.id)["ciphertext"] not in text
    assert "q" * 32 not in text
    assert BODY["ciphertext"] not in text


async def test_a_multi_upload_takes_one_inflight_slot(gfs):
    relay = gfs._app[gfs_envelope_relay_key]
    relay.submit_multi([(i, _wrap(i)) for i in (Box().id for _ in range(5))], BODY)
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


def _ids(n: int, start: int = 0) -> list[str]:
    """*n* distinct well-formed (base32, 32-char) recipient ids."""
    return [
        base64.b32encode(i.to_bytes(20, "big")).decode().lower()
        for i in range(start, start + n)
    ]


async def test_recipient_budget_matches_the_single_route(gfs):
    # One upload must never buy more queue work than the same recipients
    # sent one by one through ``/gfs/envelope`` (600 a minute per address).
    assert ENVELOPE_MULTI_RECIPIENTS_PER_MINUTE == ENVELOPE_MAX_PER_MINUTE
    assert (await _multi(gfs, _ids(MULTI_MAX_RECIPIENTS))).status == 202
    assert (await _multi(gfs, _ids(100, start=1000))).status == 202
    over = await _multi(gfs, _ids(1, start=5000))
    assert over.status == 429
    assert over.headers["Retry-After"] == "60"
    await _settle(gfs._app)


async def test_recipient_budget_charges_unique_mailboxes_once(gfs):
    # 500 entries for one mailbox are ONE recipient: charged once per upload.
    for _ in range(3):
        resp = await _multi(gfs, ["q" * 32] * MULTI_MAX_RECIPIENTS)
        assert resp.status == 202
    assert (await _multi(gfs, _ids(MULTI_MAX_RECIPIENTS))).status == 202  # 503
    assert (await _multi(gfs, _ids(97, start=1000))).status == 202  # 600
    assert (await _multi(gfs, _ids(1, start=5000))).status == 429
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


# ── Repository: body stored once, wraps per reference, caps charge both ──

A, B, C = "a" * 32, "b" * 32, "c" * 32
BODY_JSON = '{"mr_suite":"s","ciphertext":"' + "x" * 960 + '"}'
SIZE = len(BODY_JSON)


def _wj(to: str) -> str:
    """A reference's wrap JSON (per mailbox, so each is distinct)."""
    return json.dumps(_wrap(to), separators=(",", ":"))


WRAP_LEN = len(_wj(A))  # every id is 32 chars, so every wrap has this length
REF = REF_ROW_BYTES + WRAP_LEN  # one reference's pool charge


def _refs(ids) -> list[tuple[str, str]]:
    return [(i, _wj(i)) for i in ids]


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


async def test_enqueue_multi_stores_one_body_and_a_wrap_per_reference(queue, gfs_db):
    admitted = await queue.enqueue_multi(_refs([A, B, C]), BODY_JSON, **_caps())
    assert admitted == [A, B, C]
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    rows = await gfs_db.fetchall(
        "SELECT to_instance, sealed_json, size_bytes, body_id"
        " FROM gfs_envelope_queue ORDER BY id"
    )
    rows = [dict(r) for r in rows]
    assert [r["to_instance"] for r in rows] == [A, B, C]
    # Each reference holds ITS wrap — never a copy of the body.
    assert [r["sealed_json"] for r in rows] == [_wj(A), _wj(B), _wj(C)]
    assert {r["size_bytes"] for r in rows} == {SIZE + WRAP_LEN}
    assert len({r["body_id"] for r in rows}) == 1
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE + 3 * REF
    body = json.loads(BODY_JSON)
    for to in (A, B, C):
        (item,) = await queue.list_for(to, now=100)
        assert item.sealed == {**body, "wrap": _wrap(to)}
        assert item.frame_type == "envelope"
    for to in (A, B):
        (item,) = await queue.list_for(to, now=100)
        await queue.delete(item.id)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE + REF
    (item,) = await queue.list_for(C, now=100)
    await queue.delete(item.id)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_a_repeated_id_gets_one_reference_with_its_first_wrap(queue, gfs_db):
    refs = [(A, _wj(A))] + [(A, _wj(C))] * 49 + [(B, _wj(B)), (A, _wj(B))]
    assert await queue.enqueue_multi(refs, BODY_JSON, **_caps()) == [A, B]
    assert await _count(gfs_db, "gfs_envelope_queue") == 2
    (item,) = await queue.list_for(A, now=100)
    assert item.sealed["wrap"] == _wrap(A)
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE + 2 * REF


async def test_a_corrupt_body_or_wrap_is_skipped_not_wedging_the_drain(queue, gfs_db):
    await queue.enqueue_multi(_refs([A, B]), BODY_JSON, **_caps())
    await queue.enqueue(A, '{"later":1}', mailbox_pool=True, **_caps())
    await gfs_db.enqueue(
        "UPDATE gfs_envelope_queue SET sealed_json='{nope' WHERE to_instance=?"
        " AND body_id IS NOT NULL",
        (A,),
    )
    assert [i.sealed for i in await queue.list_for(A, now=100)] == [{"later": 1}]
    await gfs_db.enqueue("UPDATE gfs_envelope_bodies SET sealed_json='[1]'")
    assert await queue.list_for(B, now=100) == []


async def test_enqueue_multi_reads_the_caps_in_one_grouped_query(queue, gfs_db):
    seen: list[str] = []
    gfs_db._conn.set_trace_callback(seen.append)
    try:
        ids = _ids(50)
        assert await queue.enqueue_multi(_refs(ids), BODY_JSON, **_caps()) == ids
    finally:
        gfs_db._conn.set_trace_callback(None)
    held = [q for q in seen if "COUNT(*)" in q and "gfs_envelope_queue" in q]
    assert len(held) == 1
    assert "GROUP BY to_instance" in held[0]


async def test_reference_rows_are_bounded_by_the_pool(queue, gfs_db):
    # Each reference row costs REF_ROW_BYTES plus its wrap of the pool: a
    # tiny body can not park unbounded rows under a pool that counts the
    # body only once.
    assert REF_ROW_BYTES == 64
    ids = _ids(10)
    caps = _caps(max_total_bytes=SIZE + 10 * REF - 1, ceiling_bytes=SIZE)
    assert await queue.enqueue_multi(_refs(ids), BODY_JSON, **caps) == []
    roomy = _caps(max_total_bytes=SIZE + 10 * REF)
    assert await queue.enqueue_multi(_refs(ids), BODY_JSON, **roomy) == ids
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE + 10 * REF
    for to in ids:
        (item,) = await queue.list_for(to, now=100)
        await queue.delete(item.id)
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_a_capped_recipient_is_skipped_and_charged_body_plus_wrap(queue, gfs_db):
    frame = SIZE + WRAP_LEN
    caps = _caps(max_per_recipient=3, max_bytes_per_recipient=2 * frame + 10)
    # B is at its row cap; C lacks the room for one more frame.
    for _ in range(3):
        assert await queue.enqueue(B, "{}", mailbox_pool=True, **caps)
    assert await queue.enqueue(C, "x" * (frame + 11), mailbox_pool=True, **caps)
    assert await queue.enqueue_multi(_refs([A, B, C]), BODY_JSON, **caps) == [A]
    assert await queue.enqueue_multi(_refs([A]), BODY_JSON, **caps) == [A]
    # A's two references charge two full bodies plus their wraps: 11 more
    # bytes do not fit, although the reference rows hold no copy of the body.
    assert not await queue.enqueue(A, "y" * 11, mailbox_pool=True, **caps)
    assert await queue.enqueue(A, "y" * 10, mailbox_pool=True, **caps)
    assert await _count(gfs_db, "gfs_envelope_bodies") == 2


async def test_the_wrap_counts_against_the_recipient_byte_cap(queue, gfs_db):
    # Room for the body alone is not room for the frame.
    caps = _caps(max_bytes_per_recipient=SIZE + WRAP_LEN - 1)
    assert await queue.enqueue_multi(_refs([A]), BODY_JSON, **caps) == []
    caps = _caps(max_bytes_per_recipient=SIZE + WRAP_LEN)
    assert await queue.enqueue_multi(_refs([A]), BODY_JSON, **caps) == [A]


async def test_nothing_admitted_stores_no_body(queue, gfs_db):
    caps = _caps(max_bytes_per_recipient=SIZE - 1)
    assert await queue.enqueue_multi(_refs([A, B]), BODY_JSON, **caps) == []
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_a_full_pool_admits_only_recipients_under_their_reserve(queue, gfs_db):
    roomy = _caps(max_per_recipient=100, max_total_bytes=10**9)
    # B holds more than the reserve's 20 rows, filling the pool.
    for _ in range(21):
        assert await queue.enqueue(B, BODY_JSON, mailbox_pool=True, **roomy)
    full = _caps(
        max_per_recipient=100, max_total_bytes=SIZE * 21, ceiling_bytes=SIZE * 40
    )
    assert await queue.enqueue_multi(_refs([A, B]), BODY_JSON, **full) == [A]
    # Past the ceiling nobody gets in.
    tight = _caps(max_total_bytes=SIZE, ceiling_bytes=SIZE)
    assert await queue.enqueue_multi(_refs([C]), BODY_JSON, **tight) == []


async def test_pool_admits_everyone_under_the_total(queue):
    caps = _caps(max_total_bytes=SIZE + 3 * REF)
    assert await queue.enqueue_multi(_refs([A, B, C]), BODY_JSON, **caps) == [A, B, C]
    assert await queue.pool_bytes("envelope", "mailbox") == SIZE + 3 * REF


async def test_prune_expired_frees_refs_and_bodies(queue, gfs_db):
    await queue.enqueue_multi(_refs([A, B]), BODY_JSON, **_caps())
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
    await queue.enqueue_multi(_refs(ids), BODY_JSON, **_caps(expires_at=10**9))
    assert await mailboxes.unregister(ids[0])
    assert await _count(gfs_db, "gfs_envelope_bodies") == 1
    assert await mailboxes.prune_idle(older_than=10**8) == 1
    assert await _count(gfs_db, "gfs_envelope_bodies") == 0
    assert await queue.pool_bytes("envelope", "mailbox") == 0


async def test_recompute_pool_totals_agrees_with_the_triggers(queue, gfs_db):
    await queue.enqueue(A, "{}", mailbox_pool=True, **_caps())
    await queue.enqueue(A, '{"h":1}', **_caps())
    await queue.enqueue_multi(_refs([A, B, C]), BODY_JSON, **_caps())
    before = (
        await queue.pool_bytes("envelope", "mailbox"),
        await queue.pool_bytes("envelope", "household"),
    )
    assert before == (2 + SIZE + 3 * REF, 7)
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
    body = {"to": _entries([live.mailbox_id, later.mailbox_id]), "body": BODY}
    async with MailboxClaim(session, _cluster_url(b), live, CLUSTER_GFS_ID) as claim:
        async with session.post(
            _cluster_url(a) + ENVELOPE_MULTI_PATH, json=body
        ) as resp:
            assert resp.status == 202
        assert await claim.receive(timeout=5) == _expected(live.mailbox_id)
    queue = a.app[gfs_envelope_queue_repo_key]
    assert await queue.count_for(later.mailbox_id) == 1
    assert await _bodies(a.app) == 1
    async with MailboxClaim(session, _cluster_url(b), later, CLUSTER_GFS_ID) as claim:
        assert await claim.receive(timeout=5) == _expected(later.mailbox_id)

    async def _freed() -> bool:
        return await _bodies(a.app) == 0

    await _cluster_wait(_freed)
    assert await queue.pool_bytes("envelope", "mailbox") == 0
