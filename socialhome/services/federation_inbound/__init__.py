"""Federation inbound handlers — organized by event family (§24).

Each module in this package registers a slice of the
:class:`FederationEventType` surface with the event-dispatch registry
on :class:`FederationService`. Keeps the top-level
:mod:`federation_inbound_service` module a thin orchestrator rather
than a 600-line monolith.
"""

from .pairing import PairingInboundHandlers
from .space_membership import SpaceMembershipInboundHandlers
from .space_invites import SpaceInviteInboundHandlers
from .space_content import SpaceContentInboundHandlers

__all__ = [
    "PairingInboundHandlers",
    "SpaceMembershipInboundHandlers",
    "SpaceInviteInboundHandlers",
    "SpaceContentInboundHandlers",
]
