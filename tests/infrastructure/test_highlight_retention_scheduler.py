"""Smoke tests for :class:`HighlightRetentionScheduler`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.highlight import HighlightAudience, HighlightFrameType
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.highlight_retention_scheduler import (
    HighlightRetentionScheduler,
)
from socialhome.repositories.highlight_repo import SqliteHighlightRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.highlight_service import HighlightService


async def _seed_user(db) -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state, "
        "preferences_json) VALUES('u1','pascal','Pascal','active','{}')",
    )


async def test_expire_due_drops_expired_rows(db):
    await _seed_user(db)
    bus = EventBus()
    repo = SqliteHighlightRepo(db)
    user_repo = SqliteUserRepo(db)
    service = HighlightService(repo, user_repo, bus)

    # Hand-craft a highlight with expires_at in the past so the prune fires.
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    expired = await repo.find_or_create_today(
        author_user_id="u1",
        audience_kind=HighlightAudience.ALL_PAIRED,
        audience=(),
        highlight_date="2026-01-01",
        expires_at=past,
    )
    await repo.append_frame(
        highlight_id=expired.id,
        frame_type=HighlightFrameType.IMAGE,
        media_url="/api/media/x.webp",
    )
    expired_count, _ = await service.expire_due()
    assert expired_count >= 1
    assert await repo.get_highlight(expired.id) is None


@pytest.mark.asyncio
async def test_scheduler_start_and_stop_are_idempotent(db):
    await _seed_user(db)
    bus = EventBus()
    service = HighlightService(SqliteHighlightRepo(db), SqliteUserRepo(db), bus)
    sched = HighlightRetentionScheduler(service, interval_seconds=3600)
    await sched.start()
    await sched.start()  # idempotent — should not error
    await sched.stop()
    await sched.stop()  # idempotent
