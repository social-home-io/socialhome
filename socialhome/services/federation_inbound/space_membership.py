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
from ..child_protection_service import _VALID_MIN_AGES
from ..space_service import _space_metadata_for_federation, can_seat_remote_stub

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


class SpaceMembershipInboundHandlers:
    """Register the membership-family inbound handlers on one service."""

    __slots__ = (
        "_bus",
        "_space_repo",
        "_federation",
    )

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
            FederationEventType.SPACE_SYNC_REJECTED, self._on_sync_rejected
        )
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
        # §D1b anti-hijack — don't let a peer's SPACE_CREATED clobber the
        # config of a space we already hold under a DIFFERENT host (the
        # save() UPSERT would otherwise rewrite name/features/sequence).
        # Owner + identity_public_key columns are excluded from the UPSERT
        # SET, so this is a config-clobber guard, not an ownership change —
        # but refuse anyway for consistency with the other seating paths.
        if not await can_seat_remote_stub(
            self._space_repo, space_id, event.from_instance
        ):
            log.warning(
                "§D1b: dropping SPACE_CREATED for %s — already owned locally "
                "by another host, sender=%s",
                space_id,
                event.from_instance,
            )
            return
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
        """The owner host dissolved a space — ARCHIVE our local copy
        read-only; never hard-delete a member's local data.

        A remote dissolve must not destroy local data: instead of purging
        the content graph we flip the space to ``archived=True`` with
        ``archived_reason='dissolved'`` so the member keeps a read-only
        archive of everything they held. Only a *local* self-initiated
        :meth:`SpaceService.dissolve_space` hard-purges.

        Security: the dissolve is authoritative only from the space's
        OWNER instance. A non-owner paired peer must not be able to
        terminate your space, so we drop the event (with a warning) when
        the sender isn't ``space.owner_instance_id``. We still publish
        ``RemoteSpaceDissolved`` (drives the UI refresh + the one-time
        notification) once the archive is applied.
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            return  # already gone locally — nothing to archive
        if space.owner_instance_id != event.from_instance:
            log.warning(
                "SPACE_DISSOLVED for %s from non-owner %s (owner=%s) — dropping",
                space_id,
                event.from_instance,
                space.owner_instance_id,
            )
            return
        await self._space_repo.set_archived(space_id, True, reason="dissolved")
        await self._bus.publish(RemoteSpaceDissolved(space_id=space_id))

    async def _on_sync_rejected(self, event: "FederationEvent") -> None:
        """The host rejected our reconnect sync because we're no longer a
        member — ARCHIVE our local copy read-only (the §S-1 reconnect
        backstop). Same archive-not-delete treatment as a remote dissolve:
        an offline member that missed SPACE_DISSOLVED / a removal event
        still reconciles here instead of keeping an orphaned stub forever.

        Security: identical owner-instance guard to ``_on_dissolved`` — only
        the space's OWNER host may terminate our copy, so a non-owner peer's
        SPACE_SYNC_REJECTED is dropped with a warning. The ``reason`` must be
        a known terminal reason (``"dissolved"`` | ``"removed"``); anything
        else is dropped (don't archive on a garbage/forward-incompatible
        payload). Idempotent: a copy already in a terminal archive
        (``archived_reason`` set) is left untouched.
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        reason = str(event.payload.get("reason") or "")
        if reason not in ("dissolved", "removed"):
            log.warning(
                "SPACE_SYNC_REJECTED for %s with unknown reason %r — dropping",
                space_id,
                reason,
            )
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            return  # nothing local to archive
        if space.owner_instance_id != event.from_instance:
            log.warning(
                "SPACE_SYNC_REJECTED for %s from non-owner %s (owner=%s) — dropping",
                space_id,
                event.from_instance,
                space.owner_instance_id,
            )
            return
        if space.archived_reason:
            return  # already terminally archived — idempotent
        await self._space_repo.set_archived(space_id, True, reason=reason)
        await self._bus.publish(RemoteSpaceDissolved(space_id=space_id))

    async def _on_instance_left(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        await self._space_repo.remove_space_instance(space_id, event.from_instance)

    async def _is_from_the_host(self, event: "FederationEvent", space_id: str) -> bool:
        """Whether the §24.11-authenticated sender hosts ``space_id``.

        A ban is a statement about somebody else's standing in a space,
        and ``ban_member`` both inserts the ban row and deletes the
        member — so an unauthenticated one is a household evicting the
        space's own owner. Only the host decides that; an unknown space
        is refused too, because there is no owner to compare against and
        the sender picked the id.

        Mirrors the ``SPACE_AGE_GATE_UPDATED`` / ``SPACE_MEMBER_ROLE_CHANGED``
        host-authority guards. The roster-gossip family
        (``SPACE_MEMBER_JOINED`` / ``_LEFT``) is the other accepted shape:
        an authority signature over ``spaces.identity_public_key``, so a
        delegated admin can act while the owner is offline. Bans have no
        signing outbound today — when one is added, verify the signature
        here as a second accepted branch rather than dropping the check.
        """
        space = await self._space_repo.get(space_id)
        if space is None:
            log.warning(
                "%s for unknown space %s from %s — dropping",
                event.event_type,
                space_id,
                event.from_instance,
            )
            return False
        if space.owner_instance_id != event.from_instance:
            log.warning(
                "%s for %s from non-host %s (host=%s) — dropping",
                event.event_type,
                space_id,
                event.from_instance,
                space.owner_instance_id,
            )
            return False
        return True

    async def _on_banned(self, event: "FederationEvent") -> None:
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        user_id = str(event.payload.get("user_id") or "")
        if not space_id or not user_id:
            return
        if not await self._is_from_the_host(event, space_id):
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
        if not await self._is_from_the_host(event, space_id):
            return
        await self._space_repo.unban_member(space_id, user_id)

    async def _on_age_gate(self, event: "FederationEvent") -> None:
        """§CP.F1 — the host set/changed the space's min_age.

        Older peers may still ship ``target_audience`` in the payload; we
        ignore it (the age gate is ``min_age``-only now).
        """
        space_id = event.space_id or str(event.payload.get("space_id") or "")
        if not space_id:
            return
        space = await self._space_repo.get(space_id)
        if space is None:
            return
        # Host authority: the age gate is only valid from the owning
        # instance. Without this a malicious paired peer could send
        # SPACE_AGE_GATE_UPDATED for a space WE host and lower our gate to
        # 0, disabling child-protection enforcement (mirrors the
        # SPACE_MEMBER_ROLE_CHANGED host-authority guard).
        if space.owner_instance_id != event.from_instance:
            log.debug(
                "SPACE_AGE_GATE_UPDATED for %s from non-host %s — dropping",
                space_id,
                event.from_instance,
            )
            return
        p = event.payload
        min_age = p.get("min_age")
        if min_age is None:
            return
        # Reject a min_age outside the allowed set before it hits the
        # schema CHECK (a non-conforming peer would otherwise abort the
        # update).
        try:
            coerced_min_age = int(min_age)
        except TypeError, ValueError:
            log.warning(
                "SPACE_AGE_GATE_UPDATED for %s: non-int min_age %r",
                space_id,
                min_age,
            )
            return
        if coerced_min_age not in _VALID_MIN_AGES:
            log.warning(
                "SPACE_AGE_GATE_UPDATED for %s: ignoring invalid min_age %r",
                space_id,
                min_age,
            )
            return
        await self._space_repo.update_age_gate(space_id, min_age=coerced_min_age)

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
        # Ship both the flat fields (the existing catch-up shape, kept
        # for back-compat with peers that read them directly) AND a
        # ``space_meta`` blob — the same shape the §D1b inbound stub
        # writer consumes everywhere else. The latter is what lets
        # a remote-stub holder apply a rename without us having to
        # teach the consumer about the legacy flat layout.
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
            "space_meta": _space_metadata_for_federation(space),
        }
        try:
            await self._federation.send_event(
                to_instance_id=to_instance_id,
                event_type=FederationEventType.SPACE_CONFIG_CHANGED,
                payload=payload,
            )
        except Exception as exc:  # pragma: no cover
            log.warning("SPACE_CONFIG_CATCH_UP push failed: %s", exc)
