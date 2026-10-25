"""GFS log of signed public-moment delete certificates (v_66).

A public moment lives up to 30 days at a follower household (v_65), but its
delete crossed this server only live or from the 24 h offline queue — a
household offline longer kept showing a moment its author deleted. Owner
sign-off 4 (2026-10-25): this server keeps the author household's signed
**delete certificate** for 30 days and hands it to a follower household
when it reconnects.

A certificate is the ``MOMENT_DELETED`` origin signature on its own
(:mod:`socialhome.moment_origin`): author user id, moment id, origin
household, delete time, the household's identity key and its signature —
no content, nothing this server did not already see on
``/gfs/moments/delete`` but the delete time. It is self-verifying: this
server checks it against the key the author's household registered with
(``POST /gfs/moments/delete_cert``), and the follower checks it again
against the key it pinned when it followed — this server can neither forge
nor alter one, only withhold it (as it can any delete).

Bounds:

* one row per (author, moment) — a replay changes nothing;
* :data:`DELETE_CERT_POSTS_PER_MINUTE` posts per author household across
  the cluster, at most :data:`MAX_DELETE_CERTS_PER_AUTHOR` live per author,
  kept only while the author has a follower here;
* pruned :data:`DELETE_LOG_TTL_S` after it was written;
* at most :data:`MAX_DELETE_CERTS_PER_CONNECT` pushed on a reconnect, in
  frames of :data:`DELETE_CERTS_PER_FRAME`, only of authors a user of that
  household follows, and only those written since it was last seen.

**Cluster.** The log lives in the shared database and the push runs on the
node that took the household's hello, so no cross-node forward is needed:
whichever node holds the socket reads the same rows.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import orjson

from .envelope_relay import RELAY_SEEN_REFRESH_S
from .public import SlidingWindowCounter, node_share

if TYPE_CHECKING:
    from .cluster import ClusterService
    from .repositories import (
        AbstractGfsMomentDeleteLogRepo,
        AbstractGfsMomentFollowRepo,
    )
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: How long a certificate is kept: a moment's longest life (v_65).
DELETE_LOG_TTL_S: int = 30 * 86_400

#: Certificate posts one author household may make per minute, across the
#: cluster — the same budget as public-moment publishes and deletes
#: (``MOMENT_PUBLISHES_PER_MINUTE``), counted separately so a delete's two
#: posts (sealed delete + certificate) don't halve it.
DELETE_CERT_POSTS_PER_MINUTE: int = 30

#: Live certificates one author may hold here. A person deleting more than
#: ~33 moments a day for a month is past any honest use; past it a delete
#: still reaches followers live, only not a household offline for days.
MAX_DELETE_CERTS_PER_AUTHOR: int = 1_000

#: Certificates pushed to one household per reconnect (~70 KB at most).
MAX_DELETE_CERTS_PER_CONNECT: int = 200

#: Certificates per ``moment_delete_certs`` frame (~18 KB).
DELETE_CERTS_PER_FRAME: int = 50

#: How far before a household's last "seen" mark the push looks back. The
#: mark is refreshed every :data:`RELAY_SEEN_REFRESH_S` while its socket is
#: open, so a delete in the last stretch before it dropped is still pushed.
SEEN_SLACK_S: int = int(2 * RELAY_SEEN_REFRESH_S)

#: The WebSocket frame type a follower household reads them from. An older
#: household ignores it (an unknown frame type).
FRAME_DELETE_CERTS: str = "moment_delete_certs"


class MomentDeleteLog:
    """Keep delete certificates; push them to a reconnecting follower."""

    __slots__ = ("_cluster", "_follows", "_posts", "_repo", "_tasks", "_ws_registry")

    def __init__(
        self,
        repo: AbstractGfsMomentDeleteLogRepo,
        follows: AbstractGfsMomentFollowRepo,
        ws_registry: GfsWebSocketRegistry,
        *,
        cluster: ClusterService | None = None,
    ) -> None:
        self._repo = repo
        self._follows = follows
        self._ws_registry = ws_registry
        self._cluster = cluster
        self._posts = SlidingWindowCounter(DELETE_CERT_POSTS_PER_MINUTE)
        self._tasks: set[asyncio.Task] = set()

    def allow_post(self, instance_id: str) -> bool:
        """Count one certificate post from *instance_id*; ``False`` past this
        node's share of :data:`DELETE_CERT_POSTS_PER_MINUTE`."""
        nodes = self._cluster.live_node_count() if self._cluster is not None else 1
        return self._posts.allow(
            instance_id, limit=node_share(DELETE_CERT_POSTS_PER_MINUTE, nodes)
        )

    async def keep(
        self, *, author_user_id: str, cert: dict[str, Any], now: int | None = None
    ) -> str:
        """Keep a checked certificate. Returns ``"kept"``, ``"already"`` (a
        replay), ``"no_followers"`` (nobody here could need it) or ``"full"``
        (:data:`MAX_DELETE_CERTS_PER_AUTHOR`)."""
        now = int(time.time()) if now is None else now
        if await self._follows.follower_count(author_user_id) == 0:
            return "no_followers"
        if (
            await self._repo.count_for_author(
                author_user_id, since=now - DELETE_LOG_TTL_S
            )
            >= MAX_DELETE_CERTS_PER_AUTHOR
        ):
            return "full"
        kept = await self._repo.add(
            author_user_id=author_user_id,
            moment_id=str(cert["moment_id"]),
            cert=orjson.dumps(cert, option=orjson.OPT_SORT_KEYS).decode(),
            now=now,
        )
        return "kept" if kept else "already"

    def schedule_household_connected(self, instance_id: str) -> None:
        """Fire-and-forget :meth:`push_to` — the hello never waits on it."""
        task = asyncio.create_task(self.push_to(instance_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def push_to(self, instance_id: str, *, now: int | None = None) -> int:
        """Push the certificates *instance_id* may have missed to its socket
        on this node. Never raises; returns how many went out."""
        now = int(time.time()) if now is None else now
        sent = 0
        try:
            raw = await self._repo.for_household(
                instance_id,
                now=now,
                ttl_s=DELETE_LOG_TTL_S,
                seen_slack_s=SEEN_SLACK_S,
                limit=MAX_DELETE_CERTS_PER_CONNECT,
            )
            certs = [orjson.loads(c) for c in raw]
            for i in range(0, len(certs), DELETE_CERTS_PER_FRAME):
                chunk = certs[i : i + DELETE_CERTS_PER_FRAME]
                if not await self._ws_registry.send(
                    instance_id, {"type": FRAME_DELETE_CERTS, "certs": chunk}
                ):
                    break
                sent += len(chunk)
        except Exception:  # defensive — never fail a connect on this
            log.exception("moment_delete_log: push failed")
        if sent:
            log.info("moment_delete_log: %d delete certificate(s) pushed", sent)
        return sent

    async def prune(self, now: int | None = None) -> int:
        """Drop certificates older than :data:`DELETE_LOG_TTL_S`."""
        now = int(time.time()) if now is None else now
        return await self._repo.prune(older_than=now - DELETE_LOG_TTL_S)
