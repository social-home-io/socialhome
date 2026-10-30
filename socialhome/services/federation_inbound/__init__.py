"""Federation inbound handlers — organized by event family (§24).

Each module in this package registers a slice of the
:class:`FederationEventType` surface with the event-dispatch registry
on :class:`FederationService`. Keeps the top-level
:mod:`federation_inbound_service` module a thin orchestrator rather
than a 600-line monolith.
"""

from .bazaar_thread import BazaarThreadInboundHandlers
from .pairing import PairingInboundHandlers
from .personal_calendar import PersonalCalendarInboundHandlers
from .resync import ResyncInboundHandlers
from .space_chat import SpaceChatInboundHandlers
from .space_content import SpaceContentInboundHandlers
from .space_invites import SpaceInviteInboundHandlers
from .space_membership import SpaceMembershipInboundHandlers

__all__ = [
    "BazaarThreadInboundHandlers",
    "PairingInboundHandlers",
    "PersonalCalendarInboundHandlers",
    "ResyncInboundHandlers",
    "SpaceChatInboundHandlers",
    "SpaceContentInboundHandlers",
    "SpaceInviteInboundHandlers",
    "SpaceMembershipInboundHandlers",
]
