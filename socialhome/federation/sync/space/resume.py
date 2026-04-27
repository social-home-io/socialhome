"""``SPACE_SYNC_RESUME`` — long-offline catch-up (spec §4.4 / §11452).

When an instance reconnects after the 7-day outbox window, the
provider's queued events for it have expired. Spec §4.4.1 calls for
the receiver to ask each peer for the missed events via
``SPACE_SYNC_RESUME {space_id, since}``. The provider responds with a
**burst of individual federation events** (``SPACE_POST_CREATED``,
``SPACE_TASK_CREATED``, etc.) — not a chunked sync. Receivers dedup
against existing rows by primary key, so re-deliveries are harmless.

This module ships the protocol envelope plus the posts replay path —
the most common content type. Other resources (tasks, comments,
pages, stickies, calendar, gallery) follow the same pattern and slot
into ``handle_request`` in subsequent PRs without changing the wire
format.

Differences from ``DmHistoryProvider`` (the DM analog under
``sync/dm_history/``):

* No CHUNK / CHUNK_ACK frames — events go out individually so the
  receiver's existing inbound handlers (``federation_inbound_service``)
  apply them with no special-case logic.
* No COMPLETE marker — the absence of a terminal frame is acceptable
  because every replayed event is independently usable; the receiver
  doesn't need to know "the burst is over" to make progress.
* No per-peer ack tracking — one resume per request, fire-and-discard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ....domain.federation import FederationEventType

if TYPE_CHECKING:
    from ....domain.federation import FederationEvent
    from ....domain.post import Post
    from ....repositories.space_post_repo import AbstractSpacePostRepo
    from ....repositories.space_repo import AbstractSpaceRepo
    from ...federation_service import FederationService


log = logging.getLogger(__name__)


#: Hard cap on posts replayed per single ``SPACE_SYNC_RESUME``. Receivers
#: that need older events re-issue the request with the new high-water
#: mark. Matches the DM-history equivalent so a household with many
#: spaces doesn't burst-pin a small HA instance.
MAX_POSTS_PER_RESUME: int = 500


class SpaceSyncResumeProvider:
    """Receiver- and provider-side helper for ``SPACE_SYNC_RESUME``.

    Construct once per app and register :meth:`handle_request` for
    :data:`FederationEventType.SPACE_SYNC_RESUME`. The receiver-side
    sender is :meth:`send_request` — typically called by a reconnect
    scheduler when the federation link to a peer comes back up after
    the outbox-retention window.
    """

    __slots__ = ("_federation", "_space_repo", "_space_post_repo")

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        space_post_repo: "AbstractSpacePostRepo",
    ) -> None:
        self._federation = federation_service
        self._space_repo = space_repo
        self._space_post_repo = space_post_repo

    # ── Outbound (requester side) ─────────────────────────────────────

    async def send_request(
        self,
        *,
        space_id: str,
        instance_id: str,
        since: str,
    ) -> None:
        """Ask ``instance_id`` to replay missed events since ``since``.

        ``since`` is an ISO-8601 timestamp — typically the receiver's
        local ``MAX(created_at)`` for the space. Returns immediately;
        the response arrives as individual ``SPACE_POST_CREATED``
        events handled by ``federation_inbound_service``.
        """
        if not space_id or not instance_id or not since:
            return
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_SYNC_RESUME,
            payload={"space_id": space_id, "since": since},
            space_id=space_id,
        )

    # ── Inbound (provider side) ───────────────────────────────────────

    async def handle_request(self, event: "FederationEvent") -> int:
        """Replay missed events for one (space, peer) pair.

        Returns the number of events sent (0 if the peer isn't a member,
        the space is unknown, or there's nothing newer than ``since``).
        Membership is gated by ``list_member_instances`` — a peer that
        isn't in the space gets silently dropped, matching the §S-1
        sync-begin guard.
        """
        payload = event.payload or {}
        space_id = str(
            event.space_id or payload.get("space_id") or "",
        )
        since = str(payload.get("since") or "")
        if not space_id or not since:
            return 0
        # Validate ISO-8601 — reject malformed input rather than letting
        # the SQL ``created_at > ?`` comparison silently match nothing.
        try:
            datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            log.debug(
                "SPACE_SYNC_RESUME from %s: bad 'since' %r — dropping",
                event.from_instance,
                since,
            )
            return 0
        peers = await self._space_repo.list_member_instances(space_id)
        if event.from_instance not in peers:
            return 0

        posts = await self._space_post_repo.list_since(
            space_id,
            since,
            limit=MAX_POSTS_PER_RESUME,
        )
        sent = 0
        for post in posts:
            try:
                await self._federation.send_event(
                    to_instance_id=event.from_instance,
                    event_type=FederationEventType.SPACE_POST_CREATED,
                    payload=_post_to_payload(post),
                    space_id=space_id,
                )
                sent += 1
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "SPACE_SYNC_RESUME: replay to %s failed: %s",
                    event.from_instance,
                    exc,
                )
        return sent


def _post_to_payload(post: "Post") -> dict:
    """Shape a stored ``Post`` into the federation event payload.

    Matches what ``federation_inbound_service._post_from_payload``
    expects — keeping resume replays indistinguishable from the
    original ``SPACE_POST_CREATED`` push, so no special-case handling
    is needed on the receiver.
    """
    return {
        "id": post.id,
        "author": post.author,
        "type": post.type.value,
        "content": post.content,
        "media_url": post.media_url,
        "occurred_at": _iso(post.created_at),
    }


def _iso(value: datetime | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    return value.isoformat()
