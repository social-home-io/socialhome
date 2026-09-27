"""Release-blocker protocol tests for the per-space member-profile binding.

Marked ``@pytest.mark.security`` — CLAUDE.md requires these to run before
every commit touching federation code.

``SPACE_MEMBER_PROFILE_UPDATED`` carries a member's per-space display name
and picture. A household may only update the profile of a member seated on
THAT household: the seat is looked up under the §24.11-authenticated
``from_instance``, never a payload field. These tests pin the handler
against the REAL SQLite repos — the mocks in the unit tests cannot catch a
call that doesn't match the repo's signature.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceRole,
    SpaceType,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.services.federation_inbound_service import (
    FederationInboundService,
)

pytestmark = pytest.mark.security

SPACE_ID = "sp-profile"
OWN = "this-household"
HOUSEHOLD_B = "household-b"
HOUSEHOLD_C = "household-c"
USER_B = "u-b"
USER_C = "u-c"
USER_LOCAL = "u-local"


@pytest.fixture
async def stack(tmp_dir):
    """Real space + remote-member repos, one member seated per household."""
    db = AsyncDatabase(tmp_dir / "profile.db", batch_timeout_ms=10)
    await db.startup()
    spaces = SqliteSpaceRepo(db)
    remote = SqliteSpaceRemoteMemberRepo(db)
    await spaces.save(
        Space(
            id=SPACE_ID,
            name="Shared",
            owner_instance_id=OWN,
            owner_username="anna",
            identity_public_key="00" * 32,
            config_sequence=0,
            features=SpaceFeatures(),
            space_type=SpaceType.PRIVATE,
            join_mode=JoinMode.INVITE_ONLY,
        )
    )
    for user_id in (USER_B, USER_C, USER_LOCAL):
        await spaces.save_member(
            SpaceMember(
                space_id=SPACE_ID,
                user_id=user_id,
                role=SpaceRole.MEMBER.value,
                joined_at="2026-01-01T00:00:00+00:00",
                space_display_name="original",
            )
        )
    for instance_id, user_id in ((HOUSEHOLD_B, USER_B), (HOUSEHOLD_C, USER_C)):
        await remote.add(
            space_id=SPACE_ID,
            instance_id=instance_id,
            user_id=user_id,
            user_pk=None,
            display_name=None,
        )
    svc = FederationInboundService(
        bus=EventBus(),
        conversation_repo=AsyncMock(),
        space_post_repo=AsyncMock(),
        space_repo=spaces,
        user_repo=AsyncMock(),
        space_remote_member_repo=remote,
    )
    svc._federation_service = SimpleNamespace(own_instance_id=OWN)  # type: ignore[assignment]
    yield SimpleNamespace(svc=svc, spaces=spaces, remote=remote)
    await db.shutdown()


def _update(user_id: str, *, sender: str, name: str = "renamed") -> FederationEvent:
    return FederationEvent(
        msg_id="m1",
        event_type=FederationEventType.SPACE_MEMBER_PROFILE_UPDATED,
        from_instance=sender,
        to_instance=OWN,
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload={"user_id": user_id, "space_display_name": name},
        space_id=SPACE_ID,
    )


async def _name(stack, user_id: str) -> str | None:
    member = await stack.spaces.get_member(SPACE_ID, user_id)
    return None if member is None else member.space_display_name


async def test_household_updates_its_own_member_profile(stack):
    await stack.svc._on_space_member_profile_updated(
        _update(USER_B, sender=HOUSEHOLD_B)
    )
    assert await _name(stack, USER_B) == "renamed"


async def test_household_cannot_update_a_member_of_another_household(stack):
    await stack.svc._on_space_member_profile_updated(
        _update(USER_B, sender=HOUSEHOLD_C)
    )
    assert await _name(stack, USER_B) == "original"


async def test_household_cannot_update_a_local_member(stack):
    await stack.svc._on_space_member_profile_updated(
        _update(USER_LOCAL, sender=HOUSEHOLD_B)
    )
    assert await _name(stack, USER_LOCAL) == "original"


async def test_removed_member_seat_is_not_accepted(stack):
    await stack.remote.remove(SPACE_ID, HOUSEHOLD_B, USER_B)
    await stack.svc._on_space_member_profile_updated(
        _update(USER_B, sender=HOUSEHOLD_B)
    )
    assert await _name(stack, USER_B) == "original"


async def test_unknown_user_is_a_noop(stack):
    await stack.svc._on_space_member_profile_updated(
        _update("u-nobody", sender=HOUSEHOLD_B)
    )
    assert await stack.spaces.get_member(SPACE_ID, "u-nobody") is None
    assert await _name(stack, USER_B) == "original"
