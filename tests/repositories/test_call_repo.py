"""Tests for social_home.repositories.call_repo."""

from __future__ import annotations

import pytest

from social_home.domain.call import CallQualitySample, CallSession
from social_home.repositories.call_repo import SqliteCallRepo


@pytest.fixture
async def repo(db):
    """A fresh :class:`SqliteCallRepo` + a conversation row for FKs."""
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) "
        "VALUES(?, 'dm', datetime('now'))",
        ("conv-1",),
    )
    await db.enqueue(
        "INSERT INTO conversations(id, type, created_at) "
        "VALUES(?, 'group_dm', datetime('now'))",
        ("conv-2",),
    )
    return SqliteCallRepo(db)


def _call(
    *,
    call_id: str = "c1",
    conversation_id: str = "conv-1",
    initiator: str = "u-alice",
    callee: str | None = "u-bob",
    call_type: str = "audio",
    status: str = "ringing",
    participants: tuple[str, ...] = (),
) -> CallSession:
    return CallSession(
        id=call_id,
        conversation_id=conversation_id,
        initiator_user_id=initiator,
        callee_user_id=callee,
        call_type=call_type,
        status=status,
        participant_user_ids=participants,
    )


# ── save + get ─────────────────────────────────────────────────────────


async def test_save_call_inserts_new_row(repo):
    saved = await repo.save_call(_call())
    assert saved.id == "c1"
    assert saved.status == "ringing"
    assert saved.started_at  # DB default populated on readback.


async def test_get_call_returns_saved_row(repo):
    await repo.save_call(_call())
    fetched = await repo.get_call("c1")
    assert fetched is not None
    assert fetched.initiator_user_id == "u-alice"
    assert fetched.callee_user_id == "u-bob"
    assert fetched.call_type == "audio"


async def test_get_call_unknown_is_none(repo):
    assert await repo.get_call("no-such") is None


async def test_save_call_upserts_on_conflict(repo):
    await repo.save_call(_call(status="ringing"))
    await repo.save_call(_call(status="active"))
    fetched = await repo.get_call("c1")
    assert fetched is not None
    assert fetched.status == "active"


async def test_save_call_persists_participants(repo):
    await repo.save_call(
        _call(
            call_id="grp",
            conversation_id="conv-2",
            callee=None,
            participants=("u-a", "u-b", "u-c"),
        )
    )
    fetched = await repo.get_call("grp")
    assert fetched is not None
    assert set(fetched.participant_user_ids) == {"u-a", "u-b", "u-c"}


# ── list_active / history ──────────────────────────────────────────────


async def test_list_active_matches_initiator(repo):
    await repo.save_call(_call(status="ringing"))
    rows = await repo.list_active(user_id="u-alice")
    assert len(rows) == 1
    assert rows[0].id == "c1"


async def test_list_active_matches_callee(repo):
    await repo.save_call(_call(status="active"))
    rows = await repo.list_active(user_id="u-bob")
    assert len(rows) == 1


async def test_list_active_matches_participant(repo):
    await repo.save_call(
        _call(
            call_id="grp",
            conversation_id="conv-2",
            callee=None,
            participants=("u-x", "u-y"),
            status="active",
        )
    )
    rows = await repo.list_active(user_id="u-y")
    assert [r.id for r in rows] == ["grp"]


async def test_list_active_excludes_ended(repo):
    await repo.save_call(_call(status="ended"))
    rows = await repo.list_active(user_id="u-alice")
    assert rows == []


async def test_history_orders_newest_first(repo):
    await repo.save_call(_call(call_id="old", status="ended"))
    await repo.save_call(_call(call_id="new", status="ended"))
    # Bump the newer one's started_at.
    await repo._db.enqueue(
        "UPDATE call_sessions SET started_at=? WHERE id=?",
        ("2099-01-01T00:00:00+00:00", "new"),
    )
    rows = await repo.list_history_for_conversation("conv-1")
    ids = [r.id for r in rows]
    assert ids[0] == "new"


async def test_history_respects_limit(repo):
    for i in range(5):
        await repo.save_call(_call(call_id=f"c{i}", status="ended"))
    rows = await repo.list_history_for_conversation("conv-1", limit=3)
    assert len(rows) == 3


# ── transition ─────────────────────────────────────────────────────────


async def test_transition_updates_status_and_lifecycle(repo):
    await repo.save_call(_call())
    updated = await repo.transition(
        "c1",
        status="active",
        connected_at="2026-01-01T12:00:00+00:00",
    )
    assert updated is not None
    assert updated.status == "active"
    assert updated.connected_at == "2026-01-01T12:00:00+00:00"


async def test_transition_preserves_unspecified_fields(repo):
    await repo.save_call(_call())
    await repo.transition(
        "c1", status="active", connected_at="2026-01-01T12:00:00+00:00"
    )
    updated = await repo.transition(
        "c1",
        status="ended",
        ended_at="2026-01-01T12:05:00+00:00",
        duration_seconds=300,
    )
    assert updated is not None
    assert updated.connected_at == "2026-01-01T12:00:00+00:00"
    assert updated.duration_seconds == 300


async def test_transition_updates_participants(repo):
    await repo.save_call(_call(participants=("u-a", "u-b")))
    updated = await repo.transition(
        "c1",
        status="active",
        participant_user_ids=("u-a", "u-b", "u-c"),
    )
    assert updated is not None
    assert set(updated.participant_user_ids) == {"u-a", "u-b", "u-c"}


async def test_transition_unknown_call_returns_none(repo):
    assert await repo.transition("nope", status="ended") is None


# ── end_stale_calls ────────────────────────────────────────────────────


async def test_end_stale_calls_marks_missed_and_returns_them(repo, db):
    await repo.save_call(_call())
    # Backdate to 200 s ago so it's past the 90 s TTL.
    await db.enqueue(
        "UPDATE call_sessions SET started_at=datetime('now','-200 seconds') WHERE id=?",
        ("c1",),
    )
    missed = await repo.end_stale_calls(older_than_seconds=90)
    assert [m.id for m in missed] == ["c1"]
    assert missed[0].status == "missed"
    assert missed[0].ended_at


async def test_end_stale_calls_ignores_fresh_ringing(repo):
    await repo.save_call(_call())
    missed = await repo.end_stale_calls(older_than_seconds=90)
    assert missed == []


async def test_end_stale_calls_ignores_non_ringing(repo, db):
    await repo.save_call(_call(status="active"))
    await db.enqueue(
        "UPDATE call_sessions SET started_at=datetime('now','-300 seconds') WHERE id=?",
        ("c1",),
    )
    missed = await repo.end_stale_calls(older_than_seconds=90)
    assert missed == []


# ── quality samples ────────────────────────────────────────────────────


async def test_save_and_list_quality_samples(repo):
    await repo.save_call(_call())
    await repo.save_quality_sample(
        CallQualitySample(
            call_id="c1",
            reporter_user_id="u-alice",
            sampled_at=1700000000,
            rtt_ms=50,
            loss_pct=0.1,
        )
    )
    await repo.save_quality_sample(
        CallQualitySample(
            call_id="c1",
            reporter_user_id="u-bob",
            sampled_at=1700000010,
            rtt_ms=60,
            jitter_ms=5,
            audio_bitrate=32000,
            video_bitrate=640000,
        )
    )
    samples = await repo.list_quality_samples("c1")
    assert len(samples) == 2
    assert samples[0].rtt_ms == 50
    assert samples[1].video_bitrate == 640000


async def test_list_quality_samples_empty_when_none(repo):
    await repo.save_call(_call())
    assert await repo.list_quality_samples("c1") == []


async def test_quality_samples_cascade_with_call(repo, db):
    await repo.save_call(_call())
    await repo.save_quality_sample(
        CallQualitySample(
            call_id="c1",
            reporter_user_id="u",
            sampled_at=1,
        )
    )
    await db.enqueue("DELETE FROM call_sessions WHERE id=?", ("c1",))
    assert await repo.list_quality_samples("c1") == []
