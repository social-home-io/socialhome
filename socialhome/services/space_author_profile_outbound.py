"""Publish our authors' public profiles to a space's GFS followers (v_60).

A household that follows a public / global space through a GFS holds no
roster, so its people saw every post there as "Someone". When one of our
users posts in such a space, this service publishes their profile into it as
the generic ``author_profile`` item (:mod:`socialhome.domain.space_author_profile`)
— signed by our household identity key, encrypted under the space content
key and sent over the existing member-publish path
(:meth:`GfsMemberPublishService.schedule_item`). The GFS sees ciphertext only.

* **When:** once per author per space per content-key epoch, and again when
  the profile changes (``UserProfileUpdated`` / ``SpaceMemberProfileUpdated``)
  — every follower gets each change once. Never inlined in each post, and
  never re-sent just because time passed (v_61): a follower who subscribed
  mid-epoch gets the names from the host's targeted bundle instead.
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
* **A new follower (v_61)** is NOT answered here: the space's host sends
  them one sealed bundle of the cached, author-signed profiles, to that
  follower only (:class:`~socialhome.services.space_subscriber_key_outbound.SpaceSubscriberKeyOutbound`).
  Every item this service sends is therefore also kept in the
  author-profile cache (:meth:`attach_profile_cache`), so a host's bundle
  names its own authors.

In-memory bookkeeping: a restart sends each author's profile once more on
their next post.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..domain.events import (
    SpaceMemberProfileUpdated,
    SpacePostCreated,
    UserProfileUpdated,
)
from ..domain.federation_capabilities import FederationCapability
from ..domain.picture_hash import compute_picture_hash
from ..domain.space import PUBLIC_SPACE_TIERS
from ..domain.space_author_profile import (
    PROFILE_BIO_MAX_LENGTH,
    PROFILE_NAME_MAX_LENGTH,
    SpaceAuthorProfile,
    encode_profile_content,
)
from ..domain.space_item import ITEM_TYPE_AUTHOR_PROFILE
from ..infrastructure.event_bus import EventBus
from ..media.profile_picture import public_profile_picture
from .protection_gate import ProtectionGateMixin
from .space_item_author import build_signed_item_inner
from .space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..repositories.federation_repo import AbstractFederationRepo
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
        "_pictures",
        "_profile_cache",
        "_sent",
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
        self._profile_cache: AbstractSpaceAuthorProfileRepo | None = None
        self._writer_certs: SpaceWriterCertService | None = None

    def attach_profile_cache(
        self,
        repo: AbstractSpaceAuthorProfileRepo,
        *,
        writer_certs: SpaceWriterCertService,
    ) -> None:
        """Keep every profile item we send in the author-profile cache too,
        signed item and epoch included (0114): the host's new-follower
        bundle is built from it, and a household never receives its own
        items back. The item is kept as a follower receives it — with our
        writer cert for its epoch (*writer_certs*), which the member-publish
        path adds when it seals."""
        self._profile_cache = repo
        self._writer_certs = writer_certs

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
        if held is not None and held[0] == epoch and held[1] == rev:
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
        if self._profile_cache is not None:
            # As a follower receives it: with our writer cert for the epoch.
            # Without one there is nothing a follower could verify, so only
            # the decoded profile is kept.
            cert = (
                await self._writer_certs.own_cert(space_id, epoch)
                if self._writer_certs is not None and epoch is not None
                else None
            )
            await self._profile_cache.upsert_if_newer(
                SpaceAuthorProfile(
                    space_id=space_id,
                    author_user_id=user_id,
                    author_pk=self._own_instance_pk.hex(),
                    display_name=name,
                    bio=bio,
                    picture_hash=compute_picture_hash(picture) if picture else None,
                    profile_rev=rev,
                    ts=now.isoformat(timespec="microseconds"),
                ),
                picture_webp=picture,
                signed_item=(
                    {**inner, WRITER_CERT_FIELD: cert.to_wire()}
                    if cert is not None
                    else None
                ),
                item_epoch=epoch if cert is not None else None,
            )
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
