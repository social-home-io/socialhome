"""ProfileSyncService — re-register on UserProfileUpdated bus events.

Verifies that:

* A profile update for a registered user triggers
  ``MomentPublicService.push_profile_to_gfs(...)`` once per active
  registration.
* Updates for users with no public-Momentum opt-in are no-ops.
* Per-GFS push errors are caught + logged, never surfaced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from socialhome.domain.child_protection import (
    AccountProtectedError,
    ProtectedCapability,
)
from socialhome.domain.events import UserProfileUpdated
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.moment_public_repo import (
    SqliteMomentPublicRegistrationRepo,
)
from socialhome.services.moment_public_service import MomentPublicError
from socialhome.services.profile_sync_service import ProfileSyncService


def _event(user_id: str = "u1") -> UserProfileUpdated:
    return UserProfileUpdated(
        user_id=user_id,
        username="alice",
        display_name="Alice Z",
        bio="updated",
        picture_hash="hashv2",
        picture_webp=None,
        occurred_at=datetime(2026, 5, 6, 12, tzinfo=timezone.utc),
    )


async def _seed_two_gfses(db, user_id: str = "u1") -> None:
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES(?, 'alice', 'Alice', 'active')",
        (user_id,),
    )
    for gfs in ("g1", "g2"):
        await db.enqueue(
            "INSERT INTO gfs_connections("
            "id, gfs_instance_id, display_name, public_key, inbox_url, "
            "status, paired_at) "
            "VALUES(?, ?, ?, ?, ?, 'active', datetime('now'))",
            (
                gfs,
                f"gfs-{gfs}",
                f"GFS {gfs}",
                "aa" * 32,
                f"https://{gfs}.example",
            ),
        )


async def test_no_registrations_is_a_noop(db):
    repo = SqliteMomentPublicRegistrationRepo(db)
    bus = EventBus()
    public = AsyncMock()
    sync = ProfileSyncService(bus=bus, registration_repo=repo, public_service=public)
    sync.wire()

    await bus.publish(_event())

    public.push_profile_to_gfs.assert_not_called()


async def test_re_registers_on_every_active_gfs(db):
    await _seed_two_gfses(db)
    repo = SqliteMomentPublicRegistrationRepo(db)
    await repo.upsert(user_id="u1", gfs_id="g1")
    await repo.upsert(user_id="u1", gfs_id="g2")

    bus = EventBus()
    public = AsyncMock()
    sync = ProfileSyncService(bus=bus, registration_repo=repo, public_service=public)
    sync.wire()

    await bus.publish(_event())

    assert public.push_profile_to_gfs.await_count == 2
    sent_gfses = {
        call.kwargs["gfs_id"] for call in public.push_profile_to_gfs.await_args_list
    }
    assert sent_gfses == {"g1", "g2"}


async def test_per_gfs_failure_does_not_block_others(db):
    await _seed_two_gfses(db)
    repo = SqliteMomentPublicRegistrationRepo(db)
    await repo.upsert(user_id="u1", gfs_id="g1")
    await repo.upsert(user_id="u1", gfs_id="g2")

    bus = EventBus()
    public = AsyncMock()
    public.push_profile_to_gfs.side_effect = [
        MomentPublicError("g1 down"),
        None,
    ]
    sync = ProfileSyncService(bus=bus, registration_repo=repo, public_service=public)
    sync.wire()

    # Should NOT raise — service swallows MomentPublicError.
    await bus.publish(_event())
    assert public.push_profile_to_gfs.await_count == 2


async def test_only_pushes_for_event_user(db):
    await _seed_two_gfses(db)
    repo = SqliteMomentPublicRegistrationRepo(db)
    await db.enqueue(
        "INSERT INTO users(user_id, username, display_name, state) "
        "VALUES('u-other','bob','Bob','active')"
    )
    await repo.upsert(user_id="u-other", gfs_id="g1")
    # u1 has no registrations.
    bus = EventBus()
    public = AsyncMock()
    sync = ProfileSyncService(bus=bus, registration_repo=repo, public_service=public)
    sync.wire()

    await bus.publish(_event(user_id="u1"))

    public.push_profile_to_gfs.assert_not_called()


async def test_protected_account_is_not_pushed_and_stops_quietly(db):
    """§CP.R: an earlier registration no longer refreshes the public
    directory — the gate's refusal is expected, not a per-GFS failure."""
    await _seed_two_gfses(db)
    repo = SqliteMomentPublicRegistrationRepo(db)
    await repo.upsert(user_id="u1", gfs_id="g1")
    await repo.upsert(user_id="u1", gfs_id="g2")
    bus = EventBus()
    public = AsyncMock()
    public.push_profile_to_gfs.side_effect = AccountProtectedError(
        ProtectedCapability.PUBLIC_MOMENTS
    )
    sync = ProfileSyncService(bus=bus, registration_repo=repo, public_service=public)
    sync.wire()

    await bus.publish(_event())
    # Stops after the first refusal instead of retrying every GFS.
    assert public.push_profile_to_gfs.await_count == 1
