"""Person and household bans of a space (P7b).

A person ban names a ``user_id``; a household ban names an ``instance_id``
and covers every person of that household, members and followers alike.
Banning a household ends each of its live seats the way a removal does
(tell, tombstone, gossip) and rotates the content key ONCE.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from socialhome.authority_sig import authority_payload_verified
from socialhome.domain.events import SpaceAdminAuthorityRevoked
from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.space import (
    HostTooOldError,
    SpacePermissionError,
    SpaceRole,
)
from socialhome.services.space_service import SpaceService
from tests.services.test_space_service_follow import (
    PEER,
    U_FAN,
    U_OTHER,
    _hosted,
    _seat_remote,
)
from tests.services.test_space_service_follow import stack as _follow_stack

#: The follow tests' household stack (real SQLite, mocked federation).
stack = _follow_stack


def _removed_sends(stack) -> list[dict]:
    return [
        c.kwargs
        for c in stack.fed.send_with_mesh_fallback.await_args_list
        if c.kwargs.get("event_type") == FederationEventType.SPACE_REMOTE_MEMBER_REMOVED
    ]


async def test_banning_a_household_ends_every_seat_and_rotates_once(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.MEMBER)
    await _seat_remote(stack, space.id, U_OTHER, SpaceRole.SUBSCRIBER)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER, reason="spam"
        )
    rotate.assert_awaited_once_with(space.id)
    assert await stack.spaces.is_household_banned(space.id, PEER)
    assert (
        await stack.remote.list_for_instance(space.id, PEER, include_tombstoned=False)
        == []
    )
    assert PEER not in await stack.spaces.list_member_instances(space.id)
    # Each seat's household was told, naming the space.
    assert sorted(s["payload"]["user_id"] for s in _removed_sends(stack)) == sorted(
        [U_FAN, U_OTHER]
    )


async def test_a_household_ban_without_seats_still_lands(stack):
    """A pre-emptive ban (a household that never joined) writes the row and
    rotates nothing."""
    space = await _hosted(stack)
    with patch.object(
        SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
    ) as rotate:
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id="never-joined"
        )
    rotate.assert_not_awaited()
    assert await stack.spaces.is_household_banned(space.id, "never-joined")


async def test_banning_an_admin_household_retires_its_authority(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.ADMIN)
    revoked: list = []

    async def _rec(e):
        revoked.append(e)

    stack.bus.subscribe(SpaceAdminAuthorityRevoked, _rec)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", AsyncMock()):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    assert [(e.space_id, e.instance_id) for e in revoked] == [(space.id, PEER)]


async def test_the_owner_household_cannot_be_banned(stack):
    """Here we own the space: our household is the owner household."""
    space = await _hosted(stack)
    with pytest.raises(SpacePermissionError):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=stack.iid
        )
    assert not await stack.spaces.is_household_banned(space.id, stack.iid)


async def test_only_owner_or_admin_may_ban_a_household(stack):
    space = await _hosted(stack)
    mod = await stack.provision("mod")
    await stack.svc.add_member(
        space.id, actor_username="owner", user_id=mod.user_id, role="moderator"
    )
    with pytest.raises(SpacePermissionError):
        await stack.svc.ban_household(space.id, actor_username="mod", instance_id=PEER)
    assert not await stack.spaces.is_household_banned(space.id, PEER)


async def test_unbanning_a_household_lifts_the_row_and_reseats_nobody(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.MEMBER)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", AsyncMock()):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    await stack.svc.unban_household(space.id, actor_username="owner", instance_id=PEER)
    assert not await stack.spaces.is_household_banned(space.id, PEER)
    assert await stack.remote.get(space.id, PEER, U_FAN) is None


async def test_a_member_household_forwards_a_household_ban_to_the_host(stack):
    """Not the host and no seed: forwarded as the ``ban`` admin action
    naming the household — nothing written here."""
    space = await _hosted(stack)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )
    with patch.object(
        SpaceService,
        "_executes_locally_as_delegated_admin",
        AsyncMock(return_value=False),
    ):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    assert not await stack.spaces.is_household_banned(space.id, PEER)
    sent = stack.fed.send_with_mesh_fallback.await_args_list[-1].kwargs
    assert sent["to_instance_id"] == "elsewhere"
    assert sent["event_type"] == FederationEventType.SPACE_REMOTE_ADMIN_ACTION
    assert sent["payload"]["action"] == "ban"
    assert sent["payload"]["params"]["instance_id"] == PEER


async def test_a_household_ban_is_not_forwarded_to_a_host_too_old(stack):
    space = await _hosted(stack)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )

    async def _supports(_peer, *, min_version):
        return min_version < FederationCapability.MIN_FOR_SUBSCRIBER_SEATS

    stack.fed.peer_supports = AsyncMock(side_effect=_supports)
    with (
        patch.object(
            SpaceService,
            "_executes_locally_as_delegated_admin",
            AsyncMock(return_value=False),
        ),
        pytest.raises(HostTooOldError),
    ):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    stack.fed.send_with_mesh_fallback.assert_not_awaited()


async def test_the_host_runs_a_forwarded_household_ban(stack):
    space = await _hosted(stack)
    with patch.object(SpaceService, "ban_household", AsyncMock()) as ban:
        await stack.svc.apply_approved_admin_action(
            space.id, action="ban", params={"instance_id": "bad-house", "reason": "r"}
        )
    ban.assert_awaited_once()
    assert ban.await_args.kwargs["instance_id"] == "bad-house"
    assert ban.await_args.kwargs["reason"] == "r"


# ── The ban list reaches every member household, signed (P7b) ───────────


def _ban_broadcasts(stack) -> list[tuple]:
    return [
        (c.args[1], c.args[2], c.kwargs)
        for c in stack.fed.broadcast_to_space_members.await_args_list
        if c.args[1]
        in (
            FederationEventType.SPACE_MEMBER_BANNED,
            FederationEventType.SPACE_MEMBER_UNBANNED,
        )
    ]


async def _signed_by_space(stack, space_id, event_type, payload) -> bool:
    space = await stack.spaces.get(space_id)
    return authority_payload_verified(
        event_type=event_type.value,
        space_id=space_id,
        payload=payload,
        space_public_key_hex=space.identity_public_key,
    )


async def test_a_household_ban_and_its_unban_are_signed_to_every_member(stack):
    space = await _hosted(stack)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", AsyncMock()):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER, reason="spam"
        )
        await stack.svc.unban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    [(t1, p1, kw1), (t2, p2, kw2)] = _ban_broadcasts(stack)
    assert t1 == FederationEventType.SPACE_MEMBER_BANNED
    assert p1["instance_id"] == PEER and "user_id" not in p1
    assert p1["reason"] == "spam"
    assert t2 == FederationEventType.SPACE_MEMBER_UNBANNED
    assert p2["instance_id"] == PEER
    for t, p in ((t1, p1), (t2, p2)):
        assert await _signed_by_space(stack, space.id, t, p)
    for kw in (kw1, kw2):
        assert kw["min_proto_version"] == FederationCapability.MIN_FOR_SUBSCRIBER_SEATS


async def test_a_person_ban_and_unban_are_signed_to_every_member(stack):
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.MEMBER)
    with patch.object(SpaceService, "_rotate_and_distribute_space_key", AsyncMock()):
        await stack.svc.ban(space.id, actor_username="owner", user_id=U_FAN)
        await stack.svc.unban(space.id, actor_username="owner", user_id=U_FAN)
    [(t1, p1, _), (t2, p2, _)] = _ban_broadcasts(stack)
    assert (t1, p1["user_id"]) == (FederationEventType.SPACE_MEMBER_BANNED, U_FAN)
    assert (t2, p2["user_id"]) == (FederationEventType.SPACE_MEMBER_UNBANNED, U_FAN)
    assert "instance_id" not in p1
    assert await _signed_by_space(stack, space.id, t1, p1)
    assert await _signed_by_space(stack, space.id, t2, p2)


async def test_a_forwarded_ban_sends_no_ban_event(stack):
    space = await _hosted(stack)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )
    with patch.object(
        SpaceService,
        "_executes_locally_as_delegated_admin",
        AsyncMock(return_value=False),
    ):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
        await stack.svc.ban(space.id, actor_username="owner", user_id=U_FAN)
    assert _ban_broadcasts(stack) == []


async def test_a_seed_holding_delegated_admin_bans_a_household_locally(stack):
    """Delegation on + the seed: the admin household runs the ban itself —
    nothing forwarded, seats ended, one rotation; the authority retirement
    is the owner's (it hears the LEFT gossip), not ours."""
    space = await _hosted(stack)
    await _seat_remote(stack, space.id, U_FAN, SpaceRole.ADMIN)
    await stack.db.enqueue(
        "UPDATE spaces SET owner_instance_id='elsewhere' WHERE id=?", (space.id,)
    )
    revoked: list = []

    async def _rec(e):
        revoked.append(e)

    stack.bus.subscribe(SpaceAdminAuthorityRevoked, _rec)
    with (
        patch.object(
            SpaceService,
            "_executes_locally_as_delegated_admin",
            AsyncMock(return_value=True),
        ),
        patch.object(
            SpaceService, "_rotate_and_distribute_space_key", AsyncMock()
        ) as rotate,
    ):
        await stack.svc.ban_household(
            space.id, actor_username="owner", instance_id=PEER
        )
    rotate.assert_awaited_once_with(space.id)
    assert await stack.spaces.is_household_banned(space.id, PEER)
    assert revoked == []
    assert not any(
        c.kwargs.get("event_type") == FederationEventType.SPACE_REMOTE_ADMIN_ACTION
        for c in stack.fed.send_with_mesh_fallback.await_args_list
    )


async def test_a_ban_reason_is_capped(stack):
    space = await _hosted(stack)
    await stack.svc.ban_household(
        space.id, actor_username="owner", instance_id="x-house", reason="r" * 2000
    )
    [row] = await stack.spaces.list_household_bans(space.id)
    assert len(row["reason"]) == 500
