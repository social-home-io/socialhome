"""Zero-leak cross-household invites for private spaces (§D1b).

Three inbound event types + one outbound:

* ``SPACE_PRIVATE_INVITE`` — host → invitee's household. The plaintext
  envelope carries ONLY routing fields; space metadata (space_id,
  display hint, inviter, invite_token) lives entirely in the encrypted
  payload. §25.8.21 compliant.
* ``SPACE_PRIVATE_INVITE_ACCEPT`` — invitee → host.
* ``SPACE_PRIVATE_INVITE_DECLINE`` — invitee → host.
* ``SPACE_REMOTE_MEMBER_REMOVED`` — host → former invitee's household
  when a remote member is removed from the space.

The handler persists the invitation on receive so the UI can surface
accept / decline buttons, and on accept wires the invitee as a
:class:`SpaceRemoteMember` so the host's subsequent
``SPACE_POST_CREATED`` fan-outs include them in the recipient list.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    RemoteSpaceInviteAccepted,
    RemoteSpaceInviteDeclined,
    RemoteSpaceInviteReceived,
    RemoteSpaceMemberRemoved,
)
from ..domain.federation import FederationEventType
from ..infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from .federation_service import FederationService

log = logging.getLogger(__name__)


class PrivateSpaceInviteHandler:
    """Inbound dispatcher for the :data:`SPACE_PRIVATE_INVITE*` family."""

    __slots__ = ("_bus", "_space_repo", "_remote_members")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._remote_members = remote_member_repo

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry  # noqa: SLF001
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE,
            self._on_invite,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            self._on_accept,
        )
        registry.register(
            FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
            self._on_decline,
        )
        registry.register(
            FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            self._on_member_removed,
        )

    # ── Receive ─────────────────────────────────────────────────────────

    async def _on_invite(self, event: "FederationEvent") -> None:
        """A peer invited one of our users to their private space."""
        p = event.payload
        # All fields are in the encrypted payload — envelope plaintext
        # is strictly routing metadata. §25.8.21.
        space_id = str(p.get("space_id") or "")
        invite_token = str(p.get("invite_token") or "")
        invitee_user_id = str(p.get("invitee_user_id") or "")
        if not space_id or not invite_token or not invitee_user_id:
            log.debug(
                "SPACE_PRIVATE_INVITE from %s missing required fields",
                event.from_instance,
            )
            return
        inviter_user_id = str(p.get("inviter_user_id") or "")
        display_hint = p.get("space_display_hint")
        await self._space_repo.save_remote_invitation(
            space_id=space_id,
            invited_by=inviter_user_id,
            remote_instance_id=event.from_instance,
            remote_user_id=invitee_user_id,
            invite_token=invite_token,
            space_display_hint=(str(display_hint) if display_hint else None),
        )
        await self._bus.publish(
            RemoteSpaceInviteReceived(
                space_id=space_id,
                inviter_user_id=inviter_user_id,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_accept(self, event: "FederationEvent") -> None:
        """Our peer accepted the invite we sent — seat them as a
        :class:`SpaceRemoteMember`."""
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            log.debug(
                "SPACE_PRIVATE_INVITE_ACCEPT: unknown token from %s",
                event.from_instance,
            )
            return
        invitee_user_id = str(p.get("invitee_user_id") or "")
        invitee_pk = p.get("invitee_public_key")
        invitee_display = p.get("invitee_display_name")
        await self._remote_members.add(
            space_id=invite["space_id"],
            instance_id=event.from_instance,
            user_id=invitee_user_id,
            user_pk=str(invitee_pk) if invitee_pk else None,
            display_name=str(invitee_display) if invitee_display else None,
        )
        await self._space_repo.update_invitation_status(
            invite["id"],
            "accepted",
        )
        await self._bus.publish(
            RemoteSpaceInviteAccepted(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=invitee_user_id,
            )
        )

    async def _on_decline(self, event: "FederationEvent") -> None:
        p = event.payload
        token = str(p.get("invite_token") or "")
        if not token:
            return
        invite = await self._space_repo.get_invitation_by_token(token)
        if invite is None:
            return
        await self._space_repo.update_invitation_status(
            invite["id"],
            "declined",
        )
        await self._bus.publish(
            RemoteSpaceInviteDeclined(
                space_id=invite["space_id"],
                instance_id=event.from_instance,
                invitee_user_id=str(p.get("invitee_user_id") or ""),
            )
        )

    async def _on_member_removed(self, event: "FederationEvent") -> None:
        """The host removed us from a private space. Stop expecting new
        frames for that ``(space_id, user_id)`` pair."""
        p = event.payload
        space_id = str(p.get("space_id") or "")
        user_id = str(p.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._remote_members.remove(
            space_id,
            event.from_instance,
            user_id,
        )
        await self._bus.publish(
            RemoteSpaceMemberRemoved(
                space_id=space_id,
                instance_id=event.from_instance,
                user_id=user_id,
            )
        )
