"""One-shot capability-resync after a protocol-version upgrade (#319 ¶6).

When this build's :data:`OURS` protocol version has increased since the last
boot (the operator upgraded Social Home), we proactively ask every confirmed
peer to *re-advertise its capabilities* — scope ``"capabilities"`` only. That
refreshes our cached ``proto_version`` for each peer so feature-gating
(:meth:`FederationService.peer_supports`) reflects reality soon after an
upgrade instead of waiting for the next organic announcement.

This is deliberately the lightweight scope: one tiny
:data:`FederationEventType.INSTANCE_RESYNC_REQUEST` per peer, with **no**
space/calendar content replay — so it can never trigger a content-resync
storm. It fires at most once per upgrade, guarded by a persisted "last OURS"
on the singleton ``instance_identity`` self-row, and never on an unchanged
restart.
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


async def request_capability_resync_if_upgraded(
    *,
    federation: FederationService,
    federation_repo: AbstractFederationRepo,
    identity_repo: AbstractFederationRepo,
) -> int:
    """If ``OURS`` increased since last boot, ask each confirmed peer to
    re-advertise capabilities (scope ``"capabilities"``). Returns the number
    of resync requests sent (0 when not an upgrade). Capabilities-only —
    no content replay — so it can't cause a resync storm. Persists ``OURS``
    as the new last-seen on success.
    """
    last = await identity_repo.get_last_proto_version()
    if last is not None and last >= OURS:
        # Not an upgrade (unchanged restart, or a downgrade) — the storm guard.
        return 0

    sent = 0
    # Social peers only: a household seated from an invite link
    # (``source = space_session``) shares a space with us, not a
    # protocol relationship to renegotiate. Its capabilities travel on
    # the startup ``INSTANCE_CAPABILITIES_UPDATED`` fan-out; asking it
    # to re-advertise buys nothing and spends a relay envelope.
    peers = await federation_repo.list_social_instances()
    for peer in peers:
        if not await federation.peer_supports(
            peer.id, min_version=FederationCapability.MIN_FOR_INSTANCE_RESYNC
        ):
            continue
        # send_event is fail-soft (never raises) — per-peer failures land in
        # the outbox retry queue, so one unreachable peer can't abort the run.
        await federation.send_event(
            to_instance_id=peer.id,
            event_type=FederationEventType.INSTANCE_RESYNC_REQUEST,
            payload={"scope": "capabilities"},
        )
        sent += 1

    # Persist regardless of how many we reached so a first-boot-post-migration
    # (last is None) records OURS and doesn't re-fire on the next restart.
    await identity_repo.set_last_proto_version(OURS)
    log.info(
        "capability-resync: OURS %d > last %r — asked %d peer(s) to re-advertise",
        OURS,
        last,
        sent,
    )
    return sent
