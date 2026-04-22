"""Inbound federation handlers for space membership events (§13).

Covers seven event types that affect the ``spaces`` / ``space_members``
/ ``space_bans`` / ``space_instances`` rows locally when a paired peer
changes membership state. Each handler persists the effect and
publishes a local domain event so the admin UI sees the change.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.events import (
    RemoteSpaceCreated,
    RemoteSpaceDissolved,
    RemoteSpaceMemberBanned,
)
from ...domain.federation import FederationEventType
from ...domain.space import (
    JoinMode,
    Space,
    SpaceFeatures,
    SpaceType,
)
from ...infrastructure.event_bus import EventBus

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class SpaceMembershipInboundHandlers:
    """Register the membership-family inbound handlers on one service."""

    __slots__ = ("_bus", "_space_repo", "_federation")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
    ) -> None:
        self._bus = bus
        self._space_repo = space_repo
        self._federation: "FederationService | None" = None

    def attach_to(self, federation_service: "FederationService") -> None:
        self._federation = federation_service
        registry = federation_service._event_registry
        registry.register(FederationEventType.SPACE_CREATED, self._on_created)
        registry.register(FederationEventType.SPACE_DISSOLVED, self._on_dissolved)
        registry.register(
            FederationEventType.SPACE_INSTANCE_LEFT, self._on_instance_left
        )
        registry.register(FederationEventType.SPACE_MEMBER_BANNED, self._on_banned)
        registry.register(FederationEventType.SPACE_MEMBER_UNBANNED, self._on_unbanned)
        registry.register(FederationEventType.SPACE_AGE_GATE_UPDATED, self._on_age_gate)
        registry.register(FederationEventType.SPACE_CONFIG_CATCH_UP, self._on_catch_up)

    # ─── Handlers ────────────────────────────────────────────────────────

    async def _on_created(self, event: "FederationEvent") -> None:
        """A paired peer created a new space — mirror the row locally."""
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        p = event.payload
        name = str(p.get("name") or space_id[:8])
        identity_pk = str(p.get("identity_public_key") or "")
        if not identity_pk:
            log.debug("SPACE_CREATED missing identity_public_key")
            return
        try:
            space_type = SpaceType(str(p.get("space_type") or "private"))
        except ValueError:
            space_type = SpaceType.PRIVATE
        try:
            join_mode = JoinMode(str(p.get("join_mode") or "invite_only"))
        except ValueError:
            join_mode = JoinMode.INVITE_ONLY
        space = Space(
            id=space_id,
            name=name,
            owner_instance_id=event.from_instance,
            owner_username=str(p.get("owner_username") or ""),
            identity_public_key=identity_pk,
            config_sequence=int(p.get("config_sequence") or 0),
            features=SpaceFeatures(),
            space_type=space_type,
            join_mode=join_mode,
            description=p.get("description"),
            emoji=p.get("emoji"),
        )
        await self._space_repo.save(space)
        await self._bus.publish(
            RemoteSpaceCreated(
                space_id=space_id,
                from_instance=event.from_instance,
            )
        )

    async def _on_dissolved(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        await self._space_repo.mark_dissolved(space_id)
        await self._bus.publish(RemoteSpaceDissolved(space_id=space_id))

    async def _on_instance_left(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        await self._space_repo.remove_space_instance(space_id, event.from_instance)

    async def _on_banned(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        banned_by = event.payload.get("banned_by")
        reason = str(event.payload.get("reason") or "")[:500]
        await self._space_repo.ban_member(
            space_id=space_id,
            user_id=user_id,
            banned_by=str(banned_by) if banned_by else event.from_instance,
            reason=reason or None,
        )
        await self._bus.publish(
            RemoteSpaceMemberBanned(
                space_id=space_id,
                user_id=user_id,
                banned_by=str(banned_by) if banned_by else None,
            )
        )

    async def _on_unbanned(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        await self._space_repo.unban_member(space_id, user_id)

    async def _on_age_gate(self, event: "FederationEvent") -> None:
        """§CP.F1 — a peer set/changed the min_age or target_audience."""
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        p = event.payload
        min_age = p.get("min_age")
        target_audience = p.get("target_audience")
        if min_age is None and target_audience is None:
            return
        await self._space_repo.update_age_gate(
            space_id,
            min_age=int(min_age) if min_age is not None else None,
            target_audience=str(target_audience) if target_audience else None,
        )

    async def _on_catch_up(self, event: "FederationEvent") -> None:
        """§13 ``SPACE_CONFIG_CATCH_UP`` — a peer announces the sequence
        number of the latest config it has.

        * ``remote_seq < local_seq`` → we're ahead: push our authoritative
          ``SPACE_CONFIG_CHANGED`` to the requester so they apply it.
        * ``remote_seq == local_seq`` → nothing to do (in sync).
        * ``remote_seq > local_seq`` → we're behind: we log; the peer
          that is ahead will push us their copy when *they* hit their
          own catch-up handler.
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        remote_seq = int(event.payload.get("sequence") or 0)
        space = await self._space_repo.get(space_id)
        if space is None:
            log.debug(
                "SPACE_CONFIG_CATCH_UP for unknown space %s from %s",
                space_id,
                event.from_instance,
            )
            return
        local_seq = int(space.config_sequence or 0)
        if remote_seq > local_seq:
            log.info(
                "SPACE_CONFIG_CATCH_UP %s: we are behind peer %s (local=%d remote=%d)",
                space_id,
                event.from_instance,
                local_seq,
                remote_seq,
            )
        elif remote_seq < local_seq:
            log.debug(
                "SPACE_CONFIG_CATCH_UP %s: peer %s is behind us "
                "(local=%d remote=%d); replaying latest config",
                space_id,
                event.from_instance,
                local_seq,
                remote_seq,
            )
            await self._push_config_to(event.from_instance, space)

    async def _push_config_to(
        self,
        to_instance_id: str,
        space: "Space",
    ) -> None:
        """Send ``SPACE_CONFIG_CHANGED`` to *to_instance_id* so they can
        catch their cached copy up to our sequence number."""
        if self._federation is None:
            log.debug("config catch-up requested but federation not wired")
            return
        payload = {
            "space_id": space.id,
            "sequence": space.config_sequence,
            "event_type": "snapshot",
            "name": space.name,
            "description": space.description,
            "emoji": space.emoji,
            "join_mode": space.join_mode.value,
            "space_type": space.space_type.value,
            "features": space.features.to_wire_dict(),
            "retention_days": space.retention_days,
        }
        try:
            await self._federation.send_event(
                to_instance_id=to_instance_id,
                event_type=FederationEventType.SPACE_CONFIG_CHANGED,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("SPACE_CONFIG_CATCH_UP push failed: %s", exc)
