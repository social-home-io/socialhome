"""Shared mixins for outbound federation services that fan events to peers.

Outbound services that broadcast to confirmed peers (profile, moments,
highlights, capabilities, URL updates, presence/online-status, …) all
repeat two patterns:

1. **Enumerate confirmed peers** — list ``status="confirmed"`` instances,
   resolve our own instance id, and skip self + null ids. Fail-soft: a
   repo error yields an empty list so a transient infra blip never
   crashes the sender.
2. **Send to one peer** — call ``federation.send_event(...)`` wrapped in a
   defensive try/except that logs at debug and swallows, so one bad peer
   can't break the fan-out.

This module extracts both as behaviour-only mixins. They are
``__slots__ = ()`` (see :mod:`socialhome.services.bus_publisher` for why)
and read the ``_federation`` / ``_federation_repo`` slots the consuming
service already declares — which also lets them compose with
:class:`socialhome.services.visibility.VisibilityMixin` on the same class.

Extracting the enumeration also normalises a latent inconsistency: some
services read the public ``federation.own_instance_id`` property while
others reached into the private ``_own_instance_id`` attribute. The mixin
uses the public property uniformly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..domain.federation import FederationEventType
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class ConfirmedPeerBroadcaster:
    """Mixin: enumerate confirmed federation peers (skipping self).

    The consuming service declares ``_federation`` and ``_federation_repo``
    in its own ``__slots__``; this mixin contributes only the shared
    readers, so it owns no slots.
    """

    __slots__ = ()

    _federation: "FederationService | None"
    _federation_repo: "AbstractFederationRepo | None"

    async def confirmed_peers(self) -> list[Any]:
        """Return confirmed peer rows, excluding our own instance + nulls.

        Fail-soft: a repo error (or a missing repo) yields ``[]`` so a
        transient infra failure never crashes an outbound fan-out.
        """
        repo = self._federation_repo
        if repo is None:
            return []
        try:
            # §D2b: social surface — a ``space_session`` row (a household
            # we only share a space with, via an invite link) is NOT a
            # social peer, so read the social list, not every CONFIRMED row.
            peers = await repo.list_social_instances()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("%s: list peers failed: %s", type(self).__name__, exc)
            return []
        own = getattr(self._federation, "own_instance_id", "")
        return [
            peer
            for peer in peers
            if getattr(peer, "id", None) and getattr(peer, "id", None) != own
        ]

    async def list_confirmed_peer_ids(self) -> list[str]:
        """Convenience: just the instance ids from :meth:`confirmed_peers`."""
        return [peer.id for peer in await self.confirmed_peers()]


class SingleTargetSender:
    """Mixin: send one federation event to one peer, fail-soft.

    Wraps ``federation.send_event`` so one unreachable peer can't break a
    fan-out: failures log at debug and are swallowed. The consuming
    service declares ``_federation`` in its own ``__slots__``.
    """

    __slots__ = ()

    _federation: "FederationService | None"

    async def send_to_instance(
        self,
        instance_id: str,
        event_type: "FederationEventType",
        payload: dict,
        *,
        space_id: str | None = None,
    ) -> None:
        """Deliver ``event_type``/``payload`` to ``instance_id``, fail-soft.

        ``space_id`` is only forwarded when set, so the call shape matches
        the pre-mixin code exactly for the common (non-space) senders.
        """
        if self._federation is None:  # pragma: no cover — defensive
            return
        extra = {"space_id": space_id} if space_id is not None else {}
        try:
            await self._federation.send_event(
                to_instance_id=instance_id,
                event_type=event_type,
                payload=payload,
                **extra,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "%s: send to %s failed: %s",
                type(self).__name__,
                instance_id,
                exc,
            )
