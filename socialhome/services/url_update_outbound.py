"""Outbound federation for ``URL_UPDATED`` (§11).

When this instance's externally-reachable inbox URL changes — admin
rotates the ``external_url`` in standalone mode, Nabu Casa Remote UI
flips on/off in HA mode, etc. — we tell every confirmed peer so their
``remote_inbox_url`` tracks the move and future envelope deliveries
don't silently break.

The caller is responsible for detecting the change and invoking
:meth:`UrlUpdateOutbound.publish`. Typical wiring (PR 4): the HA
bridge hears ``federation.set_base`` from the integration, upserts
``instance_config['federation_base']``, and calls this service if the
stored value differs from the prior one.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType, InstanceSource
from .peer_outbound import ConfirmedPeerBroadcaster

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)


class UrlUpdateOutbound(ConfirmedPeerBroadcaster):
    """Fan out ``URL_UPDATED`` to every confirmed paired peer — including
    one with no address of its own (reached over the GFS relay), never a
    household met through an invite link."""

    __slots__ = ("_federation", "_federation_repo")

    # Narrow the broadcaster's optional ``_federation`` — required at
    # construction, so the direct ``send_event`` access below is non-None.
    _federation: "FederationService"

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
    ) -> None:
        self._federation = federation_service
        self._federation_repo = federation_repo

    async def publish(self, *, new_inbox_base_url: str) -> int:
        """Tell every confirmed peer about our new inbox URL.

        ``new_inbox_base_url`` is the scheme+host+path prefix — per-peer
        URLs are composed as ``f"{base}/{local_inbox_id}"`` so each peer
        gets its own secret path back.

        Returns the number of peers notified (best-effort count; failed
        sends are logged but do not raise).
        """
        base = new_inbox_base_url.rstrip("/")
        sent = 0
        for peer in await self.confirmed_peers():
            instance_id = peer.id
            if getattr(peer, "source", None) is InstanceSource.SPACE_SESSION:
                # A household met through an invite link must never learn
                # an address — the GFS shields both sides. (The social list
                # already excludes these rows; this keeps the rule local.)
                continue
            # A paired peer with NO URL of its own (paired through a GFS)
            # is still told: the envelope rides the relay, and our new URL
            # is how it can reach us directly from now on.
            local_id = getattr(peer, "local_inbox_id", None)
            if not local_id:
                continue
            peer_specific_url = f"{base}/{local_id}"
            try:
                await self._federation.send_event(
                    to_instance_id=instance_id,
                    event_type=FederationEventType.URL_UPDATED,
                    payload={"inbox_url": peer_specific_url},
                )
                sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "url-update-outbound: send to %s failed: %s",
                    instance_id,
                    exc,
                )
        log.info("url-update-outbound: notified %d peer(s) of new base %s", sent, base)
        return sent
