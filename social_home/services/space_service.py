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

import unicodedata
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from ..crypto import generate_identity_keypair
from ..domain.events import (
    CommentAdded,
    CommentDeleted,
    CommentUpdated,
    RemoteJoinRequestApproved,
    SpaceConfigChanged,
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
from ..domain.federation import FederationEventType, PairingStatus
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import compute_picture_hash
from ..domain.post import Comment, CommentType, FileMeta, Post, PostType
from ..domain.space import (
    JoinMode,
    ModerationAlreadyDecidedError,
    ModerationStatus,
    PublicSpaceLimitError,
    Space,
    SpaceConfigEventType,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpacePermissionError,
    SpaceType,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.base import row_to_dict
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo
from ..domain.media_constraints import SPACE_COVER_MAX_DIMENSION
from ..services.user_service import PROFILE_PICTURE_MAX_DIMENSION


#: Sentinel for ``update_member_profile`` partial-patch kwargs.
_UNSET_MEMBER_PROFILE = object()


#: Upper bound on simultaneously-advertised public spaces per instance
#: (spec §13). Enforced at ``create_space`` time for PUBLIC spaces.
MAX_PUBLIC_SPACES = 5

#: Post content caps — matches FeedService values.
MAX_POST_LENGTH = 10_000
MAX_COMMENT_LENGTH = 2_000


class SpaceService:
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
        "_gfs",
        "_federation_repo",
        "_federation",
        "_remote_members",
    )

    def __init__(
        self,
        space_repo: AbstractSpaceRepo,
        space_post_repo: AbstractSpacePostRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_id: str,
    ) -> None:
        self._spaces = space_repo
        self._posts = space_post_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        self._child_protection = None
        self._pictures = None
        self._covers = None
        self._gfs = None
        self._federation_repo = None
        self._federation = None
        self._remote_members = None

    def attach_child_protection(self, child_protection_service) -> None:
        """Wire §CP.F1 enforcement into add_member."""
        self._child_protection = child_protection_service

    def attach_profile_picture_repo(self, repo) -> None:
        """Wire the blob store so per-space picture uploads can land."""
        self._pictures = repo

    def attach_cover_repo(self, repo) -> None:
        """Wire the space-cover blob store (§23 customization)."""
        self._covers = repo

    def attach_gfs_connection_service(self, gfs_service) -> None:
        """Wire outbound GFS publish so ``space_type=global`` spaces
        auto-advertise without a separate admin action. Optional: when
        no GFS is paired, GfsConnectionService may be absent entirely.
        """
        self._gfs = gfs_service

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
    ) -> Space:
        """Create a new space and seat the creator as owner."""
        owner = await self._users.get(owner_username)
        if owner is None:
            raise KeyError(f"owner {owner_username!r} not found")
        if not name.strip():
            raise ValueError("space name must not be empty")

        stype = _coerce_space_type(space_type)
        jmode = _coerce_join_mode(join_mode)

        if stype is SpaceType.PUBLIC:
            count = len(await self._spaces.list_by_type(SpaceType.PUBLIC))
            if count >= MAX_PUBLIC_SPACES:
                raise PublicSpaceLimitError(
                    f"instance already advertises {count} public spaces "
                    f"(max {MAX_PUBLIC_SPACES})"
                )
            if lat is None or lon is None:
                raise ValueError("public space requires lat + lon")
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
        )
        await self._spaces.save(space)
        # Seat the creator as owner.
        await self._spaces.save_member(
            SpaceMember(
                space_id=space.id,
                user_id=owner.user_id,
                role="owner",
                joined_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        await self._spaces.add_space_instance(space.id, self._own_instance_id)
        await self._auto_publish_on_type(
            space.id,
            was_global=False,
            is_global=stype is SpaceType.GLOBAL,
        )
        return space

    async def dissolve_space(
        self,
        space_id: str,
        *,
        actor_username: str,
    ) -> None:
        """Mark a space dissolved (owner only)."""
        space = await self._require_space(space_id)
        await self._require_owner(space, actor_username)
        await self._spaces.mark_dissolved(space_id)
        await self._auto_publish_on_type(
            space_id,
            was_global=space.space_type is SpaceType.GLOBAL,
            is_global=False,
        )
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.DISSOLVED.value,
                payload={},
                sequence=sequence,
            )
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
    ) -> Space:
        """Owner or admin may update space metadata. Atomically bumps
        ``config_sequence`` and publishes :class:`SpaceConfigChanged`.

        Flipping ``space_type`` to/from ``global`` also triggers
        auto-publish/unpublish against every paired GFS
        (via :meth:`_auto_publish_on_type`).
        """
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)

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
        if features is not None:
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

        if not new_fields:
            return space

        updated = replace(space, **new_fields)
        await self._spaces.save(updated)
        sequence = await self._spaces.increment_config_sequence(space_id)
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
        await self._auto_publish_on_type(
            space_id,
            was_global=was_global,
            is_global=will_be_global,
        )
        return updated

    # ── Membership ─────────────────────────────────────────────────────

    async def add_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
        role: str = "member",
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
        return member

    async def remove_member(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        """Remove a member. Admin/owner can remove anyone; a member can
        remove themselves.
        """
        space = await self._require_space(space_id)
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        is_self = actor.user_id == user_id
        if not is_self:
            await self._require_admin_or_owner(space, actor_username)
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            return
        if target.role == "owner":
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
        if role == "owner":
            raise ValueError("use transfer_ownership to assign owner role")
        target = await self._spaces.get_member(space_id, user_id)
        if target is None:
            raise KeyError(f"user {user_id!r} is not a member")
        if target.role == "owner":
            raise SpacePermissionError("cannot demote the owner")
        await self._spaces.set_role(space_id, user_id, role)
        sequence = await self._spaces.increment_config_sequence(space_id)
        evt = (
            SpaceConfigEventType.ADMIN_GRANTED
            if role == "admin"
            else SpaceConfigEventType.ADMIN_REVOKED
        )
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=evt.value,
                payload={"user_id": user_id, "role": role},
                sequence=sequence,
            )
        )

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
        if actor is None or actor.role not in ("owner", "admin"):
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
        await self._spaces.set_role(space_id, outgoing.user_id, "admin")
        await self._spaces.set_role(space_id, to_user_id, "owner")
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
        target = await self._spaces.get_member(space_id, user_id)
        if target is not None and target.role == "owner":
            raise SpacePermissionError("cannot ban the owner")
        actor = await self._users.get(actor_username)
        assert actor is not None
        await self._spaces.ban_member(
            space_id,
            user_id,
            banned_by=actor.user_id,
            reason=reason,
        )
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_BANNED.value,
                payload={"user_id": user_id, "reason": reason},
                sequence=sequence,
            )
        )

    async def unban(
        self,
        space_id: str,
        *,
        actor_username: str,
        user_id: str,
    ) -> None:
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        await self._spaces.unban_member(space_id, user_id)
        sequence = await self._spaces.increment_config_sequence(space_id)
        await self._bus.publish(
            SpaceConfigChanged(
                space_id=space_id,
                event_type=SpaceConfigEventType.MEMBER_UNBANNED.value,
                payload={"user_id": user_id},
                sequence=sequence,
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
        audit log.
        """
        if self._federation is None or self._federation_repo is None:
            raise RuntimeError(
                "space_service: federation not attached; "
                "remote invites require a live FederationService",
            )
        space = await self._require_space(space_id)
        await self._require_admin_or_owner(space, actor_username)
        actor = await self._users.get(actor_username)
        assert actor is not None

        peer = await self._federation_repo.get_instance(invitee_instance_id)
        if peer is None or peer.status is not PairingStatus.CONFIRMED:
            raise SpacePermissionError(
                "invitee household is not a CONFIRMED peer",
            )

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
        await self._federation.send_event(
            to_instance_id=invitee_instance_id,
            event_type=FederationEventType.SPACE_PRIVATE_INVITE,
            payload={
                "space_id": space_id,
                "invite_token": token,
                "inviter_user_id": actor.user_id,
                "inviter_display_name": (actor.display_name or actor.username),
                "space_display_hint": space.name,
                "expires_at": expires,
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
        display = None
        user_pk = None
        users_repo = self._users
        if hasattr(users_repo, "get_by_id"):
            user = await users_repo.get_by_id(user_id)
            if user is not None:
                display = user.display_name or user.username
                user_pk = getattr(user, "public_key", None)
        await self._federation.send_event(
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
        await self._federation.send_event(
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
        await self._remote_members.remove(space_id, instance_id, user_id)
        await self._federation.send_event(
            to_instance_id=instance_id,
            event_type=FederationEventType.SPACE_REMOTE_MEMBER_REMOVED,
            payload={"space_id": space_id, "user_id": user_id},
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
        member = SpaceMember(
            space_id=space_id,
            user_id=user_id,
            role="member",
            joined_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._spaces.save_member(member)
        await self._bus.publish(
            SpaceMemberJoined(
                space_id=space_id,
                user_id=user_id,
                role="member",
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
            role="member",
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
                role="member",
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
        request_id = uuid.uuid4().hex
        # Persist locally so the inbound APPROVED handler can look up
        # the applicant_user_id; there's no host-side space row locally.
        await self._spaces._db.enqueue(
            """
            INSERT INTO space_join_requests(
                id, space_id, user_id, message, expires_at,
                remote_applicant_instance_id
            ) VALUES(
                ?, ?, ?, ?, datetime('now', '+7 days'), ?
            )
            """,
            (
                request_id,
                space_id,
                applicant_user_id,
                message,
                host_instance_id,
            ),
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
            "SELECT user_id FROM space_join_requests WHERE id=?",
            (request_id,),
        )
        r = row_to_dict(row)
        if r is None:
            return
        user_id = r.get("user_id")
        if not user_id:
            return
        try:
            await self.accept_invite_token(invite_token, user_id=user_id)
        except KeyError, SpacePermissionError:
            # Token already consumed or user now banned.
            pass

    # ── Space posts ────────────────────────────────────────────────────

    async def create_post(
        self,
        space_id: str,
        *,
        author_user_id: str,
        type: PostType | str,
        content: str | None = None,
        media_url: str | None = None,
        file_meta: FileMeta | None = None,
    ) -> Post | None:
        """Create a post in the space, subject to the feature's access level.

        Returns the persisted :class:`Post` for `open` / admin paths. For
        `moderated` access where the author isn't an admin, the content
        enters the moderation queue and this method returns ``None`` after
        publishing :class:`SpaceModerationQueued`.
        """
        space = await self._require_space(space_id)
        author = await self._users.get_by_user_id(author_user_id)
        if author is None:
            raise KeyError(f"user {author_user_id!r} not found")
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        if not space.features.allows(type):
            raise SpacePermissionError(f"space does not allow {type!r} posts")

        post_type = _coerce_post_type(type)
        _validate_space_content(post_type, content, file_meta)
        is_admin = member.role in ("owner", "admin")
        decision = space.features.access_decision("posts", is_admin=is_admin)
        if decision == "deny":
            raise SpacePermissionError("posting is admin-only in this space")

        post = Post(
            id=uuid.uuid4().hex,
            author=author.user_id,
            type=post_type,
            created_at=datetime.now(timezone.utc),
            content=content,
            media_url=media_url,
            file_meta=file_meta,
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
        # Verifies space exists — raises KeyError if not.
        await self._require_space(space_id)
        if post.author != editor_user_id:
            # Admin override
            editor = await self._users.get_by_user_id(editor_user_id)
            if editor is None:
                raise PermissionError("not authorised")
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in ("owner", "admin"):
                raise PermissionError("only the author or a space admin can edit")
        _validate_text_length(new_content, limit=MAX_POST_LENGTH)
        await self._posts.edit(post_id, new_content)
        refreshed = await self._posts.get(post_id)
        assert refreshed is not None  # just edited — must exist
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
        moderated_by: str | None = None
        if post.author != actor_user_id:
            # Moderation path — actor must be admin/owner
            member = await self._spaces.get_member(space_id, actor_user_id)
            if member is None or member.role not in ("owner", "admin"):
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
        return await self._posts.add_reaction(post_id, emoji, user_id)

    async def remove_reaction(
        self,
        post_id: str,
        *,
        user_id: str,
        emoji: str,
    ) -> Post:
        emoji = unicodedata.normalize("NFC", emoji.strip())
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
        # Membership check
        member = await self._spaces.get_member(space_id, author_user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
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
        if comment.author != editor_user_id:
            member = await self._spaces.get_member(space_id, editor_user_id)
            if member is None or member.role not in ("owner", "admin"):
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
            if member is None or member.role not in ("owner", "admin"):
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

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require_space(self, space_id: str) -> Space:
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        return space

    async def _require_member(
        self,
        space_id: str,
        user_id: str,
    ) -> SpaceMember:
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        return member

    async def _require_admin_or_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space.id, actor.user_id)
        if member is None or member.role not in ("owner", "admin"):
            raise SpacePermissionError("admin or owner required")
        return member

    async def _require_owner(
        self,
        space: Space,
        actor_username: str,
    ) -> SpaceMember:
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"actor {actor_username!r} not found")
        member = await self._spaces.get_member(space.id, actor.user_id)
        if member is None or member.role != "owner":
            raise SpacePermissionError("owner required")
        return member


# ─── Helpers ──────────────────────────────────────────────────────────────


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


def _validate_space_content(
    post_type: PostType,
    content: str | None,
    file_meta: FileMeta | None,
) -> None:
    if post_type is PostType.FILE and file_meta is None:
        raise ValueError("file post requires file_meta")
    if post_type in (PostType.TEXT, PostType.TRANSCRIPT):
        if not content or not content.strip():
            raise ValueError(f"{post_type.value} post requires content")
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
