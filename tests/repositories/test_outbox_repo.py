"""Tests for socialhome.repositories.outbox_repo and infrastructure.outbox_processor."""

from __future__ import annotations

import asyncio

import pytest

from socialhome.domain.federation import FederationEventType
from socialhome.infrastructure.outbox_processor import (
    BACKOFF_SECONDS,
    MAX_ATTEMPTS,
    DeliveryOutcome,
    OutboxProcessor,
)
from socialhome.repositories.outbox_repo import SqliteOutboxRepo


@pytest.fixture
async def env(tmp_dir):
    """Minimal env with an outbox repo over a real SQLite database."""
    from socialhome.crypto import generate_identity_keypair, derive_instance_id
    from socialhome.db.database import AsyncDatabase

    kp = generate_identity_keypair()
    iid = derive_instance_id(kp.public_key)
    db = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, kp.private_key.hex(), kp.public_key.hex(), "aa" * 32),
    )

    class Env:
        pass

    e = Env()
    e.db = db
    e.outbox_repo = SqliteOutboxRepo(db)
    yield e
    await db.shutdown()


async def test_outbox_full_cycle(env):
    """Enqueued entry appears in list_due; marking delivered DELETEs the row."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    due = await env.outbox_repo.list_due()
    assert len(due) == 1
    await env.outbox_repo.mark_delivered(eid)
    assert await env.outbox_repo.list_due() == []
    # mark_delivered now DELETEs the row outright — no 'delivered' tombstone
    # is left behind (nothing reads it; at-least-once is the receiver's 2xx).
    rows = await env.db.fetchall("SELECT id FROM federation_outbox WHERE id=?", (eid,))
    assert rows == []


async def test_outbox_processor_backoff(env):
    """Backoff schedule starts at 5s and caps at 4 hours."""
    assert BACKOFF_SECONDS[0] == 5
    assert BACKOFF_SECONDS[1] == 10
    assert BACKOFF_SECONDS[-1] == 14400


async def test_outbox_processor_max_attempts(env):
    """Entry transitions to 'failed' after MAX_ATTEMPTS delivery failures."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer-x",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )
    await env.db.enqueue(
        "UPDATE federation_outbox SET attempts=? WHERE id=?",
        (MAX_ATTEMPTS - 1, eid),
    )

    async def always_fail(entry):
        return DeliveryOutcome.TRANSIENT

    proc = OutboxProcessor(env.outbox_repo, always_fail, rng=lambda: 0.5)
    await proc.drain_once()

    rows = await env.db.fetchall(
        "SELECT status FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "failed"


async def test_outbox_processor_exception_retry(env):
    """A delivery callback that raises is treated as a retry, not a crash."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer-x",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )

    async def raise_always(entry):
        raise RuntimeError("network error")

    proc = OutboxProcessor(env.outbox_repo, raise_always, rng=lambda: 0.5)
    count = await proc.drain_once()
    assert count == 1

    rows = await env.db.fetchall(
        "SELECT status, attempts FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "pending"
    assert int(rows[0]["attempts"]) == 1


async def test_outbox_processor_lifecycle(env):
    """OutboxProcessor start and stop do not raise."""

    async def noop(entry):
        return DeliveryOutcome.SUCCESS

    proc = OutboxProcessor(
        env.outbox_repo,
        noop,
        poll_interval_seconds=0.01,
        rng=lambda: 0.5,
    )
    await proc.start()
    await asyncio.sleep(0.05)
    await proc.stop()


async def test_outbox_processor_drain(env):
    """drain_once delivers a pending entry and calls the callback exactly once."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.DM_MESSAGE,
        payload_json="{}",
    )
    delivered = []

    async def deliver(entry):
        delivered.append(entry.id)
        return DeliveryOutcome.SUCCESS

    proc = OutboxProcessor(env.outbox_repo, deliver, rng=lambda: 0.5)
    count = await proc.drain_once()
    assert count == 1 and delivered == [eid]


# ── §4.4.7 retention: default expires_at at enqueue ──────────────────────────


async def _expires_at(env, eid: str) -> str | None:
    rows = await env.db.fetchall(
        "SELECT expires_at FROM federation_outbox WHERE id=?", (eid,)
    )
    return rows[0]["expires_at"]


async def test_enqueue_never_drop_event_has_null_expiry(env):
    """A NEVER_DROP event (e.g. SPACE_MEMBER_BANNED) enqueues with NULL TTL."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_MEMBER_BANNED,
        payload_json="{}",
    )
    assert await _expires_at(env, eid) is None


async def test_enqueue_ordinary_event_gets_seven_day_ttl(env):
    """An ordinary event enqueues with a ~7-day retention deadline."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    iso = await _expires_at(env, eid)
    assert iso is not None
    parsed = datetime.fromisoformat(iso)
    assert now + timedelta(days=6) < parsed < now + timedelta(days=8)


async def test_enqueue_explicit_expires_at_is_respected(env):
    """An explicit expires_at from the caller overrides the default policy."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        expires_at="2030-01-01T00:00:00+00:00",
    )
    assert await _expires_at(env, eid) == "2030-01-01T00:00:00+00:00"


# ── §4.4.7 retention: expire_past_retention sweep ────────────────────────────


async def test_expire_past_retention_marks_expired_failed(env):
    """A pending row whose expires_at has passed is marked failed."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        expires_at="2000-01-01T00:00:00+00:00",
    )
    count = await env.outbox_repo.expire_past_retention("2026-01-01T00:00:00+00:00")
    assert count == 1
    rows = await env.db.fetchall(
        "SELECT status FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "failed"


async def test_expire_past_retention_skips_null_expiry(env):
    """A NEVER_DROP (NULL-expires) row is untouched even if ancient."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_MEMBER_BANNED,
        payload_json="{}",
    )
    # Backdate created_at to prove age is irrelevant for NULL-expires rows.
    await env.db.enqueue(
        "UPDATE federation_outbox SET created_at='2000-01-01 00:00:00' WHERE id=?",
        (eid,),
    )
    count = await env.outbox_repo.expire_past_retention("2026-01-01T00:00:00+00:00")
    assert count == 0
    rows = await env.db.fetchall(
        "SELECT status FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "pending"


# ── §4.4.7 retention: purge_terminal sweep ──────────────────────────────────


async def _seed_terminal(env, eid, *, status, stamp_col, stamp):
    """Insert a row directly in a terminal state with a deterministic
    terminal timestamp so the cutoff comparison doesn't rely on wall-clock."""
    await env.db.enqueue(
        "INSERT INTO federation_outbox(id, instance_id, event_type,"
        " payload_json, status, created_at) VALUES(?,?,?,?,?,?)",
        (eid, "peer", "SPACE_POST_CREATED", "{}", status, stamp),
    )
    await env.db.enqueue(
        f"UPDATE federation_outbox SET {stamp_col}=? WHERE id=?",
        (stamp, eid),
    )


async def test_purge_terminal_deletes_old_failed_keeps_new(env):
    """A failed row older than cutoff is purged; a newer one is kept."""
    await _seed_terminal(
        env,
        "old",
        status="failed",
        stamp_col="failed_at",
        stamp="2000-01-01T00:00:00+00:00",
    )
    await _seed_terminal(
        env,
        "new",
        status="failed",
        stamp_col="failed_at",
        stamp="2030-01-01T00:00:00+00:00",
    )
    n = await env.outbox_repo.purge_terminal("2026-01-01T00:00:00+00:00")
    assert n == 1
    ids = {r["id"] for r in await env.db.fetchall("SELECT id FROM federation_outbox")}
    assert ids == {"new"}


async def test_purge_terminal_reclaims_legacy_delivered(env):
    """A legacy 'delivered' row (pre-change backlog) past cutoff is reclaimed."""
    await _seed_terminal(
        env,
        "leg",
        status="delivered",
        stamp_col="delivered_at",
        stamp="2000-01-01T00:00:00+00:00",
    )
    n = await env.outbox_repo.purge_terminal("2026-01-01T00:00:00+00:00")
    assert n == 1
    rows = await env.db.fetchall("SELECT id FROM federation_outbox WHERE id='leg'")
    assert rows == []


async def test_purge_terminal_never_touches_pending(env):
    """A pending row is never purged, however old."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    await env.db.enqueue(
        "UPDATE federation_outbox SET created_at='2000-01-01T00:00:00+00:00'"
        " WHERE id=?",
        (eid,),
    )
    n = await env.outbox_repo.purge_terminal("2026-01-01T00:00:00+00:00")
    assert n == 0
    rows = await env.db.fetchall(
        "SELECT status FROM federation_outbox WHERE id=?", (eid,)
    )
    assert rows[0]["status"] == "pending"


async def test_purge_terminal_respects_limit(env):
    """With more eligible rows than ``limit``, only ``limit`` are deleted."""
    for i in range(5):
        await _seed_terminal(
            env,
            f"f{i}",
            status="failed",
            stamp_col="failed_at",
            stamp="2000-01-01T00:00:00+00:00",
        )
    n = await env.outbox_repo.purge_terminal("2026-01-01T00:00:00+00:00", limit=3)
    assert n == 3
    remaining = await env.db.fetchall("SELECT id FROM federation_outbox")
    assert len(remaining) == 2


# ── §4.4.7 back-pressure: per-peer pending cap + droppable eviction ──────────


async def _seed_pending(env, eid, *, instance_id, event_type, created_at):
    """Insert a pending outbox row with a deterministic created_at so eviction
    order (oldest-first) doesn't rely on wall-clock."""
    await env.db.enqueue(
        "INSERT INTO federation_outbox(id, instance_id, event_type,"
        " payload_json, status, created_at) VALUES(?,?,?,?,'pending',?)",
        (eid, instance_id, event_type.value, "{}", created_at),
    )


async def _pending_ids(env, instance_id):
    rows = await env.db.fetchall(
        "SELECT id FROM federation_outbox WHERE instance_id=? AND status='pending'",
        (instance_id,),
    )
    return {r["id"] for r in rows}


async def test_evict_oldest_droppable_removes_oldest_ordinary(env):
    """With three ordinary pending rows, the oldest is evicted first."""
    await _seed_pending(
        env,
        "o1",
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await _seed_pending(
        env,
        "o2",
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        created_at="2026-01-02T00:00:00+00:00",
    )
    await _seed_pending(
        env,
        "o3",
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        created_at="2026-01-03T00:00:00+00:00",
    )
    assert await env.outbox_repo.evict_oldest_droppable("peer") is True
    assert await _pending_ids(env, "peer") == {"o2", "o3"}
    # A 2nd call evicts the next-oldest.
    assert await env.outbox_repo.evict_oldest_droppable("peer") is True
    assert await _pending_ids(env, "peer") == {"o3"}


async def test_evict_oldest_droppable_skips_all_never_drop(env):
    """With only NEVER_DROP pending rows, nothing is evicted."""
    await _seed_pending(
        env,
        "n1",
        instance_id="peer",
        event_type=FederationEventType.SPACE_MEMBER_BANNED,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await _seed_pending(
        env,
        "n2",
        instance_id="peer",
        event_type=FederationEventType.UNPAIR,
        created_at="2026-01-02T00:00:00+00:00",
    )
    assert await env.outbox_repo.evict_oldest_droppable("peer") is False
    assert await _pending_ids(env, "peer") == {"n1", "n2"}


async def test_evict_oldest_droppable_never_touches_never_drop(env):
    """In a mix, only the oldest *ordinary* row is evicted; NEVER_DROP survive
    even when they are older than the evicted ordinary row."""
    await _seed_pending(
        env,
        "n_old",
        instance_id="peer",
        event_type=FederationEventType.SPACE_MEMBER_BANNED,
        created_at="2026-01-01T00:00:00+00:00",
    )
    await _seed_pending(
        env,
        "o_mid",
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        created_at="2026-01-02T00:00:00+00:00",
    )
    await _seed_pending(
        env,
        "o_new",
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        created_at="2026-01-03T00:00:00+00:00",
    )
    assert await env.outbox_repo.evict_oldest_droppable("peer") is True
    # The older NEVER_DROP row is untouched; the oldest ordinary one is gone.
    assert await _pending_ids(env, "peer") == {"n_old", "o_new"}


async def test_evict_oldest_droppable_no_rows(env):
    """No pending rows ⇒ nothing evicted, returns False."""
    assert await env.outbox_repo.evict_oldest_droppable("peer") is False


async def test_enqueue_evicts_oldest_when_over_cap(env, monkeypatch):
    """At the per-peer cap, enqueue evicts the oldest droppable row and the
    newest is kept; count stays at the cap."""
    import socialhome.repositories.outbox_repo as outbox_mod

    monkeypatch.setattr(outbox_mod, "MAX_PENDING_PER_PEER", 3)

    ids = []
    for i in range(3):
        eid = await env.outbox_repo.enqueue(
            instance_id="peer",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"e{i}",
        )
        ids.append(eid)
        # Deterministic ascending created_at so e0 is the oldest.
        await env.db.enqueue(
            "UPDATE federation_outbox SET created_at=? WHERE id=?",
            (f"2026-01-0{i + 1}T00:00:00+00:00", eid),
        )
    assert await env.outbox_repo.count_pending_for("peer") == 3

    newest = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        msg_id="e_new",
    )
    # Stayed at the cap: oldest evicted, newest present.
    assert await env.outbox_repo.count_pending_for("peer") == 3
    pending = await _pending_ids(env, "peer")
    assert newest in pending
    assert "e0" not in pending  # oldest evicted


async def test_enqueue_cap_is_per_peer(env, monkeypatch):
    """A different peer's backlog is unaffected by another peer's cap."""
    import socialhome.repositories.outbox_repo as outbox_mod

    monkeypatch.setattr(outbox_mod, "MAX_PENDING_PER_PEER", 2)

    for i in range(2):
        await env.outbox_repo.enqueue(
            instance_id="peer-a",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"a{i}",
        )
    # peer-b is independent.
    await env.outbox_repo.enqueue(
        instance_id="peer-b",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        msg_id="b0",
    )
    # Over cap for peer-a evicts only from peer-a.
    await env.outbox_repo.enqueue(
        instance_id="peer-a",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        msg_id="a2",
    )
    assert await env.outbox_repo.count_pending_for("peer-a") == 2
    assert await env.outbox_repo.count_pending_for("peer-b") == 1


async def test_enqueue_never_drop_inserted_even_over_cap(env, monkeypatch):
    """A NEVER_DROP enqueue when the backlog is entirely NEVER_DROP is still
    inserted (count exceeds the cap rather than dropping a security event)."""
    import socialhome.repositories.outbox_repo as outbox_mod

    monkeypatch.setattr(outbox_mod, "MAX_PENDING_PER_PEER", 2)

    for i in range(2):
        await env.outbox_repo.enqueue(
            instance_id="peer",
            event_type=FederationEventType.SPACE_MEMBER_BANNED,
            payload_json="{}",
            msg_id=f"n{i}",
        )
    assert await env.outbox_repo.count_pending_for("peer") == 2

    nid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.UNPAIR,
        payload_json="{}",
        msg_id="n_new",
    )
    # Nothing droppable ⇒ the new NEVER_DROP row goes in over the cap.
    assert await env.outbox_repo.count_pending_for("peer") == 3
    assert nid in await _pending_ids(env, "peer")


async def test_enqueue_at_cap_with_ordinary_evicts_ordinary_for_never_drop(
    env, monkeypatch
):
    """A NEVER_DROP enqueue at cap with droppable rows present evicts the
    oldest ordinary row to make room (and is itself inserted)."""
    import socialhome.repositories.outbox_repo as outbox_mod

    monkeypatch.setattr(outbox_mod, "MAX_PENDING_PER_PEER", 2)

    for i in range(2):
        eid = await env.outbox_repo.enqueue(
            instance_id="peer",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"o{i}",
        )
        await env.db.enqueue(
            "UPDATE federation_outbox SET created_at=? WHERE id=?",
            (f"2026-01-0{i + 1}T00:00:00+00:00", eid),
        )
    nid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_DISSOLVED,
        payload_json="{}",
        msg_id="n_new",
    )
    assert await env.outbox_repo.count_pending_for("peer") == 2
    pending = await _pending_ids(env, "peer")
    assert "o0" not in pending  # oldest ordinary evicted
    assert nid in pending


# ─── count_failed_for ──────────────────────────────────────────────────────


async def test_count_failed_for_is_zero_when_empty(env):
    """No rows at all ⇒ 0, not None."""
    assert await env.outbox_repo.count_failed_for("peer") == 0


async def test_count_failed_for_counts_only_failed_rows(env):
    """Only ``status='failed'`` rows count — pending is a different number.

    ``failed`` means permanently given up on (PERMANENT rejection or
    MAX_ATTEMPTS exhausted); those envelopes are never retried, which is
    exactly why the admin surface reports them apart from the backlog.
    """
    for i in range(3):
        await env.outbox_repo.enqueue(
            instance_id="peer",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"f{i}",
        )
    await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
        msg_id="p0",
    )
    for i in range(3):
        await env.outbox_repo.mark_failed(f"f{i}")

    assert await env.outbox_repo.count_failed_for("peer") == 3
    assert await env.outbox_repo.count_pending_for("peer") == 1


async def test_count_failed_for_is_per_instance(env):
    """Another peer's failures are not this peer's."""
    for iid in ("peer-a", "peer-b"):
        eid = await env.outbox_repo.enqueue(
            instance_id=iid,
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id=f"{iid}-1",
        )
        await env.outbox_repo.mark_failed(eid)
    await env.outbox_repo.mark_failed(
        await env.outbox_repo.enqueue(
            instance_id="peer-a",
            event_type=FederationEventType.SPACE_POST_CREATED,
            payload_json="{}",
            msg_id="peer-a-2",
        )
    )

    assert await env.outbox_repo.count_failed_for("peer-a") == 2
    assert await env.outbox_repo.count_failed_for("peer-b") == 1


async def test_count_failed_for_ignores_delivered_rows(env):
    """A delivered row is DELETEd, so it can never inflate the failed count."""
    eid = await env.outbox_repo.enqueue(
        instance_id="peer",
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload_json="{}",
    )
    await env.outbox_repo.mark_delivered(eid)
    assert await env.outbox_repo.count_failed_for("peer") == 0
