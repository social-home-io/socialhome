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
* pushed only of authors a user of that household follows, written since
  it was last seen — in pushes of at most :data:`MAX_DELETE_CERTS_PER_PUSH`
  (:data:`DELETE_CERTS_PER_AUTHOR_PER_PUSH` of one author, dealt
  round-robin), frames of :data:`DELETE_CERTS_PER_FRAME`, one push every
  :data:`DELETE_PUSH_PAUSE_S` while the socket stays open.

**Never lost to a cap.** A per (household, author) cursor
(``gfs_moment_delete_cursor``) records the last certificate that went out;
it is opened before the first frame and moved after each one, and only a
drained author's is closed. The "last seen" mark only decides where a *new*
cursor starts — so a capped push, a socket that drops mid-push, or a flood
of certificates by one author leaves the rest owed, never skipped
(adversarial review F1). And since every author gets an equal share of each
push, one author's flood delays only its own certificates.

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
    from .domain import GfsPendingDeleteCert
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

#: Certificates in one push to one household (~70 KB at most).
MAX_DELETE_CERTS_PER_PUSH: int = 200

#: …of them at most this many of one author, so a flood by one author (up
#: to :data:`MAX_DELETE_CERTS_PER_AUTHOR` made-up ids) can't crowd out the
#: others' deletes.
DELETE_CERTS_PER_AUTHOR_PER_PUSH: int = 100

#: Pause between two pushes to a household with more owed, while its socket
#: stays open — the reconnect push is bounded per minute, never cut off.
DELETE_PUSH_PAUSE_S: float = 60.0

#: Certificates per ``moment_delete_certs`` frame (~18 KB).
DELETE_CERTS_PER_FRAME: int = 50

#: How far before a household's last "seen" mark a new delivery cursor
#: starts. The mark is refreshed every :data:`RELAY_SEEN_REFRESH_S` while its
#: socket is open, so a delete in the last stretch before it dropped is
#: still pushed. (An open cursor ignores the mark — what it still owes is
#: pushed however old.)
SEEN_SLACK_S: int = int(2 * RELAY_SEEN_REFRESH_S)

#: The WebSocket frame type a follower household reads them from. An older
#: household ignores it (an unknown frame type).
FRAME_DELETE_CERTS: str = "moment_delete_certs"


class MomentDeleteLog:
    """Keep delete certificates; push them to a reconnecting follower."""

    __slots__ = (
        "_cluster",
        "_draining",
        "_follows",
        "_posts",
        "_repo",
        "_rerun",
        "_stop",
        "_tasks",
        "_ws_registry",
    )

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
        #: Households a drain runs for on this node, and those of them that
        #: reconnected meanwhile (one more push before it may stop).
        self._draining: set[str] = set()
        self._rerun: set[str] = set()
        self._stop = asyncio.Event()

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
        """Drain what *instance_id* is owed in the background — the hello
        never waits on it. One drain per household at a time: a reconnect
        while one runs asks it for one more push instead."""
        if self._stop.is_set():
            return
        if instance_id in self._draining:
            self._rerun.add(instance_id)
            return
        self._draining.add(instance_id)
        task = asyncio.create_task(self._drain(instance_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _drain(self, instance_id: str) -> None:
        """Push to *instance_id* until it is owed nothing, its socket is gone
        or this node stops — one push every :data:`DELETE_PUSH_PAUSE_S`."""
        try:
            while not self._stop.is_set():
                self._rerun.discard(instance_id)
                _sent, more = await self._push(instance_id, now=int(time.time()))
                if instance_id in self._rerun:
                    continue
                if not more:
                    return
                try:
                    await asyncio.wait_for(self._stop.wait(), DELETE_PUSH_PAUSE_S)
                except TimeoutError:
                    pass
        finally:
            # No await between the last ``_rerun`` check and here: a
            # reconnect either landed before it (and is served by the loop)
            # or finds no drain running and starts its own.
            self._draining.discard(instance_id)
            self._rerun.discard(instance_id)

    async def push_to(self, instance_id: str, *, now: int | None = None) -> int:
        """One push of what *instance_id* is owed to its socket on this node.
        Never raises; returns how many went out."""
        sent, _more = await self._push(
            instance_id, now=int(time.time()) if now is None else now
        )
        return sent

    async def _push(self, instance_id: str, *, now: int) -> tuple[int, bool]:
        """Push up to :data:`MAX_DELETE_CERTS_PER_PUSH` owed certificates,
        moving each author's cursor after every frame that went out. Returns
        how many went out and whether more is owed to a socket still there."""
        sent = 0
        more = False
        try:
            await self._repo.open_cursors(
                instance_id, now=now, ttl_s=DELETE_LOG_TTL_S, seen_slack_s=SEEN_SLACK_S
            )
            owed = await self._repo.pending(
                instance_id,
                now=now,
                ttl_s=DELETE_LOG_TTL_S,
                per_author=DELETE_CERTS_PER_AUTHOR_PER_PUSH,
                limit=MAX_DELETE_CERTS_PER_PUSH,
            )
            done: dict[str, int] = {}
            for i in range(0, len(owed), DELETE_CERTS_PER_FRAME):
                chunk = owed[i : i + DELETE_CERTS_PER_FRAME]
                frame = {
                    "type": FRAME_DELETE_CERTS,
                    "certs": [orjson.loads(c.cert) for c in chunk],
                }
                if not await self._ws_registry.send(instance_id, frame):
                    # Gone: what is left stays owed for the next connect.
                    return sent, False
                sent += len(chunk)
                await self._settle(instance_id, chunk, done, now=now)
            more = len(owed) >= MAX_DELETE_CERTS_PER_PUSH or any(
                c.author_left > done[c.author_user_id] for c in owed
            )
            if not more:
                await self._repo.close_drained_cursors(
                    instance_id, now=now, ttl_s=DELETE_LOG_TTL_S
                )
        except Exception:  # defensive — never fail a connect on this
            log.exception("moment_delete_log: push failed")
            more = False
        if sent:
            log.info("moment_delete_log: %d delete certificate(s) pushed", sent)
        return sent, more

    async def _settle(
        self,
        instance_id: str,
        chunk: list[GfsPendingDeleteCert],
        done: dict[str, int],
        *,
        now: int,
    ) -> None:
        """After *chunk* went out: move each of its authors' cursors past the
        newest of theirs in it, or close the cursor once that author's whole
        backlog (``author_left``) went out."""
        last: dict[str, GfsPendingDeleteCert] = {}
        for c in chunk:
            done[c.author_user_id] = done.get(c.author_user_id, 0) + 1
            # Ranks only go up through a push: the last one seen is the newest.
            last[c.author_user_id] = c
        for author, c in last.items():
            if done[author] >= c.author_left:
                await self._repo.close_cursor(instance_id, author)
            else:
                await self._repo.advance_cursor(
                    instance_id,
                    author,
                    created_at=c.created_at,
                    moment_id=c.moment_id,
                    now=now,
                )

    async def close(self) -> None:
        """Stop every drain — a running push finishes its frames, what is left
        stays owed. At shutdown, before the database goes."""
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def prune(self, now: int | None = None) -> int:
        """Drop certificates older than :data:`DELETE_LOG_TTL_S`, and delivery
        cursors not moved for as long. Returns the certificates dropped."""
        now = int(time.time()) if now is None else now
        await self._repo.prune_cursors(older_than=now - DELETE_LOG_TTL_S)
        return await self._repo.prune(older_than=now - DELETE_LOG_TTL_S)
