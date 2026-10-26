"""GFS moment-public registry (§Momentum-public).

Four responsibilities:

1. **User registration** — keep the public directory of opted-in users
   and the home-instance public key the GFS denormalises so any
   follower can verify the author's per-moment signature without
   round-tripping back to this registry.
2. **Follow graph** — track which instances/users follow each
   registered author, and (v_62) which moment suites each follower
   household said it can open.
3. **Fan-out** — given a signed envelope from the author's instance,
   look up the followers and push an ``incoming_public_moment`` WS
   frame to each unique follower household that can open it. The
   registry keeps no moment bytes; the only copies at rest are the
   offline ones — the same frame queued (24 h, deleted on delivery) for a
   recently seen follower household no node holds a socket for. Since
   v_62 every envelope is ciphertext the GFS cannot open (the routes
   refuse any other shape). ONE ciphertext per moment, the same bytes to
   every follower.
4. **Key routing (v_62)** — the author's household seals its moment key
   to each follower household. The GFS only *routes* and *keeps*: it tells
   the author who followed (with the follower household's published
   key-wrap key and declared suites), keeps the LATEST sealed key per
   (author, follower household) in a mailbox it can't open, pushes it at
   once or when that household next connects, answers a follower's key
   request from the mailbox when it can, and forwards it to the author
   only when it can't.

**Cluster (§24.10).** Each GFS node holds only its own ``/gfs/ws`` sockets,
behind a round-robin balancer on one shared database, so every push here
reaches households on EVERY node:

* a moment / tombstone is pushed to the followers connected here, then
  forwarded ONCE per peer node (``ClusterRelayFanOut``, audience
  ``followers`` + the author's user id); each node resolves the followers
  from the shared database and pushes to the sockets it holds. A recently
  seen follower household no node holds gets the frame queued in the shared
  relay queue (24 h — a moment's life) and drained when it reconnects;
* a sealed key whose household has no socket here stays in the shared
  mailbox and rides the coalesced drain hint (``ClusterService.hint_drain``)
  to the node that holds it, which pushes the undelivered keys;
* ``follow_changed`` / ``moment_key_request`` to the author's household are
  forwarded (audience ``instance``) when its socket is on another node.

**Reaction totals (v_69).** An author household's encrypted totals for one
of its public moments (``POST /gfs/moments/counts``, the same 8-field
envelope, today's key step) are pushed as ``incoming_public_moment_counts``
to the follower households that declared ``ops: ["counts"]`` and are
connected — here, and on the other nodes (cluster audience
``count_followers``) — and **never queued**: a follower offline now gets the
next checkpoint, or keeps its own count. Budgets, cluster-wide:
:data:`COUNTS_PER_HOUSEHOLD_PER_MINUTE` per author household (each node its
share) and :data:`COUNTS_PER_MOMENT_PER_DAY` per moment (every node counts
the updates it is forwarded too). The GFS reads no count; it learns that a
public moment's totals changed before a checkpoint.

This module *only* wires the data model + push primitives. The HTTP
routes live in ``routes/moments_public.py`` and the WS frame handler
hooks live in the GFS WS endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import orjson

from ..moment_public_seal import (
    FRAME_MOMENT,
    FRAME_MOMENT_COUNTS,
    FRAME_MOMENT_DELETE,
    FRAME_MOMENT_REACTION,
    MOMENT_OP_COUNTS,
    MOMENT_SEAL_SUITE_AESGCM256,
    RATCHET_STEP_S,
)
from .cluster_relay import (
    AUDIENCE_COUNT_FOLLOWERS,
    AUDIENCE_FOLLOWERS,
    AUDIENCE_INSTANCE,
    typed_frame,
)
from .domain import (
    ClientInstance,
    GfsMomentFollow,
    GfsMomentKeyMail,
    GfsUserRegistration,
)
from .envelope_relay import ENVELOPE_QUEUE_TTL_SECONDS
from .federation import SeenPayloadCache
from .public import SlidingWindowCounter, node_share
from .repositories import (
    AbstractGfsFederationRepo,
    AbstractGfsMomentFollowRepo,
    AbstractGfsMomentKeyMailboxRepo,
    AbstractGfsUserRegistrationRepo,
)
from .ws_registry import GfsWebSocketRegistry

if TYPE_CHECKING:
    from .cluster import ClusterService
    from .cluster_relay import ClusterRelayFanOut
    from .envelope_relay import GfsEnvelopeRelay

log = logging.getLogger(__name__)

#: Key requests one follower household may send about one author per
#: minute. A follower asks only when a moment arrives under a key it lacks,
#: so one or two a minute is plenty; more would only make the author's
#: household seal again.
KEY_REQUESTS_PER_MINUTE: int = 2

#: How far (seconds) the GFS's clock may disagree with an author household's
#: about which day an envelope's key step is — an hour, so a household whose
#: clock is a little off still publishes, and an envelope is accepted at most
#: 25 h after it was sealed: the window a follower accepts it in
#: (``SENT_AT_MAX_AGE_S`` in :mod:`socialhome.services.moment_public_inbound`).
MOMENT_STEP_SLACK_S: int = 3600

#: How long a node remembers a public moment it fanned out (or was forwarded):
#: as long as its key step can still be current (one step + the slack). A
#: re-post of the same bytes inside that time is a no-op; after it the step
#: check (:func:`envelope_step_current`) refuses it. So a captured envelope is
#: fanned out once per node, never again (#1111 follow-up).
MOMENT_SEEN_TTL_S: float = float(RATCHET_STEP_S + MOMENT_STEP_SLACK_S)

#: Most moments one node remembers (~140 B each: ~28 MiB when full). That is
#: ~190 000 public moments a day per GFS before the oldest are forgotten
#: early — and a forgotten one can be re-posted only until its step ends.
MOMENT_SEEN_MAX: int = 200_000

#: Public moments (creates + deletes) one author household may publish per
#: minute, across the whole cluster (each node allows its
#: :func:`~.public.node_share`). A household's users can post a top-level
#: moment at most once per 15 min each, so even 10 busy users replying fast
#: stay within a few a minute; 30 (one every 2 s, ~45 000 per 25 h) leaves
#: one household at most ~23 % of :data:`MOMENT_SEEN_MAX` — it can't evict
#: other authors' moments from the seen cache.
MOMENT_PUBLISHES_PER_MINUTE: int = 30

#: Follower user ids of one household named to the author per notice.
MAX_FOLLOWER_USERS_NAMED: int = 50

#: Sealed keys pushed to one household when it connects (its followed
#: authors' newest undelivered keys, oldest first). The rest wait for the
#: next connect — or the follower asks.
MAX_MAILBOX_PUSH_ON_CONNECT: int = 500

#: v_68 — sealed reactions one follower household may send toward one
#: author per minute, and in all per minute, across the cluster (each node
#: allows its :func:`~.public.node_share`). A household coalesces each
#: person's changes to one moment over 3 s, so 30 is a busy household
#: reacting to a burst of one author's moments; 120 several such authors.
REACTIONS_PER_AUTHOR_PER_MINUTE: int = 30
REACTIONS_PER_HOUSEHOLD_PER_MINUTE: int = 120

#: v_68 — how long a node remembers a relayed sealed reaction (by digest),
#: and how many: a follower's retry of a post whose answer it lost is not
#: pushed twice and spends no budget. The author's household refuses a
#: replay for 25 h on its own.
REACTION_SEEN_TTL_S: float = 600.0
REACTION_SEEN_MAX: int = 50_000


#: v_69 — reaction-total updates one author household may post a minute,
#: across the cluster (each node allows its :func:`~.public.node_share`). An
#: author household sends at most three per public moment, at fixed
#: checkpoints and only on change; ten a minute is a burst of ten moments
#: reaching a checkpoint together.
COUNTS_PER_HOUSEHOLD_PER_MINUTE: int = 10

#: v_69 — updates of one moment a day, across the cluster: every node counts
#: the ones it took AND the ones it was forwarded, so the cap holds whichever
#: node a post lands on. The author sends at +10 min, +3 h and +24 h.
COUNTS_PER_MOMENT_PER_DAY: int = 3


def envelope_step_current(key_gen: int, key_step: int, now: float) -> bool:
    """Whether *now* falls in the day of the envelope's key step (± the
    slack). ``key_gen`` is the generation's mint time and a step lasts one
    :data:`~socialhome.moment_public_seal.RATCHET_STEP_S`, so an author's
    household always seals under the step of the day it sends in — both
    values are plaintext routing fields the GFS already reads, so this check
    learns nothing new. An envelope from an earlier day is a re-post."""
    start = key_gen + key_step * RATCHET_STEP_S
    return (
        start - MOMENT_STEP_SLACK_S
        <= now
        <= start + RATCHET_STEP_S + MOMENT_STEP_SLACK_S
    )


def suites_take_counts(raw: str | None) -> bool:
    """Whether a follower household's declared suites (JSON) say it opens
    public moments AND takes reaction totals (``ops`` names ``counts``,
    v_69)."""
    if not suites_can_open(raw):
        return False
    assert raw is not None
    ops = orjson.loads(raw).get("ops")
    return isinstance(ops, list) and MOMENT_OP_COUNTS in ops


def suites_can_open(raw: str | None) -> bool:
    """Whether a follower household's declared suites (JSON) include the
    moment seal suite this build publishes under."""
    if not raw:
        return False
    try:
        parsed = orjson.loads(raw)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    seal = parsed.get("seal")
    return isinstance(seal, list) and MOMENT_SEAL_SUITE_AESGCM256 in seal


class MomentPublicRegistry:
    """Per-GFS facade over registrations + follows + fan-out + key routing."""

    __slots__ = (
        "_cluster",
        "_counts_household",
        "_counts_moment",
        "_counts_seen",
        "_fan_out_fwd",
        "_follows",
        "_instances",
        "_key_requests",
        "_mailbox",
        "_publishes",
        "_reaction_seen",
        "_reactions_author",
        "_reactions_household",
        "_relay",
        "_seen",
        "_tasks",
        "_users",
        "_ws_registry",
    )

    def __init__(
        self,
        users: AbstractGfsUserRegistrationRepo,
        follows: AbstractGfsMomentFollowRepo,
        ws_registry: GfsWebSocketRegistry,
        instances: AbstractGfsFederationRepo | None = None,
        mailbox: AbstractGfsMomentKeyMailboxRepo | None = None,
        *,
        relay: GfsEnvelopeRelay | None = None,
        fan_out: ClusterRelayFanOut | None = None,
        cluster: ClusterService | None = None,
    ) -> None:
        self._users = users
        self._follows = follows
        self._ws_registry = ws_registry
        #: Registered households — the follower's published key-wrap key
        #: the author seals the moment key to (v_62). Without it the
        #: follow notice names no key and nothing is sealed.
        self._instances = instances
        #: The latest sealed key per (author, follower household) (v_62).
        #: Without it a key reaches only a follower that is online.
        self._mailbox = mailbox
        #: The shared relay queue: a moment for a recently-seen follower
        #: household no node holds a socket for waits there (24 h).
        self._relay = relay
        #: Forwards a moment to the other cluster nodes, and a push for the
        #: author's household to the node holding its socket.
        self._fan_out_fwd = fan_out
        #: Wakes the node holding a follower's socket for a sealed key that
        #: waits in the shared mailbox (``NODE_DRAIN_HINT``).
        self._cluster = cluster
        #: Envelopes fanned out here or forwarded to / from another node —
        #: the same cache the cluster relay dedupes ``followers`` items with
        #: (:attr:`seen_cache`), so a re-POST of the same bytes to ANY node
        #: within :data:`MOMENT_SEEN_TTL_S` is a no-op.
        self._seen = SeenPayloadCache(ttl_s=MOMENT_SEEN_TTL_S, cap=MOMENT_SEEN_MAX)
        self._key_requests = SlidingWindowCounter(KEY_REQUESTS_PER_MINUTE)
        self._publishes = SlidingWindowCounter(MOMENT_PUBLISHES_PER_MINUTE)
        #: v_68 — sealed reactions: per (follower household, author) and per
        #: follower household a minute, and the digests relayed lately. How
        #: many wait queued for an offline author household is bounded by
        #: the queue itself (``RELAY_QUEUE_MAX_REACTIONS_PER_RECIPIENT``).
        self._reactions_author = SlidingWindowCounter(REACTIONS_PER_AUTHOR_PER_MINUTE)
        self._reactions_household = SlidingWindowCounter(
            REACTIONS_PER_HOUSEHOLD_PER_MINUTE
        )
        self._reaction_seen = SeenPayloadCache(
            ttl_s=REACTION_SEEN_TTL_S, cap=REACTION_SEEN_MAX
        )
        #: v_69 — reaction totals: per author household a minute, per moment a
        #: day (cluster-wide, see :meth:`note_forwarded_counts`), and the
        #: updates pushed or forwarded lately (a retried post is a no-op).
        self._counts_household = SlidingWindowCounter(COUNTS_PER_HOUSEHOLD_PER_MINUTE)
        self._counts_moment = SlidingWindowCounter(
            COUNTS_PER_MOMENT_PER_DAY, max_keys=MOMENT_SEEN_MAX, window_s=86_400.0
        )
        self._counts_seen = SeenPayloadCache(
            ttl_s=MOMENT_SEEN_TTL_S, cap=MOMENT_SEEN_MAX
        )
        self._tasks: set[asyncio.Task] = set()

    # ── Registration ────────────────────────────────────────────────────

    async def register_user(
        self,
        *,
        user_id: str,
        instance_id: str,
        username: str,
        display_name: str,
        home_instance_pk: str,
        picture_url: str | None = None,
        bio: str | None = None,
        picture_digest: str | None = None,
    ) -> GfsUserRegistration:
        reg = GfsUserRegistration(
            user_id=user_id,
            instance_id=instance_id,
            username=username,
            display_name=display_name,
            picture_url=picture_url,
            home_instance_pk=home_instance_pk,
            registered_at=int(time.time()),
            status="active",
            bio=bio,
            picture_digest=picture_digest,
        )
        await self._users.upsert(reg)
        return reg

    async def deregister_user(self, user_id: str) -> bool:
        deleted = await self._users.delete(user_id)
        return deleted > 0

    async def get_registration(self, user_id: str) -> GfsUserRegistration | None:
        return await self._users.get(user_id)

    async def is_author_household(self, user_id: str, instance_id: str) -> bool:
        """Whether *user_id* is registered here, active, by *instance_id*."""
        reg = await self._users.get(user_id)
        return (
            reg is not None
            and reg.status == "active"
            and (reg.instance_id == instance_id)
        )

    async def list_directory(
        self, *, q: str | None = None, limit: int = 200
    ) -> list[GfsUserRegistration]:
        return await self._users.list_active(q=q, limit=limit)

    async def set_picture_digest(
        self, *, user_id: str, picture_digest: str | None
    ) -> None:
        await self._users.set_picture_digest(
            user_id=user_id, picture_digest=picture_digest
        )

    # ── Follow graph ────────────────────────────────────────────────────

    async def add_follow(
        self,
        *,
        follower_user_id: str,
        follower_instance_id: str,
        followed_user_id: str,
        moment_suites: str | None = None,
    ) -> GfsMomentFollow:
        # Refuse to record a follow against an unknown / suspended
        # author so the directory stays the source of truth.
        target = await self._users.get(followed_user_id)
        if target is None or target.status != "active":
            raise LookupError(
                f"author {followed_user_id!r} not registered or suspended"
            )
        follow = GfsMomentFollow(
            follower_user_id=follower_user_id,
            follower_instance_id=follower_instance_id,
            followed_user_id=followed_user_id,
            created_at=int(time.time()),
            moment_suites=moment_suites,
        )
        await self._follows.upsert(follow)
        # Notify the author's instance: the UI follower count ticks, and
        # (v_62) it seals its moment key to the newcomer.
        await self._notify_author(target.instance_id, follow, action="add")
        return follow

    async def declare_suites(
        self, *, follower_instance_id: str, moment_suites: str
    ) -> int:
        """A household re-declares what it can open (v_62) for every follow
        it holds — a follow made before it upgraded declared nothing and so
        got no public moments. Returns how many follows changed. Nobody is
        notified: the household asks for the keys it lacks when a moment
        arrives (``key_request``)."""
        return await self._follows.set_household_suites(
            follower_instance_id, moment_suites
        )

    async def remove_follow(
        self,
        *,
        follower_user_id: str,
        followed_user_id: str,
    ) -> bool:
        instance_id = ""
        for row in await self._follows.following_of(follower_user_id):
            if row.followed_user_id == followed_user_id:
                instance_id = row.follower_instance_id
        deleted = await self._follows.delete(
            follower_user_id=follower_user_id,
            followed_user_id=followed_user_id,
        )
        if deleted == 0:
            return False
        # v_62: keys are per household — another user of the same household
        # may still follow, and then nothing about the key changes.
        still = bool(
            instance_id
            and await self._follows.follower_user_ids_at(
                followed_user_id, instance_id, limit=1
            )
        )
        if instance_id and not still and self._mailbox is not None:
            await self._mailbox.delete(followed_user_id, instance_id)
        target = await self._users.get(followed_user_id)
        if target is not None:
            await self._notify_author(
                target.instance_id,
                GfsMomentFollow(
                    follower_user_id=follower_user_id,
                    follower_instance_id=instance_id,
                    followed_user_id=followed_user_id,
                    created_at=int(time.time()),
                ),
                action="remove",
                household_still_follows=still,
            )
        return True

    async def follower_count(self, followed_user_id: str) -> int:
        return await self._follows.follower_count(followed_user_id)

    async def follower_stats(self, followed_user_id: str) -> dict[str, int]:
        """Counts only — follower users, follower households, and the
        households that declared no encrypted-moment suites (v_62: they
        can't open public moments and are sent none)."""
        households, unable = await self._follows.household_counts(followed_user_id)
        return {
            "followers": await self._follows.follower_count(followed_user_id),
            "households": households,
            "households_unable": unable,
        }

    # ── Fan-out ─────────────────────────────────────────────────────────

    def allow_publish(self, instance_id: str) -> bool:
        """Count one accepted publish or delete from *instance_id*; ``False``
        past :data:`MOMENT_PUBLISHES_PER_MINUTE` — this node's share of it
        over the live cluster nodes, so the budget is cluster-wide behind a
        round-robin balancer."""
        nodes = self._cluster.live_node_count() if self._cluster is not None else 1
        return self._publishes.allow(
            instance_id, limit=node_share(MOMENT_PUBLISHES_PER_MINUTE, nodes)
        )

    @property
    def seen_cache(self) -> SeenPayloadCache:
        """The moments this node pushed or forwarded — handed to the cluster
        relay for the ``followers`` audience, so forwarded copies count too."""
        return self._seen

    async def fan_out_moment(
        self,
        *,
        envelope: dict[str, Any],
    ) -> int:
        """Push a signed ``moment_public`` envelope to every follower
        household that can open it and return how many were reached.

        ``envelope`` must already be a dict ready to ship over the WS;
        the GFS forwards it verbatim as the ``payload`` of an
        ``incoming_public_moment`` frame so the recipient verifies the
        author's signature directly.
        """
        return await self._fan_out(envelope, frame_type=FRAME_MOMENT)

    async def fan_out_delete(
        self,
        *,
        envelope: dict[str, Any],
    ) -> int:
        return await self._fan_out(envelope, frame_type=FRAME_MOMENT_DELETE)

    async def audience_targets(self, author_user_id: str) -> list[str]:
        """The follower households of *author_user_id* that can open its
        moments — what a forwarded moment reaches (the cluster audience
        resolver for ``followers``, :mod:`.cluster_relay`)."""
        able: dict[str, bool] = {}
        for f in await self._follows.followers_of(author_user_id):
            # A household that declared the suite on any of its follows.
            able[f.follower_instance_id] = able.get(
                f.follower_instance_id, False
            ) or suites_can_open(f.moment_suites)
        return [instance_id for instance_id, can_open in able.items() if can_open]

    async def _fan_out(self, envelope: dict[str, Any], *, frame_type: str) -> int:
        """Push *envelope* to the able followers connected HERE, forward it
        once to every other node, and queue it for the recently-seen ones no
        node holds. Returns how many were pushed here."""
        author = envelope.get("author_user_id")
        if not author:
            log.warning("moment_public: envelope missing author_user_id")
            return 0
        author = str(author)
        forwarded = typed_frame(frame_type, {"payload": envelope})
        digest = SeenPayloadCache.digest(forwarded)
        if self._seen.seen(digest) or (
            self._fan_out_fwd is not None and self._fan_out_fwd.seen_frame(forwarded)
        ):
            # The same bytes again (a retried publish, here or on another
            # node): every follower already got them once.
            log.debug("moment_public.fanout: suppressing a replayed %s", frame_type)
            return 0
        self._seen.record(digest)
        able = await self.audience_targets(author)
        push = {"type": frame_type, "payload": envelope}
        delivered = 0
        missed: list[str] = []
        for instance_id in able:
            if await self._ws_registry.send(instance_id, push):
                delivered += 1
            else:
                missed.append(instance_id)
        queue_for = await self._offline_candidates(author, missed)
        # The other nodes push to the sockets they hold; the candidates none
        # of them holds are queued once their answers are in. Not forwarded
        # (one node, or the forward buffer is full): queue them now.
        forwarded_ok = self._fan_out_fwd is not None and self._fan_out_fwd.forward(
            kind=AUDIENCE_FOLLOWERS, key=author, frame=forwarded, queue_for=queue_for
        )
        queued = 0
        if queue_for and not forwarded_ok and self._relay is not None:
            queued = await self._relay.queue_relay_many(queue_for, push)
        log.info(
            "moment_public.fanout: author=%s able=%d delivered_here=%d queued_now=%d",
            author,
            len(able),
            delivered,
            queued,
        )
        return delivered

    async def _offline_candidates(self, author: str, missed: list[str]) -> list[str]:
        """The *missed* households that get an offline copy if no node holds
        them: only those seen within the queue TTL (a household that never
        connected, or vanished long ago, would only hold rows until they
        expire — and a bare hello must not earn disk)."""
        if not missed or self._relay is None:
            return []
        recent = await self._follows.recently_seen_households(
            author, within_s=ENVELOPE_QUEUE_TTL_SECONDS
        )
        return [instance_id for instance_id in missed if instance_id in recent]

    # ── Reaction totals (v_69) ──────────────────────────────────────────

    @property
    def counts_seen_cache(self) -> SeenPayloadCache:
        """The totals updates this node pushed or forwarded — the cluster
        relay dedupes ``count_followers`` items in it."""
        return self._counts_seen

    async def counts_audience_targets(self, author_user_id: str) -> list[str]:
        """The follower households of *author_user_id* that open its moments
        and declared ``ops: ["counts"]`` (on any of their follows) — the
        cluster audience resolver for ``count_followers``."""
        able: dict[str, bool] = {}
        for f in await self._follows.followers_of(author_user_id):
            able[f.follower_instance_id] = able.get(
                f.follower_instance_id, False
            ) or suites_take_counts(f.moment_suites)
        return [instance_id for instance_id, ok in able.items() if ok]

    def note_forwarded_counts(self, frame: dict) -> None:
        """Charge the per-moment budget for a totals update another node
        took (its forwarded frame), so the day's cap is cluster-wide."""
        envelope = frame.get("payload")
        if not isinstance(envelope, dict):
            return
        author, moment_id = envelope.get("author_user_id"), envelope.get("moment_id")
        if isinstance(author, str) and isinstance(moment_id, str):
            self._counts_moment.allow(f"{author}|{moment_id}")

    async def relay_counts(self, *, sender: str, envelope: dict[str, Any]) -> str:
        """Push an author household's encrypted totals (already checked:
        the shape, the author's own household, today's step) to the able
        followers connected here, and forward them once to the other nodes —
        never queued. ``"accepted"`` (also for a retried post: the same bytes
        are not pushed twice and spend nothing) or ``"rate_limited"``."""
        author = str(envelope["author_user_id"])
        forwarded = typed_frame(FRAME_MOMENT_COUNTS, {"payload": envelope})
        digest = SeenPayloadCache.digest(forwarded)
        if self._counts_seen.seen(digest):
            return "accepted"
        moment_key = f"{author}|{envelope['moment_id']}"
        nodes = self._cluster.live_node_count() if self._cluster is not None else 1
        if self._counts_moment.exhausted(moment_key) or not (
            self._counts_household.allow(
                sender, limit=node_share(COUNTS_PER_HOUSEHOLD_PER_MINUTE, nodes)
            )
        ):
            return "rate_limited"
        self._counts_moment.allow(moment_key)
        self._counts_seen.record(digest)
        push = {"type": FRAME_MOMENT_COUNTS, "payload": envelope}
        delivered = 0
        targets = await self.counts_audience_targets(author)
        for instance_id in targets:
            if await self._ws_registry.send(instance_id, push):
                delivered += 1
        if self._fan_out_fwd is not None:
            # Never queued (``queue_for`` empty, and the frame type is
            # ephemeral): an offline follower gets the next checkpoint.
            self._fan_out_fwd.forward(
                kind=AUDIENCE_COUNT_FOLLOWERS, key=author, frame=forwarded
            )
        log.info(
            "moment_public.counts: author=%s able=%d delivered_here=%d",
            author,
            len(targets),
            delivered,
        )
        return "accepted"

    # ── Key routing (v_62) ──────────────────────────────────────────────

    async def deliver_keys(
        self, *, sender_instance_id: str, deliveries: list[dict[str, Any]]
    ) -> int:
        """Keep each sealed moment key for the ONE household it names and
        push it now if that household is connected.

        A delivery is taken only when its author is registered here by the
        sending household and its ``to`` household follows that author —
        so an author can't use the GFS to push to strangers, and nobody but
        the author's household can address its followers. The GFS never
        opens ``sealed`` (it is sealed to ``to``'s key-wrap key). Returns how
        many were pushed over a live socket (the rest wait in the mailbox)."""
        delivered = 0
        authors: dict[str, bool] = {}
        for item in deliveries:
            author = str(item.get("author_user_id") or "")
            to = str(item.get("to") or "")
            if not author or not to:
                continue
            if author not in authors:
                authors[author] = await self.is_author_household(
                    author, sender_instance_id
                )
            if not authors[author]:
                continue
            if not await self._follows.follower_user_ids_at(author, to, limit=1):
                continue
            mail = GfsMomentKeyMail(
                author_user_id=author,
                follower_instance_id=to,
                key_gen=int(item.get("key_gen") or 0),
                key_step=int(item.get("key_step") or 0),
                delivery=orjson.dumps(item, option=orjson.OPT_SORT_KEYS).decode(),
            )
            # An older key than the one kept is a re-posted capture: neither
            # kept nor pushed (the household already has a newer one).
            if self._mailbox is not None and not await self._mailbox.put(mail):
                log.info(
                    "moment_public.keys: an older key for %s → %s ignored",
                    author,
                    to,
                )
                continue
            if await self._push(mail):
                delivered += 1
        log.info(
            "moment_public.keys: %d of %d sealed key(s) delivered now",
            delivered,
            len(deliveries),
        )
        return delivered

    async def _push(self, mail: GfsMomentKeyMail, *, wake: bool = True) -> bool:
        """Push *mail* to its household's socket on THIS node. With *wake*,
        a household with no socket here gets the kept key from the node that
        holds its socket: the key is (again) marked undelivered in the shared
        mailbox and the household id rides the next drain hint. Never with
        *wake* from a push the hint itself started — that would loop."""
        try:
            payload = orjson.loads(mail.delivery)
        except ValueError:  # pragma: no cover - we wrote it
            return False
        ok = await self._ws_registry.send(
            mail.follower_instance_id, {"type": "moment_key", "payload": payload}
        )
        if self._mailbox is None:
            return ok
        if ok:
            await self._mailbox.mark_delivered(
                mail.author_user_id, mail.follower_instance_id, mail.key_gen
            )
        elif wake and self._cluster is not None:
            if mail.delivered:
                await self._mailbox.mark_undelivered(
                    mail.author_user_id, mail.follower_instance_id, mail.key_gen
                )
            self._cluster.hint_drain(mail.follower_instance_id)
        return ok

    async def _send_to_household(self, instance_id: str, frame: dict) -> bool:
        """Push *frame* to *instance_id*'s socket here, or forward it once to
        the other nodes (one of them may hold it). ``True`` iff pushed here."""
        if await self._ws_registry.send(instance_id, frame):
            return True
        if self._fan_out_fwd is not None:
            self._fan_out_fwd.forward_to_household(instance_id, frame)
        return False

    def schedule_follower_connected(self, instance_id: str) -> None:
        """Fire-and-forget :meth:`on_follower_connected` — the WebSocket
        handshake never waits on it."""
        task = asyncio.create_task(self.on_follower_connected(instance_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_follower_connected(self, instance_id: str) -> int:
        """Push the sealed keys this household missed while offline (only
        the undelivered ones — a reconnect re-sends nothing it already
        got). Run on the hello, and on a ``NODE_DRAIN_HINT`` naming a
        household whose socket is on this node (a key kept by another node).
        Never raises; returns how many went out."""
        if self._mailbox is None:
            return 0
        sent = 0
        try:
            for mail in await self._mailbox.undelivered_for(
                instance_id, limit=MAX_MAILBOX_PUSH_ON_CONNECT
            ):
                if not await self._push(mail, wake=False):
                    break
                sent += 1
        except Exception:  # defensive — never fail a connect on this
            log.exception("moment_public.keys: mailbox push failed")
        if sent:
            log.info("moment_public.keys: %d waiting sealed key(s) pushed", sent)
        return sent

    async def request_key(
        self, *, follower_instance_id: str, followed_user_id: str, want_gen: int
    ) -> None:
        """A follower household has no key for generation *want_gen*.

        Only for a household that follows the author here, at most
        :data:`KEY_REQUESTS_PER_MINUTE` per (household, author). Answered
        from the mailbox when it holds that generation (or a newer one) —
        the author's household is asked only when it doesn't. Best-effort
        and silent: an unknown author, a non-follower, an offline author
        and a throttled request all look the same to the caller."""
        reg = await self._users.get(followed_user_id)
        if reg is None or reg.status != "active":
            return
        users = await self._follows.follower_user_ids_at(
            followed_user_id, follower_instance_id, limit=MAX_FOLLOWER_USERS_NAMED
        )
        if not users:
            return
        if not self._key_requests.allow(f"{follower_instance_id}|{followed_user_id}"):
            return
        if self._mailbox is not None:
            held = await self._mailbox.get(followed_user_id, follower_instance_id)
            if held is not None and held.key_gen >= want_gen:
                await self._push(held)
                return
        follower = await self._follower_keys(followed_user_id, follower_instance_id)
        if follower is None:
            return
        await self._send_to_household(
            reg.instance_id,
            {
                "type": "moment_key_request",
                "followed_user_id": followed_user_id,
                "want_gen": want_gen,
                "follower_user_ids": users,
                "follower": follower,
            },
        )

    # ── Reactions (v_68) ────────────────────────────────────────────────

    async def relay_reaction(
        self,
        *,
        follower_instance_id: str,
        followed_user_id: str,
        sealed: dict[str, str],
    ) -> str:
        """Pass a follower household's sealed reaction to the author's
        household. ``"accepted"``, ``"not_following"`` or ``"rate_limited"``.

        Only from a household that follows *followed_user_id* here (any of
        its users), never from the author's own household. The GFS can't
        open *sealed* (it is sealed to the author household's key-wrap key)
        and learns only that this follower household sent the author's
        household a reaction or a removal, and when. Limits, cluster-wide:
        :data:`REACTIONS_PER_AUTHOR_PER_MINUTE` per (household, author) and
        :data:`REACTIONS_PER_HOUSEHOLD_PER_MINUTE` per household — charged
        only for a reaction not relayed already, so a retried post spends
        nothing. Pushed to the author household's socket here, or on the
        node holding it, or queued 24 h when no node does — as a capped kind
        of relay row (``RELAY_QUEUE_MAX_REACTIONS_PER_RECIPIENT``) that only
        ever displaces an older queued reaction, never the household's
        queued moments or space items (#1179 F1)."""
        reg = await self._users.get(followed_user_id)
        if (
            reg is None
            or reg.status != "active"
            or reg.instance_id == follower_instance_id
            or not await self._follows.follower_user_ids_at(
                followed_user_id, follower_instance_id, limit=1
            )
        ):
            return "not_following"
        digest = SeenPayloadCache.digest(sealed)
        if self._reaction_seen.seen(digest):
            log.debug("moment_public.reactions: a retried reaction — not re-sent")
            return "accepted"
        nodes = self._cluster.live_node_count() if self._cluster is not None else 1
        if not self._reactions_author.allow(
            f"{follower_instance_id}|{followed_user_id}",
            limit=node_share(REACTIONS_PER_AUTHOR_PER_MINUTE, nodes),
        ) or not self._reactions_household.allow(
            follower_instance_id,
            limit=node_share(REACTIONS_PER_HOUSEHOLD_PER_MINUTE, nodes),
        ):
            return "rate_limited"
        self._reaction_seen.record(digest)
        await self._push_reaction(
            reg.instance_id,
            {
                "type": FRAME_MOMENT_REACTION,
                # The household this server authenticated — the author
                # checks it names the reactor's own household.
                "from_instance": follower_instance_id,
                "sealed": sealed,
            },
        )
        return "accepted"

    async def _push_reaction(self, author_instance_id: str, push: dict) -> None:
        """Push *push* to the author household's socket here; else forward it
        once to the other nodes, and queue it (24 h) only when none of them
        holds the socket. Nothing is charged for a reaction that was
        delivered: the queue's own per-household reaction cap
        (``RELAY_QUEUE_MAX_REACTIONS_PER_RECIPIENT``, in the shared database,
        so cluster-wide) is the only bound on queued copies (#1179 F2)."""
        if await self._ws_registry.send(author_instance_id, push):
            return
        fields = {k: v for k, v in push.items() if k != "type"}
        if self._fan_out_fwd is not None and self._fan_out_fwd.forward(
            kind=AUDIENCE_INSTANCE,
            key=author_instance_id,
            frame=typed_frame(FRAME_MOMENT_REACTION, fields),
            queue_for=[author_instance_id],
        ):
            return
        if self._relay is not None:
            await self._relay.queue_relay_many([author_instance_id], push)

    # ── Internal ────────────────────────────────────────────────────────

    async def _follower_keys(
        self, followed_user_id: str, instance_id: str
    ) -> dict[str, Any] | None:
        """The follower household's registered identity + published key-wrap
        key and its declared suites, for the author to verify
        (``verify_keywrap_binding``) and seal to. ``None`` when unknown."""
        if self._instances is None or not instance_id:
            return None
        inst: ClientInstance | None = await self._instances.get_instance(instance_id)
        if inst is None or inst.status != "active":
            return None
        suites_raw = await self._follows.suites_at(followed_user_id, instance_id)
        try:
            suites = orjson.loads(suites_raw) if suites_raw else None
        except ValueError:
            suites = None
        return {
            "instance_id": inst.instance_id,
            "identity_public_key": inst.public_key,
            "keywrap_public_key": inst.keywrap_public_key,
            "kem_suite": inst.kem_suite,
            "keywrap_sig": inst.keywrap_sig,
            "moment_suites": suites if isinstance(suites, dict) else None,
        }

    async def _notify_author(
        self,
        author_instance_id: str,
        follow: GfsMomentFollow,
        *,
        action: str,
        household_still_follows: bool | None = None,
    ) -> None:
        frame: dict[str, Any] = {
            "type": "follow_changed",
            "action": action,
            "follower_user_id": follow.follower_user_id,
            "follower_instance_id": follow.follower_instance_id,
            "followed_user_id": follow.followed_user_id,
        }
        if action == "add":
            # v_62: the follower household's published keys + suites, so the
            # author seals its moment key to the newcomer right away — the
            # same material ``new_subscriber`` carries for a space follower.
            follower = await self._follower_keys(
                follow.followed_user_id, follow.follower_instance_id
            )
            if follower is not None:
                frame["follower"] = follower
        else:
            # v_62: the author rotates only when no user of that household
            # follows any more (the key is per household).
            frame["household_still_follows"] = bool(household_still_follows)
        await self._send_to_household(author_instance_id, frame)
