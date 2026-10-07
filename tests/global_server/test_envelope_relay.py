"""Unit tests for the GFS opaque envelope relay (§D2b).

Covers the outer-shape validator, the deliver-or-queue service, and the
SQLite queue repo's retention behaviour (TTL + per-recipient cap).

The security-marked tests pin the properties the relay exists for: the
server never learns or reveals who is talking to whom, and it never opens
(or logs) the sealed box.
"""

from __future__ import annotations

import asyncio
import logging

import orjson
import pytest

from socialhome.crypto import derive_instance_id
from socialhome.global_server import envelope_relay as envelope_relay_mod
from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import (
    ENVELOPE_FRAME_TYPE,
    ENVELOPE_INSTANCE_ID_CHARS,
    ENVELOPE_MAX_BODY_BYTES,
    ENVELOPE_MAX_PER_MINUTE,
    ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    ENVELOPE_QUEUE_TTL_SECONDS,
    QUEUE_KIND_ENVELOPE,
    QUEUE_KIND_RELAY,
    RELAY_FRAME_TYPE,
    GfsEnvelopeRelay,
    InvalidEnvelope,
    validate_envelope,
)
from socialhome.global_server.repositories import (
    SqliteGfsEnvelopeQueueRepo,
    SqliteGfsFederationRepo,
)

CIPHERTEXT = "bm9uY2U:Y2lwaGVydGV4dC1ieXRlcw"
EPH_PK = "ZXBoZW1lcmFsLXB1YmxpYy1rZXktYnl0ZXM"


def _sealed(marker: str = CIPHERTEXT) -> dict[str, str]:
    return {"kem_suite": "x25519", "eph_pk": EPH_PK, "ciphertext": marker}


class _FakeRegistry:
    """Minimal ``GfsWebSocketRegistry`` stand-in."""

    def __init__(self, *, online: set[str] | None = None) -> None:
        self.online = online or set()
        self.sent: list[tuple[str, dict]] = []
        self.fail_after: int | None = None

    async def send(self, instance_id: str, payload: dict) -> bool:
        if instance_id not in self.online:
            return False
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append((instance_id, payload))
        return True


@pytest.fixture
async def wiring(gfs_db):
    """(fed_repo, queue_repo, registry, relay) over a real GFS database."""
    fed_repo = SqliteGfsFederationRepo(gfs_db)
    queue_repo = SqliteGfsEnvelopeQueueRepo(gfs_db)
    registry = _FakeRegistry()
    relay = GfsEnvelopeRelay(
        fed_repo=fed_repo,
        queue_repo=queue_repo,
        ws_registry=registry,
    )
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="recipient2home2222222222222222aa",
            display_name="Recipient",
            public_key="aa" * 32,
            status="active",
        )
    )
    return fed_repo, queue_repo, registry, relay


# ── validate_envelope ────────────────────────────────────────────────────


def test_validate_envelope_accepts_the_bootstrap_wire_shape():
    to_instance, sealed = validate_envelope(
        {"to_instance": "issuer2home222222222222222222abc", "sealed": _sealed()}
    )
    assert to_instance == "issuer2home222222222222222222abc"
    assert sealed == _sealed()


@pytest.mark.parametrize(
    "body",
    [
        "not-an-object",
        {},
        {"sealed": _sealed()},
        {"to_instance": "", "sealed": _sealed()},
        {"to_instance": 42, "sealed": _sealed()},
        {"to_instance": ["a"], "sealed": _sealed()},
        {
            "to_instance": "a" * (ENVELOPE_INSTANCE_ID_CHARS + 1),
            "sealed": _sealed(),
        },
        {"to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        {"to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sealed": "opaque"},
        {"to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "sealed": {}},
        # Missing one of the three required keys.
        {
            "to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sealed": {"kem_suite": "x25519", "eph_pk": EPH_PK},
        },
        # An EXTRA key — the shape is exact, not a superset, so a sender
        # cannot smuggle a routing hint (or its own identity) past the relay.
        {
            "to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sealed": {
                **_sealed(),
                "from_instance": "cccccccccccccccccccccccccccccccc",
            },
        },
        # Non-string / empty members.
        {
            "to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sealed": {**_sealed(), "ciphertext": ""},
        },
        {
            "to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sealed": {**_sealed(), "eph_pk": 7},
        },
    ],
)
def test_validate_envelope_rejects_malformed_bodies(body):
    with pytest.raises(InvalidEnvelope):
        validate_envelope(body)


@pytest.mark.security
def test_validate_envelope_never_inspects_the_sealed_values():
    """Suite tags are the RECIPIENT's business — the relay must not gate on
    them, or a household could not migrate to the Phase-2 hybrid suite until
    every connection server had been redeployed."""
    _to, sealed = validate_envelope(
        {
            "to_instance": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "sealed": {
                "kem_suite": "x25519+mlkem768",
                "eph_pk": EPH_PK,
                "ciphertext": CIPHERTEXT,
            },
        }
    )
    assert sealed["kem_suite"] == "x25519+mlkem768"


# ── Deliver-or-queue ─────────────────────────────────────────────────────


async def test_online_recipient_gets_exactly_type_and_sealed(wiring):
    _fed, queue_repo, registry, relay = wiring
    registry.online.add("recipient2home2222222222222222aa")

    await relay.accept("recipient2home2222222222222222aa", _sealed())

    assert registry.sent == [
        (
            "recipient2home2222222222222222aa",
            {"type": ENVELOPE_FRAME_TYPE, "sealed": _sealed()},
        )
    ]
    # A live delivery stores nothing.
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


@pytest.mark.security
async def test_frame_carries_no_field_beyond_type_and_sealed(wiring):
    _fed, _queue, registry, relay = wiring
    registry.online.add("recipient2home2222222222222222aa")

    await relay.accept("recipient2home2222222222222222aa", _sealed())

    _target, frame = registry.sent[0]
    assert set(frame) == {"type", "sealed"}


async def test_offline_recipient_is_queued(wiring):
    _fed, queue_repo, registry, relay = wiring

    await relay.accept("recipient2home2222222222222222aa", _sealed())

    assert registry.sent == []
    queued = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [q.sealed for q in queued] == [_sealed()]


async def test_queued_envelopes_drain_in_order_and_rows_go_away(wiring):
    _fed, queue_repo, registry, relay = wiring
    for i in range(3):
        await relay.accept("recipient2home2222222222222222aa", _sealed(f"ct-{i}"))
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 3

    registry.online.add("recipient2home2222222222222222aa")
    delivered = await relay.drain("recipient2home2222222222222222aa")

    assert delivered == 3
    assert [frame["sealed"]["ciphertext"] for _t, frame in registry.sent] == [
        "ct-0",
        "ct-1",
        "ct-2",
    ]
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


async def test_drain_keeps_rows_a_dying_socket_did_not_take(wiring):
    _fed, queue_repo, registry, relay = wiring
    for i in range(3):
        await relay.accept("recipient2home2222222222222222aa", _sealed(f"ct-{i}"))

    registry.online.add("recipient2home2222222222222222aa")
    registry.fail_after = 1  # the socket dies after the first frame
    delivered = await relay.drain("recipient2home2222222222222222aa")

    assert delivered == 1
    remaining = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [q.sealed["ciphertext"] for q in remaining] == ["ct-1", "ct-2"]


@pytest.mark.security
async def test_unknown_recipient_is_dropped_and_stores_nothing(wiring, gfs_db):
    _fed, _queue, registry, relay = wiring

    await relay.accept("stranger2home2222222222222222abc", _sealed())

    assert registry.sent == []
    rows = await gfs_db.fetchall("SELECT * FROM gfs_envelope_queue", ())
    assert rows == []


@pytest.mark.security
@pytest.mark.parametrize("status", ["pending", "banned"])
async def test_non_active_recipient_is_dropped_and_stores_nothing(
    wiring, gfs_db, status
):
    fed_repo, _queue, registry, relay = wiring
    await fed_repo.upsert_instance(
        ClientInstance(
            instance_id="quiet.home",
            display_name="Quiet",
            public_key="bb" * 32,
            status=status,
        )
    )

    await relay.accept("quiet.home", _sealed())

    assert registry.sent == []
    rows = await gfs_db.fetchall("SELECT * FROM gfs_envelope_queue", ())
    assert rows == []


async def test_drain_is_fail_soft_when_the_queue_lookup_breaks(wiring, caplog):
    _fed, _queue, registry, relay = wiring

    class _Broken:
        async def list_for(self, *a, **kw):
            raise RuntimeError("db gone")

    relay = GfsEnvelopeRelay(
        fed_repo=_fed,
        queue_repo=_Broken(),
        ws_registry=registry,
    )
    with caplog.at_level(logging.WARNING):
        assert await relay.drain("recipient2home2222222222222222aa") == 0


# ── Retention ────────────────────────────────────────────────────────────


async def test_expired_rows_are_swept_and_never_drained(wiring, gfs_db):
    _fed, queue_repo, registry, relay = wiring
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"stale"}',
        created_at=100,
        expires_at=200,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )

    # Past the TTL: invisible to the drain even before the sweep runs.
    assert await queue_repo.list_for("recipient2home2222222222222222aa", now=201) == []
    registry.online.add("recipient2home2222222222222222aa")
    assert await relay.drain("recipient2home2222222222222222aa") == 0

    assert await queue_repo.prune_expired(201) == 1
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


async def test_unexpired_rows_survive_the_sweep(wiring):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"fresh"}',
        created_at=100,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    assert await queue_repo.prune_expired(200) == 0
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 1


async def test_the_cap_tail_drops_and_keeps_what_was_already_accepted(wiring):
    """Evict-oldest handed an anonymous caller a delete primitive: N junk
    envelopes pushed out N legitimate queued ones and nobody learned.
    Tail-drop means a flood can only refuse itself."""
    _fed, queue_repo, _registry, _relay = wiring
    for i in range(3):
        assert (
            await queue_repo.enqueue(
                "recipient2home2222222222222222aa",
                f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"ct-{i}"}}',
                created_at=100 + i,
                expires_at=1_000_000,
                max_per_recipient=3,
                max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
            )
            is True
        )
    for i in range(3, 6):
        assert (
            await queue_repo.enqueue(
                "recipient2home2222222222222222aa",
                f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"ct-{i}"}}',
                created_at=100 + i,
                expires_at=1_000_000,
                max_per_recipient=3,
                max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
            )
            is False
        )

    kept = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [q.sealed["ciphertext"] for q in kept] == ["ct-0", "ct-1", "ct-2"]


async def test_the_201st_envelope_is_dropped_and_the_first_200_survive(wiring):
    """The flood shape, at the review's own numbers."""
    _fed, queue_repo, _registry, _relay = wiring
    for i in range(200):
        assert (
            await queue_repo.enqueue(
                "recipient2home2222222222222222aa",
                f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"real-{i}"}}',
                created_at=100 + i,
                expires_at=1_000_000,
                max_per_recipient=200,
                max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
            )
            is True
        )
    assert (
        await queue_repo.enqueue(
            "recipient2home2222222222222222aa",
            '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"flood"}',
            created_at=9_999,
            expires_at=1_000_000,
            max_per_recipient=200,
            max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
        )
        is False
    )
    kept = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert len(kept) == 200
    assert [q.sealed["ciphertext"] for q in kept] == [f"real-{i}" for i in range(200)]


async def test_the_byte_budget_is_enforced_independently_of_the_row_cap(wiring):
    """The row cap alone is not a disk bound: 2000 x 320 KiB is 625 MiB."""
    _fed, queue_repo, _registry, _relay = wiring
    blob = '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"' + "x" * 400 + '"}'
    accepted = 0
    for i in range(20):
        if await queue_repo.enqueue(
            "recipient2home2222222222222222aa",
            blob,
            created_at=100 + i,
            expires_at=1_000_000,
            max_per_recipient=1_000_000,
            max_bytes_per_recipient=2_000,
        ):
            accepted += 1
    assert accepted == 4
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 4


async def test_expired_rows_do_not_hold_a_slot(wiring):
    """Yesterday's blobs must not block today's — they are already
    invisible to the drain and the sweep will collect them."""
    _fed, queue_repo, _registry, _relay = wiring
    assert await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"stale"}',
        created_at=100,
        expires_at=200,
        max_per_recipient=1,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    assert await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"fresh"}',
        created_at=300,
        expires_at=1_000_000,
        max_per_recipient=1,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    kept = await queue_repo.list_for("recipient2home2222222222222222aa", now=300)
    assert [q.sealed["ciphertext"] for q in kept] == ["fresh"]


async def test_a_full_queue_still_answers_the_uniform_202_and_warns(
    wiring,
    caplog,
):
    """No oracle for the caller, a loud line for the operator."""
    _fed, _queue, _registry, _relay = wiring
    fed_repo, queue_repo, registry, _ = wiring
    relay = GfsEnvelopeRelay(
        fed_repo=fed_repo,
        queue_repo=queue_repo,
        ws_registry=registry,
        max_queued_per_recipient=1,
    )
    await relay.accept("recipient2home2222222222222222aa", _sealed())
    with caplog.at_level(logging.WARNING):
        # Returns None exactly as the accepted path does — the route turns
        # both into the same 202.
        assert await relay.accept("recipient2home2222222222222222aa", _sealed()) is None
    assert "queue full for recipient2home2222222222222222aa" in caplog.text
    assert CIPHERTEXT not in caplog.text
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 1


async def test_the_cap_is_per_recipient_not_global(wiring):
    fed_repo, queue_repo, _registry, _relay = wiring
    for instance in (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ):
        for i in range(3):
            await queue_repo.enqueue(
                instance,
                f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"{instance}-{i}"}}',
                created_at=100 + i,
                expires_at=1_000_000,
                max_per_recipient=2,
                max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
            )
    assert await queue_repo.count_for("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == 2
    assert await queue_repo.count_for("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb") == 2


async def test_corrupt_queue_row_is_skipped_not_fatal(wiring, gfs_db):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        "not json at all",
        created_at=100,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '"a json string, not an object"',
        created_at=101,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"good"}',
        created_at=102,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )

    kept = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [q.sealed["ciphertext"] for q in kept] == ["good"]


async def test_delete_reports_rowcount(wiring):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient2home2222222222222222aa",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"x"}',
        created_at=1,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
        max_bytes_per_recipient=ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT,
    )
    [row] = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert await queue_repo.delete(row.id) == 1
    assert await queue_repo.delete(row.id) == 0


# ── Logging discipline ───────────────────────────────────────────────────


@pytest.mark.security
async def test_no_log_record_carries_the_sealed_material(wiring, caplog):
    """The GFS may log WHO an envelope is for (it routes on that) and never
    WHAT is in it — the ciphertext and the sender ephemeral pubkey are the
    two fields that would make a log a decryption oracle's raw material."""
    _fed, _queue, registry, relay = wiring
    registry.online.add("recipient2home2222222222222222aa")

    with caplog.at_level(logging.DEBUG):
        await relay.accept("recipient2home2222222222222222aa", _sealed())
        await relay.accept("stranger2home2222222222222222abc", _sealed())
        registry.online.discard("recipient2home2222222222222222aa")
        await relay.accept("recipient2home2222222222222222aa", _sealed())
        registry.online.add("recipient2home2222222222222222aa")
        await relay.drain("recipient2home2222222222222222aa")

    for record in caplog.records:
        rendered = record.getMessage()
        assert CIPHERTEXT not in rendered
        assert EPH_PK not in rendered


# ── Constants ────────────────────────────────────────────────────────────


def test_constants_are_the_documented_values():
    """These numbers are quoted in ``docs/api.md`` and reasoned about in the
    migration header; a silent change here desynchronises both."""
    assert ENVELOPE_MAX_BODY_BYTES == 320 * 1024
    assert ENVELOPE_MAX_PER_MINUTE == 600
    assert ENVELOPE_QUEUE_TTL_SECONDS == 86_400
    assert ENVELOPE_QUEUE_MAX_PER_RECIPIENT == 2000
    assert ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT == 64 * 1024 * 1024


# ── to_instance is an identifier, not free text ──────────────────────────


FORGED_ID = (
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    "2026-09-18 00:00:00 WARNING gfs.envelope: ACCEPTED forged line"
)


@pytest.mark.security
@pytest.mark.parametrize(
    "to_instance",
    [
        FORGED_ID,
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        "\naaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",  # base32 here is lowercase
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",  # 0/1/8/9 are not base32
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # 31 chars
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # 33 chars
        "../../etc/passwd",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa=",  # base32 padding is stripped
    ],
)
def test_validate_envelope_rejects_anything_but_an_instance_id(to_instance):
    """``to_instance`` is the one field this anonymous endpoint routes on,
    stores and writes into its own logs. A length bound let a caller smuggle
    a newline into a log line; the shape check is what closes it."""
    with pytest.raises(InvalidEnvelope, match="to_instance"):
        validate_envelope({"to_instance": to_instance, "sealed": _sealed()})


def test_validate_envelope_accepts_a_real_derived_instance_id():
    """The shape is whatever ``derive_instance_id`` actually produces — not
    a guess at it."""
    real = derive_instance_id(b"\x01" * 32)
    to_instance, _sealed_out = validate_envelope(
        {"to_instance": real, "sealed": _sealed()},
    )
    assert to_instance == real


# ── Relay frames: member-published space items (migration 0014) ──────────


def _item(marker: str = "ct") -> dict:
    return {
        "space_id": "sp-1",
        "event_type": "space_item",
        "epoch": 3,
        "writer_cert": {"cert_suite": "ed25519"},
        "payload": marker,
    }


async def _send(relay, frame, *, queue_ok=True) -> int:
    target = "recipient2home2222222222222222aa"
    return await relay.fan_out_relay(
        [target], queue_ok={target} if queue_ok else set(), frame=frame
    )


async def test_relay_frame_goes_to_a_live_socket_as_a_relay_frame(wiring):
    _fed, queue_repo, registry, relay = wiring
    registry.online.add("recipient2home2222222222222222aa")

    assert await _send(relay, _item()) == 1

    assert registry.sent == [
        ("recipient2home2222222222222222aa", {"type": RELAY_FRAME_TYPE, **_item()})
    ]
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


async def test_relay_frame_is_queued_offline_and_drained_as_a_relay_frame(wiring):
    _fed, queue_repo, registry, relay = wiring
    await relay.accept("recipient2home2222222222222222aa", _sealed("env-0"))
    assert await _send(relay, _item("item-1")) == 1
    queued = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [q.frame_type for q in queued] == [QUEUE_KIND_ENVELOPE, QUEUE_KIND_RELAY]

    registry.online.add("recipient2home2222222222222222aa")
    assert await relay.drain("recipient2home2222222222222222aa") == 2

    assert [frame for _t, frame in registry.sent] == [
        {"type": ENVELOPE_FRAME_TYPE, "sealed": _sealed("env-0")},
        {"type": RELAY_FRAME_TYPE, **_item("item-1")},
    ]


async def test_relay_items_have_their_own_cap_and_never_crowd_out_envelopes(
    wiring,
):
    _fed, queue_repo, _registry, _relay = wiring
    relay = GfsEnvelopeRelay(
        fed_repo=_fed,
        queue_repo=queue_repo,
        ws_registry=_registry,
        max_queued_per_recipient=2,
    )
    for i in range(2):
        await relay.accept("recipient2home2222222222222222aa", _sealed(f"e{i}"))
    # The envelope budget is exhausted, yet a relay item still queues …
    assert await _send(relay, _item()) == 1
    # … and a relay item never consumed envelope budget.
    await relay.accept("recipient2home2222222222222222aa", _sealed("e-late"))
    rows = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert sum(1 for r in rows if r.frame_type == QUEUE_KIND_ENVELOPE) == 2


async def test_the_relay_queue_keeps_the_newest_at_its_cap(wiring, monkeypatch):
    _fed, queue_repo, _registry, relay = wiring
    monkeypatch.setattr(envelope_relay_mod, "RELAY_QUEUE_MAX_PER_RECIPIENT", 1)
    assert await _send(relay, _item("first")) == 1
    assert await _send(relay, _item("second")) == 1
    rows = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [r.sealed["payload"] for r in rows] == ["second"]


async def test_an_item_over_the_caps_on_its_own_is_not_queued(
    wiring, monkeypatch, caplog
):
    _fed, queue_repo, _registry, relay = wiring
    monkeypatch.setattr(envelope_relay_mod, "RELAY_QUEUE_MAX_BYTES_PER_RECIPIENT", 10)
    with caplog.at_level(logging.WARNING, logger="socialhome.global_server"):
        assert await _send(relay, _item("big")) == 0
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0
    assert "exceeds the relay queue caps" in caplog.text
    assert "big" not in caplog.text


async def test_a_corrupt_frame_type_is_refused_by_the_database(gfs_db):
    with pytest.raises(Exception):
        await gfs_db.transact(
            lambda conn: conn.execute(
                "INSERT INTO gfs_envelope_queue(to_instance, sealed_json,"
                " created_at, expires_at, frame_type) VALUES(?,?,?,?,?)",
                ("x" * 32, "{}", 0, 1, "bogus"),
            )
        )


async def test_a_target_not_in_queue_ok_gets_no_row(wiring):
    _fed, queue_repo, _registry, relay = wiring
    assert await _send(relay, _item(), queue_ok=False) == 0
    assert await queue_repo.count_for("recipient2home2222222222222222aa") == 0


async def test_the_server_wide_relay_byte_cap_holds_by_eviction(wiring, monkeypatch):
    _fed, queue_repo, _registry, relay = wiring
    one = len(orjson.dumps(_item("x")))
    monkeypatch.setattr(envelope_relay_mod, "RELAY_QUEUE_MAX_TOTAL_BYTES", one + 1)
    assert await _send(relay, _item("x")) == 1
    assert await _send(relay, _item("y")) == 1
    rows = await queue_repo.list_for("recipient2home2222222222222222aa", now=0)
    assert [r.sealed["payload"] for r in rows] == ["y"]
    assert await queue_repo.relay_bytes(0) <= one + 1


async def test_live_pushes_run_concurrently_but_bounded(wiring, monkeypatch):
    _fed, _queue, _registry, relay = wiring
    monkeypatch.setattr(envelope_relay_mod, "RELAY_FAN_OUT_CONCURRENCY", 3)
    in_flight = 0
    peak = 0

    class _Slow:
        async def send(self, instance_id, payload):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return True

    relay._ws_registry = _Slow()
    targets = [f"t{i}" for i in range(12)]
    assert await relay.fan_out_relay(targets, queue_ok=set(), frame=_item()) == 12
    assert 1 < peak <= 3


# ── Background hand-off: uniform timing on POST /gfs/envelope ────────────

RECIPIENT = "recipient2home2222222222222222aa"


class _FakeCluster:
    """Records ``hint_drain`` calls — the cross-node drain signal."""

    def __init__(self) -> None:
        self.hinted: list[str] = []

    def hint_drain(self, instance_id: str) -> None:
        self.hinted.append(instance_id)


async def test_submit_returns_before_the_db_work_runs(wiring, monkeypatch):
    """The route answers without awaiting ``accept`` — a blocked lookup must
    not hold the caller, or response timing becomes the oracle."""
    _fed, queue_repo, _registry, relay = wiring
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocked(self, to_instance, sealed):
        started.set()
        await release.wait()

    monkeypatch.setattr(GfsEnvelopeRelay, "accept", _blocked)
    assert relay.submit(RECIPIENT, _sealed()) is None
    await asyncio.wait_for(started.wait(), timeout=1)
    assert relay.in_flight == 1
    release.set()
    await relay.close()
    assert relay.in_flight == 0


async def test_submit_runs_accept_in_the_background(wiring):
    _fed, queue_repo, _registry, relay = wiring
    relay.submit(RECIPIENT, _sealed())
    await relay.close()
    assert await queue_repo.count_for(RECIPIENT) == 1


async def test_saturation_drops_and_warns_once_not_per_envelope(
    wiring, monkeypatch, caplog
):
    fed_repo, queue_repo, registry, _relay = wiring
    relay = GfsEnvelopeRelay(
        fed_repo=fed_repo,
        queue_repo=queue_repo,
        ws_registry=registry,
        max_inflight=2,
    )
    release = asyncio.Event()

    async def _blocked(self, to_instance, sealed):
        await release.wait()

    monkeypatch.setattr(GfsEnvelopeRelay, "accept", _blocked)
    with caplog.at_level(logging.WARNING):
        for _ in range(10):
            relay.submit(RECIPIENT, _sealed())
    assert relay.in_flight == 2
    warnings = [r for r in caplog.records if "saturated" in r.getMessage()]
    assert len(warnings) == 1
    assert RECIPIENT not in warnings[0].getMessage()
    release.set()
    await relay.close()


async def test_saturation_warning_is_repeated_after_the_interval(
    wiring, monkeypatch, caplog
):
    fed_repo, queue_repo, registry, _relay = wiring
    now = [1000.0]
    relay = GfsEnvelopeRelay(
        fed_repo=fed_repo,
        queue_repo=queue_repo,
        ws_registry=registry,
        max_inflight=0,
        clock=lambda: now[0],
    )
    with caplog.at_level(logging.WARNING):
        relay.submit(RECIPIENT, _sealed())
        relay.submit(RECIPIENT, _sealed())
        now[0] += envelope_relay_mod.ENVELOPE_SATURATION_WARN_INTERVAL_S + 1
        relay.submit(RECIPIENT, _sealed())
    warnings = [r.getMessage() for r in caplog.records if "saturated" in r.getMessage()]
    assert len(warnings) == 2
    # The second warning reports what was dropped since the first.
    assert "2 envelope(s)" in warnings[1]


async def test_a_background_failure_is_logged_not_lost(wiring, monkeypatch, caplog):
    _fed, _queue, _registry, relay = wiring

    async def _boom(self, to_instance, sealed):
        raise RuntimeError("db gone")

    monkeypatch.setattr(GfsEnvelopeRelay, "accept", _boom)
    with caplog.at_level(logging.WARNING):
        relay.submit(RECIPIENT, _sealed())
        await relay.close()
    assert any(
        r.levelname == "WARNING" and "background accept failed" in r.getMessage()
        for r in caplog.records
    )
    assert relay.in_flight == 0


async def test_close_cancels_work_that_will_not_finish(wiring, monkeypatch):
    _fed, _queue, _registry, relay = wiring

    async def _forever(self, to_instance, sealed):
        await asyncio.Event().wait()

    monkeypatch.setattr(GfsEnvelopeRelay, "accept", _forever)
    relay.submit(RECIPIENT, _sealed())
    await asyncio.sleep(0)
    await relay.close(timeout=0.05)
    assert relay.in_flight == 0


async def test_submits_for_one_recipient_land_in_submit_order(wiring, monkeypatch):
    """Background tasks must not reorder one sender's envelopes: the first
    submit is made the SLOWEST to look up, and still queues first."""
    _fed, queue_repo, _registry, relay = wiring
    real_get = SqliteGfsFederationRepo.get_instance
    delays = iter([0.05, 0.02, 0.0])

    async def _slow_get(self, instance_id):
        await asyncio.sleep(next(delays))
        return await real_get(self, instance_id)

    monkeypatch.setattr(SqliteGfsFederationRepo, "get_instance", _slow_get)
    for i in range(3):
        relay.submit(RECIPIENT, _sealed(f"ct-{i}"))
    await relay.close()
    rows = await queue_repo.list_for(RECIPIENT, now=0)
    assert [r.sealed["ciphertext"] for r in rows] == ["ct-0", "ct-1", "ct-2"]


# ── Cross-node drain hint ────────────────────────────────────────────────


async def test_an_offline_enqueue_hints_the_cluster(wiring):
    _fed, _queue, _registry, relay = wiring
    cluster = _FakeCluster()
    relay.attach_cluster(cluster)
    await relay.accept(RECIPIENT, _sealed())
    assert cluster.hinted == [RECIPIENT]


async def test_a_live_delivery_or_an_unknown_recipient_hints_nothing(wiring):
    _fed, _queue, registry, relay = wiring
    cluster = _FakeCluster()
    relay.attach_cluster(cluster)
    await relay.accept("stranger2home2222222222222222abc", _sealed())
    registry.online.add(RECIPIENT)
    await relay.accept(RECIPIENT, _sealed())
    assert cluster.hinted == []


async def test_a_tail_dropped_envelope_hints_nothing(wiring):
    fed_repo, queue_repo, registry, _relay = wiring
    relay = GfsEnvelopeRelay(
        fed_repo=fed_repo,
        queue_repo=queue_repo,
        ws_registry=registry,
        max_queued_per_recipient=1,
    )
    cluster = _FakeCluster()
    relay.attach_cluster(cluster)
    await relay.accept(RECIPIENT, _sealed("a"))
    await relay.accept(RECIPIENT, _sealed("b"))
    assert cluster.hinted == [RECIPIENT]


async def test_a_queued_relay_item_hints_the_cluster(wiring):
    _fed, _queue, _registry, relay = wiring
    cluster = _FakeCluster()
    relay.attach_cluster(cluster)
    assert await _send(relay, _item()) == 1
    assert cluster.hinted == [RECIPIENT]


async def test_without_a_cluster_an_enqueue_still_works(wiring):
    _fed, queue_repo, _registry, relay = wiring
    await relay.accept(RECIPIENT, _sealed())
    assert await queue_repo.count_for(RECIPIENT) == 1


# ── Drain serialisation ──────────────────────────────────────────────────


async def test_concurrent_drains_never_deliver_an_envelope_twice(wiring):
    """A hello drain and a cluster-hint drain can race for one household;
    without a per-instance lock both list the same rows and both send."""
    _fed, queue_repo, registry, relay = wiring
    for i in range(3):
        await relay.accept(RECIPIENT, _sealed(f"ct-{i}"))
    registry.online.add(RECIPIENT)

    counts = await asyncio.gather(relay.drain(RECIPIENT), relay.drain(RECIPIENT))

    assert sorted(counts) == [0, 3]
    assert [f["sealed"]["ciphertext"] for _t, f in registry.sent] == [
        "ct-0",
        "ct-1",
        "ct-2",
    ]
    assert await queue_repo.count_for(RECIPIENT) == 0
    # The per-instance locks are dropped once nobody holds or waits on them.
    assert relay.lock_count == 0


async def test_keyed_locks_serialise_per_key_and_clean_up():
    locks = envelope_relay_mod.KeyedLocks()
    order: list[str] = []

    async def _hold(key: str, tag: str, pause: float) -> None:
        async with locks.hold(key):
            order.append(f"{tag}-in")
            await asyncio.sleep(pause)
            order.append(f"{tag}-out")

    await asyncio.gather(
        _hold("a", "a1", 0.02), _hold("a", "a2", 0), _hold("b", "b1", 0)
    )
    # a2 waited for a1; b1 ran alongside.
    assert order.index("a2-in") > order.index("a1-out")
    assert order.index("b1-in") < order.index("a1-out")
    assert len(locks) == 0


async def test_keyed_locks_release_on_error():
    locks = envelope_relay_mod.KeyedLocks()
    with pytest.raises(RuntimeError):
        async with locks.hold("a"):
            raise RuntimeError("x")
    assert len(locks) == 0
    async with locks.hold("a"):
        pass
