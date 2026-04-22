"""Inbound federation handlers for space invitations + join requests (§11.2).

Covers the two workflow families:

* **Invitations** — a space admin invites a user on another instance.
  ``SPACE_INVITE`` / ``SPACE_INVITE_VIA`` / ``SPACE_ACCEPT``.
* **Join requests** — a user on another instance wants to join a
  local space. ``SPACE_JOIN_REQUEST`` + status events.

Each handler persists the invite/request row via the space repo so
the admin UI sees pending work. Also emits a local
:class:`DomainEvent` so notifications fire.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.events import (
    RemoteJoinRequestApproved,
    RemoteJoinRequestDenied,
    RemoteSpaceInviteReceived,
    RemoteSpaceJoinRequestReceived,
)
from ...domain.federation import FederationEventType
from ...infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class SpaceInviteInboundHandlers:
    """Register invitation + join-request inbound handlers."""

    __slots__ = ("_bus", "_space_repo")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry
        registry.register(FederationEventType.SPACE_INVITE, self._on_invite)
        registry.register(FederationEventType.SPACE_INVITE_VIA, self._on_invite_via)

        registry.register(FederationEventType.SPACE_JOIN_REQUEST, self._on_join_request)
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_VIA,
            self._on_join_request_via,
        )
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_REPLY_VIA,
            self._on_join_request_status,
        )
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
            self._on_join_request_status,
        )
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_DENIED,
            self._on_join_request_status,
        )
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_EXPIRED,
            self._on_join_request_status,
        )
        registry.register(
            FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN,
            self._on_join_request_status,
        )

    # ─── Invitations ────────────────────────────────────────────────────

    async def _on_invite(self, event: "FederationEvent") -> None:
        """A peer invited a local user to their space."""
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        invitee = str(event.payload.get("invitee_user_id") or "")
        inviter = str(event.payload.get("inviter_user_id") or "")
        if not space_id or not invitee or not inviter:
            log.debug("SPACE_INVITE missing required field")
            return
        # Persist so the admin UI can accept or decline.
        await self._space_repo.save_invitation(
            space_id=space_id,
            invited_user_id=invitee,
            invited_by=inviter,
        )
        await self._bus.publish(
            RemoteSpaceInviteReceived(
                space_id=space_id,
                inviter_user_id=inviter,
                invitee_user_id=invitee,
            )
        )

    async def _on_invite_via(self, event: "FederationEvent") -> None:
        """Relayed invite via an intermediary. Same persistence as _on_invite."""
        await self._on_invite(event)

    # ─── Join requests ──────────────────────────────────────────────────

    async def _on_join_request(self, event: "FederationEvent") -> None:
        """A remote user wants to join a local space."""
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            log.debug("SPACE_JOIN_REQUEST missing required field")
            return
        message = event.payload.get("message")
        applicant_pk = event.payload.get("applicant_pk")
        request_id = event.payload.get("request_id")
        await self._space_repo.save_join_request(
            space_id=space_id,
            user_id=user_id,
            message=str(message) if message else None,
            remote_applicant_instance_id=event.from_instance,
            remote_applicant_pk=(str(applicant_pk) if applicant_pk else None),
            request_id=str(request_id) if request_id else None,
        )
        await self._bus.publish(
            RemoteSpaceJoinRequestReceived(
                space_id=space_id,
                requester_user_id=user_id,
            )
        )

    async def _on_join_request_via(self, event: "FederationEvent") -> None:
        """Relayed join request via an intermediary."""
        await self._on_join_request(event)

    async def _on_join_request_status(self, event: "FederationEvent") -> None:
        """SPACE_JOIN_REQUEST_APPROVED / DENIED / EXPIRED / WITHDRAWN /
        REPLY_VIA. For REPLY_VIA the body carries the final status as a
        payload field so the intermediary can forward.

        On APPROVED for cross-household (§D2) requests the payload
        carries an ``invite_token``; we dispatch a domain event so the
        applicant-side service can auto-consume + seat the user as a
        member without requiring the applicant to click anything.
        """
        request_id = str(event.payload.get("request_id") or "")
        if not request_id:
            return
        status_map = {
            FederationEventType.SPACE_JOIN_REQUEST_APPROVED: "approved",
            FederationEventType.SPACE_JOIN_REQUEST_DENIED: "denied",
            FederationEventType.SPACE_JOIN_REQUEST_EXPIRED: "expired",
            FederationEventType.SPACE_JOIN_REQUEST_WITHDRAWN: "withdrawn",
            FederationEventType.SPACE_JOIN_REQUEST_REPLY_VIA: str(
                event.payload.get("status") or "expired",
            ),
        }
        status = status_map.get(event.event_type, "expired")
        reviewed_by = event.payload.get("reviewed_by")
        await self._space_repo.update_join_request_status(
            request_id,
            status,
            reviewed_by=str(reviewed_by) if reviewed_by else None,
        )
        if (
            event.event_type == FederationEventType.SPACE_JOIN_REQUEST_APPROVED
            and event.payload.get("invite_token")
        ):
            await self._bus.publish(
                RemoteJoinRequestApproved(
                    request_id=request_id,
                    space_id=str(
                        event.payload.get("space_id") or event.space_id or "",
                    ),
                    invite_token=str(event.payload.get("invite_token")),
                )
            )
        elif event.event_type == FederationEventType.SPACE_JOIN_REQUEST_DENIED:
            await self._bus.publish(
                RemoteJoinRequestDenied(
                    request_id=request_id,
                    space_id=str(
                        event.payload.get("space_id") or event.space_id or "",
                    ),
                )
            )
