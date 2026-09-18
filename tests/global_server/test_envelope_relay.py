"""Unit tests for the GFS opaque envelope relay (§D2b).

Covers the outer-shape validator, the deliver-or-queue service, and the
SQLite queue repo's retention behaviour (TTL + per-recipient cap).

The security-marked tests pin the properties the relay exists for: the
server never learns or reveals who is talking to whom, and it never opens
(or logs) the sealed box.
"""

from __future__ import annotations

import logging

import pytest

from socialhome.global_server.domain import ClientInstance
from socialhome.global_server.envelope_relay import (
    ENVELOPE_FRAME_TYPE,
    ENVELOPE_INSTANCE_ID_MAX_CHARS,
    ENVELOPE_MAX_BODY_BYTES,
    ENVELOPE_MAX_PER_MINUTE,
    ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    ENVELOPE_QUEUE_TTL_SECONDS,
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
            instance_id="recipient.home",
            display_name="Recipient",
            public_key="aa" * 32,
            inbox_url="http://recipient.home/wh",
            status="active",
        )
    )
    return fed_repo, queue_repo, registry, relay


# ── validate_envelope ────────────────────────────────────────────────────


def test_validate_envelope_accepts_the_bootstrap_wire_shape():
    to_instance, sealed = validate_envelope(
        {"to_instance": "issuer.home", "sealed": _sealed()}
    )
    assert to_instance == "issuer.home"
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
            "to_instance": "x" * (ENVELOPE_INSTANCE_ID_MAX_CHARS + 1),
            "sealed": _sealed(),
        },
        {"to_instance": "a.home"},
        {"to_instance": "a.home", "sealed": "opaque"},
        {"to_instance": "a.home", "sealed": {}},
        # Missing one of the three required keys.
        {"to_instance": "a.home", "sealed": {"kem_suite": "x25519", "eph_pk": EPH_PK}},
        # An EXTRA key — the shape is exact, not a superset, so a sender
        # cannot smuggle a routing hint (or its own identity) past the relay.
        {"to_instance": "a.home", "sealed": {**_sealed(), "from_instance": "me.home"}},
        # Non-string / empty members.
        {"to_instance": "a.home", "sealed": {**_sealed(), "ciphertext": ""}},
        {"to_instance": "a.home", "sealed": {**_sealed(), "eph_pk": 7}},
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
            "to_instance": "a.home",
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
    registry.online.add("recipient.home")

    await relay.accept("recipient.home", _sealed())

    assert registry.sent == [
        ("recipient.home", {"type": ENVELOPE_FRAME_TYPE, "sealed": _sealed()})
    ]
    # A live delivery stores nothing.
    assert await queue_repo.count_for("recipient.home") == 0


@pytest.mark.security
async def test_frame_carries_no_field_beyond_type_and_sealed(wiring):
    _fed, _queue, registry, relay = wiring
    registry.online.add("recipient.home")

    await relay.accept("recipient.home", _sealed())

    _target, frame = registry.sent[0]
    assert set(frame) == {"type", "sealed"}


async def test_offline_recipient_is_queued(wiring):
    _fed, queue_repo, registry, relay = wiring

    await relay.accept("recipient.home", _sealed())

    assert registry.sent == []
    queued = await queue_repo.list_for("recipient.home", now=0)
    assert [q.sealed for q in queued] == [_sealed()]


async def test_queued_envelopes_drain_in_order_and_rows_go_away(wiring):
    _fed, queue_repo, registry, relay = wiring
    for i in range(3):
        await relay.accept("recipient.home", _sealed(f"ct-{i}"))
    assert await queue_repo.count_for("recipient.home") == 3

    registry.online.add("recipient.home")
    delivered = await relay.drain("recipient.home")

    assert delivered == 3
    assert [frame["sealed"]["ciphertext"] for _t, frame in registry.sent] == [
        "ct-0",
        "ct-1",
        "ct-2",
    ]
    assert await queue_repo.count_for("recipient.home") == 0


async def test_drain_keeps_rows_a_dying_socket_did_not_take(wiring):
    _fed, queue_repo, registry, relay = wiring
    for i in range(3):
        await relay.accept("recipient.home", _sealed(f"ct-{i}"))

    registry.online.add("recipient.home")
    registry.fail_after = 1  # the socket dies after the first frame
    delivered = await relay.drain("recipient.home")

    assert delivered == 1
    remaining = await queue_repo.list_for("recipient.home", now=0)
    assert [q.sealed["ciphertext"] for q in remaining] == ["ct-1", "ct-2"]


@pytest.mark.security
async def test_unknown_recipient_is_dropped_and_stores_nothing(wiring, gfs_db):
    _fed, _queue, registry, relay = wiring

    await relay.accept("stranger.home", _sealed())

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
            inbox_url="http://quiet.home/wh",
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
        assert await relay.drain("recipient.home") == 0


# ── Retention ────────────────────────────────────────────────────────────


async def test_expired_rows_are_swept_and_never_drained(wiring, gfs_db):
    _fed, queue_repo, registry, relay = wiring
    await queue_repo.enqueue(
        "recipient.home",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"stale"}',
        created_at=100,
        expires_at=200,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )

    # Past the TTL: invisible to the drain even before the sweep runs.
    assert await queue_repo.list_for("recipient.home", now=201) == []
    registry.online.add("recipient.home")
    assert await relay.drain("recipient.home") == 0

    assert await queue_repo.prune_expired(201) == 1
    assert await queue_repo.count_for("recipient.home") == 0


async def test_unexpired_rows_survive_the_sweep(wiring):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient.home",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"fresh"}',
        created_at=100,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )
    assert await queue_repo.prune_expired(200) == 0
    assert await queue_repo.count_for("recipient.home") == 1


async def test_per_recipient_cap_evicts_the_oldest(wiring):
    _fed, queue_repo, _registry, _relay = wiring
    for i in range(5):
        await queue_repo.enqueue(
            "recipient.home",
            f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"ct-{i}"}}',
            created_at=100 + i,
            expires_at=1_000_000,
            max_per_recipient=3,
        )

    kept = await queue_repo.list_for("recipient.home", now=0)
    assert [q.sealed["ciphertext"] for q in kept] == ["ct-2", "ct-3", "ct-4"]


async def test_the_cap_is_per_recipient_not_global(wiring):
    fed_repo, queue_repo, _registry, _relay = wiring
    for instance in ("a.home", "b.home"):
        for i in range(3):
            await queue_repo.enqueue(
                instance,
                f'{{"kem_suite":"x25519","eph_pk":"e","ciphertext":"{instance}-{i}"}}',
                created_at=100 + i,
                expires_at=1_000_000,
                max_per_recipient=2,
            )
    assert await queue_repo.count_for("a.home") == 2
    assert await queue_repo.count_for("b.home") == 2


async def test_corrupt_queue_row_is_skipped_not_fatal(wiring, gfs_db):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient.home",
        "not json at all",
        created_at=100,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )
    await queue_repo.enqueue(
        "recipient.home",
        '"a json string, not an object"',
        created_at=101,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )
    await queue_repo.enqueue(
        "recipient.home",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"good"}',
        created_at=102,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )

    kept = await queue_repo.list_for("recipient.home", now=0)
    assert [q.sealed["ciphertext"] for q in kept] == ["good"]


async def test_delete_reports_rowcount(wiring):
    _fed, queue_repo, _registry, _relay = wiring
    await queue_repo.enqueue(
        "recipient.home",
        '{"kem_suite":"x25519","eph_pk":"e","ciphertext":"x"}',
        created_at=1,
        expires_at=1_000_000,
        max_per_recipient=ENVELOPE_QUEUE_MAX_PER_RECIPIENT,
    )
    [row] = await queue_repo.list_for("recipient.home", now=0)
    assert await queue_repo.delete(row.id) == 1
    assert await queue_repo.delete(row.id) == 0


# ── Logging discipline ───────────────────────────────────────────────────


@pytest.mark.security
async def test_no_log_record_carries_the_sealed_material(wiring, caplog):
    """The GFS may log WHO an envelope is for (it routes on that) and never
    WHAT is in it — the ciphertext and the sender ephemeral pubkey are the
    two fields that would make a log a decryption oracle's raw material."""
    _fed, _queue, registry, relay = wiring
    registry.online.add("recipient.home")

    with caplog.at_level(logging.DEBUG):
        await relay.accept("recipient.home", _sealed())
        await relay.accept("stranger.home", _sealed())
        registry.online.discard("recipient.home")
        await relay.accept("recipient.home", _sealed())
        registry.online.add("recipient.home")
        await relay.drain("recipient.home")

    for record in caplog.records:
        rendered = record.getMessage()
        assert CIPHERTEXT not in rendered
        assert EPH_PK not in rendered


# ── Constants ────────────────────────────────────────────────────────────


def test_constants_are_the_documented_values():
    """These numbers are quoted in ``docs/api.md`` and reasoned about in the
    migration header; a silent change here desynchronises both."""
    assert ENVELOPE_MAX_BODY_BYTES == 320 * 1024
    assert ENVELOPE_MAX_PER_MINUTE == 30
    assert ENVELOPE_QUEUE_TTL_SECONDS == 86_400
    assert ENVELOPE_QUEUE_MAX_PER_RECIPIENT == 200
