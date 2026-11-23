"""Capability re-sync at startup (#319 ¶6, v_96).

We ask confirmed peers to *re-advertise their capabilities* — scope
``"capabilities"`` only — in two cases, one path:

* **After an upgrade** (this build's :data:`OURS` increased since the last
  boot): every peer that has the handler (``MIN_FOR_INSTANCE_RESYNC``), so
  our cached ``proto_version`` for each peer — and with it
  :meth:`FederationService.peer_supports` — reflects reality soon after an
  upgrade instead of waiting for the next organic announcement. At most once
  per upgrade, guarded by a persisted "last OURS" on the singleton
  ``instance_identity`` self-row.
* **On every other restart** (v_96): only peers at
  ``MIN_FOR_MOMENT_REACH``. Their capabilities carry ``moment_look_hops``
  (how far their people look for moments, decision C), which we keep in
  memory only (``FederationService.peer_moment_look_hops``) — lost on our
  restart, and a peer re-sends it on its own only when *it* restarts or the
  value changes. Until it answers, no hop-4–5 moment copy goes to it. Older
  peers are not asked: they never send the field.

This is deliberately the lightweight scope: one tiny
:data:`FederationEventType.INSTANCE_RESYNC_REQUEST` per peer, answered by one
``INSTANCE_CAPABILITIES_UPDATED`` (a few hundred bytes each way), with **no**
space/calendar content replay — so it can never trigger a content-resync
storm. A peer that is offline at our startup answers once it is back (the
request rides the outbox).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from socialhome.domain.federation import FederationEventType
from socialhome.domain.federation_capabilities import (
    OURS,
    FederationCapability,
)

if TYPE_CHECKING:
    from socialhome.federation.federation_service import FederationService
    from socialhome.repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: The latest-only outbox key of the request (local, never on the wire): a
#: peer offline across several of our restarts keeps one queued request.
RESYNC_CAPABILITIES_OUTBOX_KEY: str = "resync:capabilities"


async def request_capability_resync(
    *,
    federation: FederationService,
    federation_repo: AbstractFederationRepo,
    identity_repo: AbstractFederationRepo,
) -> int:
    """Ask confirmed social peers to re-advertise capabilities (scope
    ``"capabilities"``): every peer with the handler if ``OURS`` increased
    since last boot, else only v_96 peers (to re-learn their
    ``moment_look_hops``). Returns the number of resync requests sent.
    Capabilities-only — no content replay — so it can't cause a resync
    storm. Persists ``OURS`` as the new last-seen after an upgrade.
    """
    last = await identity_repo.get_last_proto_version()
    # A downgrade or an unchanged restart is not an upgrade (the storm guard
    # for older peers); v_96 peers are still asked for their moment look.
    upgraded = last is None or last < OURS
    min_version = (
        FederationCapability.MIN_FOR_INSTANCE_RESYNC
        if upgraded
        else FederationCapability.MIN_FOR_MOMENT_REACH
    )

    sent = 0
    # Social peers only — and here that is genuinely enough. This asks a
    # peer to re-advertise ITS capabilities to us; a household seated
    # from an invite link (``source = space_session``) is reachable only
    # through the connection-server relay, so the request and its answer
    # would cost two relay envelopes to learn something we get for free:
    # our own startup ``INSTANCE_CAPABILITIES_UPDATED`` fan-out DOES
    # include space-session peers (see
    # ``CapabilitiesOutbound.confirmed_peers``), and their announcement
    # to us rides their fan-out in the same way. Nor does such a household
    # get moments (no ``moment_look_hops`` to re-learn).
    peers = await federation_repo.list_social_instances()
    for peer in peers:
        if not await federation.peer_supports(peer.id, min_version=min_version):
            continue
        # send_event is fail-soft (never raises) — per-peer failures land in
        # the outbox retry queue, so one unreachable peer can't abort the run.
        await federation.send_event(
            to_instance_id=peer.id,
            event_type=FederationEventType.INSTANCE_RESYNC_REQUEST,
            payload={"scope": "capabilities"},
            outbox_key=RESYNC_CAPABILITIES_OUTBOX_KEY,
        )
        sent += 1

    if upgraded:
        # Persist regardless of how many we reached so a first boot post
        # migration (last is None) records OURS and doesn't re-fire.
        await identity_repo.set_last_proto_version(OURS)
    log.info(
        "capability-resync: OURS %d, last %r (%s) — asked %d peer(s) to re-advertise",
        OURS,
        last,
        "upgrade" if upgraded else "v_96 moment look",
        sent,
    )
    return sent
