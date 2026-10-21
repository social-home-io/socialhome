"""Publish our authors' public profiles to a space's GFS followers (v_60).

A household that follows a public / global space through a GFS holds no
roster, so its people saw every post there as "Someone". When one of our
users posts in such a space, this service publishes their profile into it as
the generic ``author_profile`` item (:mod:`socialhome.domain.space_author_profile`)
— signed by our household identity key, encrypted under the space content
key and sent over the existing member-publish path
(:meth:`GfsMemberPublishService.schedule_item`). The GFS sees ciphertext only.

* **When:** on the author's first post in the space, then only when the
  profile really changes (``UserProfileUpdated`` / ``SpaceMemberProfileUpdated``).
  Every follower gets each change once. A content-key rotation or a restart
  sends nothing: the dedupe is the persisted cache row's ``profile_rev``,
  not anything in memory. A follower who subscribes later gets the names
  from the host's targeted bundle, which carries each item under the epoch
  it was published at.
* **Picture:** the signed content names the picture by its hash. The bytes
  ride beside it as the unsigned sidecar (``picture``) only when the
  picture changed. So a name or bio change is a 1–4 KiB item, and only a
  new picture is a 64 KiB one. The public re-encode is cached per source
  picture (:class:`~socialhome.media.profile_picture.PublicPictureCache`).
* **What:** the per-space name, else the household name; the household bio;
  the per-space picture, else the household one, re-encoded to at most
  256 px and 24 KB (``None`` when it can't fit). A **protected** account
  (§CP.R) sends its name only — no bio, no picture. So does a person who
  turned off "Show my picture and bio to followers of global spaces"
  (``UserPreferences.hide_profile_from_followers``, owner decision M6).
  Flipping that switch (``UserPreferencesChanged``) republishes the
  profile, name only or full, in every space it was published in.
* **Version gate:** an older follower drops an unknown ``space_item`` type
  with a WARNING, and followers are anonymous, so their versions are
  unknown. The profile is published only while every member household
  whose version we know is at least
  :data:`~socialhome.domain.federation_capabilities.FederationCapability.MIN_FOR_SPACE_AUTHOR_PROFILE`.
* **Where:** only a public / global space that admits followers — a
  private space's channel readers are all members and hold the roster.
* **A new follower (v_61)** is NOT answered here: the space's host sends
  them one sealed bundle of the cached, author-signed profiles, to that
  follower only (:class:`~socialhome.services.space_subscriber_key_outbound.SpaceSubscriberKeyOutbound`).
  Every item this service sends is therefore also kept in the
  author-profile cache (:meth:`attach_profile_cache`), so a host's bundle
  names its own authors.

No in-memory bookkeeping: what was sent is the cache row
(:meth:`attach_profile_cache`), so a restart or a rotation re-sends nothing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..domain.events import (
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserPreferencesChanged,
    UserProfileUpdated,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import PUBLIC_SPACE_TIERS
from ..domain.space_author_profile import (
    PROFILE_BIO_MAX_LENGTH,
    PROFILE_NAME_MAX_LENGTH,
    PROFILE_PICTURE_FIELD,
    SpaceAuthorProfile,
    encode_picture_sidecar,
    encode_profile_content,
    profile_picture_digest,
)
from ..domain.space_item import ITEM_TYPE_AUTHOR_PROFILE
from ..infrastructure.event_bus import EventBus
from ..media.profile_picture import PublicPictureCache
from .protection_gate import ProtectionGateMixin
from .space_item_author import build_signed_item_inner
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.preferences_repo import AbstractPreferencesRepo
    from ..repositories.profile_picture_repo import AbstractProfilePictureRepo
    from ..repositories.space_author_profile_repo import (
        AbstractSpaceAuthorProfileRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_member_publish_service import GfsMemberPublishService
    from .space_crypto_service import SpaceContentEncryption
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)


class SpaceAuthorProfileOutbound(ProtectionGateMixin):
    """See the module docstring."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_crypto",
        "_federation_repo",
        "_member_gfs",
        "_own_instance_id",
        "_own_instance_pk",
        "_own_seed",
        "_picture_cache",
        "_pictures",
        "_preferences",
        "_profile_cache",
        "_spaces",
        "_users",
        "_writer_certs",
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
        preferences_repo: AbstractPreferencesRepo,
        picture_cache: PublicPictureCache | None = None,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._users = user_repo
        self._pictures = picture_repo
        self._federation_repo = federation_repo
        self._crypto = space_crypto
        self._member_gfs = member_gfs
        self._preferences = preferences_repo
        self._own_instance_id = own_instance_id
        self._own_instance_pk = own_instance_pk
        self._own_seed = own_identity_seed
        self._picture_cache = (
            picture_cache if picture_cache is not None else PublicPictureCache()
        )
        self._child_protection = None
        self._profile_cache: AbstractSpaceAuthorProfileRepo | None = None
        self._writer_certs: SpaceWriterCertService | None = None

    def attach_profile_cache(
        self,
        repo: AbstractSpaceAuthorProfileRepo,
        *,
        writer_certs: SpaceWriterCertService,
    ) -> None:
        """Wire the author-profile cache: the record of what we published
        (the dedupe, persisted) and, on a seed holder, the signed items the
        host's new-follower bundle forwards. The bundle sends each item as
        a follower receives it: with our writer cert for its epoch
        (*writer_certs*) and the original picture. Until this is wired,
        nothing is published."""
        self._profile_cache = repo
        self._writer_certs = writer_certs

    def wire(self) -> None:
        self._bus.subscribe(SpacePostCreated, self._on_post)
        self._bus.subscribe(UserProfileUpdated, self._on_profile)
        self._bus.subscribe(SpaceMemberProfileUpdated, self._on_space_profile)
        self._bus.subscribe(SpaceMemberLeft, self._on_left)
        self._bus.subscribe(UserPreferencesChanged, self._on_preferences)

    # ── Triggers ──────────────────────────────────────────────────────

    async def _on_post(self, event: SpacePostCreated) -> None:
        # A local post only (an inbound copy names its origin household).
        if not event.space_id or event.origin_instance_id is not None:
            return
        await self.publish(event.space_id, event.post.author)

    async def _on_profile(self, event: UserProfileUpdated) -> None:
        await self._republish(event.user_id)

    async def _on_preferences(self, event: UserPreferencesChanged) -> None:
        # The "show my picture and bio" switch: name only, or full again,
        # wherever the profile went. The rev dedupe skips a no-op.
        if "hide_profile_from_followers" in event.changed:
            await self._republish(event.user_id)

    async def _republish(self, user_id: str) -> None:
        """Re-check ``user_id``'s profile in every space it was published in."""
        if self._profile_cache is None:
            return
        for space_id in await self._profile_cache.list_spaces_for_author(user_id):
            await self.publish(space_id, user_id)

    async def _on_space_profile(self, event: SpaceMemberProfileUpdated) -> None:
        if event.origin_instance_id is not None or self._profile_cache is None:
            return
        if await self._profile_cache.get(event.space_id, event.user_id) is not None:
            await self.publish(event.space_id, event.user_id)

    async def _on_left(self, event: SpaceMemberLeft) -> None:
        """A member left or was removed: forget their cached profile, so a
        host never forwards it to a new follower again."""
        if self._profile_cache is None:
            return
        try:
            await self._profile_cache.delete(event.space_id, event.user_id)
        except Exception:
            log.exception("space_author_profile: forgetting a departed member failed")

    # ── Publishing ────────────────────────────────────────────────────

    async def publish(self, space_id: str, user_id: str) -> bool:
        """Publish ``user_id``'s profile into ``space_id`` when it differs
        from what we last published there (see the module docstring).
        ``True`` when one was scheduled; it is recorded as published once a
        server took it. Never raises."""
        try:
            return await self._publish(space_id, user_id)
        except Exception:
            log.exception("space_author_profile: publish failed for space=%s", space_id)
            return False

    async def _publish(self, space_id: str, user_id: str) -> bool:
        if self._profile_cache is None:
            return False
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
        if member is None:
            # Not (or no longer) a member here: nothing to name.
            return False
        name = (
            (member.space_display_name if member else None)
            or user.display_name
            or user.username
        )[:PROFILE_NAME_MAX_LENGTH]
        # Name only for a protected account (§CP.R) or a person who hides
        # their picture and bio from followers (M6).
        protected = (
            await self._is_protected(user_id)
            or (await self._preferences.get_user(user_id)).hide_profile_from_followers
        )
        bio = None if protected else (user.bio or None)
        if bio is not None:
            bio = bio[:PROFILE_BIO_MAX_LENGTH]
        picture = None if protected else await self._picture(space_id, user_id)
        picture_hash = profile_picture_digest(picture) if picture else None
        content, rev = encode_profile_content(
            display_name=name, bio=bio, picture_hash=picture_hash
        )
        # The dedupe is what we last published here, persisted: a restart
        # or a content-key rotation sends nothing.
        held = await self._profile_cache.get(space_id, user_id)
        if held is not None and held.profile.profile_rev == rev:
            return False
        epoch = await self._crypto.get_current_epoch(space_id)
        now = datetime.now(UTC)
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
        sidecar = encode_picture_sidecar(picture) if picture else None
        # The bytes go out only with a new picture: a name or bio change
        # keeps the follower's held picture of the same signed hash.
        new_picture = sidecar is not None and (
            held is None or held.profile.picture_hash != picture_hash
        )
        outbound = {**inner, PROFILE_PICTURE_FIELD: sidecar} if new_picture else inner
        cache = self._profile_cache
        row = SpaceAuthorProfile(
            space_id=space_id,
            author_user_id=user_id,
            author_pk=self._own_instance_pk.hex(),
            display_name=name,
            bio=bio,
            picture_hash=picture_hash,
            profile_rev=rev,
            ts=now.isoformat(timespec="microseconds"),
        )
        bundle_copy = await self._bundle_copy(space_id, epoch, inner, sidecar)

        async def _published() -> None:
            # Recorded only once a server took it: the record is the dedupe,
            # so a publish no server took (no cert yet after a rotation, a
            # server down) is sent again on the next trigger.
            await cache.upsert_if_newer(row, picture_webp=picture, **bundle_copy)

        return self._member_gfs.schedule_item(
            space_id,
            user_id,
            ITEM_TYPE_AUTHOR_PROFILE,
            outbound,
            on_published=_published,
        )

    async def _bundle_copy(
        self, space_id: str, epoch: int | None, inner: dict, sidecar: str | None
    ) -> dict:
        """``signed_item`` / ``item_epoch`` for the cache. On a seed holder
        (only a host builds bundles) that is the item as a follower receives
        it: our writer cert for its epoch and the picture sidecar. Elsewhere,
        or with no cert to verify by, it is nothing."""
        if (
            self._writer_certs is None
            or epoch is None
            or await self._spaces.get_space_seed(space_id) is None
        ):
            return {"signed_item": None, "item_epoch": None}
        cert = await self._writer_certs.own_cert(space_id, epoch)
        if cert is None:
            return {"signed_item": None, "item_epoch": None}
        item = {**inner, WRITER_CERT_FIELD: cert.to_wire()}
        if sidecar is not None:
            item[PROFILE_PICTURE_FIELD] = sidecar
        return {"signed_item": item, "item_epoch": epoch}

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
        raw, source_hash = got
        return await self._picture_cache.get(source_hash, raw)
