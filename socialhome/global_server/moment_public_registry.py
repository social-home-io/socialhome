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
   registry holds zero moment bytes — the fan-out is in-memory only,
   then forgotten — and since v_62 every envelope is ciphertext the GFS
   cannot open (the routes refuse any other shape). ONE ciphertext per
   moment, the same bytes to every follower.
4. **Key routing (v_62)** — the author's household seals its moment key
   to each follower household. The GFS only *routes* and *keeps*: it tells
   the author who followed (with the follower household's published
   key-wrap key and declared suites), keeps the LATEST sealed key per
   (author, follower household) in a mailbox it can't open, pushes it at
   once or when that household next connects, answers a follower's key
   request from the mailbox when it can, and forwards it to the author
   only when it can't.

This module *only* wires the data model + push primitives. The HTTP
routes live in ``routes/moments_public.py`` and the WS frame handler
hooks live in the GFS WS endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..moment_public_seal import MOMENT_SEAL_SUITE_AESGCM256
from .domain import (
    ClientInstance,
    GfsMomentFollow,
    GfsMomentKeyMail,
    GfsUserRegistration,
)
from .public import SlidingWindowCounter
from .repositories import (
    AbstractGfsFederationRepo,
    AbstractGfsMomentFollowRepo,
    AbstractGfsMomentKeyMailboxRepo,
    AbstractGfsUserRegistrationRepo,
)
from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Key requests one follower household may send about one author per
#: minute. A follower asks only when a moment arrives under a key it lacks,
#: so one or two a minute is plenty; more would only make the author's
#: household seal again.
KEY_REQUESTS_PER_MINUTE: int = 2

#: Follower user ids of one household named to the author per notice.
MAX_FOLLOWER_USERS_NAMED: int = 50

#: Sealed keys pushed to one household when it connects (its followed
#: authors' newest undelivered keys, oldest first). The rest wait for the
#: next connect — or the follower asks.
MAX_MAILBOX_PUSH_ON_CONNECT: int = 500


def suites_can_open(raw: str | None) -> bool:
    """Whether a follower household's declared suites (JSON) include the
    moment seal suite this build publishes under."""
    if not raw:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(parsed, dict):
        return False
    seal = parsed.get("seal")
    return isinstance(seal, list) and MOMENT_SEAL_SUITE_AESGCM256 in seal


class MomentPublicRegistry:
    """Per-GFS facade over registrations + follows + fan-out + key routing."""

    __slots__ = (
        "_follows",
        "_instances",
        "_key_requests",
        "_mailbox",
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
        self._key_requests = SlidingWindowCounter(KEY_REQUESTS_PER_MINUTE)
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
        return await self._fan_out(envelope, frame_type="incoming_public_moment")

    async def fan_out_delete(
        self,
        *,
        envelope: dict[str, Any],
    ) -> int:
        return await self._fan_out(envelope, frame_type="incoming_public_moment_delete")

    async def _fan_out(self, envelope: dict[str, Any], *, frame_type: str) -> int:
        author = envelope.get("author_user_id")
        if not author:
            log.warning("moment_public: envelope missing author_user_id")
            return 0
        followers = await self._follows.followers_of(str(author))
        able: dict[str, bool] = {}
        for f in followers:
            # A household that declared the suite on any of its follows.
            able[f.follower_instance_id] = able.get(
                f.follower_instance_id, False
            ) or suites_can_open(f.moment_suites)
        delivered = 0
        frame = {"type": frame_type, "payload": envelope}
        for instance_id, can_open in able.items():
            if can_open and await self._ws_registry.send(instance_id, frame):
                delivered += 1
        log.info(
            "moment_public.fanout: author=%s households=%d able=%d delivered=%d",
            author,
            len(able),
            sum(able.values()),
            delivered,
        )
        return delivered

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
                delivery=json.dumps(item, separators=(",", ":"), sort_keys=True),
            )
            if self._mailbox is not None:
                await self._mailbox.put(mail)
            if await self._push(mail):
                delivered += 1
        log.info(
            "moment_public.keys: %d of %d sealed key(s) delivered now",
            delivered,
            len(deliveries),
        )
        return delivered

    async def _push(self, mail: GfsMomentKeyMail) -> bool:
        try:
            payload = json.loads(mail.delivery)
        except ValueError:  # pragma: no cover - we wrote it
            return False
        ok = await self._ws_registry.send(
            mail.follower_instance_id, {"type": "moment_key", "payload": payload}
        )
        if ok and self._mailbox is not None:
            await self._mailbox.mark_delivered(
                mail.author_user_id, mail.follower_instance_id, mail.key_gen
            )
        return ok

    def schedule_follower_connected(self, instance_id: str) -> None:
        """Fire-and-forget :meth:`on_follower_connected` — the WebSocket
        handshake never waits on it."""
        task = asyncio.create_task(self.on_follower_connected(instance_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_follower_connected(self, instance_id: str) -> int:
        """Push the sealed keys this household missed while offline (only
        the undelivered ones — a reconnect re-sends nothing it already
        got). Never raises; returns how many went out."""
        if self._mailbox is None:
            return 0
        sent = 0
        try:
            for mail in await self._mailbox.undelivered_for(
                instance_id, limit=MAX_MAILBOX_PUSH_ON_CONNECT
            ):
                if not await self._push(mail):
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
        await self._ws_registry.send(
            reg.instance_id,
            {
                "type": "moment_key_request",
                "followed_user_id": followed_user_id,
                "want_gen": want_gen,
                "follower_user_ids": users,
                "follower": follower,
            },
        )

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
            suites = json.loads(suites_raw) if suites_raw else None
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
        await self._ws_registry.send(author_instance_id, frame)
