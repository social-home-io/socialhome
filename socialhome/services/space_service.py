"""Space service — spaces, membership, invites, join requests, space posts.

Covers the core space lifecycle a v1 household needs:

* Create and dissolve a space (owner only).
* Update space name / features / join-mode / retention (owner or admin)
  with an atomic ``config_sequence`` bump for federation ordering.
* Member management — add / remove / set-role / list, plus bans.
* Invites (create token, accept), join requests (open→approve/deny).
* Space posts — create, edit, delete, reactions, comments. Access-level
  routing (open / moderated / admin_only) runs through the moderation
  queue for non-admin members.

Permissions enforced here (route layer never duplicates them):

* ``_require_member(space_id, user_id)`` for any read or member-level
  mutation.
* ``_require_admin_or_owner`` for config updates, bans, invites.
* ``_require_owner`` for dissolve + ownership transfer.

Polls, tasks, pages and calendar events on a space are delegated to their
own sibling services. The space-posts code here deliberately stops short
of them.
"""

from __future__ import annotations

import base64
import logging
import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..crypto import generate_identity_keypair

if TYPE_CHECKING:
    import pathlib

    from ..federation.route_discovery import RouteDiscoveryService
    from ..federation.routed_envelope import SpaceRoutedHandler
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    LocalSpaceInviteCreated,
    PostDeleted,
    PostEdited,
    RemoteJoinRequestApproved,
    SpaceConfigChanged,
    SpaceLocationFeatureEnabled,
    SpaceLocationModeChanged,
    SpaceJoinApproved,
    SpaceJoinDenied,
    SpaceJoinRequested,
    SpaceMemberJoined,
    SpaceMemberLeft,
    SpaceMemberProfileUpdated,
    SpaceModerationApproved,
    SpaceModerationQueued,
    SpaceModerationRejected,
    SpacePostCreated,
    SpacePostModerated,
)
from ..domain.federation import (
    BehindMember,
    FederationEventType,
    PairingStatus,
    SpaceVersionCompat,
)
from ..domain.federation_capabilities import (
    OURS,
    FederationCapability,
    space_features_missing_below,
)
from ..media.cleanup import unlink_media
from .space_purge import purge_space_and_media
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..domain.post import (
    FEED_POST_MAX_IMAGES,
    Comment,
    CommentType,
    FileMeta,
    LocationData,
    Post,
    PostType,
)
from ..domain.presence import truncate_coord
from ..domain.space import (
    PUBLIC_SPACE_TIERS,
    SPACE_CATEGORIES,
    JoinMode,
    ModerationAlreadyDecidedError,
    ModerationStatus,
    PublicSpaceLimitError,
    RemoteAdminOutcome,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceRole,
    SpaceType,
    normalize_category,
    normalize_min_age,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.base import row_to_dict
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from ..domain.media_constraints import (
    SPACE_COVER_MAX_DIMENSION,
    SPACE_ICON_MAX_DIMENSION,
)
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION
from .space_crypto_service import (
    KEY_SUITE_AESGCM_256,
    SUPPORTED_KEY_SUITES,
    UnsupportedKeySuite,
    sign_authority_event,
    strip_authority_sig_fields,
)
from .space_member_guard import SpaceMemberGuardMixin


log = logging.getLogger(__name__)


#: Sentinel for ``update_member_profile`` partial-patch kwargs.
_UNSET_MEMBER_PROFILE = object()


#: Wire-suite tag for the delegated-admin signing-seed share (v_22).
#: The ``space_seed`` field of a ``SPACE_ADMIN_KEY_SHARE`` payload carries
#: the raw 32-byte Ed25519 seed, b64url-encoded. The suite tag travels
#: alongside it so a future seed format (e.g. a hybrid PQ signing key) is a
#: suite-bump rather than a breaking change; receivers reject unknown
#: suites (crypto-suite rule, CLAUDE.md) — there is no default fallback.
SEED_SUITE_ED25519 = "ed25519-seed"

#: Supported ``seed_suite`` values a receiver accepts. Unknown → drop.
SUPPORTED_SEED_SUITES: frozenset[str] = frozenset({SEED_SUITE_ED25519})


class UnsupportedSeedSuite(ValueError):
    """Raised when a ``SPACE_ADMIN_KEY_SHARE`` carries an unknown
    ``seed_suite``. The receiver drops the event rather than guessing the
    seed format — distributing/accepting a signing key under an
    unrecognised scheme would be a fail-open hazard."""


#: Upper bound on simultaneously-advertised public spaces per instance
#: (spec §13). Enforced at ``create_space`` time for PUBLIC spaces.
MAX_PUBLIC_SPACES = 5

#: ``SPACE_CATEGORIES`` + ``normalize_category`` (§23.50) live in the pure
#: ``domain.space`` module so the GFS server can import them without pulling in
#: this service; they are imported above and re-exported for the existing
#: ``services.space_service`` call sites (e.g. ``routes/spaces.py``).

#: Post content caps — matches FeedService values.
MAX_POST_LENGTH = 10_000
MAX_COMMENT_LENGTH = 2_000


class SpaceService(SpaceMemberGuardMixin):
    """Orchestrates space lifecycle + member + post flows."""

    __slots__ = (
        "_spaces",
        "_posts",
        "_users",
        "_bus",
        "_own_instance_id",
        "_child_protection",
        "_pictures",
        "_covers",
        "_icons",
        "_gfs",
        "_federation_repo",
        "_federation",
        "_remote_members",
        "_redeem_coordinator",
        "_space_crypto",
        "_gfs_mirror",
        "_subscriber_keys",
        "_media_dir",
        "_gallery",
        "_bazaar",
    )

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        space_post_repo: AbstractSpacePostRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_id: str,
        media_dir: "pathlib.Path | None" = None,
    ) -> None:
        self._spaces = space_repo
        self._posts = space_post_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        # When set, a deleted space post's media file(s) are removed once
        # the post row + its gallery system-album mirror are gone.
        self._media_dir = media_dir
        self._child_protection = None
        self._pictures = None
        self._covers = None
        self._icons = None
        self._gfs = None
        self._gfs_mirror = None
        self._subscriber_keys = None
        self._federation_repo = None
        self._federation = None
        self._remote_members = None
        self._redeem_coordinator = None
        self._space_crypto = None
        # Optional: lets ``dissolve_space`` unlink native gallery uploads
        # for the space. When unset, gallery media files are skipped
        # (post media still cleaned via ``space_post_repo``).
        self._gallery = None
        self._bazaar = None

    def attach_gallery_repo(self, gallery_repo) -> None:
        """Wire the gallery repo so a hard-deleted space's gallery media
        files are unlinked alongside its post media."""
        self._gallery = gallery_repo

    def attach_bazaar_repo(self, bazaar_repo) -> None:
        """Wire the bazaar repo so a hard-deleted space's listing photos
        (which live only on the listing, not the wrapper post) are
        unlinked too."""
        self._bazaar = bazaar_repo

    def attach_child_protection(self, child_protection_service) -> None:
        """Wire §CP.F1 enforcement into add_member."""
        self._child_protection = child_protection_service

    def attach_profile_picture_repo(self, repo) -> None:
        """Wire the blob store so per-space picture uploads can land."""
        self._pictures = repo

    def attach_cover_repo(self, repo) -> None:
        """Wire the space-cover blob store (§23 customization)."""
        self._covers = repo

    def attach_icon_repo(self, repo) -> None:
        """Wire the space-icon (avatar) blob store (§23 customization)."""
        self._icons = repo

    def attach_space_crypto_service(self, space_crypto) -> None:
        """Wire SpaceContentEncryption so §D1b invite/redeem envelopes
        can ship the current epoch's space content key — the symmetric
        AES-256 secret that decrypts every event in the space. Required
        for cross-household members to read content; without it, the
        receiver's local ``space_keys`` row stays empty and every
        ``decrypt`` call against an inbound event raises."""
        self._space_crypto = space_crypto

    def attach_gfs_connection_service(self, gfs_service) -> None:
        """Wire outbound GFS publish so ``space_type=global`` spaces
        auto-advertise without a separate admin action. Optional: when
        no GFS is paired, GfsConnectionService may be absent entirely.
        """
        self._gfs = gfs_service

    def attach_gfs_space_mirror(self, mirror) -> None:
        """Wire the GFS-discovered-space on-ramp so ``subscribe_to_space``
        can mirror a remote listing onto a local stub row (and register this
        household on the GFS relay) before seating the subscriber. Optional:
        absent when no GFS is paired — subscribe then behaves exactly as it
        did before, 404ing on an unknown space id.
        """
        self._gfs_mirror = mirror

    def attach_subscriber_key_outbound(self, subscriber_key_outbound) -> None:
        """Wire the Phase-5b subscriber content-key producer so a
        forward-secrecy rekey also reaches GFS *subscribers*. They hold a
        read-only subscription and are never in ``space_instances``, so the
        member fan-out misses them entirely. Optional: absent when no GFS is
        paired — rotation then behaves exactly as before.
        """
        self._subscriber_keys = subscriber_key_outbound

    def attach_federation(
        self,
        federation_service,
        federation_repo,
        remote_member_repo,
    ) -> None:
        """Wire §D1b cross-household-invite outbound. Optional: when
        federation isn't initialised yet (early boot) or tests don't
        need it, remains None and :meth:`invite_remote_user` raises.

        Also subscribes to :class:`RemoteJoinRequestApproved` so §D2
        federated join-request approvals auto-consume the invite
        token on the applicant's side.
        """
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._remote_members = remote_member_repo
        self._bus.subscribe(
            RemoteJoinRequestApproved,
            self._on_remote_join_request_approved_bus,
        )

    def attach_redeem_coordinator(self, coordinator) -> None:
        """Wire the §D2 cross-instance invite-token redeem driver.

        Optional: when not attached (early boot / unit tests),
        :meth:`redeem_invite_token` falls back to the local-only
        :meth:`accept_invite_token` path and refuses non-local
        ``issuer_instance_id`` requests.
        """
        self._redeem_coordinator = coordinator

    def attach_mesh(
        self,
        *,
        route_service: RouteDiscoveryService,
        routed_handler: SpaceRoutedHandler,
    ) -> None:
        """Wire the §D2-PR2 federation-mesh routing pair.

        Thin delegation to :meth:`FederationService.attach_mesh` —
        mesh state lives on the federation service so every space-
        content fanout (not just the private-invite family) benefits
        from per-peer mesh fallback. Kept here so existing wiring
        sites don't have to change their call site, but the source
        of truth is the federation service itself.

        Raises ``RuntimeError`` if :meth:`attach_federation` hasn't
        run yet — there's nowhere to hang the mesh refs without a
        live :class:`FederationService`.
        """
        if self._federation is None:
            raise RuntimeError(
                "space_service.attach_mesh: federation not attached; "
                "call attach_federation first",
            )
        self._federation.attach_mesh(
            route_service=route_service,
            routed_handler=routed_handler,
        )

    async def _on_remote_join_request_approved_bus(
        self,
        event: RemoteJoinRequestApproved,
    ) -> None:
        await self.on_remote_join_request_approved(
            event.request_id,
            invite_token=event.invite_token,
        )

    async def _auto_publish_on_type(
        self,
        space_id: str,
        *,
        was_global: bool,
        is_global: bool,
    ) -> None:
        """Fan publish/unpublish calls out to every active GFS when a
        space crosses the global boundary. Failures are logged inside
        :class:`GfsConnectionService`; never raised.
        """
        if self._gfs is None or was_global == is_global:
            return
        if is_global:
            await self._gfs.publish_space_to_all(space_id)
        else:
            await self._gfs.unpublish_space_from_all(space_id)

    async def _ensure_content_key(self, space_id: str) -> None:
        """Mint epoch 0 for a space that sits in a public tier.

        A PUBLIC / GLOBAL space's audience may be GFS *subscribers* only —
        nobody is ever invited cross-household, so the §D1b invite-metadata
        builder (the only other caller of ``initialise_for_space``) never
        runs. Without a key the public-relay producers log "no content key …
        cannot relay" forever, and a new subscriber never receives one
        either. Both of those can happen before the first post, so the key is
        established the moment the space enters a public tier.

        Idempotent (``initialise_for_space`` is a no-op when a key exists, so
        an existing epoch is never rotated away) and fail-soft: the crypto
        service is optional in several stacks, and a minting failure must not
        abort space creation or a config change.
        """
        if self._space_crypto is None:
            return
        try:
            await self._space_crypto.initialise_for_space(space_id)
        except Exception:
            log.exception("failed to mint content key for space=%s", space_id)

    async def set_cover(
        self,
        space_id: str,
        *,
        actor_username: str,
        raw_bytes: bytes,
    ) -> Space:
        """Transcode the upload to WebP, persist, bump cover_hash, and
        publish :class:`SpaceConfigChanged` so federation + WS fan out.
        """
        if self._covers is None:
            raise RuntimeError("cover repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        webp = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=SPACE_COVER_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp)
        await self._covers.set(
            space_id,
            bytes_webp=webp,
            hash=hash_,
            width=SPACE_COVER_MAX_DIMENSION,
            height=SPACE_COVER_MAX_DIMENSION,
        )
        await self._spaces.set_cover_hash(space_id, hash_)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, cover_hash=hash_)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.COVER_UPDATED.value,
                payload={"cover_hash": hash_},
                sequence=sequence,
            )
        )
        return updated

    async def clear_cover(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> Space:
        if self._covers is None:
            raise RuntimeError("cover repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._covers.clear(space_id)
        await self._spaces.set_cover_hash(space_id, None)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, cover_hash=None)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.COVER_UPDATED.value,
                payload={"cover_hash": None},
                sequence=sequence,
            )
        )
        return updated

    async def set_icon(
        self,
        space_id: str,
        *,
        actor_username: str,
        raw_bytes: bytes,
    ) -> Space:
        """Transcode the upload to a small square WebP, persist as the
        space icon (avatar), bump icon_hash, and publish
        :class:`SpaceConfigChanged`. Mirrors :meth:`set_cover`."""
        if self._icons is None:
            raise RuntimeError("icon repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        webp = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=SPACE_ICON_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp)
        await self._icons.set(
            space_id,
            bytes_webp=webp,
            hash=hash_,
            width=SPACE_ICON_MAX_DIMENSION,
            height=SPACE_ICON_MAX_DIMENSION,
        )
        await self._spaces.set_icon_hash(space_id, hash_)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, icon_hash=hash_)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.ICON_UPDATED.value,
                payload={"icon_hash": hash_},
                sequence=sequence,
            )
        )
        return updated

    async def clear_icon(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> Space:
        if self._icons is None:
            raise RuntimeError("icon repo not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._icons.clear(space_id)
        await self._spaces.set_icon_hash(space_id, None)
        sequence = await self._spaces.increment_config_sequence(space_id)
        updated = replace(space, icon_hash=None)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.ICON_UPDATED.value,
                payload={"icon_hash": None},
                sequence=sequence,
            )
        )
        return updated

    # ── Space lifecycle ────────────────────────────────────────────────

    async def create_space(
        self,
        *,
        owner_username: str,
        name: str,
        description: str | None = None,
        emoji: str | None = None,
        space_type: SpaceType | str = SpaceType.PRIVATE,
        join_mode: JoinMode | str = JoinMode.INVITE_ONLY,
        features: SpaceFeatures | None = None,
        retention_days: int | None = None,
        retention_exempt_types: tuple[str, ...] | list[str] | None = None,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float | None = None,
        category: str | None = None,
    ) -> Space:
        """Create a new space and seat the creator as owner."""
        owner = await self._users.get(owner_username)
        if owner is None:
            raise KeyError(f"owner {owner_username!r} not found")
        if not name.strip():
            raise ValueError("space name must not be empty")

        stype = _coerce_space_type(space_type)
        jmode = _coerce_join_mode(join_mode)

        if category is not None and category not in SPACE_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")

        if stype is SpaceType.PUBLIC:
            count = len(await self._spaces.list_by_type(SpaceType.PUBLIC))
            if count >= MAX_PUBLIC_SPACES:
                raise PublicSpaceLimitError(
                    f"instance already advertises {count} public spaces "
                    f"(max {MAX_PUBLIC_SPACES})"
                )
        else:
            # Non-public spaces never carry location metadata.
            lat = lon = radius_km = None

        kp = generate_identity_keypair()
        exempt_types = _normalise_exempt_types(retention_exempt_types)
        space = Space(
            id=uuid.uuid4().hex,
            name=name.strip(),
            owner_instance_id=self._own_instance_id,
            owner_username=owner.username,
            identity_public_key=kp.public_key.hex(),
            config_sequence=0,
            features=features or SpaceFeatures(),
            space_type=stype,
            join_mode=jmode,
            description=description.strip() if description else None,
            emoji=emoji,
            retention_days=retention_days
            if (retention_days is None or retention_days > 0)
            else None,
            retention_exempt_types=exempt_types,
            lat=_round4(lat),
            lon=_round4(lon),
            radius_km=radius_km,
            category=category,
        )
        await self._spaces.save(space)
        # Persist the space's Ed25519 PRIVATE seed (KEK-wrapped at rest) so
        # this household — the space owner — can later sign space-authority
        # events. The matching public half is published as
        # ``identity_public_key``; the seed NEVER federates.
        await self._spaces.set_space_seed(space.id, kp.private_key)
        # Seat the creator as owner.
        await self._spaces.save_member(
            SpaceMember(
                space_id=space.id,
                user_id=owner.user_id,
                role=SpaceRole.OWNER,
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        await self._spaces.add_space_instance(space.id, self._own_instance_id)
        if stype in PUBLIC_SPACE_TIERS:
            await self._ensure_content_key(space.id)
        await self._auto_publish_on_type(
            space.id,
            was_global=False,
            is_global=stype is SpaceType.GLOBAL,
        )
        return space

    async def ensure_space_seed(self, space_id: str) -> bytes | None:
        """Return the space's raw 32-byte Ed25519 seed, minting one if needed.

        * If a seed is already stored, return it unchanged.
        * If the column is NULL **and this household owns the space**, mint a
          fresh keypair, persist the KEK-wrapped seed, replace
          ``identity_public_key`` with the new public half, and return the new
          seed. Pre-upgrade owned spaces had their private key discarded at
          create time, so a *fresh* identity is the only recovery — the old
          public key has no recoverable private half.
        * If the column is NULL and the space is **not** owned by this
          household, return ``None`` — we never held its private key and must
          never mint a new identity for a space hosted elsewhere.

        Not yet called outside tests; this is the accessor a later
        space-authority signing task wires in.
        """
        space = await self._spaces.get(space_id)
        if space is None:
            raise KeyError(f"space {space_id!r} not found")

        existing = await self._spaces.get_space_seed(space_id)
        if existing is not None:
            return existing

        owned = (
            self._own_instance_id is not None
            and space.owner_instance_id == self._own_instance_id
        )
        if not owned:
            return None

        kp = generate_identity_keypair()
        # Replace the published public key via a targeted update (``save``'s
        # upsert no longer touches identity_public_key, so a remote stub
        # re-save can't clobber it), then store the matching wrapped seed.
        await self._spaces.set_space_pubkey(space_id, kp.public_key.hex())
        await self._spaces.set_space_seed(space_id, kp.private_key)
        return kp.private_key

    async def dissolve_space(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> None:
        """Hard-delete a space and all its content (owner only).

        Dissolution is a permanent removal, not a soft archive:

        1. unpublish from any paired GFS (while the row still exists);
        2. broadcast :data:`FederationEventType.SPACE_DISSOLVED` to every
           member household (explicitly — not via the config-change
           subscriber — so propagation can't silently no-op if that
           subscriber isn't wired) so they hard-delete their copy too;
        3. publish :class:`SpaceConfigChanged` (``DISSOLVED``) for the
           local realtime fan-out so connected tabs drop the space;
        4. drop the entire content graph (FK cascade) and unlink every
           on-disk media file the space owned.

        Steps 1–3 run *before* the purge because the broadcast + WS
        fan-out resolve recipients from the membership / instance rows
        the cascade is about to delete.
        """
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        await self._auto_publish_on_type(
            space_id,
            was_global=space.space_type is SpaceType.GLOBAL,
            is_global=False,
        )
        if self._federation is not None:
            try:
                await self._federation.broadcast_to_space_members(
                    space_id,
                    FederationEventType.SPACE_DISSOLVED,
                    {"space_id": space_id},
                )
            except Exception:
                log.exception("SPACE_DISSOLVED broadcast failed for space=%s", space_id)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.DISSOLVED.value,
                payload={},
                sequence=sequence,
            )
        )
        await purge_space_and_media(
            space_repo=self._spaces,
            post_repo=self._posts,
            gallery_repo=self._gallery,
            bazaar_repo=self._bazaar,
            media_dir=self._media_dir,
            space_id=space_id,
        )

    async def _forward_admin_action_if_remote(
        self,
        space: Space,
        actor_username: str,
        action: str,
        params: dict,
    ) -> bool:
        """Forward an admin-level mutation to the host when this space is
        hosted on another household (#114, cross-household admin actions,
        v_15+).

        Generalises the kick path in :meth:`remove_member`: a remote admin
        can't mutate their local stub for config / ban / archive (the
        stub isn't authoritative and the change wouldn't federate), so we
        ship a :data:`FederationEventType.SPACE_REMOTE_ADMIN_ACTION` intent
        envelope to the host. The host re-validates the actor's role and
        runs the real method; the result federates back via the normal
        outbounds.

        Returns ``True`` when the action was forwarded — the caller MUST
        then return without touching the local stub. Returns ``False``
        when this instance owns the space, so the caller proceeds with the
        normal local path.

        Raises :class:`SpacePermissionError` when the host is too old to
        handle the action (sub-``MIN_FOR_REMOTE_ADMIN_ACTION``) rather than
        silently mutating the stub into divergence with the host.
        """
        if (
            self._own_instance_id is None
            or not space.owner_instance_id
            or space.owner_instance_id == self._own_instance_id
        ):
            return False  # we are the host — run locally
        if self._federation is None:
            raise RuntimeError(
                "remote-admin action requires federation to be attached",
            )
        if not await self._federation.peer_supports(
            space.owner_instance_id,
            min_version=FederationCapability.MIN_FOR_REMOTE_ADMIN_ACTION,
        ):
            raise SpacePermissionError(
                "this space's host doesn't support remote admin actions yet "
                "— ask the host's operator to upgrade",
            )
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        await self._federation.send_with_mesh_fallback(
            to_instance_id=space.owner_instance_id,
            event_type=FederationEventType.SPACE_REMOTE_ADMIN_ACTION,
            payload={
                "space_id": space.id,
                "actor_user_id": actor.user_id,
                "actor_instance_id": self._own_instance_id,
                "action": action,
                "params": params,
            },
            space_id=space.id,
        )
        return True

    async def _executes_locally_as_delegated_admin(self, space: Space) -> bool:
        """Whether this household may execute an admin action LOCALLY +
        authoritatively instead of forwarding to the (offline) owner host.

        True iff ALL hold (owner-offline-spaces epic, Phase 4a/4b):

        * the space is hosted on ANOTHER household (we're not the owner host);
        * the owner opted in via ``features.delegated_admin_authority``; and
        * THIS household holds the space's Ed25519 signing seed (delivered via
          SPACE_ADMIN_KEY_SHARE, v_22) — so any roster/config event we emit is
          space-authority-signed and every member (incl. the offline owner on
          reconnect) accepts it by verifying the signature, not ``from_instance``.

        When False, the caller falls through to the v_15
        ``_forward_admin_action_if_remote`` path (Phase 6 gates that behind
        owner approval). Fail-closed: any error resolving the seed → False, so
        we forward rather than mutate a non-authoritative local stub. Used by
        both :meth:`update_config` and the removal-class actions (ban / remove)
        so the local-vs-forward decision stays uniform.
        """
        is_remote_host = bool(space.owner_instance_id) and (
            space.owner_instance_id != self._own_instance_id
        )
        if not is_remote_host or not space.features.delegated_admin_authority:
            return False
        try:
            return await self._spaces.get_space_seed(space.id) is not None
        except Exception:
            log.exception(
                "_executes_locally_as_delegated_admin: get_space_seed failed for %s",
                space.id,
            )
            return False

    async def _share_admin_signing_seed(
        self,
        space: Space,
        *,
        instance_id: str,
    ) -> None:
        """Ship the space's Ed25519 signing seed to a REMOTE admin household
        (delegated-admin authority, v_22).

        SECURITY: this distributes the space's PRIVATE signing key, so it is
        narrowly gated and fail-closed:

        * Only when this household OWNS the space (the owner is the sole
          authoritative holder of the seed).
        * Only when ``features.delegated_admin_authority`` is enabled — the
          owner's explicit opt-in.
        * Only to the admin's ``instance_id``, via the encrypted peer-pair
          path (:meth:`FederationService.send_with_mesh_fallback` → the
          directional session key). The seed is NEVER broadcast.
        * Only when the admin household advertises
          :data:`FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE`; a
          sub-v_22 peer has no handler, so we SKIP the send and warn rather
          than blast a private key at a peer that would drop it.

        Callers MUST pre-check ownership + the flag (the
        ``set_remote_member_role`` / ``update_config`` call sites do); this
        helper re-asserts ownership defensively and no-ops if it can't mint
        the seed. Failures are logged and swallowed — a missed share never
        breaks the triggering role/config write; the admin simply can't act
        offline until a later re-share succeeds.
        """
        if self._federation is None:
            return
        if (
            self._own_instance_id is None
            or space.owner_instance_id != self._own_instance_id
        ):
            # Not the owner — we don't hold the authoritative seed.
            return
        if not await self._federation.peer_supports(
            instance_id,
            min_version=FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE,
        ):
            log.warning(
                "delegated-admin: skipping signing-seed share to %s for space "
                "%s — peer is below v_%d (can't act on space authority offline "
                "until it upgrades)",
                instance_id,
                space.id,
                FederationCapability.MIN_FOR_SPACE_ADMIN_KEY_SHARE,
            )
            return
        seed = await self.ensure_space_seed(space.id)
        if seed is None:
            log.warning(
                "delegated-admin: no signing seed available for space %s — "
                "cannot share to %s",
                space.id,
                instance_id,
            )
            return
        payload = {
            "space_id": space.id,
            "space_seed": base64.urlsafe_b64encode(seed).decode("ascii"),
            "seed_suite": SEED_SUITE_ED25519,
        }
        try:
            # Audit: log the key-blast radius at INFO (recipient + space).
            log.info(
                "delegated-admin: sharing space signing seed for %s to admin "
                "household %s",
                space.id,
                instance_id,
            )
            await self._federation.send_with_mesh_fallback(
                to_instance_id=instance_id,
                event_type=FederationEventType.SPACE_ADMIN_KEY_SHARE,
                payload=payload,
                space_id=space.id,
            )
        except Exception:
            log.exception(
                "delegated-admin: signing-seed share to %s for space %s failed",
                instance_id,
                space.id,
            )

    async def broadcast_remote_member_joined(
        self,
        space_id: str,
        *,
        instance_id: str,
        user_id: str,
        user_pk: str | None,
        display_name: str | None,
        role: str = SpaceRole.MEMBER,
    ) -> None:
        """Host-side roster JOINED gossip for a newly-accepted remote member
        (v_23). Called by :meth:`PrivateSpaceInviteHandler._on_accept` after it
        seats the accepting peer, so every member household converges its
        roster. No-ops gracefully if the space is gone or we hold no seed."""
        space = await self._spaces.get(space_id)
        if space is None:
            return
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=instance_id,
            display_name=display_name,
            user_pk=user_pk,
            role=role,
            tombstoned=False,
        )

    async def _emit_member_roster_gossip(
        self,
        space: Space,
        *,
        user_id: str,
        instance_id: str,
        display_name: str | None,
        user_pk: str | None,
        role: str,
        tombstoned: bool,
    ) -> None:
        """Broadcast an authority-signed roster event to every member household.

        Peer-replicates one roster mutation (v_23) so EVERY member household
        converges its local view, not just the host. A seat / role-change ships
        :data:`FederationEventType.SPACE_MEMBER_JOINED` (the join doubles as the
        role upsert); a removal / kick / ban ships
        :data:`FederationEventType.SPACE_MEMBER_LEFT`.

        Trust model — **authority-signed, not sender-gated.** The payload is
        signed with the space's Ed25519 seed (:func:`sign_authority_event`);
        any receiver trusts it by verifying against
        ``spaces.identity_public_key`` regardless of which household relayed it.
        Only a seed-holder (this phase: the owner host) can sign — if we don't
        hold the seed (non-owned / pre-upgrade non-owner space) we SKIP signing
        + gossip gracefully and fall back to today's host-only behaviour.

        Versioning — ``member_version`` and ``roster_version`` are both sourced
        from the space's dedicated atomic ``roster_sequence`` (bumped once
        here), DECOUPLED from ``config_sequence``. A roster mutation advances
        only the roster counter; a real config edit advances only the config
        counter. This stops a ``set_role`` / ``ban`` from bumping the
        config-LWW version, which used to leave a member household's stub
        ``config_sequence`` lagging the host's so a delegated admin's offline
        config edit collided with the owner at the SAME sequence. The
        receiver's version-guarded CRDT merge (``apply_member_event``) uses
        ``member_version`` to converge regardless of delivery order; a
        replayed/stale event is dropped. ``roster_sequence`` is backfilled from
        ``config_sequence`` once on migration 0036 so it stays strictly above
        every prior ``member_version`` (which was sourced from the old shared
        counter).

        Delivery — fan-out via :meth:`FederationService.broadcast_to_space_members`
        which targets ``space_instances`` (member households only — the
        non-member-relay rule holds; NEVER ``broadcast_to_all``), gated per
        recipient on :data:`FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP`
        so a sub-v_23 household is skipped silently (it keeps learning the
        roster via the snapshot / §25.6 sync path). Failures are logged and
        swallowed — a missed gossip never breaks the triggering write; the next
        sync reconciles.
        """
        if self._federation is None:
            return
        # Only a seed-holder can sign. ensure_space_seed returns None for a
        # space this household doesn't own (and can't recover a seed for) —
        # skip gossip rather than crash.
        try:
            seed = await self.ensure_space_seed(space.id)
        except Exception:
            log.exception("roster-gossip: ensure_space_seed failed for %s", space.id)
            return
        if seed is None:
            # A delegation-ON space whose host ISN'T us but for which we hold
            # no seed is an anomaly — Phase-1 SPACE_ADMIN_KEY_SHARE should have
            # delivered it, so a delegated admin can't sign roster gossip and
            # offline-of-owner convergence silently regresses. Surface it at
            # WARNING so it's diagnosable; still skip gracefully (don't crash).
            is_owner_host = (
                self._own_instance_id is not None
                and space.owner_instance_id == self._own_instance_id
            )
            if space.features.delegated_admin_authority and not is_owner_host:
                log.warning(
                    "roster-gossip: no signing seed for delegated-admin space "
                    "%s — Phase-1 key share missing; skipping authority gossip "
                    "(roster won't converge offline-of-owner)",
                    space.id,
                )
            return
        try:
            version = await self._spaces.increment_roster_sequence(space.id)
        except Exception:
            log.exception("roster-gossip: roster_sequence bump failed for %s", space.id)
            return
        event_type = (
            FederationEventType.SPACE_MEMBER_LEFT
            if tombstoned
            else FederationEventType.SPACE_MEMBER_JOINED
        )
        payload: dict = {
            "space_id": space.id,
            "user_id": user_id,
            "instance_id": instance_id,
            "display_name": display_name,
            "user_pk": user_pk,
            "role": role,
            "member_version": version,
            "roster_version": version,
        }
        # Sign over the bare payload (no sig fields), then merge the two sig
        # fields in. strip_authority_sig_fields is used identically on the
        # verify side so the canonical bytes match.
        signed = sign_authority_event(
            event_type=event_type.value,
            space_id=space.id,
            payload=strip_authority_sig_fields(payload),
            space_seed=seed,
        )
        payload.update(signed)
        try:
            await self._federation.broadcast_to_space_members(
                space.id,
                event_type,
                payload,
                min_proto_version=(FederationCapability.MIN_FOR_SPACE_ROSTER_GOSSIP),
            )
        except Exception:
            log.exception(
                "roster-gossip: %s broadcast failed for %s",
                event_type.value,
                space.id,
            )

    async def archive_space(self, space_id: str, *, actor_username: str) -> None:
        """Archive a space (owner / admin) — a soft, reversible removal.

        The space stays readable but becomes **read-only** and drops out
        of active space lists; nothing is deleted. The read-only state
        federates to member households over the existing
        ``SPACE_CONFIG_CHANGED`` + ``space_meta`` path (``archived`` is a
        space_meta field), so members apply it on their stub via
        :func:`stub_space_from_metadata`. Reverse with
        :meth:`unarchive_space`.
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if await self._forward_admin_action_if_remote(
            space, actor_username, "archive", {}
        ):
            return
        await self._apply_archive(space_id, True, SpaceConfigEventType.ARCHIVED)

    async def unarchive_space(self, space_id: str, *, actor_username: str) -> None:
        """Restore an archived space to read-write + active lists (owner/admin)."""
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if space.archived_reason:
            # A space terminated on its host (``dissolved``/``removed``) is a
            # read-only archive — it can't be revived from a member's side.
            raise SpacePermissionError(
                "This space ended on its host — it can't be unarchived."
            )
        if await self._forward_admin_action_if_remote(
            space, actor_username, "unarchive", {}
        ):
            return
        await self._apply_archive(space_id, False, SpaceConfigEventType.UNARCHIVED)

    async def _apply_archive(
        self,
        space_id: str,
        archived: bool,
        event_type: SpaceConfigEventType,
    ) -> None:
        await self._spaces.set_archived(space_id, archived)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=event_type.value,
                payload={"archived": archived},
                sequence=sequence,
            )
        )

    async def space_version_compat(
        self, space_id: str, *, actor_username: str
    ) -> SpaceVersionCompat:
        """Per-space protocol-version compatibility of member households (#319 ¶5).

        Powers the space-admin banner that warns "these space features won't
        work until member household X upgrades." A household that has never
        advertised capabilities (``capabilities_seen_at is None``) is
        EXCLUDED — it's mid-first-handshake, not genuinely behind, so counting
        its conservative default ``proto_version`` would phantom-nag.
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if self._federation_repo is None:
            return SpaceVersionCompat(
                ours=OURS,
                min_member_proto_version=None,
                lagging_features=(),
                behind_members=(),
            )
        members = await self._federation_repo.list_instances_in_space(space_id)
        min_known: int | None = None
        behind: list[BehindMember] = []
        for m in members:
            if m.capabilities_seen_at is None:
                continue  # mid-handshake — phantom-nag guard.
            if min_known is None or m.proto_version < min_known:
                min_known = m.proto_version
            if m.proto_version < OURS:
                lacking = space_features_missing_below(m.proto_version)
                if lacking:
                    behind.append(
                        BehindMember(
                            instance_id=m.id,
                            display_name=m.effective_display_name,
                            proto_version=m.proto_version,
                            lacking_features=tuple(lacking),
                        )
                    )
        lagging = (
            tuple(space_features_missing_below(min_known))
            if min_known is not None
            else ()
        )
        return SpaceVersionCompat(
            ours=OURS,
            min_member_proto_version=min_known,
            lagging_features=lagging,
            behind_members=tuple(behind),
        )

    async def update_config(
        self,
        space_id: str,
        *,
        actor_username: str,
        name: str | None = None,
        description: str | None = None,
        emoji: str | None = None,
        features: SpaceFeatures | None = None,
        join_mode: JoinMode | str | None = None,
        space_type: SpaceType | str | None = None,
        retention_days: int | None = None,
        retention_exempt_types: tuple[str, ...] | list[str] | None = None,
        about_markdown: str | None | object = _UNSET_MEMBER_PROFILE,
        bot_enabled: bool | None = None,
        category: str | None = None,
    ) -> Space:
        """Owner or admin may update space metadata. Atomically bumps
        ``config_sequence`` and publishes :class:`SpaceConfigChanged`.

        Flipping ``space_type`` to/from ``global`` also triggers
        auto-publish/unpublish against every paired GFS
        (via :meth:`_auto_publish_on_type`).
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)

        if category is not None and category not in SPACE_CATEGORIES:
            raise ValueError(f"unknown category {category!r}")

        # SECURITY: toggling delegated_admin_authority is OWNER-only — it is
        # the owner's policy switch that authorises (and triggers distribution
        # of) the space signing seed to admin households. A host-local admin
        # must not be able to enact it, and on a member household no local user
        # holds OWNER so this also blocks a remote admin from initiating the
        # flip (the gate runs before the cross-household forward below, so it
        # can't be laundered through the host's re-execute-as-owner path).
        if (
            features is not None
            and features.delegated_admin_authority
            != space.features.delegated_admin_authority
        ):
            await self._require_owner(space, actor_username)

        # Delegated-admin authoritative path (v_24): when this space has
        # ``delegated_admin_authority`` ON and THIS household holds the space
        # signing seed (delivered to a remote admin via SPACE_ADMIN_KEY_SHARE,
        # or already held by the owner), a local admin can change config
        # AUTHORITATIVELY even while the owner is offline. We do NOT forward to
        # the host — we execute locally (bump config_sequence, persist) and the
        # SpaceConfigOutbound broadcast carries the space-authority signature so
        # every member household (including the offline owner on reconnect)
        # accepts it by verifying the signature, not by trusting from_instance.
        #
        # ``_require_admin_or_owner`` above already proved the actor is an
        # admin/owner; the delegated_admin_authority flip itself stayed
        # OWNER-only (gate above), so this never lets a non-owner enact the
        # delegation policy. When delegation is OFF or no seed is held, fall
        # through to today's v_15 forward-to-host behaviour.
        is_remote_host = bool(space.owner_instance_id) and (
            space.owner_instance_id != self._own_instance_id
        )
        executes_locally = await self._executes_locally_as_delegated_admin(space)

        # Cross-household admin config edit — forward to the host (v_15+) unless
        # we are an authoritative seed-holding delegated admin (handled locally).
        if is_remote_host and not executes_locally:
            fwd: dict = {}
            if name is not None:
                fwd["name"] = name
            if description is not None:
                fwd["description"] = description
            if emoji is not None:
                fwd["emoji"] = emoji
            if features is not None:
                fwd["features"] = features.to_wire_dict()
            if join_mode is not None:
                fwd["join_mode"] = _coerce_join_mode(join_mode).value
            if space_type is not None:
                fwd["space_type"] = _coerce_space_type(space_type).value
            if retention_days is not None:
                fwd["retention_days"] = retention_days
            if retention_exempt_types is not None:
                fwd["retention_exempt_types"] = list(retention_exempt_types)
            if about_markdown is not _UNSET_MEMBER_PROFILE:
                fwd["about_markdown"] = about_markdown
            if bot_enabled is not None:
                fwd["bot_enabled"] = bool(bot_enabled)
            if category is not None:
                fwd["category"] = category
            if await self._forward_admin_action_if_remote(
                space, actor_username, "update_config", fwd
            ):
                return space

        # SECURITY (v_24): the publication tier (``space_type``) is owner/quorum
        # gated (v_16 ``SpaceApprovalService``) and the v_15 forward path
        # deliberately EXCLUDES it from ``_REMOTE_CONFIG_FIELDS`` so the host
        # drops a remote admin's tier change. The v_24 delegated-admin
        # local-execute path runs the full body below, so without this guard a
        # seed-holding delegated admin on a remote-owned space could flip
        # PRIVATE→PUBLIC/GLOBAL locally with ZERO quorum. Mirror the forward-path
        # exclusion: a non-owner-host caller may NOT enact a tier change — it
        # must go through the owner / multi-admin approval flow. (The owner-host
        # path and the quorum proposal flow are unchanged; this only blocks the
        # local-execute shortcut.)
        if (
            is_remote_host
            and space_type is not None
            and _coerce_space_type(space_type) is not space.space_type
        ):
            raise SpacePermissionError(
                "publication tier change requires owner / multi-admin approval",
            )

        payload: dict = {}
        new_fields: dict = {}
        was_global = space.space_type is SpaceType.GLOBAL
        will_be_global = was_global
        if name is not None:
            new = name.strip()
            if not new:
                raise ValueError("space name must not be empty")
            new_fields["name"] = new
            payload["name"] = new
        if description is not None:
            new_fields["description"] = description.strip() or None
            payload["description"] = new_fields["description"]
        if emoji is not None:
            new_fields["emoji"] = emoji or None
            payload["emoji"] = new_fields["emoji"]
        location_mode_changed = False
        location_feature_just_enabled = False
        delegated_admin_just_enabled = False
        if features is not None:
            location_mode_changed = (
                features.location_mode != space.features.location_mode
            )
            # Track OFF→ON transition so we can nudge members after the write.
            location_feature_just_enabled = (
                not space.features.location and features.location
            )
            # Track delegated-admin OFF→ON so we can distribute the space's
            # signing seed to remote admins after the write (v_22). Only the
            # False→True edge triggers a share — True→False leaves already-
            # shared seeds in place (deeper revocation is a later phase).
            delegated_admin_just_enabled = (
                not space.features.delegated_admin_authority
                and features.delegated_admin_authority
            )
            new_fields["features"] = features
            payload["features"] = features.to_wire_dict()
        if join_mode is not None:
            jmode = _coerce_join_mode(join_mode)
            new_fields["join_mode"] = jmode
            payload["join_mode"] = jmode.value
        if space_type is not None:
            stype = _coerce_space_type(space_type)
            if stype is SpaceType.PUBLIC and space.space_type is not SpaceType.PUBLIC:
                count = len(await self._spaces.list_by_type(SpaceType.PUBLIC))
                if count >= MAX_PUBLIC_SPACES:
                    raise PublicSpaceLimitError(
                        f"instance already advertises {count} public spaces "
                        f"(max {MAX_PUBLIC_SPACES})",
                    )
            new_fields["space_type"] = stype
            payload["space_type"] = stype.value
            will_be_global = stype is SpaceType.GLOBAL
        if retention_days is not None:
            # Zero or negative means "no retention limit" → None
            new_fields["retention_days"] = (
                retention_days if retention_days > 0 else None
            )
            payload["retention_days"] = new_fields["retention_days"]
        if retention_exempt_types is not None:
            exempt = _normalise_exempt_types(retention_exempt_types)
            new_fields["retention_exempt_types"] = exempt
            payload["retention_exempt_types"] = list(exempt)
        if about_markdown is not _UNSET_MEMBER_PROFILE:
            # Narrow the ``str | None | object`` sentinel to a ``str | None``
            # for mypy — once past the sentinel check, only real values remain.
            raw: str | None = about_markdown  # type: ignore[assignment]
            cleaned = (raw or "").strip() or None
            if cleaned and len(cleaned) > 8000:
                raise ValueError("about_markdown must be ≤ 8000 chars")
            new_fields["about_markdown"] = cleaned
            payload["about_markdown"] = cleaned
        if bot_enabled is not None:
            new_fields["bot_enabled"] = bool(bot_enabled)
            payload["bot_enabled"] = bool(bot_enabled)
        if category is not None:
            new_fields["category"] = category
            payload["category"] = category

        if not new_fields:
            return space

        updated = replace(space, **new_fields)
        await self._spaces.save(updated)
        sequence = await self._spaces.increment_config_sequence(space_id)
        # v_24 LWW: record THIS household as the last-applied config author so
        # the ``(config_sequence, author)`` tie-break key matches what every
        # receiver records from the signed broadcast (SpaceConfigOutbound ships
        # ``config_author_instance = own``). Without this, a local authoritative
        # edit leaves the author NULL → fallback ``owner_instance_id``, which
        # mis-orders a concurrent same-sequence peer edit on the editing
        # household while clean members order it correctly → permanent
        # divergence. The owner host records itself here too (it is the author
        # of its own edits), consistent with what its broadcast signs.
        if self._own_instance_id:
            await self._spaces.set_config_author(space_id, self._own_instance_id)
        if "space_type" in new_fields:
            event_type = SpaceConfigEventType.PUBLIC_MODE_CHANGED.value
        elif set(payload.keys()) == {"name"}:
            event_type = SpaceConfigEventType.RENAME.value
        else:
            event_type = SpaceConfigEventType.FEATURE_CHANGED.value
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=event_type,
                payload=payload,
                sequence=sequence,
            )
        )
        if new_fields.get("space_type") in PUBLIC_SPACE_TIERS:
            # PRIVATE/HOUSEHOLD → PUBLIC/GLOBAL: the space becomes relayable,
            # so it needs the content key the relay + subscriber-handoff paths
            # encrypt under. No-op when it already has one.
            await self._ensure_content_key(space_id)
        await self._auto_publish_on_type(
            space_id,
            was_global=was_global,
            is_global=will_be_global,
        )
        if location_mode_changed:
            # §23.8.6: refire latest presence so receivers see the new
            # privacy tier within seconds rather than waiting for the
            # next HA push. SpaceLocationOutbound listens.
            await self._bus.publish(
                SpaceLocationModeChanged(
                    space_id=space_id,
                    new_mode=updated.features.location_mode,
                ),
            )
        if location_feature_just_enabled:
            # Nudge members to opt in. Look up the actor's user_id so the
            # notification handler can exclude them.
            actor = await self._users.get(actor_username)
            actor_user_id = actor.user_id if actor is not None else ""
            await self._bus.publish(
                SpaceLocationFeatureEnabled(
                    space_id=space_id,
                    space_name=updated.name,
                    actor_user_id=actor_user_id,
                )
            )
        # Delegated-admin authority just flipped ON: distribute the space's
        # signing seed to every current REMOTE admin household (v_22). Only
        # the owner holds the authoritative seed, so this is a no-op on a
        # member household editing config remotely (which forwarded the edit
        # to the host above and returned before reaching here).
        if (
            delegated_admin_just_enabled
            and space.owner_instance_id == self._own_instance_id
            and self._federation is not None
            and self._remote_members is not None
        ):
            for admin_instance in await self._remote_members.list_admin_instances(
                space_id
            ):
                await self._share_admin_signing_seed(
                    updated, instance_id=admin_instance
                )
        return updated

    # ── Membership ─────────────────────────────────────────────────────

    async def add_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        role: str = SpaceRole.MEMBER,
    ) -> SpaceMember:
        """Add a member directly. Used for the owner-admin path and for
        accepting an invite on this instance. Regular members join via
        invite / join-request flows below.

        §CP.F1: when a :class:`ChildProtectionService` is attached, this
        path enforces the space's ``min_age`` against the user's
        ``declared_age``.
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        # §CP.F1 — block underage minors when CP is wired in.
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=role,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=role,
            )
        )
        # v_23 — peer-replicate the seat to every member household so their
        # rosters converge (not just the host's). A local user's home instance
        # is ours.
        joined_users = await self._users.list_by_ids({user_id})
        joined_user = joined_users[0] if joined_users else None
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=self._own_instance_id or "",
            display_name=joined_user.display_name if joined_user else None,
            user_pk=joined_user.public_key if joined_user else None,
            role=role,
            tombstoned=False,
        )
        # §CP audit — record joined action for minors. No-op when the
        # user isn't CP-covered or when CP isn't wired.
        if self._child_protection is not None:
            actor = await self._users.get(actor_username)
            actor_uid = actor.user_id if actor is not None else actor_username
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="joined",
                actor_id=actor_uid,
            )
        return member

    async def invite_local_user(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> str:
        """Issue a pending invitation to a same-household user. The
        invitee accepts via :meth:`accept_local_invite` (or declines
        via :meth:`decline_local_invite`) before they're seated.

        Pascal's report: "if I invite local member to space — they
        should receive a join request like all others". Before this
        method existed, the route handler called :meth:`add_member`
        directly and the user appeared in the space without ever
        being asked. Cross-household invites already require accept
        (§D1b); this lifts the local path to the same shape.

        Returns the new invitation id so the route can echo it back.
        Idempotent against an existing pending invite for the same
        ``(space_id, user_id)`` — re-issuing returns the existing id
        rather than creating a duplicate row.
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        # Already a member → 409-shape error so the route can map.
        if await self._spaces.get_member(space_id, user_id) is not None:
            raise SpacePermissionError(
                f"user {user_id!r} is already a member",
            )
        # §CP.F1 — block underage minors here too, so they never see
        # the accept prompt for a space they couldn't actually join.
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        # Idempotent re-issue: look for an existing pending row.
        existing = await self._spaces.list_pending_local_invites_for(user_id)
        for row in existing:
            if row.get("space_id") == space_id:
                return str(row["id"])
        actor = await self._users.get(actor_username)
        invited_by = actor.user_id if actor is not None else actor_username
        invitation_id = await self._spaces.save_invitation(
            space_id=space_id,
            invited_user_id=user_id,
            invited_by=invited_by,
        )
        await self._bus.publish(
            LocalSpaceInviteCreated(
                space_id=space_id,
                invitation_id=invitation_id,
                invited_user_id=user_id,
                invited_by=invited_by,
            )
        )
        return invitation_id

    async def accept_local_invite(
        self,
        invitation_id: str,
        *,
        user_id: str,
    ) -> SpaceMember:
        """Invitee accepts a same-household invite. Seats the user
        via the existing :meth:`add_member` and marks the row
        accepted. Refuses if the row belongs to a different user or
        already resolved (defence — the route only surfaces pending
        invites for the caller, but a stale request shouldn't sneak
        someone in).
        """
        row = await self._spaces.get_invitation(invitation_id)
        if row is None:
            raise KeyError(invitation_id)
        if row.get("invited_user_id") != user_id:
            raise SpacePermissionError(
                "invite belongs to a different user",
            )
        if row.get("remote_instance_id"):
            raise SpacePermissionError(
                "use /api/remote_invites for cross-household invites",
            )
        if row.get("status") != "pending":
            raise SpacePermissionError(
                f"invite already {row.get('status')!r}",
            )
        space = await self._require_space(row["space_id"])
        if await self._spaces.is_banned(space.id, user_id):
            await self._spaces.update_invitation_status(
                invitation_id,
                "declined",
            )
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        # §CP.F1 — invite creation gates the invitee, but protection may be
        # enabled *after* the invite was sent; re-check at acceptance so a
        # newly-protected minor can't accept a stale invite into an
        # age-restricted space.
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space.id, user_id)
        member = SpaceMember(
            space_id=space.id,
            user_id=user_id,
            role=SpaceRole.MEMBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._spaces.update_invitation_status(invitation_id, "accepted")
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space.id,
                user_id=user_id,
                role=SpaceRole.MEMBER,
            )
        )
        return member

    async def decline_local_invite(
        self,
        invitation_id: str,
        *,
        user_id: str,
    ) -> None:
        """Invitee declines a same-household invite. Marks the row
        declined; no membership row is created. Idempotent — a
        second decline (or accept-after-decline) is a no-op."""
        row = await self._spaces.get_invitation(invitation_id)
        if row is None:
            raise KeyError(invitation_id)
        if row.get("invited_user_id") != user_id:
            raise SpacePermissionError(
                "invite belongs to a different user",
            )
        if row.get("remote_instance_id"):
            raise SpacePermissionError(
                "use /api/remote_invites for cross-household invites",
            )
        if row.get("status") == "pending":
            await self._spaces.update_invitation_status(
                invitation_id,
                "declined",
            )

    async def remove_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        """Remove a member. Admin/owner can remove anyone; a member can
        remove themselves.

        Cross-household admin actions (#114 phase 2): when this space
        is hosted on another household (``space.owner_instance_id !=
        self.own_instance_id``) AND the actor is *not* removing
        themselves, route through ``SPACE_REMOTE_ADMIN_KICK`` to the
        host. The host validates the actor's role in
        ``space_remote_members.role`` and dispatches the actual kick.
        Self-leave on a remote space still works locally — the user is
        dropping their stub membership; the host learns via
        ``SPACE_MEMBER_LEFT`` outbound below.
        """
        space = await self._require_space(space_id)
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        is_self = actor.user_id == user_id
        if not is_self:
            await self._require_admin_or_owner(space, actor_username)
        # Cross-household admin kick — forward to the host, UNLESS this household
        # is a seed-holding delegated admin (delegation ON) acting while the
        # owner is offline (Phase 4b): then we tombstone LOCALLY, the
        # SPACE_MEMBER_LEFT gossip below is space-authority-signed, and the rekey
        # runs for forward secrecy — no forward. Delegation OFF / no seed keeps
        # the v_15 forward path (Phase 6 gates that behind owner approval).
        delegated_local = await self._executes_locally_as_delegated_admin(space)
        if (
            not is_self
            and not delegated_local
            and self._own_instance_id is not None
            and space.owner_instance_id
            and space.owner_instance_id != self._own_instance_id
        ):
            if self._federation is None:
                raise RuntimeError(
                    "remote-admin kick requires federation to be attached",
                )
            await self._federation.send_with_mesh_fallback(
                to_instance_id=space.owner_instance_id,
                event_type=FederationEventType.SPACE_REMOTE_ADMIN_KICK,
                payload={
                    "space_id": space_id,
                    "actor_user_id": actor.user_id,
                    "actor_instance_id": self._own_instance_id,
                    "target_user_id": user_id,
                },
                space_id=space_id,
            )
            return
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            return
        if target.role == SpaceRole.OWNER:
            raise SpacePermissionError(
                "owner cannot be removed (transfer ownership first)"
            )
        await self._spaces.delete_member(space_id, user_id)
        await self._bus.publish(
            SpaceMemberLeft(
                space_id=space_id,
                user_id=user_id,
            )
        )
        # v_23 — peer-replicate the removal so every member household
        # tombstones this user in their roster.
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=self._own_instance_id or "",
            display_name=None,
            user_pk=None,
            role=target.role,
            tombstoned=True,
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="removed",
                actor_id=actor.user_id,
            )
        await self._rotate_and_distribute_space_key(space_id)

    async def set_role(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        role: str,
    ) -> None:
        """Only the owner can promote/demote admins. Owner cannot be demoted."""
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        if role == SpaceRole.OWNER:
            raise ValueError("use transfer_ownership to assign owner role")
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            raise KeyError(f"user {user_id!r} is not a member")
        if target.role == SpaceRole.OWNER:
            raise SpacePermissionError("cannot demote the owner")
        await self._spaces.set_role(space_id, user_id, role)
        evt = (
            SpaceConfigEventType.ADMIN_GRANTED
            if role == SpaceRole.ADMIN
            else SpaceConfigEventType.ADMIN_REVOKED
        )
        # A role change is a ROSTER mutation, not a config edit — it must NOT
        # advance config_sequence (that lagged member stubs and collided
        # offline-of-owner config edits). The roster effect federates via
        # _emit_member_roster_gossip below (roster_sequence). The local bus
        # event still fires for realtime/UI, carrying the CURRENT config
        # sequence unchanged.
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=evt.value,
                payload={"user_id": user_id, "role": role},
                sequence=space.config_sequence,
            )
        )
        # v_23 — peer-replicate the role change (a JOINED gossip doubles as the
        # role upsert) so every member household's roster reflects the new role.
        role_users = await self._users.list_by_ids({user_id})
        role_user = role_users[0] if role_users else None
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=self._own_instance_id or "",
            display_name=role_user.display_name if role_user else None,
            user_pk=role_user.public_key if role_user else None,
            role=role,
            tombstoned=False,
        )

    async def apply_remote_admin_kick(
        self,
        space_id: str,
        *,
        actor_instance_id: str,
        actor_user_id: str,
        target_user_id: str,
    ) -> None:
        """Host-side dispatch for ``SPACE_REMOTE_ADMIN_KICK`` (#114 phase 2).

        The §24.11 pipeline already verified the envelope's
        signature; this method validates the actor's *role* in
        ``space_remote_members.role`` and then dispatches to the
        appropriate local kick path. Three branches:

        * Actor's row is ``admin`` and target is a LOCAL user — call
          :meth:`_remove_local_member_unchecked` (we've already
          validated authority).
        * Actor's row is ``admin`` and target is a REMOTE user (on
          some peer household) — call :meth:`remove_remote_member`'s
          internal path which scrubs ``space_remote_members`` +
          broadcasts ``SPACE_REMOTE_MEMBER_REMOVED`` + rotates.
        * Actor not found / not admin — silently drop. The host
          should not leak permission state via differential errors;
          a stale promotion that's since been revoked is the most
          likely cause and just doing nothing is safe.

        Owner can NOT be kicked through this path — same invariant
        as :meth:`remove_member`.
        """
        if self._remote_members is None:
            log.warning(
                "apply_remote_admin_kick: federation not attached; dropping",
            )
            return
        space = await self._spaces.get(space_id)
        if space is None:
            log.debug(
                "apply_remote_admin_kick: unknown space=%s — dropping",
                space_id,
            )
            return
        if (
            self._own_instance_id is None
            or space.owner_instance_id != self._own_instance_id
        ):
            log.debug(
                "apply_remote_admin_kick: space=%s not hosted here — dropping",
                space_id,
            )
            return
        actor = await self._remote_members.get(
            space_id,
            actor_instance_id,
            actor_user_id,
        )
        if actor is None or actor.role != SpaceRole.ADMIN:
            log.info(
                "apply_remote_admin_kick: actor %s@%s not an admin "
                "of space=%s — dropping",
                actor_user_id,
                actor_instance_id,
                space_id,
            )
            return
        # Look up the target — could be a local user or a remote member.
        local_target = await self._spaces.get_member(space_id, target_user_id)
        if local_target is not None:
            if local_target.role == SpaceRole.OWNER:
                log.info(
                    "apply_remote_admin_kick: owner of space=%s cannot "
                    "be kicked through this path — dropping",
                    space_id,
                )
                return
            owner_username = space.owner_username
            await self.remove_member(
                space_id,
                actor_username=owner_username,
                user_id=target_user_id,
            )
            return
        # Remote target — search all remote-member rows for the user_id.
        remote_rows = await self._remote_members.list_for_space(space_id)
        match = next(
            (r for r in remote_rows if r.user_id == target_user_id),
            None,
        )
        if match is None:
            log.info(
                "apply_remote_admin_kick: target %s not in space=%s — dropping",
                target_user_id,
                space_id,
            )
            return
        await self.remove_remote_member(
            space_id,
            actor_username=space.owner_username,
            instance_id=match.instance_id,
            user_id=target_user_id,
        )

    #: Fields a remote admin may set via ``update_config`` over
    #: ``SPACE_REMOTE_ADMIN_ACTION``. Whitelisted so an unexpected wire
    #: key can never become an ``update_config`` keyword (collision with
    #: ``actor_username`` / unknown kwarg → ``TypeError``).
    #:
    #: ``space_type`` is intentionally absent — the publication tier
    #: (public / global) is gated behind multi-admin approval (v_16,
    #: :class:`SpaceApprovalService`), so it is never applied via a direct
    #: config forward. The host drops it here even if an old / forged client
    #: includes it; a remote admin changes it through the ``propose`` verb.
    _REMOTE_CONFIG_FIELDS = frozenset(
        {
            "name",
            "description",
            "emoji",
            "features",
            "join_mode",
            "retention_days",
            "retention_exempt_types",
            "about_markdown",
            "bot_enabled",
            "category",
        }
    )

    #: The admin actions a remote household may forward (the gate / approval
    #: substrate only makes sense for these; anything else is dropped at the
    #: door so no phantom owner-approval is ever enqueued).
    _FORWARDABLE_ADMIN_ACTIONS = frozenset(
        {"update_config", "archive", "unarchive", "ban", "unban", "invite"}
    )

    async def apply_remote_admin_action(
        self,
        space_id: str,
        *,
        actor_instance_id: str,
        actor_user_id: str,
        action: str,
        params: dict | None = None,
    ) -> RemoteAdminOutcome:
        """Host-side gate for ``SPACE_REMOTE_ADMIN_ACTION`` (v_15+).

        Generalises :meth:`apply_remote_admin_kick`: the §24.11 pipeline
        already verified the envelope signature, so this validates the
        actor's *role* in ``space_remote_members.role`` and, if the actor
        is an admin, decides whether to run the action **now** or hold it
        for the owner.

        The decision is the space's ``delegated_admin_authority`` opt-in:

        * **ON** → run the real host-side admin method **as the owner**
          immediately (returns :attr:`RemoteAdminOutcome.EXECUTED`); the
          result federates back to every member through the normal
          outbounds (``SPACE_CONFIG_CHANGED`` etc.).
        * **OFF** (default, least-privilege) → do NOT execute; return
          :attr:`RemoteAdminOutcome.NEEDS_OWNER_APPROVAL` so the caller
          can enqueue an owner-approval (the enqueue is a later task).

        Scope is admin-level mutations only: ``update_config``,
        ``archive`` / ``unarchive``, ``ban`` / ``unban``, ``invite``.
        Owner-only
        actions (dissolve, transfer-ownership, role assignment) are NOT
        forwardable and never reach this dispatcher. Unknown actions,
        unauthenticated actors, and spaces not hosted here are silently
        dropped (:attr:`RemoteAdminOutcome.DROPPED`) — no differential
        errors, same posture as the kick path (the most likely cause is a
        promotion that's since been revoked).
        """
        if self._remote_members is None:
            log.warning(
                "apply_remote_admin_action: federation not attached; dropping",
            )
            return RemoteAdminOutcome.DROPPED
        space = await self._spaces.get(space_id)
        if space is None:
            log.debug(
                "apply_remote_admin_action: unknown space=%s — dropping",
                space_id,
            )
            return RemoteAdminOutcome.DROPPED
        if (
            self._own_instance_id is None
            or space.owner_instance_id != self._own_instance_id
        ):
            log.debug(
                "apply_remote_admin_action: space=%s not hosted here — dropping",
                space_id,
            )
            return RemoteAdminOutcome.DROPPED
        actor = await self._remote_members.get(
            space_id,
            actor_instance_id,
            actor_user_id,
        )
        if actor is None or actor.role != SpaceRole.ADMIN:
            log.info(
                "apply_remote_admin_action: actor %s@%s not an admin "
                "of space=%s — dropping",
                actor_user_id,
                actor_instance_id,
                space_id,
            )
            return RemoteAdminOutcome.DROPPED
        if action not in self._FORWARDABLE_ADMIN_ACTIONS:
            log.info(
                "apply_remote_admin_action: unknown action=%r for space=%s — dropping",
                action,
                space_id,
            )
            return RemoteAdminOutcome.DROPPED
        if not space.features.delegated_admin_authority:
            log.info(
                "apply_remote_admin_action: delegation OFF for space=%s — "
                "holding action=%r for owner approval",
                space_id,
                action,
            )
            return RemoteAdminOutcome.NEEDS_OWNER_APPROVAL
        await self._run_admin_action(space, space.owner_username, action, params or {})
        return RemoteAdminOutcome.EXECUTED

    async def apply_approved_admin_action(
        self,
        space_id: str,
        *,
        action: str,
        params: dict | None = None,
    ) -> None:
        """Execute a previously owner-approved forwarded admin action as the owner.

        The owner-approval gate has already passed; this just re-validates the
        space is still hosted here and runs the action (same path as the
        delegation-ON case).
        """
        space = await self._spaces.get(space_id)
        if space is None:
            return
        if (
            self._own_instance_id is None
            or space.owner_instance_id != self._own_instance_id
        ):
            return
        await self._run_admin_action(space, space.owner_username, action, params or {})

    async def _run_admin_action(
        self,
        space: Space,
        owner_username: str,
        action: str,
        params: dict,
    ) -> None:
        """Run a forwarded admin action as the owner. The actor-role gate and
        the delegation/approval decision have already passed."""
        space_id = space.id
        owner = owner_username
        p = params
        match action:
            case "update_config":
                kwargs = {k: v for k, v in p.items() if k in self._REMOTE_CONFIG_FIELDS}
                feats = kwargs.get("features")
                if feats is not None:
                    new_features = SpaceFeatures.from_wire_dict(feats)
                    # delegated_admin_authority is OWNER-ONLY: a remote-forwarded
                    # config edit must never change it (otherwise an approved /
                    # self-authorized edit could grant or revoke delegation
                    # itself). Pin it to the space's current value regardless of
                    # the wire.
                    kwargs["features"] = replace(
                        new_features,
                        delegated_admin_authority=(
                            space.features.delegated_admin_authority
                        ),
                    )
                await self.update_config(space_id, actor_username=owner, **kwargs)
            case "archive":
                await self.archive_space(space_id, actor_username=owner)
            case "unarchive":
                await self.unarchive_space(space_id, actor_username=owner)
            case "ban":
                target_id = str(p.get("user_id") or "")
                if not target_id:
                    return
                reason = p.get("reason")
                await self.ban(
                    space_id,
                    actor_username=owner,
                    user_id=target_id,
                    reason=reason if isinstance(reason, str) else None,
                )
            case "unban":
                target_id = str(p.get("user_id") or "")
                if not target_id:
                    return
                await self.unban(space_id, actor_username=owner, user_id=target_id)
            case "invite":
                invitee_instance_id = str(p.get("invitee_instance_id") or "")
                invitee_user_id = str(p.get("invitee_user_id") or "")
                if not invitee_instance_id or not invitee_user_id:
                    return
                # Run AS OWNER (is_owner_host True) → invite_remote_user mints
                # directly and never re-enters the OFF forward branch, so no loop.
                await self.invite_remote_user(
                    space.id,
                    actor_username=owner,
                    invitee_instance_id=invitee_instance_id,
                    invitee_user_id=invitee_user_id,
                )
            case _:
                log.info(
                    "apply_remote_admin_action: unknown action=%r for "
                    "space=%s — dropping",
                    action,
                    space_id,
                )

    async def set_remote_member_role(
        self,
        space_id: str,
        *,
        actor_username: str,
        instance_id: str,
        user_id: str,
        role: str,
    ) -> None:
        """Cross-household admin promotion (#114, PR #434).

        Only the owner can promote/demote admins (mirrors
        :meth:`set_role` for local members). Updates the host's
        ``space_remote_members.role`` and broadcasts
        ``SPACE_MEMBER_ROLE_CHANGED`` to every member household so
        each household's local view of the roster stays in sync.

        Owner role is not assignable to a remote member — ownership
        carries local-only privileges (dissolve, ownership transfer)
        that can't sensibly cross households.
        """
        if self._federation is None or self._remote_members is None:
            raise RuntimeError("federation not attached")
        if role not in (SpaceRole.ADMIN, SpaceRole.MEMBER):
            raise ValueError(
                f"remote member role must be 'admin' or 'member', got {role!r}",
            )
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        target = await self._remote_members.get(space_id, instance_id, user_id)
        if target is None:
            raise KeyError(
                f"remote member {user_id!r}@{instance_id!r} not found in {space_id!r}",
            )
        if target.role == role:
            return
        await self._remote_members.set_role(space_id, instance_id, user_id, role)
        evt = (
            SpaceConfigEventType.ADMIN_GRANTED
            if role == SpaceRole.ADMIN
            else SpaceConfigEventType.ADMIN_REVOKED
        )
        # A role change is a ROSTER mutation, not a config edit — it must NOT
        # advance config_sequence. The roster effect federates via
        # _emit_member_roster_gossip below (roster_sequence); the local bus
        # event carries the CURRENT config sequence unchanged.
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=evt.value,
                payload={
                    "user_id": user_id,
                    "instance_id": instance_id,
                    "role": role,
                },
                sequence=space.config_sequence,
            )
        )
        broadcast = await self._federation.broadcast_to_space_members(
            space_id,
            FederationEventType.SPACE_MEMBER_ROLE_CHANGED,
            {
                "space_id": space_id,
                "user_id": user_id,
                "instance_id": instance_id,
                "role": role,
            },
        )
        # DURABILITY GAP: the mesh fan-out has NO outbox (unlike send_event),
        # so a member household we couldn't reach never gets this role change
        # — and the v_23 roster gossip right below does NOT rescue it: it
        # rides the same ``broadcast_to_space_members`` helper and fails for
        # the same peers. The local write genuinely succeeded, so the request
        # stays 200 rather than lying about it; a durable outbox for space
        # gossip is deliberately out of scope here. Until there is one, a
        # WARNING naming what did not propagate is the operator's only signal
        # that a remote member's role is now stale on its own household.
        if broadcast is not None and getattr(broadcast, "failed", 0):
            log.warning(
                "set_remote_member_role: space=%s role=%s for %s@%s did not "
                "reach %d/%d member household(s): %s — those households keep "
                "the old role until the next full sync (no outbox on the "
                "mesh path)",
                space_id,
                role,
                user_id,
                instance_id,
                broadcast.failed,
                broadcast.attempted,
                ", ".join(
                    f"{r.instance_id}={r.error}" for r in broadcast.results if not r.ok
                ),
            )
        # v_23 — peer-replicate the role change as an authority-signed JOINED
        # gossip (doubles as the role upsert) so every member household's
        # roster converges, not just the witnesses of the legacy
        # SPACE_MEMBER_ROLE_CHANGED broadcast above.
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=instance_id,
            display_name=target.display_name,
            user_pk=target.user_pk,
            role=role,
            tombstoned=False,
        )
        # Delegated-admin authority (v_22): when the owner has opted in and
        # this newly-promoted remote member is an ADMIN, ship the space's
        # Ed25519 signing seed to that admin's household so it can sign
        # space-authority events with the owner offline. Owner-only (the
        # local set_role path needs no share — a local admin's household
        # already holds the seed) and gated on the recipient's capability.
        if (
            role == SpaceRole.ADMIN
            and space.features.delegated_admin_authority
            and space.owner_instance_id == self._own_instance_id
        ):
            await self._share_admin_signing_seed(space, instance_id=instance_id)

    # ── Per-space profile (§4.1.6) ─────────────────────────────────────

    async def update_member_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
        space_display_name: str | None | object = _UNSET_MEMBER_PROFILE,
    ) -> SpaceMember:
        """Patch member display-name override. Picture mutations go
        through :meth:`set_member_picture` / :meth:`clear_member_picture`.
        Only the member themselves or a space admin may patch."""
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        if space_display_name is not _UNSET_MEMBER_PROFILE:
            raw: str | None = space_display_name  # type: ignore[assignment]
            next_name = (raw.strip() if raw else None) or None
            await self._spaces.set_member_profile(
                space_id,
                user_id,
                space_display_name=next_name,
                picture_hash=member.picture_hash,
            )
            member = replace(member, space_display_name=next_name)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=member.space_display_name,
                picture_hash=member.picture_hash,
            )
        )
        return member

    async def set_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
        raw_bytes: bytes,
    ) -> SpaceMember:
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        webp = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=PROFILE_PICTURE_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp)
        await self._pictures.set_member_picture(
            space_id,
            user_id,
            bytes_webp=webp,
            hash=hash_,
            width=PROFILE_PICTURE_MAX_DIMENSION,
            height=PROFILE_PICTURE_MAX_DIMENSION,
        )
        await self._spaces.set_member_profile(
            space_id,
            user_id,
            space_display_name=member.space_display_name,
            picture_hash=hash_,
        )
        updated = replace(member, picture_hash=hash_)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=updated.space_display_name,
                picture_hash=hash_,
                picture_webp=webp,
            )
        )
        return updated

    async def clear_member_picture(
        self,
        space_id: str,
        user_id: str,
        *,
        actor_user_id: str,
    ) -> SpaceMember:
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise KeyError(f"user {user_id!r} is not a member")
        await self._require_self_or_space_admin(
            space_id,
            member=member,
            actor_user_id=actor_user_id,
        )
        await self._pictures.clear_member_picture(space_id, user_id)
        await self._spaces.set_member_profile(
            space_id,
            user_id,
            space_display_name=member.space_display_name,
            picture_hash=None,
        )
        updated = replace(member, picture_hash=None)
        await self._bus.publish(
            SpaceMemberProfileUpdated(
                space_id=space_id,
                user_id=user_id,
                space_display_name=updated.space_display_name,
                picture_hash=None,
            )
        )
        return updated

    async def _require_self_or_space_admin(
        self,
        space_id: str,
        *,
        member: SpaceMember,
        actor_user_id: str,
    ) -> None:
        if member.user_id == actor_user_id:
            return
        actor = await self._spaces.get_member(space_id, actor_user_id)
        if actor is None or actor.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
            raise PermissionError(
                "only the member or a space admin may change this profile",
            )

    async def transfer_ownership(
        self,
        space_id: str,
        *,
        actor_username: str,
        to_user_id: str,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        new_owner_member = await self._spaces.get_member(space_id, to_user_id)
        if new_owner_member is None:
            raise KeyError(f"user {to_user_id!r} is not a member")
        # The outgoing owner becomes admin; the new owner becomes owner.
        outgoing = await self._users.get(actor_username)
        assert outgoing is not None
        await self._spaces.set_role(space_id, outgoing.user_id, SpaceRole.ADMIN)
        await self._spaces.set_role(space_id, to_user_id, SpaceRole.OWNER)
        new_owner_user = await self._users.get_by_user_id(to_user_id)
        updated = replace(
            space,
            owner_username=(
                new_owner_user.username
                if new_owner_user is not None
                else space.owner_username
            ),
        )
        await self._spaces.save(updated)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.OWNERSHIP_TRANSFERRED.value,
                payload={"new_owner_user_id": to_user_id},
                sequence=sequence,
            )
        )

    async def ban(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        reason: str | None = None,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        # Owner-offline-spaces epic (Phase 4b): a seed-holding delegated admin
        # on a remote-owned space with delegation ON bans LOCALLY +
        # authoritatively (the SPACE_MEMBER_LEFT gossip below is
        # space-authority-signed + the rekey runs for forward secrecy), instead
        # of forwarding to the offline owner. Delegation OFF / no seed → keep
        # the v_15 forward-to-host path (Phase 6 gates that behind approval).
        if not await self._executes_locally_as_delegated_admin(space):
            if await self._forward_admin_action_if_remote(
                space, actor_username, "ban", {"user_id": user_id, "reason": reason}
            ):
                return
        target = await self._spaces.get_member(space_id, user_id)
        if target is not None and target.role == SpaceRole.OWNER:
            raise SpacePermissionError("cannot ban the owner")
        actor = await self._users.get(actor_username)
        assert actor is not None
        await self._spaces.ban_member(
            space_id,
            user_id,
            banned_by=actor.user_id,
            reason=reason,
        )
        # A ban is a ROSTER mutation, not a config edit — it must NOT advance
        # config_sequence. The removal federates via the LEFT gossip below
        # (roster_sequence); the local bus event carries the CURRENT config
        # sequence unchanged.
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_BANNED.value,
                payload={"user_id": user_id, "reason": reason},
                sequence=space.config_sequence,
            )
        )
        # v_23 — a ban is a removal: peer-replicate a LEFT gossip so every
        # member household tombstones the banned user in their roster.
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=self._own_instance_id or "",
            display_name=None,
            user_pk=None,
            role=target.role if target is not None else SpaceRole.MEMBER,
            tombstoned=True,
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="blocked",
                actor_id=actor.user_id,
            )
        await self._rotate_and_distribute_space_key(space_id)

    async def unban(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        if await self._forward_admin_action_if_remote(
            space, actor_username, "unban", {"user_id": user_id}
        ):
            return
        await self._spaces.unban_member(space_id, user_id)
        # Unban clears a host-local ban flag only (the ban list never lived on
        # member stubs, and unban re-adds no membership). It is a ROSTER /
        # moderation event, not a config edit — it must NOT advance
        # config_sequence, and it isn't federated as config (see
        # SpaceConfigOutbound._ROSTER_EVENT_TYPES). The local bus event still
        # fires for realtime/UI, carrying the CURRENT config sequence.
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_UNBANNED.value,
                payload={"user_id": user_id},
                sequence=space.config_sequence,
            )
        )

    # ── Invites / join requests ────────────────────────────────────────

    async def create_invite_token(
        self,
        space_id: str,
        *,
        actor_username: str,
        uses: int = 1,
        expires_at: str | None = None,
    ) -> str:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        actor = await self._users.get(actor_username)
        assert actor is not None
        return await self._spaces.create_invite_token(
            space_id,
            created_by=actor.user_id,
            uses=max(1, int(uses)),
            expires_at=expires_at,
        )

    async def _send_invite_envelope(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """Ship a private-invite-family envelope to a remote instance.

        Delegates to :meth:`FederationService.send_with_mesh_fallback`
        — direct delivery for CONFIRMED peers, SPACE_ROUTED multi-hop
        for unpaired / unreachable ones, failure otherwise. Raises
        :class:`SpacePermissionError` when neither path is available
        so the route handler returns 4xx instead of 200.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError("federation not attached")
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=to_instance_id,
            event_type=event_type,
            payload=payload,
        )
        if not result.ok:
            raise SpacePermissionError("no path to invitee household")

    async def _rotate_and_distribute_space_key(self, space_id: str) -> None:
        """Forward-secrecy: mint a fresh epoch + ship the new key to
        remaining members (#121).

        Called after every member-removal path — local removal, ban,
        and §D1b cross-household kick. Without rotation, a removed
        member who keeps their old at-rest key bytes would still be
        able to decrypt every future post in the space; that defeats
        the entire reason the host kicked them.

        The fan-out targets ``space_instances`` (via
        ``broadcast_to_space_members``), which the cross-household
        kick path scrubs of the removed peer right before calling
        here — so the new key naturally never lands at the kicked
        household. Local-only kicks broadcast to the remaining mesh
        of peer households; the kicked local user's own household
        re-imports the new key in-place (a no-op for them since
        they're not in ``space_members`` anymore).

        GFS *subscribers* are not member households — they hold a
        read-only subscription and never appear in ``space_instances``
        — so the broadcast above misses them and every relayed frame
        they receive would stop decrypting until their next GFS
        reconnect. For a PUBLIC/GLOBAL space published to a GFS, the
        rotation therefore also re-runs the Phase-5b subscriber
        reconcile, which re-seals the new epoch's key per subscriber
        through the content-blind relay (same verified seal path; the
        tier + seed-holder gates live there).

        Failures are logged and swallowed — a rotation that can't
        federate is still better than no rotation, and the kick
        itself succeeded. A subsequent member action retries
        rotation; in steady state the §25.6 sync handshake will
        catch up any peer that missed the rekey.
        """
        if self._space_crypto is None or self._federation is None:
            return
        try:
            new_epoch = await self._space_crypto.rotate_epoch(space_id)
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: rotate_epoch failed for %s",
                space_id,
            )
            return
        try:
            exported = await self._space_crypto.export_current_key(space_id)
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: export_current_key failed for %s",
                space_id,
            )
            return
        if exported is None:
            return
        epoch, raw_key = exported
        if epoch != new_epoch:
            log.warning(
                "rotate_and_distribute_space_key: epoch drift for %s "
                "(rotated=%d, exported=%d)",
                space_id,
                new_epoch,
                epoch,
            )
        meta = {
            "epoch": epoch,
            "key_suite": KEY_SUITE_AESGCM_256,
            "key_base64": base64.b64encode(raw_key).decode("ascii"),
            # Phase 4b — the minting household, so concurrent rotations to
            # the same epoch converge on the smallest-``rotated_by`` key on
            # every receiver. Additive optional field: an older peer simply
            # ignores it and falls back to last-writer-wins.
            "rotated_by": self._own_instance_id,
        }
        # SECURITY (rekey authority gate): a rekey PINS the epoch onto its key,
        # so the receiver authenticates the rotator before importing. Sign the
        # inner ``space_content_key`` meta with the space's Ed25519 seed — held
        # by the owner AND by delegated admins (Phase-1 SPACE_ADMIN_KEY_SHARE),
        # so both legit rotators can sign. Use the SAME helpers +
        # ``strip_authority_sig_fields`` config uses so the canonical bytes
        # match on both sides. The OWNER host with NO seed (pre-Phase-0 owned
        # space) ships UNSIGNED — the receiver accepts it via the legacy
        # ``from_instance == owner`` back-compat path.
        try:
            seed = await self._spaces.get_space_seed(space_id)
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: get_space_seed failed for %s",
                space_id,
            )
            seed = None
        if seed is not None:
            signed = sign_authority_event(
                event_type="space_key_exchange_rekey",
                space_id=space_id,
                payload=strip_authority_sig_fields(meta),
                space_seed=seed,
            )
            meta.update(signed)
        payload = {"space_id": space_id, "space_content_key": meta}
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
                payload,
            )
        except Exception:
            log.exception(
                "rotate_and_distribute_space_key: rekey broadcast failed for %s",
                space_id,
            )
        # GFS subscribers (not member households — see the docstring). Never
        # raises by contract, but stay defensive: the kick already succeeded.
        if self._subscriber_keys is not None:
            try:
                await self._subscriber_keys.reconcile_space_everywhere(space_id)
            except Exception:
                log.exception(
                    "rotate_and_distribute_space_key: GFS subscriber re-seal "
                    "failed for %s",
                    space_id,
                )

    async def invite_remote_user(
        self,
        space_id: str,
        *,
        actor_username: str,
        invitee_instance_id: str,
        invitee_user_id: str,
    ) -> str:
        """§D1b — invite a user on another household into this space.

        Only valid when the invitee's household is a CONFIRMED peer of
        ours. Sends a zero-leak ``SPACE_PRIVATE_INVITE`` envelope (all
        space metadata rides inside the encrypted payload; see
        :data:`FederationEventType.SPACE_PRIVATE_INVITE`).
        Returns the invite token so callers can echo it in their own
        audit log, or an empty string when the invite was forwarded to the
        host for owner approval (delegation OFF).
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError(
                "space_service: federation not attached; "
                "remote invites require a live FederationService",
            )
        space = await self._require_space(space_id)
        actor_member = await self._require_admin_or_owner(space, actor_username)
        # §D3 (Phase 3) — delegated-admin authority gate. The owner/host of a
        # space can always invite. A NON-owner ADMIN (a delegated admin whose
        # household doesn't host the space) may only mint an authoritative
        # invite when the owner opted in via ``delegated_admin_authority`` —
        # otherwise the invite would seat + key-hand-off a member behind the
        # owner's back. The flag-ON path works today because the delegated
        # admin holds the space signing seed (Phase-1 SPACE_ADMIN_KEY_SHARE),
        # so the JOINED roster gossip it emits is authority-signed.
        is_owner_host = (
            self._own_instance_id is not None
            and space.owner_instance_id == self._own_instance_id
        )
        if (
            not is_owner_host
            and actor_member.role == SpaceRole.ADMIN
            and not space.features.delegated_admin_authority
        ):
            # Delegation OFF: the delegated admin can't mint authoritatively, so
            # forward the invite to the host as an owner-approval request
            # (Phase 6). On the owner's approval the host mints the real
            # SPACE_PRIVATE_INVITE as owner. No local token exists yet — return
            # "" to signal "forwarded". If the host is too old to handle remote
            # admin actions, _forward_admin_action_if_remote raises
            # SpacePermissionError — let it propagate (correct fallback).
            forwarded = await self._forward_admin_action_if_remote(
                space,
                actor_username,
                "invite",
                {
                    "invitee_instance_id": invitee_instance_id,
                    "invitee_user_id": invitee_user_id,
                },
            )
            if forwarded:
                return ""
            # Entered the OFF gate but couldn't forward (degenerate: no host
            # instance id) — never mint locally; fail closed.
            raise SpacePermissionError(
                "delegated admin authority is not enabled for this space — "
                "owner approval required",
            )
        actor = await self._users.get(actor_username)
        assert actor is not None

        # Short-TTL (5 min) single-use token minted by create_invite_token;
        # reusable with the existing POST /api/spaces/join path once the
        # invitee accepts.
        expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        token = await self._spaces.create_invite_token(
            space_id,
            created_by=actor.user_id,
            uses=1,
            expires_at=expires,
        )
        await self._spaces.save_remote_invitation(
            space_id=space_id,
            invited_by=actor.user_id,
            remote_instance_id=invitee_instance_id,
            remote_user_id=invitee_user_id,
            invite_token=token,
            space_display_hint=space.name,
        )
        # §25.8.21 — zero-leak envelope: space_id + invite_token + all
        # space metadata ride in the *encrypted* payload only. We do
        # NOT pass space_id to send_event (would put it in plaintext).
        # ``invitee_user_id`` is required so the receiving instance can
        # fan the invite out to the right local user — without it,
        # :meth:`PrivateSpaceInviteHandler._on_invite` early-returns
        # and ``GET /api/remote_invites`` stays empty on the recipient.
        await self._send_invite_envelope(
            to_instance_id=invitee_instance_id,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE,
            payload={
                "space_id": space_id,
                "invite_token": token,
                "invitee_user_id": invitee_user_id,
                "inviter_user_id": actor.user_id,
                "inviter_display_name": (actor.display_name or actor.username),
                "space_display_hint": space.name,
                "expires_at": expires,
                # #648 — our household's Ed25519 identity pubkey. A member
                # that joins over the mesh is not a paired peer of ours, so
                # it has no ``remote_instances`` row and no way to verify the
                # per-chunk signatures on the §25.6 catch-up stream we're
                # about to send it; every chunk was dropped at "sync chunk
                # from unknown instance". Rides in the *sealed* payload like
                # everything else here (§25.8.21), and the receiver stores it
                # only if ``derive_instance_id(pk)`` matches our
                # authenticated instance id — so it cannot assert an
                # identity its own id doesn't already commit to. Absent on
                # an older sender: the receiver just keeps the previous
                # behaviour (works for a directly-paired host).
                "host_identity_pk": self._federation.own_identity_pk.hex(),
                # §D1b — the receiver needs enough metadata to seat a
                # local *stub* of this space (so it shows up in their
                # /spaces list after accept), without us shipping any
                # data the joiner couldn't already read from the host's
                # SPACE_CONFIG_CHANGED stream after pairing. Keep the
                # shape under one ``space_meta`` key so older receivers
                # ignore it cleanly via dict-get semantics — no version
                # bump needed.
                "space_meta": await build_space_snapshot_for_federation(
                    space,
                    space_repo=self._spaces,
                    remote_member_repo=self._remote_members,
                    user_repo=self._users,
                    own_instance_id=self._own_instance_id,
                    cover_repo=self._covers,
                    icon_repo=self._icons,
                    space_crypto_service=self._space_crypto,
                ),
            },
        )
        return token

    async def accept_remote_invite(
        self,
        *,
        token: str,
        user_id: str,
    ) -> None:
        """§D1b — invitee side: accept a cross-household private-space
        invite. Sends a SPACE_PRIVATE_INVITE_ACCEPT back to the host.
        """
        if self._federation is None:
            raise RuntimeError("federation not attached")
        invite = await self._spaces.get_invitation_by_token(token)
        if invite is None:
            raise KeyError("invite token invalid or expired")
        host_instance = invite.get("remote_instance_id")
        if not host_instance:
            raise ValueError("not a cross-household invite")
        # §CP.F1 — block an under-age protected minor BEFORE we send the
        # ACCEPT envelope, so the host never sees a join the invitee's
        # household refuses to seat (no split-brain). The host's ``min_age``
        # rides in the invite's ``space_meta`` and is persisted on the stub
        # (see ``_space_metadata_for_federation`` / ``stub_space_from_metadata``),
        # so the gate is effective for cross-household spaces too.
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(
                invite["space_id"],
                user_id,
            )
        # Look up the accepting user so the ACCEPT envelope can carry
        # their human-readable name to the host. The protocol method is
        # ``get_by_user_id``; an earlier draft of this code looked up
        # ``get_by_id`` (which doesn't exist on the repo), so the
        # ``hasattr`` guard was always False → ``display`` stayed None
        # → the host's member roster showed the raw ``user_id`` instead
        # of the user's display name. Symptom Pascal hit when
        # Jacqueline accepted his invite.
        display = None
        user_pk = None
        user = await self._users.get_by_user_id(user_id)
        if user is not None:
            display = user.display_name or user.username
            user_pk = getattr(user, "public_key", None)
        await self._send_invite_envelope(
            to_instance_id=host_instance,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE_ACCEPT,
            payload={
                "invite_token": token,
                "invitee_user_id": user_id,
                "invitee_public_key": user_pk,
                "invitee_display_name": display,
            },
        )
        await self._spaces.update_invitation_status(
            invite["id"],
            "accepted",
        )
        # Record the (space_id, host_instance_id) mapping locally so
        # later space-scoped events the peer mints (RSVPs, comments,
        # …) federate back to the host. Without this row,
        # ``broadcast_to_space_members`` returns no targets on the
        # invitee side and the events go nowhere.
        await self._spaces.add_space_instance(
            invite["space_id"],
            host_instance,
        )
        # Kick a §25.6 catch-up so we pull the space's historical posts /
        # gallery / media from the host. When the host is reachable only via the
        # mesh (not a confirmed direct peer), the SpaceSyncScheduler never
        # triggers — so initiate it explicitly here. No-op + fail-soft for a
        # confirmed host or an unwired sync stack.
        await self._federation.begin_mesh_catchup_sync(
            space_id=invite["space_id"],
            host_instance_id=host_instance,
        )
        # §D1b — seat the local membership row pointing at the stub
        # spaces row that was created when SPACE_PRIVATE_INVITE
        # arrived (see ``PrivateSpaceInviteHandler._on_invite``).
        # ``list_for_user`` JOINs on ``space_members``, so this is
        # what actually surfaces the space in the invitee's
        # ``/api/spaces``. If the stub doesn't exist (older sender,
        # no metadata shipped), the FK constraint will trip and we
        # log+skip — the invitee can still accept, just won't see
        # the space until upstream upgrades.
        stub = await self._spaces.get(invite["space_id"])
        if stub is not None:
            await self._spaces.save_member(
                SpaceMember(
                    space_id=invite["space_id"],
                    user_id=user_id,
                    role=SpaceRole.MEMBER.value,
                    joined_at=datetime.now(timezone.utc).isoformat(),
                )
            )
        else:
            log.warning(
                "accept_remote_invite: stub space row missing for"
                " space_id=%s — invitee will see the invite as accepted"
                " but the space won't show up locally until SPACE_CONFIG"
                "_CHANGED upserts the stub. Sender on older protocol?",
                invite["space_id"],
            )

    async def decline_remote_invite(
        self,
        *,
        token: str,
        user_id: str,
    ) -> None:
        if self._federation is None:
            raise RuntimeError("federation not attached")
        invite = await self._spaces.get_invitation_by_token(token)
        if invite is None:
            raise KeyError("invite token invalid or expired")
        host_instance = invite.get("remote_instance_id")
        if not host_instance:
            raise ValueError("not a cross-household invite")
        await self._send_invite_envelope(
            to_instance_id=host_instance,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE_DECLINE,
            payload={
                "invite_token": token,
                "invitee_user_id": user_id,
            },
        )
        await self._spaces.update_invitation_status(
            invite["id"],
            "declined",
        )

    async def remove_remote_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        instance_id: str,
        user_id: str,
    ) -> None:
        """§D1b — drop a remote member + tell their household."""
        if self._federation is None or self._remote_members is None:
            raise RuntimeError("federation not attached")
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        # Capture role/display BEFORE the tombstone so the LEFT gossip carries
        # the member's last-known attributes.
        removed = await self._remote_members.get(space_id, instance_id, user_id)
        await self._remote_members.remove(space_id, instance_id, user_id)
        # Audit-fix (HIGH from PR #429 review): if that was the
        # last remote member from this peer instance, also drop the
        # ``space_instances`` row so subsequent broadcasts via
        # ``broadcast_to_space_members`` stop trying to deliver
        # content to the kicked household. Without this, future
        # space posts / comments / reactions keep getting shipped
        # to a household that no longer has anyone in the space —
        # transport-rule-compliant (the envelope is encrypted) but
        # wasteful, and could leak metadata about the space's
        # activity to the no-longer-member relay.
        rows = await self._remote_members.list_for_space(space_id)
        still_present = any(r.instance_id == instance_id for r in rows)
        if not still_present:
            await self._spaces.remove_space_instance(space_id, instance_id)
        await self._send_invite_envelope(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            payload={"space_id": space_id, "user_id": user_id},
        )
        # v_23 — peer-replicate the kick so every member household tombstones
        # this user in their roster.
        await self._emit_member_roster_gossip(
            space,
            user_id=user_id,
            instance_id=instance_id,
            display_name=removed.display_name if removed else None,
            user_pk=removed.user_pk if removed else None,
            role=removed.role if removed else SpaceRole.MEMBER,
            tombstoned=True,
        )
        await self._rotate_and_distribute_space_key(space_id)

    async def redeem_invite_token(
        self,
        token: str,
        *,
        user_id: str,
        issuer_instance_id: str | None = None,
    ) -> dict:
        """Consume an invite token, possibly via a cross-instance round-trip.

        When ``issuer_instance_id`` is None or matches our own instance,
        falls through to the local :meth:`accept_invite_token` and
        returns ``{space_id, role}``. Otherwise delegates to the
        attached :class:`SpaceInviteTokenRedeemCoordinator` which
        handshakes with the issuer and returns the same shape. Raises
        :class:`SpacePermissionError` (unpaired / banned / denied) or
        ``TimeoutError`` (issuer unreachable).
        """
        if issuer_instance_id is None or issuer_instance_id == self._own_instance_id:
            member = await self.accept_invite_token(token, user_id=user_id)
            return {"space_id": member.space_id, "role": member.role}
        if self._redeem_coordinator is None:
            raise SpacePermissionError(
                "cross-instance invite redeem is not available on this host",
            )
        return await self._redeem_coordinator.request_redeem(
            token,
            viewer_user_id=user_id,
            issuer_instance_id=issuer_instance_id,
        )

    async def accept_invite_token(
        self,
        token: str,
        *,
        user_id: str,
    ) -> SpaceMember:
        """Consume an invite token and enroll ``user_id`` as a member."""
        row = await self._spaces.consume_invite_token(token)
        if row is None:
            raise KeyError("invite token invalid, expired, or exhausted")
        space_id = row["space_id"]
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                "banned from this space",
                banned=True,
            )
        # §CP.F1 — an invite link must not seat an under-age protected minor
        # in an age-restricted space. (The token is already consumed above;
        # a blocked minor burns one use, which beats letting them in.)
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=SpaceRole.MEMBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=SpaceRole.MEMBER,
            )
        )
        return member

    async def request_join(
        self,
        space_id: str,
        *,
        user_id: str,
        message: str | None = None,
    ) -> str:
        space = await self._require_space(space_id)
        if space.join_mode is JoinMode.INVITE_ONLY:
            raise SpacePermissionError("space is invite-only")
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError("banned from this space", banned=True)
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is not None:
            raise ValueError("already a member")
        request_id = await self._spaces.save_join_request(
            space_id,
            user_id,
            message=message,
        )
        await self._bus.publish(
            SpaceJoinRequested(
                space_id=space_id,
                user_id=user_id,
                request_id=request_id,
                message=message,
            )
        )
        return request_id

    async def approve_join_request(
        self,
        request_id: str,
        *,
        actor_username: str,
    ) -> SpaceMember | None:
        """Approve a pending join request.

        For local applicants, seats the user as a member and returns the
        :class:`SpaceMember`. For §D2 remote applicants, instead produces
        a short-TTL single-use invite token, fires it back via a
        :data:`SPACE_JOIN_REQUEST_APPROVED` envelope, and returns None —
        the applicant's household finalises the join with
        :meth:`accept_invite_token`.
        """
        actor = await self._users.get(actor_username)
        assert actor is not None
        space_id_row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            """
            SELECT space_id, user_id,
                   remote_applicant_instance_id
              FROM space_join_requests WHERE id=?
            """,
            (request_id,),
        )
        row = row_to_dict(space_id_row)
        if row is None:
            raise KeyError(f"join request {request_id!r} not found")
        space = await self._require_space(row["space_id"])
        await self._require_admin_or_owner(space, actor_username)
        remote_instance = row.get("remote_applicant_instance_id")
        # §CP.F1 — a protected minor must not be seated in an over-age space
        # via the request→approve flow either. Check BEFORE flipping the
        # request to "approved" so a blocked minor's request stays pending
        # (never "approved but unseated"). No-op for remote applicants (no
        # local protection record) and when CP isn't wired.
        if self._child_protection is not None and not remote_instance:
            await self._child_protection.check_space_age_gate(
                row["space_id"],
                row["user_id"],
            )
        await self._spaces.update_join_request_status(
            request_id,
            "approved",
            reviewed_by=actor.user_id,
        )
        if remote_instance:
            # §D2 — cross-household approval. Mint an invite token and
            # federate it back; the applicant's household consumes via
            # the existing POST /api/spaces/join path.
            if self._federation is None:
                raise RuntimeError(
                    "space_service: federation not attached; "
                    "cannot approve remote join request",
                )
            expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            token = await self._spaces.create_invite_token(
                row["space_id"],
                created_by=actor.user_id,
                uses=1,
                expires_at=expires,
            )
            await self._federation.send_event(
                to_instance_id=remote_instance,
                event_type=FederationEventType.SPACE_JOIN_REQUEST_APPROVED,
                payload={
                    "request_id": request_id,
                    "space_id": row["space_id"],
                    "invite_token": token,
                    "reviewed_by": actor.user_id,
                },
            )
            await self._bus.publish(
                SpaceJoinApproved(
                    space_id=row["space_id"],
                    user_id=row["user_id"],
                    request_id=request_id,
                    approved_by=actor.user_id,
                )
            )
            return None

        member = SpaceMember(
            space_id=row["space_id"],
            user_id=row["user_id"],
            role=SpaceRole.MEMBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceJoinApproved(
                space_id=row["space_id"],
                user_id=row["user_id"],
                request_id=request_id,
                approved_by=actor.user_id,
            )
        )
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=row["space_id"],
                user_id=row["user_id"],
                role=SpaceRole.MEMBER,
            )
        )
        return member

    async def deny_join_request(
        self,
        request_id: str,
        *,
        actor_username: str,
    ) -> None:
        actor = await self._users.get(actor_username)
        assert actor is not None
        # Look up the request first so we can emit the right event.
        row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            """
            SELECT space_id, user_id, remote_applicant_instance_id
              FROM space_join_requests WHERE id=?
            """,
            (request_id,),
        )
        r = row_to_dict(row)
        await self._spaces.update_join_request_status(
            request_id,
            "denied",
            reviewed_by=actor.user_id,
        )
        if r is not None:
            remote_instance = r.get("remote_applicant_instance_id")
            if remote_instance and self._federation is not None:
                await self._federation.send_event(
                    to_instance_id=remote_instance,
                    event_type=FederationEventType.SPACE_JOIN_REQUEST_DENIED,
                    payload={
                        "request_id": request_id,
                        "space_id": r["space_id"],
                        "reviewed_by": actor.user_id,
                    },
                )
            await self._bus.publish(
                SpaceJoinDenied(
                    space_id=r["space_id"],
                    user_id=r["user_id"],
                    request_id=request_id,
                    denied_by=actor.user_id,
                )
            )

    async def request_join_remote(
        self,
        space_id: str,
        *,
        applicant_user_id: str,
        host_instance_id: str,
        message: str | None = None,
    ) -> str:
        """§D2 — applicant side: federate a join-request to a remote
        global-space host. The host must be a CONFIRMED peer. Persists
        a local pending-request row keyed by the generated
        ``request_id`` so :meth:`on_remote_join_request_approved` can
        match the inbound approval back to this user.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError("federation not attached")
        peer = await self._federation_repo.get_instance(host_instance_id)
        if peer is None or peer.status is not PairingStatus.CONFIRMED:
            raise SpacePermissionError(
                "host household is not a CONFIRMED peer — pair first",
            )
        # Persist locally so the inbound APPROVED handler can look up
        # the applicant_user_id; there's no host-side space row locally.
        # §Audit #11: route through ``save_join_request`` rather than
        # poking the repo's private ``_db`` — no SQL in services.
        request_id = await self._spaces.save_join_request(
            space_id,
            applicant_user_id,
            message=message,
            ttl_days=7,
            remote_applicant_instance_id=host_instance_id,
        )
        await self._federation.send_event(
            to_instance_id=host_instance_id,
            event_type=FederationEventType.SPACE_JOIN_REQUEST,
            payload={
                "request_id": request_id,
                "space_id": space_id,
                "user_id": applicant_user_id,
                "message": message,
            },
            space_id=space_id,
        )
        return request_id

    async def on_remote_join_request_approved(
        self,
        request_id: str,
        *,
        invite_token: str,
    ) -> None:
        """Auto-consume the invite token returned with a
        :data:`SPACE_JOIN_REQUEST_APPROVED` envelope so the applicant
        becomes a space member without further UI clicks.
        """
        row = await self._spaces._db.fetchone(  # type: ignore[attr-defined]
            "SELECT user_id, space_id, remote_applicant_instance_id"
            " FROM space_join_requests WHERE id=?",
            (request_id,),
        )
        r = row_to_dict(row)
        if r is None:
            return
        user_id = r.get("user_id")
        if not user_id:
            return
        # The host-minted invite token row lives in the HOST's DB, not
        # ours — ``remote_applicant_instance_id`` carries that host. Route
        # through the cross-instance redeem path (consumes on the host and
        # seats locally). For a LOCAL approval (host None/self),
        # redeem_invite_token falls back to accept_invite_token.
        host = r.get("remote_applicant_instance_id")
        try:
            await self.redeem_invite_token(
                invite_token,
                user_id=user_id,
                issuer_instance_id=host,
            )
        except KeyError:
            # Token already consumed/replayed — expected on a duplicate
            # APPROVED envelope; nothing to seat.
            log.debug(
                "join-request %s approval: token already consumed for space=%s user=%s",
                request_id,
                r.get("space_id"),
                user_id,
            )
        except (SpacePermissionError, TimeoutError) as exc:
            # A genuine failure — host unreachable, denied, or banned.
            # Don't crash the inbound pipeline, but surface it: a silent
            # drop here strands the applicant with no membership and no
            # signal. No existing "join failed" notification event to
            # publish, so the WARNING log is the observable signal.
            log.warning(
                "join-request %s approval failed to seat user=%s in "
                "space=%s via host=%s: %s",
                request_id,
                user_id,
                r.get("space_id"),
                host,
                exc,
            )

    # ── Space posts ────────────────────────────────────────────────────

    async def create_post(
        self,
        space_id: str,
        *,
        author_user_id: str,
        type: PostType | str,
        content: str | None = None,
        media_url: str | None = None,
        image_urls: tuple[str, ...] | list[str] = (),
        file_meta: FileMeta | None = None,
        location: LocationData | None = None,
        linked_highlight_id: str | None = None,
        hidden_from_feed: bool = False,
    ) -> Post | None:
        """Create a post in the space, subject to the feature's access level.

        Returns the persisted :class:`Post` for `open` / admin paths. For
        `moderated` access where the author isn't an admin, the content
        enters the moderation queue and this method returns ``None`` after
        publishing :class:`SpaceModerationQueued`.
        """
        space = await self._require_writable_space(space_id)
        author = await self._users.get_by_user_id(author_user_id)
        if author is None:
            raise KeyError(f"user {author_user_id!r} not found")
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        self._assert_writable_member(member, action="post")
        if not space.features.allows(type):
            raise SpacePermissionError(f"space does not allow {type!r} posts")

        post_type = _coerce_post_type(type)
        image_urls_tuple = tuple(image_urls)
        _validate_space_content(
            post_type,
            content,
            file_meta,
            location,
            image_urls_tuple,
        )
        is_admin = member.role in (SpaceRole.OWNER, SpaceRole.ADMIN)
        is_host = (
            self._own_instance_id is not None
            and space.owner_instance_id == self._own_instance_id
        )
        # posts_access (deny / moderation-queue) is HOST-authoritative: a remote
        # member composes into their local stub and the post federates to the host.
        # Enforcing the host's MODERATED/ADMIN_ONLY policy locally would dead-end the
        # post in a moderation queue this household can't resolve (the queue isn't
        # federated to the host's admins). The host applies its own policy to content
        # it hosts. On a stub, proceed.
        decision = (
            space.features.access_decision("posts", is_admin=is_admin)
            if is_host
            else "allow"
        )
        if decision == "deny":
            raise SpacePermissionError("posting is admin-only in this space")

        # Truncate to 4dp at the service boundary regardless of what the
        # client sent — the column never holds higher precision than the
        # federated form (§GPS truncation).
        if location is not None:
            location = LocationData(
                lat=truncate_coord(location.lat) or 0.0,
                lon=truncate_coord(location.lon) or 0.0,
                label=location.label,
            )

        post = Post(
            id=uuid.uuid4().hex,
            author=author.user_id,
            type=post_type,
            created_at=datetime.now(timezone.utc),
            content=content,
            media_url=None if post_type is PostType.IMAGE else media_url,
            image_urls=image_urls_tuple,
            file_meta=file_meta,
            location=location,
            linked_highlight_id=linked_highlight_id,
            hidden_from_feed=hidden_from_feed,
        )
        if decision == "queue":
            now = datetime.now(timezone.utc)
            item = SpaceModerationItem(
                id=uuid.uuid4().hex,
                space_id=space_id,
                feature="posts",
                action="create",
                submitted_by=author.user_id,
                payload={
                    "post_id": post.id,
                    "type": post_type.value,
                    "content": content,
                    "media_url": media_url,
                    "file_meta": _file_meta_to_payload(file_meta),
                    "location": (
                        {
                            "lat": location.lat,
                            "lon": location.lon,
                            "label": location.label,
                        }
                        if location is not None
                        else None
                    ),
                },
                current_snapshot=None,
                submitted_at=now,
                expires_at=now + timedelta(days=7),
                status=ModerationStatus.PENDING,
            )
            await self._spaces.save_moderation_item(item)
            await self._bus.publish(SpaceModerationQueued(item=item))
            return None

        await self._persist_post(space_id, post)
        return post

    async def _persist_post(self, space_id: str, post: Post) -> Post:
        """Persist a Post and publish SpacePostCreated.

        Shared by the direct ``create_post`` path and the moderation-approve
        path so both produce identical state transitions and federation
        broadcasts.
        """
        await self._posts.save(space_id, post)
        await self._bus.publish(
            SpacePostCreated(
                post=post,
                space_id=space_id,
            )
        )
        return post

    # ── Moderation queue admin API ─────────────────────────────────────

    async def list_pending_moderation(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> list[SpaceModerationItem]:
        """List pending queue items (admin-only)."""
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        return await self._spaces.list_moderation_queue(
            space_id,
            status=ModerationStatus.PENDING,
        )

    async def approve_moderation_item(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_username: str,
    ) -> Post:
        """Approve a queued post. Persists the post and marks the item
        APPROVED. Raises :class:`ModerationAlreadyDecidedError` if the
        item is not in ``PENDING`` status.
        """
        space = await self._require_space(space_id)
        actor = await self._require_admin_or_owner(space, actor_username)
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id:
            raise KeyError(f"moderation item {item_id!r} not found")
        if item.status is not ModerationStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}",
            )

        post = _post_from_queue_payload(item)
        await self._persist_post(space_id, post)
        await self._spaces.update_moderation_item_status(
            item_id,
            status=ModerationStatus.APPROVED,
            reviewed_by=actor.user_id,
        )
        approved = replace(
            item,
            status=ModerationStatus.APPROVED,
            reviewed_by=actor.user_id,
            reviewed_at=datetime.now(timezone.utc),
        )
        await self._bus.publish(SpaceModerationApproved(item=approved))
        return post

    async def reject_moderation_item(
        self,
        space_id: str,
        item_id: str,
        *,
        actor_username: str,
        reason: str | None = None,
    ) -> None:
        """Reject a queued item; item status becomes REJECTED."""
        space = await self._require_space(space_id)
        actor = await self._require_admin_or_owner(space, actor_username)
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id:
            raise KeyError(f"moderation item {item_id!r} not found")
        if item.status is not ModerationStatus.PENDING:
            raise ModerationAlreadyDecidedError(
                f"item {item_id!r} is already {item.status.value}",
            )

        await self._spaces.update_moderation_item_status(
            item_id,
            status=ModerationStatus.REJECTED,
            reviewed_by=actor.user_id,
            rejection_reason=reason,
        )
        rejected = replace(
            item,
            status=ModerationStatus.REJECTED,
            reviewed_by=actor.user_id,
            reviewed_at=datetime.now(timezone.utc),
            rejection_reason=reason,
        )
        await self._bus.publish(SpaceModerationRejected(item=rejected))

    async def edit_post(
        self,
        post_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Post:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            raise KeyError("post already deleted")
        # Verifies space exists + is writable (not archived) — raises if not.
        await self._require_writable_space(space_id)
        if post.author != editor_user_id:
            # Admin override
            editor = await self._users.get_by_user_id(editor_user_id)
            if editor is None:
                raise PermissionError("not authorised")
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError("only the author or a space admin can edit")
        _validate_text_length(new_content, limit=MAX_POST_LENGTH)
        await self._posts.edit(post_id, new_content)
        refreshed = await self._posts.get(post_id)
        assert refreshed is not None  # just edited — must exist
        # Bus fan-out so subscribers (system-album bridge, search index,
        # SPACE_POST_UPDATED federation outbound) can react. ``space_id``
        # gates the federation broadcast so we don't accidentally fan
        # a household-feed edit out to space members.
        await self._bus.publish(PostEdited(post=refreshed[1], space_id=space_id))
        return refreshed[1]

    async def delete_post(
        self,
        post_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            return
        # Capture media URLs before soft_delete nulls them; unlinked after
        # the PostDeleted publish unmirrors the shared gallery item.
        media = [post.media_url, *post.image_urls] if self._media_dir else []
        moderated_by: str | None = None
        if post.author != actor_user_id:
            # Moderation path — actor must be admin/owner
            member = await self._spaces.get_member(space_id, actor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError("only the author or a space admin can delete")
            moderated_by = actor_user_id
        await self._posts.soft_delete(post_id, moderated_by=moderated_by)
        if moderated_by is not None:
            refreshed = await self._posts.get(post_id)
            assert refreshed is not None  # just soft-deleted — row still exists
            await self._bus.publish(
                SpacePostModerated(
                    space_id=space_id,
                    post=refreshed[1],
                    moderated_by=actor_user_id,
                )
            )
        # Generic post-deleted event — fires on both author + moderation
        # paths so cross-cutting subscribers (system-album bridge, search
        # index, federation outbound for SPACE_POST_DELETED) have a single
        # hook regardless of who deleted the row. ``space_id`` gates the
        # outbound broadcast so household-feed deletes stay local.
        await self._bus.publish(PostDeleted(post_id=post_id, space_id=space_id))
        if self._media_dir is not None:
            for url in media:
                await unlink_media(self._media_dir, url)

    async def add_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = unicodedata.normalize("NFC", emoji.strip())
        if not emoji:
            raise ValueError("emoji must not be empty")
        got = await self._posts.get(post_id)
        if got is not None:
            space_id, _post = got
            await self._require_writable_space(space_id)
            await self._reject_subscriber(space_id, user_id, action="react")
        return await self._posts.add_reaction(post_id, emoji, user_id)

    async def remove_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = unicodedata.normalize("NFC", emoji.strip())
        got = await self._posts.get(post_id)
        if got is not None:
            space_id, _post = got
            await self._reject_subscriber(space_id, user_id, action="react")
        return await self._posts.remove_reaction(post_id, emoji, user_id)

    async def add_comment(
        self,
        post_id: str,
        *,
        author_user_id: str,
        content: str | None = None,
        media_url: str | None = None,
        parent_id: str | None = None,
        comment_type: CommentType | str = CommentType.TEXT,
    ) -> Comment:
        got = await self._posts.get(post_id)
        if got is None:
            raise KeyError(f"space post {post_id!r} not found")
        space_id, post = got
        if post.deleted:
            raise KeyError("cannot comment on deleted post")
        await self._require_writable_space(space_id)
        # Membership check
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        # Subscribers may comment when the space's
        # ``allow_subscriber_comment`` opt-in is on.  Non-subscribers
        # short-circuit before the (cheap) space lookup.
        space = (
            await self._spaces.get(space_id)
            if member.role == SpaceRole.SUBSCRIBER
            else None
        )
        self._assert_writable_member(member, action="comment", space=space)
        ctype = _coerce_comment_type(comment_type)
        if ctype is CommentType.TEXT:
            _validate_text_length(content, limit=MAX_COMMENT_LENGTH)
            if not content or not content.strip():
                raise ValueError("comment content required")
        elif ctype is CommentType.IMAGE and not media_url:
            raise ValueError("image comment requires media_url")
        if parent_id is not None:
            parent = await self._posts.get_comment(parent_id)
            if parent is None or parent.post_id != post_id:
                raise KeyError(f"parent comment {parent_id!r} not in this post")
        comment = Comment(
            id=uuid.uuid4().hex,
            post_id=post.id,
            author=author_user_id,
            type=ctype,
            created_at=datetime.now(timezone.utc),
            parent_id=parent_id,
            content=content,
            media_url=media_url,
        )
        await self._posts.add_comment(comment)
        await self._posts.increment_comment_count(post_id)
        await self._bus.publish(
            CommentAdded(post_id=post_id, comment=comment, space_id=space_id),
        )
        return comment

    async def list_comments(self, post_id: str) -> list[Comment]:
        """Return every comment on a space post in chronological order.

        Used by ``GET /api/spaces/{id}/posts/{post_id}/comments``.
        Bare delegation to the repo — the route layer is responsible
        for the (cheap) membership check before calling. Soft-deleted
        comments are excluded by the repo's filter so the response
        matches what the SPA expects to render.
        """
        return await self._posts.list_comments(post_id)

    async def edit_comment(
        self,
        comment_id: str,
        *,
        editor_user_id: str,
        new_content: str,
    ) -> Comment:
        """Edit a space comment's body. Author-or-space-admin only."""
        comment = await self._posts.get_comment(comment_id)
        if comment is None or comment.deleted:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.type is not CommentType.TEXT:
            raise ValueError("only text comments can be edited")
        got = await self._posts.get(comment.post_id)
        if got is None:
            raise KeyError("post disappeared")
        space_id, _post = got
        await self._require_writable_space(space_id)
        if comment.author != editor_user_id:
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError(
                    "only the author or a space admin can edit this comment",
                )
        _validate_text_length(new_content, limit=MAX_COMMENT_LENGTH)
        if not new_content.strip():
            raise ValueError("comment body cannot be empty")
        await self._posts.edit_comment(comment_id, new_content)
        updated = await self._posts.get_comment(comment_id)
        assert updated is not None
        await self._bus.publish(
            CommentUpdated(
                post_id=updated.post_id,
                comment=updated,
                space_id=space_id,
            ),
        )
        return updated

    async def delete_comment(
        self,
        comment_id: str,
        *,
        actor_user_id: str,
    ) -> None:
        comment = await self._posts.get_comment(comment_id)
        if comment is None:
            raise KeyError(f"comment {comment_id!r} not found")
        if comment.deleted:
            return
        got = await self._posts.get(comment.post_id)
        if got is None:
            raise KeyError("post disappeared")
        space_id, _post = got
        if comment.author != actor_user_id:
            member = await self._spaces.get_member(space_id, actor_user_id)
            if member is None or member.role not in (SpaceRole.OWNER, SpaceRole.ADMIN):
                raise PermissionError(
                    "only the author or a space admin can delete this comment"
                )
        await self._posts.soft_delete_comment(comment_id)
        await self._posts.decrement_comment_count(comment.post_id)
        await self._bus.publish(
            CommentDeleted(
                post_id=comment.post_id,
                comment_id=comment_id,
                space_id=space_id,
            ),
        )

    async def list_feed(
        self,
        space_id: str,
        *,
        before: str | None = None,
        limit: int = 20,
    ) -> list[Post]:
        await self._require_space(space_id)
        limit = max(1, min(int(limit), 50))
        return await self._posts.list_feed(space_id, before=before, limit=limit)

    async def get_space(self, space_id: str) -> Space | None:
        """Public read of a space row (``None`` when missing). Used by
        sibling services (e.g. :class:`BazaarService`) that need to read
        a space's feature flags without reaching into the repo directly."""
        return await self._spaces.get(space_id)

    # ── Sidebar pins + aliases (convenience) ───────────────────────────

    async def pin(
        self,
        user_id: str,
        space_id: str,
        position: int = 0,
    ) -> None:
        await self._require_space(space_id)
        await self._spaces.pin_sidebar(user_id, space_id, int(position))

    async def unpin(self, user_id: str, space_id: str) -> None:
        await self._spaces.unpin_sidebar(user_id, space_id)

    async def set_alias(
        self,
        space_id: str,
        *,
        username: str,
        alias: str,
    ) -> None:
        await self._require_space(space_id)
        await self._spaces.set_space_alias(space_id, username, alias)

    # ── Subscriptions (read-only membership) ───────────────────────────
    #
    # Subscribe = add self to the space as a :class:`SpaceMember` with
    # ``role='subscriber'``. Same content delivery as real members: the
    # normal sync + fan-out stream applies. Write paths (post / comment
    # / reaction) gate on role and reject subscribers — they're strictly
    # read-only.
    #
    # Note the name: we use *subscribe* rather than *follow* because the
    # frontend already uses "followed spaces" for a different concept
    # (a dashboard pin list over spaces the user is a full member of —
    # see ``users.preferences_json['followed_space_ids']`` and
    # ``corner_service``). The two features are distinct on purpose.
    #
    # Constraints:
    # * Only ``PUBLIC`` and ``GLOBAL`` spaces are subscribable. Private /
    #   household spaces require an invite.
    # * If the caller is already a member (any non-subscriber role),
    #   subscribe is a no-op — we do not demote real members.
    # * Subscribe respects bans + the §CP.F1 age gate, same as
    #   ``add_member``.

    async def subscribe_to_space(self, user_id: str, space_id: str) -> None:
        # GFS on-ramp: a space discovered through a paired GFS has no local
        # ``spaces`` row yet, so ``_require_space`` below would 404 it. Mirror
        # the GFS listing onto a local stub FIRST (see
        # :class:`GfsSpaceMirrorService` for the TOFU trust boundary). A
        # mirror that returns ``None`` (no GFS knows it / unverifiable pin)
        # falls through to the unchanged 404.
        gfs_id: str | None = None
        if self._gfs_mirror is not None and await self._spaces.get(space_id) is None:
            mirrored = await self._gfs_mirror.ensure_mirror(space_id)
            if mirrored is not None:
                gfs_id = mirrored[1]
        space = await self._require_space(space_id)
        if space.space_type not in PUBLIC_SPACE_TIERS:
            raise SpacePermissionError(
                "only public / global spaces can be subscribed to",
            )
        if await self._spaces.is_banned(space_id, user_id):
            raise SpacePermissionError(
                f"user {user_id!r} is banned from this space",
                banned=True,
            )
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is not None:
            # Already a member (any role) — no-op. Never demote.
            return
        if self._child_protection is not None:
            await self._child_protection.check_space_age_gate(space_id, user_id)
        # ORDER MATTERS: the GFS-side subscriber registration happens only
        # AFTER every local refusal (public-tier, ban, §CP.F1 age gate) has
        # passed. A locally-refused user must never end up on the GFS's
        # ``space_subscribers`` set — that would fan relayed space content at
        # this household on their behalf. A failing GFS subscribe propagates:
        # no local member row is seated for a relay we never registered for.
        if gfs_id is not None and self._gfs_mirror is not None:
            await self._gfs_mirror.subscribe_to_gfs(space_id, gfs_id)
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role=SpaceRole.SUBSCRIBER,
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role=SpaceRole.SUBSCRIBER,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="joined",
                actor_id=user_id,
            )

    async def unsubscribe_from_space(self, user_id: str, space_id: str) -> None:
        """Remove a self-subscription. No-op if the user isn't a
        subscriber — real members must use ``remove_member`` /
        ``leave_space`` (we refuse to silently demote them by
        "unsubscribing")."""
        existing = await self._spaces.get_member(space_id, user_id)
        if existing is None or existing.role != SpaceRole.SUBSCRIBER:
            return
        await self._spaces.delete_member(space_id, user_id)
        await self._bus.publish(
            SpaceMemberLeft(
                space_id=space_id,
                user_id=user_id,
            )
        )
        if self._child_protection is not None:
            await self._child_protection.record_membership_change(
                user_id=user_id,
                space_id=space_id,
                action="removed",
                actor_id=user_id,
            )
        await self._maybe_purge_gfs_mirror(space_id)

    async def _maybe_purge_gfs_mirror(self, space_id: str) -> None:
        """Drop a GFS-mirrored stub once its last local subscriber leaves.

        Both of the things this does — telling every paired GFS we are gone,
        and deleting the row with its posts / gallery / bazaar / media — are
        irreversible and visible to third parties, so they run only on
        *positive* evidence that the row is a GFS mirror:

        * the space is ``GLOBAL`` (what the mirror seats), owned by another
          instance, and we hold no space seed for it (we are not its
          authority); **and**
        * a ``public_space_cache`` row exists for the id — i.e. some paired
          GFS directory actually advertised this space
          (``GfsSpaceMirrorService.was_gfs_listed``).

        Without that evidence the row may be a public/global stub learned
        from a direct peer, which has nothing to do with any GFS: fanning a
        signed, identity-bound unsubscribe at every GFS operator would
        disclose a relationship with a space they never knew about, and the
        purge would destroy a space we were never asked to forget. In that
        case the local member removal (already done by the caller) is all
        that happens — losing a stub row is worse than keeping an inert one.

        The GFS-side unsubscribe is best-effort (a down GFS must not block the
        local leave); the local purge then cascades from ``spaces``, which is
        what drops the space's ``space_keys`` row — the content key goes with
        the mirror rather than lingering for a space we can no longer read.
        """
        if self._gfs_mirror is None:
            return
        space = await self._spaces.get(space_id)
        if space is None or space.owner_instance_id == self._own_instance_id:
            return
        try:
            if await self._spaces.get_space_seed(space_id) is not None:
                # We hold authority for this space — not a passive mirror.
                return
        except RuntimeError:
            # Seed access is unavailable (no KEK wired). We cannot establish
            # that we are *not* this space's authority, and this path is
            # destructive — fail safe by doing nothing.
            log.debug(
                "space %s: cannot read the space seed — skipping mirror purge",
                space_id,
            )
            return
        if await self._spaces.list_members(space_id):
            # Another local user still subscribes — keep the mirror.
            return
        proven_mirror = space.space_type is SpaceType.GLOBAL and (
            await self._gfs_mirror.was_gfs_listed(space_id)
        )
        if not proven_mirror:
            log.debug(
                "space %s: not provably a GFS mirror (type=%s, gfs-listed=no)"
                " — leaving the row alone and sending no GFS unsubscribe",
                space_id,
                space.space_type,
            )
            return
        await self._gfs_mirror.unsubscribe(space_id)
        await purge_space_and_media(
            space_repo=self._spaces,
            post_repo=self._posts,
            gallery_repo=self._gallery,
            bazaar_repo=self._bazaar,
            media_dir=self._media_dir,
            space_id=space_id,
        )

    async def list_subscriptions(self, user_id: str) -> list[dict]:
        return await self._spaces.list_subscriptions_for_user(user_id)

    async def list_pending_join_request_space_ids(
        self,
        user_id: str,
    ) -> list[str]:
        """Space ids the caller has an outstanding (``pending``) join
        request for — used by the SPA to restore "Request pending" on
        the browser/detail cards after a reload."""
        return await self._spaces.list_pending_join_request_space_ids_for_user(
            user_id,
        )

    async def is_subscribed(self, user_id: str, space_id: str) -> bool:
        member = await self._spaces.get_member(space_id, user_id)
        return member is not None and member.role == SpaceRole.SUBSCRIBER

    # ── Sidebar links (§23 — admin-configurable quick-links) ───────────

    async def list_links(self, space_id: str, *, actor_user_id: str) -> list[dict]:
        await self._require_member(space_id, actor_user_id)
        return await self._spaces.list_links(space_id)

    async def upsert_link(
        self,
        *,
        space_id: str,
        actor_username: str,
        link_id: str | None,
        label: str,
        url: str,
        position: int,
    ) -> dict:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        label = label.strip()
        url = url.strip()
        if not label:
            raise ValueError("label must not be empty")
        if not url:
            raise ValueError("url must not be empty")
        link_id = link_id or uuid.uuid4().hex
        await self._spaces.upsert_link(
            link_id=link_id,
            space_id=space_id,
            label=label,
            url=url,
            position=int(position),
        )
        return {
            "id": link_id,
            "label": label,
            "url": url,
            "position": int(position),
        }

    async def delete_link(
        self,
        *,
        link_id: str,
        actor_username: str,
    ) -> None:
        link = await self._spaces.get_link(link_id)
        if link is None:
            raise KeyError(f"link {link_id!r} not found")
        space = await self._require_space(link["space_id"])
        await self._require_admin_or_owner(space, actor_username)
        await self._spaces.delete_link(link_id)

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_space(self, space_id: str) -> Space:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _require_writable_space(self, space_id: str) -> Space:
        """Like :meth:`_require_space` but also rejects an **archived**
        space — archive is read-only, so content writes (post / comment /
        edit / react) raise :class:`SpacePermissionError` until the space
        is unarchived. Reads keep using :meth:`_require_space`."""
        space = await self._require_space(space_id)
        if space.archived:
            raise SpacePermissionError(
                "space is archived (read-only) — unarchive it to make changes",
            )
        return space

    async def _require_member(
        self,
        space_id: str,
        user_id: str,
    ) -> SpaceMember:
        return await self._member_or_raise(space_id, user_id)

    async def _reject_subscriber(
        self,
        space_id: str,
        user_id: str,
        *,
        action: str = "post",
        space: "Space | None" = None,
    ) -> None:
        """Raise :class:`SpacePermissionError` if ``user_id`` is a
        subscriber of ``space_id`` and the action isn't admin-opted-in
        for subscribers.  Reactions and comments can be opted in via
        ``SpaceFeatures.allow_subscriber_react`` /
        ``allow_subscriber_comment``; posts always remain member-only.

        Used on write paths that haven't already fetched the member
        row.  For paths that have already fetched the member row,
        prefer :meth:`_assert_writable_member` to avoid a second
        lookup.
        """
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            return
        if space is None and member.role == SpaceRole.SUBSCRIBER:
            space = await self._spaces.get(space_id)
        self._assert_writable_member(member, action=action, space=space)

    @staticmethod
    def _assert_writable_member(
        member: SpaceMember,
        *,
        action: str = "post",
        space: "Space | None" = None,
    ) -> None:
        """Raise :class:`SpacePermissionError` if ``member`` is a
        read-only subscriber and the action isn't admin-opted-in for
        subscribers.  Use on every write path after the membership
        lookup.
        """
        if member.role != SpaceRole.SUBSCRIBER:
            return
        # Subscriber-engagement opt-ins (§23.49).  Admins may allow
        # subscribers to react and/or comment without making them full
        # members.  Posts (action=='post') stay strictly member-only.
        if space is not None:
            if action == "react" and space.features.allow_subscriber_react:
                return
            if action == "comment" and space.features.allow_subscriber_comment:
                return
        raise SpacePermissionError(
            f"subscribers can only read — joining as a member is required to {action}",
        )

    async def _require_admin_or_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._actor_or_raise(actor_username)
        return await self._role_or_raise(
            space.id,
            actor.user_id,
            (SpaceRole.OWNER, SpaceRole.ADMIN),
            message="admin or owner required",
        )

    async def _require_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._actor_or_raise(actor_username)
        return await self._role_or_raise(
            space.id,
            actor.user_id,
            (SpaceRole.OWNER,),
            message="owner required",
        )


# ─── Helpers ──────────────────────────────────────────────────────────────


async def build_space_snapshot_for_federation(
    space: Space,
    *,
    space_repo,
    remote_member_repo,
    user_repo,
    own_instance_id: str,
    cover_repo=None,
    icon_repo=None,
    space_crypto_service=None,
) -> dict:
    """:func:`_space_metadata_for_federation` + a roster of every
    member of this space.

    The roster lets a §D1b joiner mirror the full member list
    locally — without it the joiner's Members tab on a stub of
    Pascal's space shows only herself. Each row carries
    ``(user_id, instance_id, display_name, role, joined_at)`` —
    the minimum the receiver needs to write a
    :class:`SpaceRemoteMember` row that the route handler then
    merges into ``GET /api/spaces/{id}/members``.

    The host's own local users ship with ``instance_id`` set to
    *our* instance, because from the joiner's perspective every
    member of this space lives somewhere else.
    """
    meta = _space_metadata_for_federation(space)
    local_members = await space_repo.list_members(space.id)
    local_users = await user_repo.list_by_ids({m.user_id for m in local_members})
    name_by_id = {u.user_id: u.display_name for u in local_users}
    roster: list[dict] = []
    for m in local_members:
        roster.append(
            {
                "user_id": m.user_id,
                "instance_id": own_instance_id,
                "display_name": name_by_id.get(m.user_id, ""),
                "role": m.role,
                "joined_at": m.joined_at,
                # v_23 — local members have no per-row gossip version yet
                # (the column lives on space_remote_members, not
                # space_members), so they ship as 0; the receiver's merge
                # treats any later gossip as strictly newer.
                "member_version": 0,
            }
        )
    if remote_member_repo is not None:
        for r in await remote_member_repo.list_for_space(space.id):
            roster.append(
                {
                    "user_id": r.user_id,
                    "instance_id": r.instance_id,
                    "display_name": r.display_name or "",
                    "role": r.role,
                    "joined_at": r.joined_at or "",
                    # Federated peers carry a public_key on the host
                    # side; ship it so the joiner can record it too
                    # and later verify signed events from this user.
                    "user_pk": r.user_pk,
                    # v_23 — the merged convergence version so a freshly-
                    # invited joiner starts already at the host's roster
                    # state and a later in-flight gossip can't regress it.
                    "member_version": r.member_version,
                }
            )
    meta["roster"] = roster
    # The space's dedicated monotonic roster_sequence, shipped for
    # forward-compat / parity with config_sequence. NB: no receiver currently
    # reads roster_version for staleness detection — the per-roster-entry
    # member_version (above) is what the CRDT merge keys on, and the stub's
    # roster_sequence is round-tripped via the base meta's "roster_sequence"
    # key (see _space_metadata_for_federation). Decoupled from config_sequence
    # (migration 0036); backfilled from it so it stays strictly above every
    # prior member_version.
    meta["roster_version"] = space.roster_sequence
    # §D1b cover federation (#116) — ship the actual WebP bytes
    # alongside ``cover_hash``. Without them, the joiner's stub
    # renders the gradient fallback even when the host has a
    # custom cover. Capped at SPACE_COVER_MAX_DIMENSION on the
    # host side, so the payload stays under ~150 kB even at the
    # densest end. Base64 because the envelope is JSON.
    if cover_repo is not None and space.cover_hash:
        cover = await cover_repo.get(space.id)
        if cover is not None:
            bytes_webp, _hash = cover
            meta["cover_webp_base64"] = base64.b64encode(bytes_webp).decode("ascii")
    # §D1b icon federation — ship the space icon (avatar) WebP bytes the
    # same way as the cover, so a joiner's stub shows the real icon rather
    # than falling back to the emoji. Small (≤256 px), so cheap to inline.
    if icon_repo is not None and space.icon_hash:
        icon = await icon_repo.get(space.id)
        if icon is not None:
            icon_webp, _ih = icon
            meta["icon_webp_base64"] = base64.b64encode(icon_webp).decode("ascii")
    # §D1b content-key handoff (#117) — the space content key is the
    # symmetric AES-256 secret that decrypts every event in this
    # space. Ship it inside the (already-encrypted to the invitee
    # instance) envelope so the new member can read posts, comments,
    # reactions etc. without us having to bolt on per-user
    # asymmetric key delivery. The envelope-level encryption is the
    # security contract here: §D1b promises GFS sees only routing,
    # and an attacker who can already read the envelope payload is
    # the host on either end and already has the key. NEVER ship
    # this dict outside an encrypted federation envelope.
    if space_crypto_service is not None:
        # Ensure the membership-gated content key exists before we try to
        # hand it off. A space shared only over a mesh route may never have
        # minted one (live mesh posts use the per-route SPACE_ROUTED seal,
        # not the content key), so export_current_key would return None and
        # the new member would get no key — leaving them unable to decrypt
        # space content and breaking the §25.6 catch-up sync (whose exporter
        # encrypts each chunk under this key). initialise_for_space is a
        # no-op when a key already exists. Only the space's host reaches this
        # builder (the §D1b invite + redeem-ACK paths), so minting under the
        # local KEK is correct.
        await space_crypto_service.initialise_for_space(space.id)
        key_info = await space_crypto_service.export_current_key(space.id)
        if key_info is not None:
            epoch, raw_key = key_info
            # ``key_suite`` is the forward-compat lever — see
            # :data:`KEY_SUITE_AESGCM_256` in space_crypto_service.
            # Mirrors the ``kem_suite`` convention in
            # ``routed_crypto.py`` so a future PQ-protected variant
            # is a wire-additive change; older receivers reject
            # unknown suites rather than silently fall back.
            meta["space_content_key"] = {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
            }
    return meta


def _space_metadata_for_federation(space: Space) -> dict:
    """Snapshot the space's user-visible config for federation envelopes.

    Used by the §D1b invite + redeem-ACK paths so the joiner's
    instance has enough material to seat a local stub row in
    ``spaces`` (name, owner identity, feature toggles, etc.). Mirrors
    the columns ``Space`` already exposes — we deliberately don't
    include host-private state (admins list, ban list, cover bytes;
    those federate over their own dedicated events).
    """
    return {
        "name": space.name,
        "emoji": space.emoji,
        "description": space.description,
        "owner_instance_id": space.owner_instance_id,
        "owner_username": space.owner_username,
        "identity_public_key": space.identity_public_key,
        "config_sequence": space.config_sequence,
        # Hybrid Logical Clock for the config-LWW tie-break (migration 0037).
        # Round-tripped into receiver stubs so two seed-holders' genuinely
        # concurrent same-sequence edits resolve to the LATER one (greater HLC)
        # on every household, deterministically. Missing on an older sender →
        # stub fails soft to "0-0", which ties under the LWW and falls back to
        # the author tie-break (behaviour-identical to pre-0037).
        "config_hlc": space.config_hlc,
        # The dedicated monotonic roster counter (decoupled from
        # config_sequence on migration 0036). Round-tripped into receiver
        # stubs so a delegated admin's offline-of-owner roster gossip emits
        # versions anchored at the host's value — strictly above every other
        # household's stored member_version — instead of restarting at 1 and
        # being dropped by the version-guarded CRDT merge (C1 regression).
        # Missing on an older sender → stub fails soft to config_sequence.
        "roster_sequence": space.roster_sequence,
        "space_type": space.space_type.value,
        "join_mode": space.join_mode.value,
        # Canonical full wire form (all SpaceFeatures fields) so no toggle is
        # silently dropped — receivers ignore unknown keys, so adding the
        # access-level + subscriber fields here is additive and fail-soft.
        # Notable consumers of the now-federated fields:
        # * ``allowed_post_types`` (§23.49) + the ``*_access`` levels — a member
        #   household enforces the host's per-feature restriction locally when
        #   its users compose (each instance gates ``create_post`` against its
        #   own stub). An older sender omitting a field → the receiver defaults
        #   to all-allowed / OPEN (the historical behaviour).
        # * ``delegated_admin_authority`` — a §D1b joiner's stub (and a member
        #   applying the config-flip broadcast) needs delegation ON locally
        #   before its SPACE_ADMIN_KEY_SHARE handler accepts the signing seed.
        #   Missing on an older sender → receiver defaults OFF (fail-soft).
        "features": space.features.to_wire_dict(),
        "tz": space.tz,
        # §CP.F1 — federate the host's age gate so member households enforce
        # it on their own join paths (a protected minor below ``min_age`` is
        # refused locally). Missing on an older sender → stub defaults to
        # min_age=0 (no restriction), so this is additive + fail-soft.
        "min_age": space.min_age,
        # §23.50 discovery hint — optional, fail-soft (missing → "general").
        "category": normalize_category(space.category),
        "cover_hash": space.cover_hash,
        "icon_hash": space.icon_hash,
        "about_markdown": space.about_markdown,
        # Read-only archive state — federates so member households go
        # read-only too. Reversible (an unarchive ships archived=False).
        "archived": space.archived,
    }


async def apply_space_content_key_from_metadata(
    space_id: str,
    *,
    meta: dict,
    space_crypto_service,
) -> None:
    """Persist the §D1b shipped space content key on the receiver side.

    The envelope carrying ``meta`` is itself encrypted to this
    instance (§D1b zero-leak), so by the time we read
    ``meta["space_content_key"]`` it's plaintext only inside this
    process. We immediately re-wrap with the local KEK via
    :meth:`SpaceContentEncryption.import_key` so it lands in
    ``space_keys`` with the same at-rest shape every locally-minted
    key has — the at-rest invariant is "wrapped by THIS instance's
    KEK", and the import path preserves it.

    No-op when the receiver doesn't have a SpaceContentEncryption
    service wired (e.g. test stacks with no KEK), when the host
    didn't ship a key (legacy sender, or pre-key-init space), or
    when the payload is malformed (defensive — we'd rather keep
    the user able to *see* the stub than crash the accept handler).
    """
    if space_crypto_service is None:
        return
    payload = meta.get("space_content_key")
    if not isinstance(payload, dict):
        return
    # Forward-compat lever — receivers reject unknown suites rather
    # than fall back to a default. Senders that don't include
    # ``key_suite`` (this build's first revision) default to the
    # single value we support today.
    suite = payload.get("key_suite", KEY_SUITE_AESGCM_256)
    if suite not in SUPPORTED_KEY_SUITES:
        log.warning(
            "apply_space_content_key_from_metadata: unsupported key_suite "
            "%r for %s; receiver will stay unable to decrypt until "
            "upgraded.",
            suite,
            space_id,
        )
        raise UnsupportedKeySuite(
            f"space content key advertises unsupported key_suite={suite!r}; "
            f"this build supports {sorted(SUPPORTED_KEY_SUITES)!r}",
        )
    epoch = payload.get("epoch")
    key_b64 = payload.get("key_base64")
    if not isinstance(key_b64, str) or epoch is None:
        return
    # Phase 4b — the minting household, used by ``import_key`` as a
    # deterministic tiebreak when two delegated admins rotate to the same
    # epoch concurrently. Absent on a pre-Phase-4b sender → None (degrades to
    # last-writer-wins, the historical behaviour).
    rotated_by = payload.get("rotated_by")
    if rotated_by is not None and not isinstance(rotated_by, str):
        rotated_by = None
    try:
        raw = base64.b64decode(key_b64)
    except Exception:  # pragma: no cover — defensive
        log.warning(
            "apply_space_content_key_from_metadata: invalid base64 for %s",
            space_id,
        )
        return
    if len(raw) != 32:
        log.warning(
            "apply_space_content_key_from_metadata: wrong key length %d for %s",
            len(raw),
            space_id,
        )
        return
    try:
        await space_crypto_service.import_key(
            space_id, int(epoch), raw, rotated_by=rotated_by
        )
    except Exception:  # pragma: no cover — defensive
        log.exception(
            "apply_space_content_key_from_metadata: import_key raised for %s",
            space_id,
        )


async def apply_space_cover_from_metadata(
    space_id: str,
    *,
    meta: dict,
    cover_repo,
) -> None:
    """When a §D1b stub-creation event carries the host's WebP cover
    bytes (``meta['cover_webp_base64']``), decode + persist them via
    the supplied cover repo so the joiner's ``/api/spaces/{id}/cover``
    serves the real image instead of the gradient placeholder.

    No-op when ``cover_repo`` isn't wired, when the host didn't ship
    bytes (older sender), or when base64 decoding fails (the receiver
    just keeps the gradient fallback rather than crashing).
    """
    if cover_repo is None:
        return
    b64 = meta.get("cover_webp_base64")
    if not isinstance(b64, str) or not b64:
        return
    try:
        bytes_webp = base64.b64decode(b64)
    except Exception:  # pragma: no cover — defensive
        log.warning(
            "apply_space_cover_from_metadata: invalid base64 for space %s",
            space_id,
        )
        return
    cover_hash = str(meta.get("cover_hash") or "")
    if not cover_hash:
        return
    # ``width`` / ``height`` aren't shipped today — the SPA renders
    # the cover at whatever native dimensions the WebP carries, so
    # passing 0 here is fine. The repo signature requires the
    # kwargs; later we can ship dimensions in ``space_meta`` too.
    await cover_repo.set(
        space_id,
        bytes_webp=bytes_webp,
        hash=cover_hash,
        width=0,
        height=0,
    )


async def apply_space_icon_from_metadata(
    space_id: str,
    *,
    meta: dict,
    icon_repo,
) -> None:
    """Persist the host's icon WebP bytes (``meta['icon_webp_base64']``) on
    a joiner so ``/api/spaces/{id}/icon`` serves the real avatar. Mirrors
    :func:`apply_space_cover_from_metadata`; no-op when the repo isn't wired,
    the host shipped no bytes, or decoding fails."""
    if icon_repo is None:
        return
    b64 = meta.get("icon_webp_base64")
    if not isinstance(b64, str) or not b64:
        return
    try:
        icon_webp = base64.b64decode(b64)
    except Exception:  # pragma: no cover — defensive
        log.warning(
            "apply_space_icon_from_metadata: invalid base64 for space %s",
            space_id,
        )
        return
    icon_hash = str(meta.get("icon_hash") or "")
    if not icon_hash:
        return
    await icon_repo.set(
        space_id,
        bytes_webp=icon_webp,
        hash=icon_hash,
        width=0,
        height=0,
    )


def stub_space_from_metadata(
    space_id: str,
    *,
    host_instance_id: str,
    meta: dict,
) -> Space:
    """Build a :class:`Space` from a federation metadata payload (the
    counterpart to :func:`_space_metadata_for_federation`).

    Used by the §D1b inbound paths — ``PrivateSpaceInviteHandler``
    on receipt of ``SPACE_PRIVATE_INVITE`` and the receiver side of
    ``SpaceInviteTokenRedeemCoordinator`` on receipt of the ACK —
    to seat a local **stub** row in ``spaces``. The joiner doesn't
    own the space, but having the row locally is what makes the
    space show up in their ``/api/spaces`` once they accept and a
    matching ``space_members`` row is inserted.

    ``host_instance_id`` is the envelope's authenticated ``from_instance``
    and becomes the stub's ``owner_instance_id`` (the issuer-supplied
    ``meta["owner_instance_id"]`` is deliberately ignored — see below). The
    resulting Space's ``owner_instance_id != my_instance`` is the runtime
    signal for "this is a remote space" downstream.
    """
    # Faithful inverse of ``_space_metadata_for_federation``'s
    # ``to_wire_dict``. ``from_wire_dict`` parses ``location_mode`` +
    # ``allowed_post_types`` with the SAME defaults this used to do by hand
    # (calendar=True, etc.) and ALSO reconstructs the access-level +
    # subscriber + delegated-admin fields the host now ships. A field absent
    # on an older sender falls back to the dataclass default (all-allowed /
    # OPEN / OFF), so absent-field behaviour is unchanged.
    feats_in = meta.get("features") or {}
    features = SpaceFeatures.from_wire_dict(feats_in)
    return Space(
        id=space_id,
        name=str(meta.get("name") or "Untitled space"),
        emoji=meta.get("emoji"),
        description=meta.get("description"),
        # §D1b — the owner is the AUTHENTICATED envelope sender, never the
        # issuer-controlled meta["owner_instance_id"]. In every legit flow
        # the snapshot is built by the owning host itself, so meta's claim
        # equals host_instance_id; ignoring the claim closes a footgun where
        # a malicious issuer could stamp a spoofed owner on a brand-new stub
        # (which the can_seat_remote_stub guard then trusts on later events).
        owner_instance_id=host_instance_id,
        owner_username=str(meta.get("owner_username") or ""),
        identity_public_key=str(meta.get("identity_public_key") or ""),
        config_sequence=int(meta.get("config_sequence") or 0),
        # Adopt the winning edit's HLC (migration 0037) so a subsequent LOCAL
        # edit ticks causally after it. Fail-soft to "0-0" for an older sender,
        # which ties under the LWW and falls back to the author tie-break.
        config_hlc=str(meta.get("config_hlc") or "0-0"),
        # Anchor the stub's roster counter to the host's, failing soft to
        # config_sequence on an older sender (see _coerce_roster_sequence).
        roster_sequence=_coerce_roster_sequence(meta),
        features=features,
        space_type=_coerce_space_type(meta.get("space_type") or "private"),
        join_mode=_coerce_join_mode(meta.get("join_mode") or "invite_only"),
        tz=str(meta.get("tz") or "UTC"),
        cover_hash=meta.get("cover_hash"),
        icon_hash=meta.get("icon_hash"),
        about_markdown=meta.get("about_markdown"),
        archived=bool(meta.get("archived", False)),
        # §CP.F1 — carry the host's age gate into the stub so the joiner
        # household enforces it locally. Fail-soft: older sender omits it
        # → min_age 0 (no restriction).
        min_age=_coerce_min_age(meta.get("min_age")),
        category=normalize_category(meta.get("category")),
    )


def _coerce_roster_sequence(meta: dict) -> int:
    """Anchor a stub's roster counter from a federation meta payload.

    Reads ``roster_sequence`` when the sender ships it; fails soft to
    ``config_sequence`` when absent (an older sender / pre-fix snapshot).
    config_sequence was the pre-commit gossip source, so it stays ≥ every
    historical member_version — keeping the stub monotonically anchored so a
    delegated admin's offline-of-owner roster gossip emits versions strictly
    above every other household's stored member_version (C1 regression).
    """
    raw = meta.get("roster_sequence")
    if raw is None:
        raw = meta.get("config_sequence") or 0
    return int(raw)


def _coerce_min_age(value: object) -> int:
    """Clamp a federated ``min_age`` to the allowed set ({0,13,16,18}).

    Thin alias for :func:`socialhome.domain.space.normalize_min_age`, kept
    for the existing federation importers.
    """
    return normalize_min_age(value)


async def can_seat_remote_stub(
    space_repo,
    space_id: str,
    issuer_instance_id: str,
) -> bool:
    """§D1b anti-hijack — may an inbound stub for *space_id* shipped by
    *issuer_instance_id* be seated / overwritten?

    Returns ``False`` when a local ``spaces`` row already exists owned by a
    **different** instance: a remote peer must not ship metadata that
    clobbers a space we already hold under another host (which would
    rewrite its name/owner/config and import a foreign content key). A
    brand-new space (no local row) is always seatable, and re-seating a
    row already owned by this issuer is fine.

    ``issuer_instance_id`` MUST be the authenticated envelope sender
    (``event.from_instance`` / the redeemed issuer), never the
    attacker-controlled ``meta["owner_instance_id"]`` — otherwise a
    malicious host could spoof the claimed owner to pass the check.
    Mirrors the host-authority guard in
    ``FederationInboundService._on_space_config_changed``.
    """
    existing = await space_repo.get(space_id)
    return existing is None or existing.owner_instance_id == issuer_instance_id


def _coerce_space_type(value: SpaceType | str) -> SpaceType:
    if isinstance(value, SpaceType):
        return value
    try:
        return SpaceType(value)
    except ValueError as exc:
        raise ValueError(f"invalid space type {value!r}") from exc


def _coerce_join_mode(value: JoinMode | str) -> JoinMode:
    if isinstance(value, JoinMode):
        return value
    try:
        return JoinMode(value)
    except ValueError as exc:
        raise ValueError(f"invalid join mode {value!r}") from exc


def _coerce_post_type(value: PostType | str) -> PostType:
    if isinstance(value, PostType):
        return value
    try:
        return PostType(value)
    except ValueError as exc:
        raise ValueError(f"invalid post type {value!r}") from exc


def _coerce_comment_type(value: CommentType | str) -> CommentType:
    if isinstance(value, CommentType):
        return value
    try:
        return CommentType(value)
    except ValueError as exc:
        raise ValueError(f"invalid comment type {value!r}") from exc


#: Cap for the optional location-post label. Mirrors
#: feed_service.LOCATION_LABEL_MAX so the household + space surfaces
#: agree.
LOCATION_LABEL_MAX = 80


def _validate_space_content(
    post_type: PostType,
    content: str | None,
    file_meta: FileMeta | None,
    location: LocationData | None = None,
    image_urls: tuple[str, ...] = (),
) -> None:
    if post_type is PostType.FILE and file_meta is None:
        raise ValueError("file post requires file_meta")
    if post_type is PostType.IMAGE:
        if not image_urls:
            raise ValueError("image post requires at least one image_url")
        if len(image_urls) > FEED_POST_MAX_IMAGES:
            raise ValueError(
                f"image post may carry at most {FEED_POST_MAX_IMAGES} images",
            )
    if post_type is PostType.LOCATION:
        if location is None:
            raise ValueError("location post requires lat/lon")
        if location.label is not None and len(location.label) > LOCATION_LABEL_MAX:
            raise ValueError(
                f"location label exceeds {LOCATION_LABEL_MAX} characters",
            )
    if post_type in (PostType.TEXT, PostType.TRANSCRIPT):
        if not content or not content.strip():
            raise ValueError(f"{post_type.value} post requires content")
    if post_type is not PostType.IMAGE and image_urls:
        raise ValueError(
            f"{post_type.value} post must not carry image_urls",
        )
    _validate_text_length(content, limit=MAX_POST_LENGTH)


def _validate_text_length(
    content: str | None,
    *,
    limit: int,
) -> None:
    if content is None:
        return
    if len(content) > limit:
        raise ValueError(f"content exceeds maximum length of {limit} characters")


def _round4(value: float | None) -> float | None:
    """Truncate a GPS coordinate to 4dp (§25 rule)."""
    if value is None:
        return None
    return round(float(value), 4)


def _file_meta_to_payload(fm: FileMeta | None) -> dict | None:
    if fm is None:
        return None
    return {
        "url": fm.url,
        "mime_type": fm.mime_type,
        "original_name": fm.original_name,
        "size_bytes": fm.size_bytes,
    }


def _post_from_queue_payload(item: SpaceModerationItem) -> Post:
    """Rebuild a :class:`Post` from a moderation-queue payload.

    Kept in sync with the shape we serialise in :meth:`SpaceService.create_post`
    when ``decision == "queue"``. Any change to that shape must be mirrored
    here or approved items lose fields in round-trip.
    """
    payload = item.payload
    raw_fm = payload.get("file_meta")
    file_meta: FileMeta | None = None
    if raw_fm:
        try:
            file_meta = FileMeta(
                url=str(raw_fm.get("url", "")),
                mime_type=str(raw_fm.get("mime_type", "")),
                original_name=str(raw_fm.get("original_name", "")),
                size_bytes=int(raw_fm.get("size_bytes", 0)),
            )
        except TypeError, ValueError:
            file_meta = None
    return Post(
        id=str(payload.get("post_id") or uuid.uuid4().hex),
        author=item.submitted_by,
        type=_coerce_post_type(str(payload.get("type") or "text")),
        created_at=item.submitted_at,
        content=payload.get("content"),
        media_url=payload.get("media_url"),
        file_meta=file_meta,
    )


def _normalise_exempt_types(
    value: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(str(t).strip() for t in value if str(t).strip())
