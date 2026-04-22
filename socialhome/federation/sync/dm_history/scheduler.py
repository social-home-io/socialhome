"""Initiation triggers for DM history sync.

Two entry points fire a :data:`FederationEventType.DM_HISTORY_REQUEST`
at priority :data:`P4_DM` on the reconnect queue:

1. :class:`PairingConfirmed` — first-time pairing; catch up on every
   conversation that already has a member on the new peer.
2. :class:`ConnectionReachable` — a peer came back online after being
   unreachable; request history for every conversation we share.

Both paths apply the same per ``(peer, conversation)`` rate limit
(:data:`RATE_LIMIT_SECONDS`) to prevent a thundering-herd when a
household returns from a multi-day outage.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ....domain.events import ConnectionReachable, PairingConfirmed
from ....domain.federation import FederationEventType
from ....infrastructure.reconnect_queue import P4_DM

if TYPE_CHECKING:
    from ....infrastructure.event_bus import EventBus
    from ....infrastructure.reconnect_queue import ReconnectSyncQueue
    from ....repositories.conversation_repo import AbstractConversationRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


#: Minimum seconds between two DM_HISTORY_REQUESTs for the same
#: ``(peer, conversation)`` pair. One hour matches the S-6 anti-flood
#: window used by space sync.
RATE_LIMIT_SECONDS: float = 3600.0


class DmHistoryScheduler:
    """Subscribes to pair + reachability events and enqueues history
    requests for the affected conversations.
    """

    __slots__ = (
        "_bus",
        "_federation",
        "_conversation_repo",
        "_queue",
        "_own_instance_id",
        "_last_request_at",
    )

    def __init__(
        self,
        *,
        bus: "EventBus",
        federation: "FederationService",
        conversation_repo: "AbstractConversationRepo",
        queue: "ReconnectSyncQueue",
        own_instance_id: str,
    ) -> None:
        self._bus = bus
        self._federation = federation
        self._conversation_repo = conversation_repo
        self._queue = queue
        self._own_instance_id = own_instance_id
        # (instance_id, conversation_id) → monotonic timestamp of last
        # request. Per-process only — after a restart we allow one more
        # request, which is fine because the provider is idempotent.
        self._last_request_at: dict[tuple[str, str], float] = {}

    def wire(self) -> None:
        """Subscribe to pair + reachability events.

        Safe to call during :meth:`create_app` — the bus accepts
        subscribers before publishers exist.
        """
        self._bus.subscribe(PairingConfirmed, self._on_pairing_confirmed)
        self._bus.subscribe(ConnectionReachable, self._on_connection_reachable)

    # ─── Event handlers ──────────────────────────────────────────────────

    async def _on_pairing_confirmed(self, event: PairingConfirmed) -> None:
        if event.instance_id == self._own_instance_id:
            return
        await self._enqueue_for_peer(event.instance_id)

    async def _on_connection_reachable(
        self,
        event: ConnectionReachable,
    ) -> None:
        if event.instance_id == self._own_instance_id:
            return
        await self._enqueue_for_peer(event.instance_id)

    async def _enqueue_for_peer(self, instance_id: str) -> int:
        """Enqueue DM_HISTORY_REQUEST for every conversation shared with
        ``instance_id``. Returns the count enqueued (after rate-limiting).
        """
        try:
            conversation_ids = (
                await self._conversation_repo.list_conversations_with_remote_member(
                    instance_id
                )
            )
        except Exception as exc:  # pragma: no cover
            log.warning(
                "dm history: failed to list conversations for %s: %s",
                instance_id,
                exc,
            )
            return 0

        enqueued = 0
        for conv_id in conversation_ids:
            if not self._rate_limit_allow(instance_id, conv_id):
                continue
            self._queue.enqueue(
                P4_DM,
                self._make_request_factory(instance_id, conv_id),
                f"dm_history_request({instance_id}, {conv_id})",
            )
            enqueued += 1
        return enqueued

    # ─── Public API (admin route / tests) ────────────────────────────────

    async def enqueue_request(
        self,
        *,
        instance_id: str,
        conversation_id: str,
    ) -> bool:
        """Force a single DM_HISTORY_REQUEST regardless of rate limit.
        Returns ``True`` if the item landed on the queue.
        """
        self._queue.enqueue(
            P4_DM,
            self._make_request_factory(instance_id, conversation_id),
            f"dm_history_request({instance_id}, {conversation_id})",
        )
        self._last_request_at[(instance_id, conversation_id)] = time.monotonic()
        return True

    # ─── Internals ───────────────────────────────────────────────────────

    def _rate_limit_allow(self, instance_id: str, conversation_id: str) -> bool:
        key = (instance_id, conversation_id)
        now = time.monotonic()
        last = self._last_request_at.get(key)
        if last is not None and (now - last) < RATE_LIMIT_SECONDS:
            return False
        self._last_request_at[key] = now
        return True

    def _make_request_factory(self, instance_id: str, conversation_id: str):
        async def _send() -> None:
            # Local cursor: the latest created_at we already have for
            # this conversation. The provider will stream everything
            # strictly newer than that.
            since_iso = await self._latest_message_iso(conversation_id)
            await self._federation.send_event(
                to_instance_id=instance_id,
                event_type=FederationEventType.DM_HISTORY_REQUEST,
                payload={
                    "conversation_id": conversation_id,
                    "since": since_iso or "",
                },
            )

        return _send

    async def _latest_message_iso(self, conversation_id: str) -> str | None:
        # Most recent message first; take its created_at as the cursor.
        latest = await self._conversation_repo.list_messages(
            conversation_id,
            limit=1,
        )
        if not latest:
            return None
        return latest[0].created_at.isoformat()
