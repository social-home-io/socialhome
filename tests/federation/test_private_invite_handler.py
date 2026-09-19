"""Inbound coverage for :class:`PrivateSpaceInviteHandler`.

Complements :mod:`test_private_invite_zero_leak` (outbound) by
exercising every inbound event type: invite received, accept, decline,
member removed, plus the missing-field skip branches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from socialhome.domain.space import RemoteAdminOutcome, SpaceRole
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.infrastructure.event_bus import EventBus


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event) -> None:
        self.events.append(event)


def _event(event_type: str, payload: dict, *, from_instance: str = "peer-1"):
    # FederationEvent is a dataclass, but we only use .payload,
    # .from_instance, and .space_id attrs — a namespace is cheaper and
    # clearer. ``space_id`` mirrors the real envelope's routing field
    # (None here unless a test sets it via the payload).
    return SimpleNamespace(
        event_type=event_type,
        payload=payload,
        from_instance=from_instance,
        space_id=payload.get("space_id"),
    )


def _invitation(
    invitation_id,
    *,
    space_id="sp-a",
    remote_instance_id="peer-1",
    remote_user_id="u1",
    status="pending",
    expires_at=None,
):
    """A ``space_invitations`` row as ``get_invitation_by_token`` returns it.

    The accept path binds every one of these columns to the envelope now,
    so a test row that omits them is a test that cannot pass.
    """
    if expires_at is None:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    return {
        "id": invitation_id,
        "space_id": space_id,
        "remote_instance_id": remote_instance_id,
        "remote_user_id": remote_user_id,
        "status": status,
        "expires_at": expires_at,
    }


@pytest.fixture
def handler():
    bus = _RecordingBus()
    space_repo = AsyncMock()
    space_repo.save_remote_invitation = AsyncMock()
    space_repo.get_invitation_by_token = AsyncMock()
    space_repo.update_invitation_status = AsyncMock()
    # No pre-existing local space row by default (the normal first-invite
    # case) so the §D1b anti-hijack guard (can_seat_remote_stub) allows the
    # stub. Tests that exercise a collision override this per-test.
    space_repo.get = AsyncMock(return_value=None)
    remote_members = AsyncMock()
    remote_members.add = AsyncMock()
    remote_members.remove = AsyncMock()
    # No live seat for the accepting household by default — the accept path
    # refuses to re-seat a household that already holds one with a
    # different role.
    remote_members.get = AsyncMock(return_value=None)
    cover_repo = AsyncMock()
    cover_repo.set = AsyncMock()
    icon_repo = AsyncMock()
    icon_repo.set = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
        cover_repo=cover_repo,
        icon_repo=icon_repo,
    )
    return SimpleNamespace(
        h=h,
        bus=bus,
        space_repo=space_repo,
        remote_members=remote_members,
        cover_repo=cover_repo,
        icon_repo=icon_repo,
    )


async def test_invite_happy_path(handler):
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp1",
            "invite_token": "tkn",
            "invitee_user_id": "u1",
            "inviter_user_id": "u2",
            "space_display_hint": "Board",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_awaited_once()
    assert any(isinstance(e, RemoteSpaceInviteReceived) for e in handler.bus.events)


async def test_invite_missing_fields_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE", {})
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()
    assert handler.bus.events == []


async def test_accept_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_ACCEPT", {"invite_token": "x"})
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_accept_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = _invitation(42)
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {
            "invite_token": "abc",
            "invitee_user_id": "u1",
            "invitee_public_key": "pk",
            "invitee_display_name": "Bob",
        },
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_awaited_once()
    handler.space_repo.update_invitation_status.assert_awaited_with(42, "accepted")
    assert any(isinstance(e, RemoteSpaceInviteAccepted) for e in handler.bus.events)


async def test_accept_broadcasts_member_joined_gossip(handler):
    """v_23: seating an accepting peer on the host side broadcasts a
    SPACE_MEMBER_JOINED roster gossip to every member household so their
    rosters converge — delegated to SpaceService when wired."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(7)
    space_service = AsyncMock()
    handler.h.attach_space_service(space_service)
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {
            "invite_token": "abc",
            "invitee_user_id": "u1",
            "invitee_public_key": "pk",
            "invitee_display_name": "Bob",
        },
    )
    await handler.h._on_accept(ev)
    space_service.broadcast_remote_member_joined.assert_awaited_once()
    kw = space_service.broadcast_remote_member_joined.await_args.kwargs
    assert kw["instance_id"] == "peer-1"
    assert kw["user_id"] == "u1"


async def test_accept_without_space_service_still_seats(handler):
    """No SpaceService wired (early boot / unit stack) → accept still seats
    the member; the gossip is just skipped."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(8)
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_awaited_once()


async def test_decline_no_token_noops(handler):
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_unknown_token_noops(handler):
    handler.space_repo.get_invitation_by_token.return_value = None
    ev = _event("SPACE_PRIVATE_INVITE_DECLINE", {"invite_token": "nope"})
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_decline_happy_path(handler):
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        5, space_id="sp-b"
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_DECLINE",
        {"invite_token": "tk", "invitee_user_id": "u1"},
    )
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_awaited_with(5, "declined")
    assert any(isinstance(e, RemoteSpaceInviteDeclined) for e in handler.bus.events)


async def test_member_removed_missing_fields_noops(handler):
    ev = _event("SPACE_REMOTE_MEMBER_REMOVED", {})
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_not_awaited()


async def test_member_removed_happy_path(handler):
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-c", "user_id": "u-bye"},
    )
    await handler.h._on_member_removed(ev)
    handler.remote_members.remove.assert_awaited_once_with(
        "sp-c",
        "peer-1",
        "u-bye",
    )
    assert any(isinstance(e, RemoteSpaceMemberRemoved) for e in handler.bus.events)


async def test_joined_gossip_registers_broadcast_target(handler):
    """A JOINED roster-gossip event registers the member's household in
    ``space_instances`` so this household's broadcast_to_space_members can
    reach a member it learned ONLY via gossip (no direct invite/accept).

    Regression: without add_space_instance the gossip-learned member was
    absent from space_instances and the offline-of-owner fan-out skipped them.
    """
    payload = {
        "space_id": "sp-gossip",
        "user_id": "u-new",
        "instance_id": "inst-new",
        "member_version": 3,
    }
    ev = _event("SPACE_MEMBER_JOINED", payload, from_instance="inst-new")
    # Isolate the Fix-B behaviour: bypass the signature/space-key verify leg.
    with patch.object(
        PrivateSpaceInviteHandler,
        "_verify_roster_gossip",
        AsyncMock(return_value=("sp-gossip", payload)),
    ):
        await handler.h._on_space_member_joined(ev)

    handler.remote_members.apply_member_event.assert_awaited_once()
    handler.space_repo.add_space_instance.assert_awaited_once_with(
        "sp-gossip", "inst-new"
    )


async def test_left_gossip_does_not_register_broadcast_target(handler):
    """A LEFT (tombstoned) roster-gossip event still merges the CRDT but must
    NOT add the household to space_instances (matches the not-tombstoned gate
    in the fix)."""
    payload = {
        "space_id": "sp-gossip",
        "user_id": "u-bye",
        "instance_id": "inst-bye",
        "member_version": 4,
    }
    ev = _event("SPACE_MEMBER_LEFT", payload, from_instance="inst-bye")
    with patch.object(
        PrivateSpaceInviteHandler,
        "_verify_roster_gossip",
        AsyncMock(return_value=("sp-gossip", payload)),
    ):
        await handler.h._on_space_member_left(ev)

    handler.remote_members.apply_member_event.assert_awaited_once()
    handler.space_repo.add_space_instance.assert_not_awaited()


async def test_subscriber_gossip_is_mirrored_as_a_follower_seat(handler):
    """A ``subscriber`` role on the wire is a cross-household Follower
    (v_30) and is mirrored verbatim — ``space_remote_members.role``
    admits it since migration 0054."""
    payload = {
        "space_id": "sp-gossip",
        "user_id": "u-fan",
        "instance_id": "inst-fan",
        "role": "subscriber",
        "member_version": 5,
    }
    ev = _event("SPACE_MEMBER_JOINED", payload, from_instance="inst-fan")
    with patch.object(
        PrivateSpaceInviteHandler,
        "_verify_roster_gossip",
        AsyncMock(return_value=("sp-gossip", payload)),
    ):
        await handler.h._on_space_member_joined(ev)

    assert (
        handler.remote_members.apply_member_event.await_args.kwargs["role"]
        == "subscriber"
    )


async def test_out_of_vocabulary_gossip_role_keeps_the_mutation(handler):
    """A role this household's CHECK rejects — ``owner`` (the host ships
    the raw ``space_members.role``), or one from a future version — used
    to raise IntegrityError out of ``apply_member_event`` and take the
    WHOLE event down with it, tombstone included; the version guard then
    refused the retry at the same ``member_version``, so a removal lost
    this way was unhealable. Coerce to the least-privileged real seat and
    keep the removal: losing "which seat" is recoverable from the next
    snapshot, losing "they left" is not."""
    payload = {
        "space_id": "sp-gossip",
        "user_id": "u-boss",
        "instance_id": "inst-boss",
        "role": "owner",
        "member_version": 6,
    }
    ev = _event("SPACE_MEMBER_LEFT", payload, from_instance="inst-boss")
    with patch.object(
        PrivateSpaceInviteHandler,
        "_verify_roster_gossip",
        AsyncMock(return_value=("sp-gossip", payload)),
    ):
        await handler.h._on_space_member_left(ev)

    kwargs = handler.remote_members.apply_member_event.await_args.kwargs
    assert kwargs["role"] == "member"
    assert kwargs["tombstoned"] is True


async def test_invite_with_roster_seats_remote_members_for_each_peer(handler):
    """#115 — the ``space_meta`` blob carries a ``roster`` of every
    member already in the space. The joiner's instance writes each
    of them into ``space_remote_members`` so her local Members tab
    shows the full household-spanning roster (not just herself).
    The invitee's *own* user_id is skipped — that comes via
    ``space_members`` when she accepts."""
    handler.remote_members.add = AsyncMock()
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "roster": [
                    {
                        "user_id": "u-pascal",
                        "instance_id": "peer-1",
                        "display_name": "Pascal",
                        "role": "owner",
                    },
                    {
                        "user_id": "u-anna",
                        "instance_id": "peer-1",
                        "display_name": "Anna",
                        "role": "member",
                    },
                    {
                        "user_id": "u-self",
                        "instance_id": "peer-2",
                        "display_name": "Me",
                        "role": "member",
                    },
                ],
            },
        },
    )
    await handler.h._on_invite(ev)
    # Two roster entries (Pascal + Anna) get seated as remote
    # members; the invitee's own row is skipped.
    assert handler.remote_members.add.await_count == 2
    seated_ids = {
        call.kwargs["user_id"] for call in handler.remote_members.add.await_args_list
    }
    assert seated_ids == {"u-pascal", "u-anna"}
    # The snapshot's role is mirrored, not a blanket ``member``: seating
    # a Follower as a full member on every OTHER household in the space
    # is what made those households accept its writes. ``owner`` has no
    # remote row shape, so it coerces DOWN to ``member``.
    roles = {
        call.kwargs["user_id"]: call.kwargs["role"]
        for call in handler.remote_members.add.await_args_list
    }
    assert roles == {"u-pascal": "member", "u-anna": "member"}


async def test_invite_roster_mirrors_a_follower_as_a_follower(handler):
    """A ``subscriber`` entry stays a subscriber in the mirror — that row
    is what the receiving household's §24.11 space-writer gate reads to
    refuse the follower's writes."""
    handler.remote_members.add = AsyncMock()
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "roster": [
                    {
                        "user_id": "u-follower",
                        "instance_id": "peer-9",
                        "display_name": "Fran",
                        "role": "subscriber",
                    },
                    {
                        "user_id": "u-future",
                        "instance_id": "peer-9",
                        "display_name": "Future",
                        "role": "overlord",
                    },
                ],
            },
        },
    )
    await handler.h._on_invite(ev)
    roles = {
        call.kwargs["user_id"]: call.kwargs["role"]
        for call in handler.remote_members.add.await_args_list
    }
    # An out-of-vocabulary role coerces down to the least-privileged real
    # seat rather than raising out of the whole apply.
    assert roles == {"u-follower": "subscriber", "u-future": "member"}


async def test_invite_with_cover_bytes_writes_to_cover_repo(handler):
    """#116 — when ``space_meta`` carries ``cover_webp_base64`` we
    persist the bytes via the cover repo so the stub renders the
    host's real cover image instead of the gradient placeholder.
    Without ``cover_hash`` the write is skipped (defensive)."""
    import base64

    fake_webp = b"RIFF\x00\x00\x00\x00WEBPVP8L"
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-cover",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "cover_hash": "deadbeef",
                "cover_webp_base64": base64.b64encode(fake_webp).decode("ascii"),
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.cover_repo.set.assert_awaited_once()
    call = handler.cover_repo.set.call_args
    assert call.args[0] == "sp-cover"
    assert call.kwargs["bytes_webp"] == fake_webp
    assert call.kwargs["hash"] == "deadbeef"


async def test_invite_with_icon_bytes_writes_to_icon_repo(handler):
    """Icon federation — ``space_meta.icon_webp_base64`` persists to the
    icon repo so the joiner's stub shows the host's real avatar instead of
    the emoji fallback. Mirrors the cover-bytes path."""
    import base64

    fake_webp = b"RIFF\x00\x00\x00\x00WEBPVP8L-icon"
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-icon",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "icon_hash": "cafef00d",
                "icon_webp_base64": base64.b64encode(fake_webp).decode("ascii"),
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.icon_repo.set.assert_awaited_once()
    call = handler.icon_repo.set.call_args
    assert call.args[0] == "sp-icon"
    assert call.kwargs["bytes_webp"] == fake_webp
    assert call.kwargs["hash"] == "cafef00d"


async def test_invite_skips_cover_write_when_no_bytes(handler):
    """Older senders that don't ship cover bytes leave the cover
    repo untouched. The stub still gets seated; the SPA falls back
    to the gradient placeholder."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-no-cover",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_meta": {
                "name": "Family",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc",
                "cover_hash": "deadbeef",
                # no cover_webp_base64 — cover repo stays untouched.
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.cover_repo.set.assert_not_awaited()


async def test_invite_with_space_meta_seats_local_stub(handler):
    """B2: inbound SPACE_PRIVATE_INVITE carrying ``space_meta`` seats a
    local stub row so accept can immediately insert the joiner's
    ``space_members`` row pointing at it."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_display_hint": "Family",
            "space_meta": {
                "name": "Family",
                "emoji": "🏡",
                "owner_instance_id": "peer-1",
                "owner_username": "pascal",
                "identity_public_key": "abc123",
                "config_sequence": 1,
                "space_type": "private",
                "join_mode": "invite_only",
                "features": {"calendar": True, "gallery": True},
                "tz": "Europe/Berlin",
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save.assert_awaited_once()
    saved = handler.space_repo.save.call_args.args[0]
    assert saved.id == "sp-remote"
    assert saved.name == "Family"
    assert saved.emoji == "🏡"
    assert saved.owner_instance_id == "peer-1"
    assert saved.owner_username == "pascal"
    assert saved.features.calendar is True
    assert saved.features.gallery is True


async def test_invite_refuses_stub_when_space_owned_by_another_host(handler):
    """§D1b anti-hijack — if we already hold this space_id under a DIFFERENT
    host, a SPACE_PRIVATE_INVITE for it must NOT overwrite our row. The
    guard compares the authenticated sender (from_instance='peer-1') against
    the existing owner ('the-real-host'); a spoofed meta.owner_instance_id
    that matches the existing owner does NOT help the attacker."""
    handler.space_repo.get = AsyncMock(
        return_value=SimpleNamespace(owner_instance_id="the-real-host"),
    )
    ev = _event(  # from_instance defaults to 'peer-1' (the malicious inviter)
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-collide",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-evil",
            "space_meta": {
                "name": "Spoofed",
                "owner_instance_id": "the-real-host",  # spoofed to match
                "owner_username": "x",
                "identity_public_key": "abc",
            },
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save.assert_not_awaited()


async def test_invite_without_space_meta_skips_stub_creation(handler):
    """Pre-B2 senders ship no ``space_meta``; we MUST stay quiet
    rather than synthesise garbage into the stub. The receiver still
    sees the invitation banner — only the local-stub seat is skipped
    until the issuer upgrades."""
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp1",
            "invite_token": "tkn",
            "invitee_user_id": "u-self",
            "inviter_user_id": "u-pascal",
            "space_display_hint": "Family",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save.assert_not_awaited()


async def test_member_removed_clears_local_stub_when_last_member_leaves(handler):
    """B2: when the host kicks the joiner from a remote space and
    they were the only local ``space_members`` row, we mark the stub
    dissolved so the SPA stops listing it. Locally-owned spaces
    (different owner_instance_id) are NEVER dissolved on a remote
    SPACE_REMOTE_MEMBER_REMOVED."""
    handler.space_repo.delete_member = AsyncMock()
    handler.space_repo.list_members = AsyncMock(return_value=[])
    stub_space = SimpleNamespace(owner_instance_id="peer-1")
    handler.space_repo.get = AsyncMock(return_value=stub_space)
    handler.space_repo.mark_dissolved = AsyncMock()
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-remote", "user_id": "u-self"},
    )
    await handler.h._on_member_removed(ev)
    handler.space_repo.delete_member.assert_awaited_once_with(
        "sp-remote",
        "u-self",
    )
    handler.space_repo.mark_dissolved.assert_awaited_once_with("sp-remote")


async def test_member_removed_keeps_locally_owned_space(handler):
    """If the row we keep for this space_id is *ours* (owner_instance_id
    matches our own), the SPACE_REMOTE_MEMBER_REMOVED event is for one
    of OUR remote members on someone else's instance. We must NOT
    dissolve our own space row in that case."""
    handler.space_repo.delete_member = AsyncMock()
    handler.space_repo.list_members = AsyncMock(return_value=[])
    own_space = SimpleNamespace(owner_instance_id="us")
    handler.space_repo.get = AsyncMock(return_value=own_space)
    handler.space_repo.mark_dissolved = AsyncMock()
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-ours", "user_id": "u-they"},
        from_instance="peer-1",
    )
    await handler.h._on_member_removed(ev)
    handler.space_repo.mark_dissolved.assert_not_awaited()


async def test_remote_admin_kick_dispatches_to_space_service():
    """Cross-household admin kick (#114 phase 2): handler decodes the
    payload and forwards to ``SpaceService.apply_remote_admin_kick``.
    The actual role check + dispatch lives in the service so this
    test only verifies the plumbing."""
    bus = _RecordingBus()
    space_repo = AsyncMock()
    remote_members = AsyncMock()
    space_service = AsyncMock()
    space_service.apply_remote_admin_kick = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_KICK",
        {
            "space_id": "sp-kick",
            "actor_user_id": "u-admin-on-A",
            "actor_instance_id": "instance-A",
            "target_user_id": "u-target",
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_kick(ev)
    space_service.apply_remote_admin_kick.assert_awaited_once_with(
        "sp-kick",
        actor_instance_id="instance-A",
        actor_user_id="u-admin-on-A",
        target_user_id="u-target",
    )


async def test_remote_admin_kick_without_space_service_drops():
    """If no SpaceService is wired (test stacks, early boot), the
    handler logs a warning and drops the event rather than crashing."""
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_KICK",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "actor_instance_id": "i",
            "target_user_id": "t",
        },
    )
    # Should not raise.
    await h._on_remote_admin_kick(ev)


async def test_remote_admin_kick_missing_fields_skipped():
    space_service = AsyncMock()
    space_service.apply_remote_admin_kick = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event("SPACE_REMOTE_ADMIN_KICK", {"space_id": "sp"})
    await h._on_remote_admin_kick(ev)
    space_service.apply_remote_admin_kick.assert_not_awaited()


async def test_remote_admin_action_dispatches_to_space_service():
    """Generic cross-household admin action (v_15): handler decodes the
    payload and forwards to ``SpaceService.apply_remote_admin_action``."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp-cfg",
            "actor_user_id": "u-admin",
            "actor_instance_id": "instance-A",
            "action": "update_config",
            "params": {"name": "Renamed"},
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    space_service.apply_remote_admin_action.assert_awaited_once_with(
        "sp-cfg",
        actor_instance_id="instance-A",
        actor_user_id="u-admin",
        action="update_config",
        params={"name": "Renamed"},
    )


async def test_remote_admin_action_ignores_forged_actor_instance():
    """SECURITY: a payload-supplied actor_instance_id must NOT override the
    signed envelope's from_instance — otherwise a confirmed peer could
    impersonate another household's admin. The service must be called with
    the signer (from_instance), not the forged claim."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "victim-admin",
            "actor_instance_id": "instance-VICTIM-ADMIN",  # forged
            "action": "archive",
            "params": {},
        },
        from_instance="instance-MALLORY",  # the actual (signed) sender
    )
    await h._on_remote_admin_action(ev)
    kwargs = space_service.apply_remote_admin_action.call_args.kwargs
    assert kwargs["actor_instance_id"] == "instance-MALLORY"


async def test_remote_admin_kick_ignores_forged_actor_instance():
    """SECURITY: same binding for the kick — from_instance wins over a
    payload-supplied actor_instance_id."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_kick = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_KICK",
        {
            "space_id": "sp",
            "actor_user_id": "victim-admin",
            "actor_instance_id": "instance-VICTIM-ADMIN",  # forged
            "target_user_id": "u-target",
        },
        from_instance="instance-MALLORY",
    )
    await h._on_remote_admin_kick(ev)
    kwargs = space_service.apply_remote_admin_kick.call_args.kwargs
    assert kwargs["actor_instance_id"] == "instance-MALLORY"


async def test_remote_admin_action_missing_action_skipped():
    """No ``action`` field → drop without calling the service."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {"space_id": "sp", "actor_user_id": "u", "actor_instance_id": "i"},
    )
    await h._on_remote_admin_action(ev)
    space_service.apply_remote_admin_action.assert_not_awaited()


async def test_remote_admin_action_without_space_service_drops():
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "actor_instance_id": "i",
            "action": "archive",
        },
    )
    await h._on_remote_admin_action(ev)  # Should not raise.


async def test_remote_admin_action_non_dict_params_coerced():
    """A malformed (non-dict) ``params`` becomes an empty dict so the
    service never sees a non-mapping."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "actor_instance_id": "i",
            "action": "archive",
            "params": "not-a-dict",
        },
    )
    await h._on_remote_admin_action(ev)
    assert space_service.apply_remote_admin_action.call_args.kwargs["params"] == {}


async def test_remote_admin_action_needs_owner_approval_enqueues():
    """Delegation OFF: ``apply_remote_admin_action`` returns
    NEEDS_OWNER_APPROVAL → the handler records a pending owner approval,
    binding the actor to the *signed* envelope (from_instance)."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock(
        return_value=RemoteAdminOutcome.NEEDS_OWNER_APPROVAL,
    )
    approval_service = AsyncMock()
    approval_service.enqueue_owner_approval = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    h.attach_approval_service(approval_service)
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp-cfg",
            "actor_user_id": "u-admin",
            "actor_instance_id": "instance-FORGED",  # ignored
            "action": "update_config",
            "params": {"name": "Renamed"},
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    approval_service.enqueue_owner_approval.assert_awaited_once_with(
        "sp-cfg",
        actor_instance="instance-A",
        actor_user="u-admin",
        fwd_action="update_config",
        fwd_params={"name": "Renamed"},
    )


async def test_remote_admin_action_executed_does_not_enqueue():
    """Delegation ON: EXECUTED → no owner approval recorded."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock(
        return_value=RemoteAdminOutcome.EXECUTED,
    )
    approval_service = AsyncMock()
    approval_service.enqueue_owner_approval = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    h.attach_approval_service(approval_service)
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "action": "archive",
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    approval_service.enqueue_owner_approval.assert_not_awaited()


async def test_remote_admin_action_dropped_does_not_enqueue():
    """A DROPPED action (validation failed inside the service) must NOT
    create a pending proposal — the actor-is-admin check front-runs."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock(
        return_value=RemoteAdminOutcome.DROPPED,
    )
    approval_service = AsyncMock()
    approval_service.enqueue_owner_approval = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    h.attach_approval_service(approval_service)
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "action": "archive",
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    approval_service.enqueue_owner_approval.assert_not_awaited()


async def test_remote_admin_action_needs_approval_without_approval_service():
    """NEEDS_OWNER_APPROVAL but no approval_service wired → log + return,
    no crash and nothing enqueued."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock(
        return_value=RemoteAdminOutcome.NEEDS_OWNER_APPROVAL,
    )
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    assert h._approval_service is None
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "action": "archive",
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)  # Should not raise.


async def test_remote_admin_action_propose_still_routes_to_approval():
    """Regression: the ``propose`` verb still routes to the approval
    service and never touches apply_remote_admin_action."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    approval_service = AsyncMock()
    approval_service.apply_remote_propose = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    h.attach_approval_service(approval_service)
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "action": "propose",
            "params": {"action": "dissolve", "params": {}},
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    approval_service.apply_remote_propose.assert_awaited_once()
    space_service.apply_remote_admin_action.assert_not_awaited()


async def test_remote_admin_action_vote_still_routes_to_approval():
    """Regression: the ``vote`` verb still routes to the approval service."""
    space_service = AsyncMock()
    space_service.apply_remote_admin_action = AsyncMock()
    approval_service = AsyncMock()
    approval_service.apply_remote_vote = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_service=space_service,
    )
    h.attach_approval_service(approval_service)
    ev = _event(
        "SPACE_REMOTE_ADMIN_ACTION",
        {
            "space_id": "sp",
            "actor_user_id": "u",
            "action": "vote",
            "params": {"proposal_id": "p1", "vote": "approve"},
        },
        from_instance="instance-A",
    )
    await h._on_remote_admin_action(ev)
    approval_service.apply_remote_vote.assert_awaited_once()
    space_service.apply_remote_admin_action.assert_not_awaited()


async def test_attach_space_service_wires_post_construction():
    """The wiring helper sets the slot used by the kick handler."""
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
    )
    assert h._space_service is None
    sentinel = object()
    h.attach_space_service(sentinel)
    assert h._space_service is sentinel


async def test_space_location_updated_upserts_when_sender_is_a_member():
    """Inbound SPACE_LOCATION_UPDATED from a confirmed remote member
    persists into ``space_remote_member_locations`` so the space map
    surfaces the pin."""
    from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember

    locations = AsyncMock()
    locations.upsert = AsyncMock()
    remote_members = AsyncMock()
    remote_members.get = AsyncMock(
        return_value=SpaceRemoteMember(
            space_id="sp-map",
            instance_id="peer-jq",
            user_id="uid-jacqueline",
        ),
    )
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=remote_members,
        remote_location_repo=locations,
    )
    ev = _event(
        "SPACE_LOCATION_UPDATED",
        {
            "space_id": "sp-map",
            "user_id": "uid-jacqueline",
            "mode": "gps",
            "lat": 48.1351,
            "lon": 11.5820,
            "accuracy_m": 12.0,
        },
        from_instance="peer-jq",
    )
    await h._on_space_location_updated(ev)
    locations.upsert.assert_awaited_once()
    loc = locations.upsert.call_args.args[0]
    assert loc.space_id == "sp-map"
    assert loc.instance_id == "peer-jq"
    assert loc.user_id == "uid-jacqueline"
    assert loc.mode == "gps"
    assert loc.latitude == 48.1351
    assert loc.longitude == 11.5820


async def test_space_location_updated_drops_when_sender_not_a_member():
    """A spoofed SPACE_LOCATION_UPDATED from a household whose user
    isn't in ``space_remote_members`` MUST NOT persist."""
    locations = AsyncMock()
    locations.upsert = AsyncMock()
    remote_members = AsyncMock()
    remote_members.get = AsyncMock(return_value=None)  # not a member
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=remote_members,
        remote_location_repo=locations,
    )
    ev = _event(
        "SPACE_LOCATION_UPDATED",
        {
            "space_id": "sp",
            "user_id": "uid-stranger",
            "mode": "gps",
            "lat": 0,
            "lon": 0,
        },
        from_instance="peer-rogue",
    )
    await h._on_space_location_updated(ev)
    locations.upsert.assert_not_awaited()


async def test_space_location_updated_skips_invalid_mode():
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        remote_location_repo=AsyncMock(),
    )
    ev = _event(
        "SPACE_LOCATION_UPDATED",
        {"space_id": "sp", "user_id": "u", "mode": "garbage"},
    )
    await h._on_space_location_updated(ev)


async def test_the_duplicate_role_change_handler_is_gone():
    """F2 — ``EventDispatchRegistry.dispatch`` runs EVERY handler bound to
    an event type, so an unguarded duplicate is not shadowed by its
    guarded sibling: it applies the mutation the sibling refused. This
    family's only ``SPACE_MEMBER_ROLE_CHANGED`` handler is the
    host-authority-gated one in
    :class:`FederationInboundService`."""
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
    )
    assert not hasattr(h, "_on_role_changed")


async def test_key_exchange_rekey_imports_new_epoch_key():
    """Host rotates the space epoch on a member kick and ships the
    new key via SPACE_KEY_EXCHANGE_REKEY (#121). Receiver must
    persist via the same ``apply_space_content_key_from_metadata``
    helper the §D1b accept path uses — re-wrap under local KEK so
    future SPACE_POST_CREATED inbounds decrypt with the new key."""
    bus = _RecordingBus()
    space_repo = AsyncMock()
    # Owner back-compat path: the §24.11-authenticated from_instance ("peer-1",
    # the _event default) IS the space owner, so an UNSIGNED rekey applies.
    space_repo.get = AsyncMock(
        return_value=SimpleNamespace(
            owner_instance_id="peer-1", identity_public_key="00" * 32
        )
    )
    remote_members = AsyncMock()
    space_crypto = AsyncMock()
    space_crypto.import_key = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
        space_crypto_service=space_crypto,
    )
    # 32 bytes, base64-encoded — matches what
    # ``export_current_key`` would have shipped on the host side.
    import base64

    new_key = bytes(range(32))
    ev = _event(
        "SPACE_KEY_EXCHANGE_REKEY",
        {
            "space_id": "sp-rekey",
            "space_content_key": {
                "epoch": 7,
                "key_suite": "aesgcm-256",
                "key_base64": base64.b64encode(new_key).decode("ascii"),
            },
        },
    )
    await h._on_key_exchange_rekey(ev)
    space_crypto.import_key.assert_awaited_once_with(
        "sp-rekey", 7, new_key, rotated_by=None
    )


async def test_key_exchange_rekey_threads_rotated_by_through():
    """Phase 4b: the rekey handler passes the minting household's id
    (``rotated_by``) through to ``import_key`` so the receiver's
    collision-safe tiebreak can converge two concurrent rotations."""
    import base64
    from unittest.mock import AsyncMock

    space_repo = AsyncMock()
    # Owner back-compat path (from_instance "peer-1" == owner): an unsigned
    # rekey with a non-empty rotated_by applies and threads the minter through.
    space_repo.get = AsyncMock(
        return_value=SimpleNamespace(
            owner_instance_id="peer-1", identity_public_key="00" * 32
        )
    )
    space_crypto = AsyncMock()
    space_crypto.import_key = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=AsyncMock(),
        space_crypto_service=space_crypto,
    )
    new_key = bytes(range(32))
    ev = _event(
        "SPACE_KEY_EXCHANGE_REKEY",
        {
            "space_id": "sp-rekey",
            "space_content_key": {
                "epoch": 4,
                "key_suite": "aesgcm-256",
                "key_base64": base64.b64encode(new_key).decode("ascii"),
                "rotated_by": "inst-minter-7",
            },
        },
    )
    await h._on_key_exchange_rekey(ev)
    space_crypto.import_key.assert_awaited_once_with(
        "sp-rekey", 4, new_key, rotated_by="inst-minter-7"
    )


async def test_key_exchange_rekey_missing_space_id_skipped():
    space_crypto = AsyncMock()
    space_crypto.import_key = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=AsyncMock(),
        space_crypto_service=space_crypto,
    )
    ev = _event("SPACE_KEY_EXCHANGE_REKEY", {})
    await h._on_key_exchange_rekey(ev)
    space_crypto.import_key.assert_not_awaited()


async def test_attach_to_registers_handlers():
    """`attach_to` wires every event-type → handler binding the
    private-invite family handles. New rekey handler added in #121 for
    forward-secrecy on member kick."""
    from socialhome.domain.federation import FederationEventType

    bus = EventBus()
    space_repo = AsyncMock()
    remote_members = AsyncMock()
    h = PrivateSpaceInviteHandler(
        bus=bus,  # type: ignore[arg-type]
        space_repo=space_repo,
        remote_member_repo=remote_members,
    )

    class _FakeRegistry:
        def __init__(self) -> None:
            self.bindings: dict = {}

        def register(self, event_type, handler):
            self.bindings[event_type] = handler

    class _FakeFedSvc:
        def __init__(self) -> None:
            self._event_registry = _FakeRegistry()

    fed = _FakeFedSvc()
    h.attach_to(fed)  # type: ignore[arg-type]
    assert set(fed._event_registry.bindings.keys()) == {
        FederationEventType.SPACE_PRIVATE_INVITE,
        FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
        FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
        FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_REMOTE_ADMIN_KICK,
        FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
        FederationEventType.SPACE_ADMIN_PROPOSAL_UPDATED,
        FederationEventType.SPACE_LOCATION_UPDATED,
        FederationEventType.SPACE_ADMIN_KEY_SHARE,
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_MEMBER_LEFT,
        FederationEventType.SPACE_SESSION_CLEANUP,
    }


# ── #648: the host's identity key rides the sealed invite ─────────────


def _host_identity() -> tuple[str, str]:
    """Return ``(instance_id, identity_pk_hex)`` for a real keypair."""
    kp = generate_identity_keypair()
    return derive_instance_id(kp.public_key), kp.public_key.hex()


def _invite_with(host_pk, *, space_id="sp-mesh"):
    return {
        "space_id": space_id,
        "invite_token": "tkn",
        "invitee_user_id": "u1",
        "inviter_user_id": "u2",
        "space_display_hint": "Board",
        "host_identity_pk": host_pk,
        # A stub is only seated when space_meta is present, and the key is
        # recorded alongside it.
        "space_meta": {"name": "Board"},
    }


async def test_invite_records_host_identity_pk_when_it_derives_to_sender(handler):
    """The host's key is stored so mesh-routed sync chunks can be verified.

    A mesh-joined member has no ``remote_instances`` row for the host, so
    without this the §25.6 receiver drops every chunk (#648).
    """
    host_id, host_pk = _host_identity()
    ev = _event("SPACE_PRIVATE_INVITE", _invite_with(host_pk), from_instance=host_id)

    await handler.h._on_invite(ev)

    handler.space_repo.set_host_identity_pk.assert_awaited_once_with("sp-mesh", host_pk)


async def test_invite_refuses_host_identity_pk_that_derives_elsewhere(handler):
    """A key that isn't the sender's own is refused.

    An instance id IS the hash of its identity key, so a sender can only
    ever assert its own real key — anything else is a bug or an attempt to
    bind another household's identity to this space. Same binding v_21
    uses for ``target_eph_pk``.
    """
    _host_id, someone_elses_pk = _host_identity()
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        _invite_with(someone_elses_pk),
        from_instance="an-unrelated-instance-id",
    )

    await handler.h._on_invite(ev)

    handler.space_repo.set_host_identity_pk.assert_not_awaited()


async def test_invite_without_host_identity_pk_is_unchanged(handler):
    """An older sender ships no key — previous behaviour, no write."""
    host_id, _pk = _host_identity()
    payload = _invite_with(None)
    payload.pop("host_identity_pk")
    ev = _event("SPACE_PRIVATE_INVITE", payload, from_instance=host_id)

    await handler.h._on_invite(ev)

    handler.space_repo.set_host_identity_pk.assert_not_awaited()


@pytest.mark.parametrize(
    "bad",
    [
        "nothex!!",
        "aabb",  # right charset, wrong length
        1234,  # not a string at all
    ],
)
async def test_invite_refuses_malformed_host_identity_pk(handler, bad):
    """Malformed input is dropped, not stored and not raised."""
    host_id, _pk = _host_identity()
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        _invite_with(bad),
        from_instance=host_id,
    )

    await handler.h._on_invite(ev)

    handler.space_repo.set_host_identity_pk.assert_not_awaited()


# ── Roster authority: who may send us what (F1 / F6 / F8) ─────────────


async def test_invite_for_a_space_we_host_is_refused(handler):
    """F1 — ``SPACE_PRIVATE_INVITE`` is the HOST inviting one of our users.
    An invitation naming a space *we* host is a household writing itself a
    ticket it can redeem a moment later with
    ``SPACE_PRIVATE_INVITE_ACCEPT`` — which seats it, overwriting whatever
    read-only seat it was actually given."""
    handler.space_repo.get = AsyncMock(
        return_value=SimpleNamespace(owner_instance_id="us"),
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-ours",
            "invite_token": "forged",
            "invitee_user_id": "u-attacker",
            "inviter_user_id": "u-attacker",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_not_awaited()
    assert handler.bus.events == []


async def test_invite_for_a_stub_from_a_third_party_is_refused(handler):
    """Same rule seen from a member household: only the space's own host
    may invite into it."""
    handler.space_repo.get = AsyncMock(
        return_value=SimpleNamespace(owner_instance_id="the-real-host"),
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "forged",
            "invitee_user_id": "u1",
            "inviter_user_id": "u2",
        },
        from_instance="peer-rogue",
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_not_awaited()


async def test_invite_from_the_host_of_a_known_stub_is_accepted(handler):
    """A re-invite for a space we already hold a stub of is the normal
    case (a second user of ours invited later) and stays green."""
    handler.space_repo.get = AsyncMock(
        return_value=SimpleNamespace(owner_instance_id="peer-1"),
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE",
        {
            "space_id": "sp-remote",
            "invite_token": "tkn2",
            "invitee_user_id": "u-second",
            "inviter_user_id": "u-host",
        },
    )
    await handler.h._on_invite(ev)
    handler.space_repo.save_remote_invitation.assert_awaited_once()


async def test_accept_from_a_household_other_than_the_invitee_is_refused(handler):
    """F1 — ``get_invitation_by_token`` matches on the token alone, so the
    accept has to be bound to the household the invitation was addressed
    to. Otherwise anyone who learns a token seats themselves."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        1, remote_instance_id="peer-invitee"
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
        from_instance="peer-rogue",
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_accept_with_a_mismatched_invitee_user_is_refused(handler):
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        2, remote_user_id="u-invited"
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u-somebody-else"},
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_accept_of_an_already_used_invitation_is_refused(handler):
    """One invitation, one seat. Replaying an accepted token is how a
    household that was later demoted walks its old seat back in."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        3, status="accepted"
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_a_late_accept_of_a_pending_invitation_still_seats(handler):
    """``space_invitations.expires_at`` is a 15-minute TTL inherited from
    the invite token, but the invitee's pending list has no expiry filter
    — every real invite outlives it. Refusing on that column would
    silently drop the accept of an invitation the UI is still offering,
    so the one-shot ``status`` is the guard, not the clock."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        4,
        expires_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_awaited_once()


async def test_accept_seats_the_member_role_explicitly(handler):
    """``add`` defaults to ``member``; the accept path says so out loud so
    a future default change can't silently promote through this door."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(9)
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
    )
    await handler.h._on_accept(ev)
    assert (
        handler.remote_members.add.await_args.kwargs["role"] == SpaceRole.MEMBER.value
    )


async def test_accept_never_raises_an_existing_follower_seat(handler):
    """F1's payload: ``add``'s ``ON CONFLICT … DO UPDATE SET role`` turns a
    re-seat into a promotion. A household that already holds a live
    ``subscriber`` seat cannot climb out of it through an accept — the
    role change is the host's decision and rides
    ``SPACE_MEMBER_ROLE_CHANGED``."""
    from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember

    handler.space_repo.get_invitation_by_token.return_value = _invitation(10)
    handler.remote_members.get = AsyncMock(
        return_value=SpaceRemoteMember(
            space_id="sp-a",
            instance_id="peer-1",
            user_id="u1",
            role=SpaceRole.SUBSCRIBER.value,
        ),
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_ACCEPT",
        {"invite_token": "abc", "invitee_user_id": "u1"},
    )
    await handler.h._on_accept(ev)
    handler.remote_members.add.assert_not_awaited()


async def test_decline_from_another_household_is_refused(handler):
    """A token is not a capability: a third party must not be able to
    cancel somebody else's pending invitation."""
    handler.space_repo.get_invitation_by_token.return_value = _invitation(
        11, remote_instance_id="peer-invitee"
    )
    ev = _event(
        "SPACE_PRIVATE_INVITE_DECLINE",
        {"invite_token": "tk", "invitee_user_id": "u1"},
        from_instance="peer-rogue",
    )
    await handler.h._on_decline(ev)
    handler.space_repo.update_invitation_status.assert_not_awaited()


async def test_member_removed_does_not_delete_a_local_member_of_a_space_we_host(
    handler,
):
    """F8 — the ``space_remote_members`` half of this handler is scoped to
    the sender's own household; the ``space_members`` half was not, so any
    peer could delete any of OUR users from a space WE host."""
    handler.space_repo.delete_member = AsyncMock()
    handler.space_repo.list_members = AsyncMock(return_value=[])
    handler.space_repo.get = AsyncMock(
        return_value=SimpleNamespace(owner_instance_id="us"),
    )
    ev = _event(
        "SPACE_REMOTE_MEMBER_REMOVED",
        {"space_id": "sp-ours", "user_id": "u-victim"},
        from_instance="peer-rogue",
    )
    await handler.h._on_member_removed(ev)
    handler.space_repo.delete_member.assert_not_awaited()
    # The sender's OWN seat is still dropped — that half was always safe.
    handler.remote_members.remove.assert_awaited_once_with(
        "sp-ours", "peer-rogue", "u-victim"
    )


async def _location_handler(role=SpaceRole.MEMBER.value, *, seat=True):
    from socialhome.repositories.space_remote_member_repo import SpaceRemoteMember

    locations = AsyncMock()
    locations.upsert = AsyncMock()
    remote_members = AsyncMock()
    remote_members.get = AsyncMock(
        return_value=(
            SpaceRemoteMember(
                space_id="sp-a",
                instance_id="peer-1",
                user_id="u1",
                role=role,
            )
            if seat
            else None
        ),
    )
    h = PrivateSpaceInviteHandler(
        bus=_RecordingBus(),  # type: ignore[arg-type]
        space_repo=AsyncMock(),
        remote_member_repo=remote_members,
        remote_location_repo=locations,
    )
    return h, locations, remote_members


async def test_space_location_updated_refuses_a_subscriber_seat():
    """F6 — the seat lookup had no role filter, so a read-only household
    wrote pins onto every member's space map."""
    h, locations, _ = await _location_handler(SpaceRole.SUBSCRIBER.value)
    ev = _event(
        "SPACE_LOCATION_UPDATED",
        {"space_id": "sp-a", "user_id": "u1", "mode": "gps", "lat": 1.0, "lon": 2.0},
    )
    await h._on_space_location_updated(ev)
    locations.upsert.assert_not_awaited()


async def test_space_location_updated_pins_the_routing_space_id():
    """F6 — the handler resolved ``payload["space_id"] or event.space_id``,
    the inverse of the §24.11 writer gate's precedence. Routing ``A`` plus
    payload ``B`` therefore passed the gate on the space the sender may
    write and landed the row in the space it may not."""
    h, locations, _ = await _location_handler()
    ev = SimpleNamespace(
        event_type="SPACE_LOCATION_UPDATED",
        from_instance="peer-1",
        space_id="sp-a",
        payload={
            "space_id": "sp-somewhere-else",
            "user_id": "u1",
            "mode": "gps",
            "lat": 1.0,
            "lon": 2.0,
        },
    )
    await h._on_space_location_updated(ev)
    locations.upsert.assert_not_awaited()


async def test_space_location_updated_accepts_a_matching_pair():
    """Both fields present and equal is what every shipping sender emits."""
    h, locations, _ = await _location_handler()
    ev = _event(
        "SPACE_LOCATION_UPDATED",
        {"space_id": "sp-a", "user_id": "u1", "mode": "gps", "lat": 1.0, "lon": 2.0},
    )
    await h._on_space_location_updated(ev)
    locations.upsert.assert_awaited_once()
