"""Inbound handler for §319.6 ``INSTANCE_RESYNC_REQUEST``.

A confirmed peer can ask us to re-broadcast state for a named scope:

* ``"capabilities"`` — re-advertise this build's ``proto_version`` (the
  same envelope :class:`CapabilitiesOutbound` sends at startup / on pair).
* ``"space:<id>"`` — replay the space's content (posts, comments, tasks,
  pages, stickies, calendar, gallery), reusing the §4.4
  ``SPACE_SYNC_RESUME`` machinery. **Membership-gated** inside
  :meth:`SpaceSyncResumeProvider.replay_space_to` — a non-member household
  receives nothing.
* ``"calendar:<id>"`` — replay only the space's calendar events. Same
  membership gate via :meth:`SpaceSyncResumeProvider.replay_calendar_to`.

The OUTBOUND side (a request *we* send to a peer) is driven by the
operator endpoint ``POST /api/admin/federation/resync``, which gates on
:data:`FederationCapability.MIN_FOR_INSTANCE_RESYNC` so the request never
reaches a peer with no handler.

Replay / resend are fail-soft (they swallow per-target send errors and
never raise), and the event-dispatch registry swallows handler exceptions
besides — so no ``try/except`` is needed here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ...domain.federation import FederationEventType

if TYPE_CHECKING:
    from ...domain.federation import FederationEvent
    from ...federation.federation_service import FederationService
    from ...federation.sync.space.resume import SpaceSyncResumeProvider
    from ..capabilities_outbound import CapabilitiesOutbound

log = logging.getLogger(__name__)

#: Beginning-of-time ``since`` so a resync replays the full backlog rather
#: than only events newer than some high-water mark.
EPOCH = "1970-01-01T00:00:00+00:00"


class ResyncInboundHandlers:
    """Registers :data:`FederationEventType.INSTANCE_RESYNC_REQUEST`."""

    __slots__ = ("_capabilities_outbound", "_federation", "_space_resume")

    def __init__(
        self,
        *,
        capabilities_outbound: CapabilitiesOutbound,
        space_resume: SpaceSyncResumeProvider,
    ) -> None:
        self._capabilities_outbound = capabilities_outbound
        self._space_resume = space_resume
        self._federation: FederationService | None = None

    def attach_to(self, federation_service: FederationService) -> None:
        """Register the resync handler on the service's event registry."""
        self._federation = federation_service
        federation_service._event_registry.register(
            FederationEventType.INSTANCE_RESYNC_REQUEST,
            self._on_resync_request,
        )

    async def _on_resync_request(self, event: FederationEvent) -> None:
        scope = str(event.payload.get("scope") or "")
        requester = event.from_instance
        if scope == "capabilities":
            log.info("INSTANCE_RESYNC_REQUEST from %s: capabilities", requester)
            await self._capabilities_outbound.resend_to(requester)
        elif scope.startswith("space:"):
            space_id = scope[len("space:") :]
            if not space_id:
                log.debug(
                    "INSTANCE_RESYNC_REQUEST from %s: empty space id — dropping",
                    requester,
                )
                return
            log.info(
                "INSTANCE_RESYNC_REQUEST from %s: space %s",
                requester,
                space_id,
            )
            await self._space_resume.replay_space_to(
                space_id=space_id,
                instance_id=requester,
                since=EPOCH,
            )
        elif scope.startswith("calendar:"):
            space_id = scope[len("calendar:") :]
            if not space_id:
                log.debug(
                    "INSTANCE_RESYNC_REQUEST from %s: empty calendar id — dropping",
                    requester,
                )
                return
            log.info(
                "INSTANCE_RESYNC_REQUEST from %s: calendar %s",
                requester,
                space_id,
            )
            await self._space_resume.replay_calendar_to(
                space_id=space_id,
                instance_id=requester,
                since=EPOCH,
            )
        else:
            log.debug(
                "INSTANCE_RESYNC_REQUEST from %s: unknown scope %r — dropping",
                requester,
                scope,
            )
