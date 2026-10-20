"""Publish our authors' public profiles to a space's GFS followers (v_60).

A household that follows a public / global space through a GFS holds no
roster, so its people saw every post there as "Someone". When one of our
users posts in such a space, this service publishes their profile into it as
the generic ``author_profile`` item (:mod:`socialhome.domain.space_author_profile`)
— signed by our household identity key, encrypted under the space content
key and sent over the existing member-publish path
(:meth:`GfsMemberPublishService.schedule_item`). The GFS sees ciphertext only.

* **When:** once per author per space per content-key epoch, again when the
  profile changes (``UserProfileUpdated`` / ``SpaceMemberProfileUpdated``)
  and again on the author's next post a day later, so a follower that
  subscribed mid-epoch learns the name soon. Never inlined in each post.
* **What:** the per-space name, else the household name; the household bio;
  the per-space picture, else the household one, re-encoded to at most
  256 px and 24 KB (``None`` when it can't fit). A **protected** account
  (§CP.R) sends its name only — no bio, no picture.
* **Version gate:** an older follower drops an unknown ``space_item`` type
  with a WARNING, and followers are anonymous, so their versions are
  unknown. The profile is published only while every member household
  whose version we know is at least
  :data:`~socialhome.domain.federation_capabilities.FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE`.
* **Where:** only a public / global space that admits followers — a
  private space's channel readers are all members and hold the roster.
* **A new follower (v_61):** the host, notified by the GFS
  (``new_subscriber``, right after it sealed the content key to them),
  republishes its own authors' profiles and sends ``SPACE_FOLLOWER_JOINED``
  to its member households (v_61+); each republishes its authors' profiles.
  At most once per space per :data:`FOLLOWER_REPUBLISH_COOLDOWN`. The nudge
  carries no content and never names the follower; the GFS learns nothing
  it does not already: the items are the usual ciphertext ``space_item`` s.

In-memory bookkeeping: a restart sends each author's profile once more on
their next post.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.events import (
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserProfileUpdated,
)
from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import PUBLIC_SPACE_TIERS
from ..domain.space_author_profile import (
    PROFILE_BIO_MAX_LENGTH,
    PROFILE_NAME_MAX_LENGTH,
    encode_profile_content,
)
from ..domain.space_item import ITEM_TYPE_AUTHOR_PROFILE
from ..infrastructure.event_bus import EventBus
from ..media.profile_picture import public_profile_picture
from .protection_gate import ProtectionGateMixin
from .space_item_author import build_signed_item_inner

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_member_publish_service import GfsMemberPublishService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)

#: A profile already sent this epoch goes out again on the author's next
#: post once this old, so followers who subscribed since get it too.
PROFILE_RESEND_AFTER: timedelta = timedelta(hours=24)

#: One follower-driven republish per space at most this often — a burst of
#: subscribes (or a hostile host) cannot make a household spam the GFS. A
#: follower inside the window is not dropped: one trailing republish runs
#: when it ends, covering everyone who subscribed meanwhile.
FOLLOWER_REPUBLISH_COOLDOWN: timedelta = timedelta(seconds=30)


class SpaceAuthorProfileOutbound(ProtectionGateMixin):
    """See the module docstring."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_crypto",
        "_federation",
        "_federation_repo",
        "_member_gfs",
        "_own_instance_id",
        "_own_instance_pk",
        "_own_seed",
        "_pending",
        "_pictures",
        "_posts",
        "_republished",
        "_sent",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        picture_repo: AbstractProfilePictureRepo,
        federation_repo: AbstractFederationRepo,
        space_crypto: SpaceContentEncryption,
        member_gfs: GfsMemberPublishService,
        own_instance_id: str,
        own_instance_pk: bytes,
        own_identity_seed: bytes,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._users = user_repo
        self._pictures = picture_repo
        self._federation_repo = federation_repo
        self._crypto = space_crypto
        self._member_gfs = member_gfs
        self._own_instance_id = own_instance_id
        self._own_instance_pk = own_instance_pk
        self._own_seed = own_identity_seed
        self._child_protection = None
        #: ``(space_id, user_id)`` → ``(epoch, profile_rev, sent_at)``.
        self._sent: dict[tuple[str, str], tuple[int | None, str, datetime]] = {}
        #: ``space_id`` → when its authors were last republished for a new
        #: follower (v_61).
        self._republished: dict[str, datetime] = {}
        #: The one trailing republish per space waiting out the cooldown.
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._federation: FederationService | None = None
        self._posts: AbstractSpacePostRepo | None = None

    def attach_follower_republish(
        self,
        *,
        federation_service: FederationService,
        space_post_repo: AbstractSpacePostRepo,
    ) -> None:
        """Wire the v_61 new-follower republish: who has posted in a space,
        and the federation the host nudges its members over (and receives
        the host's nudge on)."""
        self._federation = federation_service
        self._posts = space_post_repo
        federation_service._event_registry.register(
            FederationEventType.SPACE_FOLLOWER_JOINED, self._on_follower_joined
        )

    def wire(self) -> None:
        self._bus.subscribe(SpacePostCreated, self._on_post)
        self._bus.subscribe(UserProfileUpdated, self._on_profile)
        self._bus.subscribe(SpaceMemberProfileUpdated, self._on_space_profile)

    # ── Triggers ──────────────────────────────────────────────────────

    async def _on_post(self, event: SpacePostCreated) -> None:
        # A local post only (an inbound copy names its origin household).
        if not event.space_id or event.origin_instance_id is not None:
            return
        await self.publish(event.space_id, event.post.author)

    async def _on_profile(self, event: UserProfileUpdated) -> None:
        for space_id, user_id in list(self._sent):
            if user_id == event.user_id:
                await self.publish(space_id, user_id, changed=True)

    async def _on_space_profile(self, event: SpaceMemberProfileUpdated) -> None:
        if event.origin_instance_id is not None:
            return
        if (event.space_id, event.user_id) in self._sent:
            await self.publish(event.space_id, event.user_id, changed=True)

    # ── A new follower (v_61) ─────────────────────────────────────────

    async def on_new_follower(self, space_id: str) -> None:
        """The host was told a follower subscribed (and sealed the content
        key to them): republish our authors and nudge the member
        households. Never raises."""
        try:
            await self.republish_space(space_id, nudge=True)
        except Exception:
            log.exception(
                "space_author_profile: new-follower republish failed for %s",
                space_id,
            )

    async def _nudge_members(self, space_id: str) -> None:
        if self._federation is None:
            return
        await self._federation.broadcast_to_space_members(
            space_id,
            FederationEventType.SPACE_FOLLOWER_JOINED,
            {"space_id": space_id},
            min_proto_version=FederationCapability.MIN_FOR_FOLLOWER_JOINED,
        )

    async def _on_follower_joined(self, event: FederationEvent) -> None:
        """The host's nudge: republish our authors in that space. Only the
        space's host may send it (anyone else is ignored at WARNING)."""
        space_id = str(event.space_id or event.payload.get("space_id") or "")
        space = await self._spaces.get(space_id) if space_id else None
        if space is None or event.from_instance != space.owner_instance_id:
            log.warning(
                "space_author_profile: SPACE_FOLLOWER_JOINED from %s is not "
                "from the host of space %s — ignored",
                event.from_instance,
                space_id,
            )
            return
        await self.republish_space(space_id)

    async def republish_space(self, space_id: str, *, nudge: bool = False) -> bool:
        """Publish the profile of every local user with a post in
        *space_id*, whatever was sent before — at most once per
        :data:`FOLLOWER_REPUBLISH_COOLDOWN`. Inside the window one trailing
        republish is scheduled for its end instead (never dropped). With
        *nudge* (the host) each run also tells the member households.
        ``True`` when it ran now."""
        now = datetime.now(UTC)
        last = self._republished.get(space_id)
        if last is not None and now - last < FOLLOWER_REPUBLISH_COOLDOWN:
            if space_id not in self._pending:
                wait = (last + FOLLOWER_REPUBLISH_COOLDOWN - now).total_seconds()
                self._pending[space_id] = asyncio.create_task(
                    self._trailing(space_id, wait, nudge=nudge),
                    name=f"author-profile-republish-{space_id}",
                )
            return False
        self._republished[space_id] = now
        authors: set[str] = {u for (s, u) in self._sent if s == space_id}
        if self._posts is not None:
            authors |= await self._posts.list_author_ids(space_id)
        for user_id in sorted(authors):
            self._sent.pop((space_id, user_id), None)
            await self.publish(space_id, user_id)
        if nudge:
            await self._nudge_members(space_id)
        return True

    async def _trailing(self, space_id: str, wait: float, *, nudge: bool) -> None:
        try:
            await asyncio.sleep(max(wait, 0.0))
        finally:
            self._pending.pop(space_id, None)
        try:
            await self.republish_space(space_id, nudge=nudge)
        except Exception:
            log.exception(
                "space_author_profile: trailing republish failed for %s", space_id
            )

    async def stop(self) -> None:
        """Cancel the trailing republishes (graceful shutdown)."""
        tasks = list(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()

    # ── Publishing ────────────────────────────────────────────────────

    async def publish(
        self, space_id: str, user_id: str, *, changed: bool = False
    ) -> bool:
        """Publish ``user_id``'s profile into ``space_id`` when it is due
        (see the module docstring). ``True`` when one was scheduled.
        Never raises."""
        try:
            return await self._publish(space_id, user_id, changed=changed)
        except Exception:
            log.exception("space_author_profile: publish failed for space=%s", space_id)
            return False

    async def _publish(self, space_id: str, user_id: str, *, changed: bool) -> bool:
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.space_type not in PUBLIC_SPACE_TIERS
            or not space.features.allow_subscribers
        ):
            return False
        user = await self._users.get_by_user_id(user_id)
        if user is None:
            return False
        if not await self._members_understand(space_id):
            log.info(
                "space_author_profile: a member household of space %s is below "
                "v_%d — no profile published",
                space_id,
                FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE,
            )
            return False
        member = await self._spaces.get_member(space_id, user_id)
        name = (
            (member.space_display_name if member else None)
            or user.display_name
            or user.username
        )[:PROFILE_NAME_MAX_LENGTH]
        protected = await self._is_protected(user_id)
        bio = None if protected else (user.bio or None)
        if bio is not None:
            bio = bio[:PROFILE_BIO_MAX_LENGTH]
        picture = None if protected else await self._picture(space_id, user_id)
        content, rev = encode_profile_content(
            display_name=name, bio=bio, picture_webp=picture
        )
        epoch = await self._crypto.get_current_epoch(space_id)
        now = datetime.now(UTC)
        held = self._sent.get((space_id, user_id))
        if (
            held is not None
            and held[0] == epoch
            and held[1] == rev
            and now - held[2] < PROFILE_RESEND_AFTER
        ):
            return False
        if changed and held is not None and held[1] == rev:
            return False
        inner = build_signed_item_inner(
            item_type=ITEM_TYPE_AUTHOR_PROFILE,
            item_target=user_id,
            space_id=space_id,
            post_id=user_id,
            author_user_id=user_id,
            author_username=user.username,
            author_pk=self._own_instance_pk,
            author_identity_seed=self._own_seed,
            origin_instance_id=self._own_instance_id,
            author_identity_anchor=user.identity_anchor,
            ts=now.isoformat(),
            content=content,
        )
        if not self._member_gfs.schedule_item(
            space_id, user_id, ITEM_TYPE_AUTHOR_PROFILE, inner
        ):
            return False
        self._sent[(space_id, user_id)] = (epoch, rev, now)
        return True

    async def _members_understand(self, space_id: str) -> bool:
        """Every member household whose version we know is v_60+."""
        for instance_id in await self._spaces.list_member_instances(space_id):
            if instance_id == self._own_instance_id:
                continue
            peer = await self._federation_repo.get_instance(instance_id)
            if peer is None:
                continue  # mesh-only / unknown: no version to go by
            if (
                int(peer.proto_version or 1)
                < FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE
            ):
                return False
        return True

    async def _picture(self, space_id: str, user_id: str) -> bytes | None:
        got = await self._pictures.get_member_picture(space_id, user_id)
        if got is None:
            got = await self._pictures.get_user_picture(user_id)
        if got is None:
            return None
        return await public_profile_picture(got[0])
