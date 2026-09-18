"""Space repository — persistence for spaces, members, bans, invitations,
join requests, instance links and config-sequence bookkeeping.

The :class:`AbstractSpaceRepo` protocol is the service-facing surface. The
concrete :class:`SqliteSpaceRepo` implements it against the v1 schema.

What lives here:

* Space CRUD (``save`` / ``get`` / ``list_by_type`` / ``mark_dissolved``).
* Member CRUD (``save_member`` / ``delete_member`` / ``get_member`` /
  ``list_members`` / ``set_role``).
* Cross-instance bookkeeping (``add_space_instance`` / ``list_member_instances``).
* Bans (``ban_member`` / ``unban_member`` / ``list_bans``).
* Invites (``create_invite_token`` / ``consume_invite_token``;
  ``save_invitation`` / ``get_invitation`` / ``update_invitation_status``).
* Join requests (``save_join_request`` / ``list_pending_join_requests`` /
  ``update_join_request_status``).
* Sidebar pins and personal space aliases.
* Atomic ``increment_config_sequence``.

Posts, tasks, pages and calendar events are handled by dedicated repos
(:mod:`post_repo`, :mod:`task_repo`, …) — keeping those out of here prevents
this module from becoming another 1000-line dumping ground.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..infrastructure.hlc import HLC
from ..domain.space import (
    JoinMode,
    ModerationStatus,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpaceRole,
    SpaceType,
)
from .base import bool_col, dump_json, load_json, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractSpaceRepo(Protocol):
    # ── Spaces ─────────────────────────────────────────────────────────
    async def save(self, space: Space) -> Space: ...
    async def get(self, space_id: str) -> Space | None: ...
    async def set_space_seed(self, space_id: str, seed: bytes) -> None: ...
    async def set_space_pubkey(self, space_id: str, public_key_hex: str) -> None: ...
    async def get_space_seed(self, space_id: str) -> bytes | None: ...
    async def set_host_identity_pk(self, space_id: str, pk_hex: str) -> None: ...
    async def get_host_identity_pk(self, space_id: str) -> str | None: ...
    async def set_cover_hash(
        self,
        space_id: str,
        cover_hash: str | None,
    ) -> None: ...
    async def set_icon_hash(
        self,
        space_id: str,
        icon_hash: str | None,
    ) -> None: ...
    async def set_tz(self, space_id: str, tz: str) -> None: ...
    async def list_by_type(self, space_type: SpaceType) -> list[Space]: ...
    async def list_for_user(self, user_id: str) -> list[Space]: ...
    async def list_location_shared_spaces_for_user(
        self, user_id: str
    ) -> list[Space]: ...
    async def list_user_memberships_with_location_feature(
        self, user_id: str
    ) -> list[dict]: ...
    async def list_subscriptions_for_user(self, user_id: str) -> list[dict]: ...
    async def list_all(self) -> list[Space]: ...
    async def mark_dissolved(self, space_id: str) -> None: ...
    async def set_archived(
        self, space_id: str, archived: bool, reason: str | None = None
    ) -> None: ...
    async def purge(self, space_id: str) -> None: ...
    async def increment_config_sequence(self, space_id: str) -> int: ...
    async def increment_roster_sequence(self, space_id: str) -> int: ...
    async def get_config_author(self, space_id: str) -> str | None: ...
    async def set_config_author(self, space_id: str, instance_id: str) -> None: ...
    async def update_age_gate(
        self,
        space_id: str,
        *,
        min_age: int | None = None,
    ) -> None: ...

    # ── Members ────────────────────────────────────────────────────────
    async def save_member(self, member: SpaceMember) -> SpaceMember: ...
    async def get_member(self, space_id: str, user_id: str) -> SpaceMember | None: ...
    async def list_members(self, space_id: str) -> list[SpaceMember]: ...
    async def delete_member(self, space_id: str, user_id: str) -> None: ...
    async def set_role(self, space_id: str, user_id: str, role: str) -> None: ...
    async def set_member_location_sharing(
        self,
        space_id: str,
        user_id: str,
        enabled: bool,
    ) -> bool: ...
    async def set_member_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        space_display_name: str | None = None,
        picture_hash: str | None | object = None,
    ) -> None: ...
    async def list_local_member_user_ids(self, space_id: str) -> list[str]: ...
    async def list_subscribed_space_ids(self) -> list[str]: ...

    # ── Instances that mirror this space ───────────────────────────────
    async def add_space_instance(self, space_id: str, instance_id: str) -> None: ...
    async def remove_space_instance(self, space_id: str, instance_id: str) -> None: ...
    async def instance_in_any_space(self, instance_id: str) -> bool: ...
    async def list_member_instances(self, space_id: str) -> list[str]: ...

    # ── Bans ───────────────────────────────────────────────────────────
    async def ban_member(
        self,
        space_id: str,
        user_id: str,
        banned_by: str,
        *,
        identity_pk: str | None = None,
        reason: str | None = None,
    ) -> None: ...
    async def unban_member(self, space_id: str, user_id: str) -> None: ...
    async def is_banned(self, space_id: str, user_id: str) -> bool: ...
    async def list_bans(self, space_id: str) -> list[dict]: ...

    # ── Moderation queue ──────────────────────────────────────────────
    async def save_moderation_item(self, item: SpaceModerationItem) -> None: ...
    async def list_moderation_queue(
        self,
        space_id: str,
        *,
        status: ModerationStatus | None = None,
        limit: int = 100,
    ) -> list[SpaceModerationItem]: ...
    async def get_moderation_item(
        self,
        item_id: str,
    ) -> SpaceModerationItem | None: ...
    async def update_moderation_item_status(
        self,
        item_id: str,
        *,
        status: ModerationStatus,
        reviewed_by: str,
        rejection_reason: str | None = None,
    ) -> None: ...

    # ── Invite tokens ──────────────────────────────────────────────────
    async def create_invite_token(
        self,
        space_id: str,
        created_by: str,
        *,
        uses: int = 1,
        expires_at: str | None = None,
        token: str | None = None,
        role: str = SpaceRole.MEMBER.value,
        gfs_id: str | None = None,
        gfs_token: str | None = None,
        gfs_url: str | None = None,
    ) -> str: ...
    async def consume_invite_token(
        self,
        token: str,
        *,
        redeemer_user_id: str | None = None,
    ) -> dict | None: ...
    async def list_live_invite_tokens(self, space_id: str) -> list[dict]: ...
    async def delete_invite_token(self, space_id: str, token: str) -> dict | None: ...

    # ── Invitations ────────────────────────────────────────────────────
    async def save_invitation(
        self,
        space_id: str,
        invited_user_id: str,
        invited_by: str,
        *,
        ttl_days: int = 7,
    ) -> str: ...
    async def save_remote_invitation(
        self,
        space_id: str,
        *,
        invited_by: str,
        remote_instance_id: str,
        remote_user_id: str,
        invite_token: str,
        space_display_hint: str | None = None,
        ttl_minutes: int = 15,
    ) -> str: ...
    async def get_invitation(self, invitation_id: str) -> dict | None: ...
    async def get_invitation_by_token(self, token: str) -> dict | None: ...
    async def list_pending_remote_invites_for(
        self,
        user_id: str,
    ) -> list[dict]: ...
    async def list_pending_local_invites_for(
        self,
        user_id: str,
    ) -> list[dict]: ...
    async def update_invitation_status(
        self,
        invitation_id: str,
        status: str,
    ) -> None: ...
    async def is_user_remote_member(
        self,
        space_id: str,
        user_id: str,
    ) -> bool:
        """Whether ``user_id`` accepted a remote (cross-household) invite
        for ``space_id`` — i.e. is a member of a peer-owned space via
        the §D1b invitation flow. Distinct from
        :meth:`get_member`, which only sees rows in the local
        ``space_members`` table.
        """
        ...

    # ── Join requests ──────────────────────────────────────────────────
    async def save_join_request(
        self,
        space_id: str,
        user_id: str,
        *,
        message: str | None = None,
        ttl_days: int = 7,
        remote_applicant_instance_id: str | None = None,
        remote_applicant_pk: str | None = None,
        request_id: str | None = None,
    ) -> str: ...
    async def list_pending_join_requests(self, space_id: str) -> list[dict]: ...
    async def list_pending_join_request_space_ids_for_user(
        self,
        user_id: str,
    ) -> list[str]: ...
    async def update_join_request_status(
        self,
        request_id: str,
        status: str,
        *,
        reviewed_by: str | None = None,
    ) -> None: ...
    async def list_expired_join_requests(self) -> list[dict]: ...

    # ── Sidebar + aliases ──────────────────────────────────────────────
    async def pin_sidebar(
        self,
        user_id: str,
        space_id: str,
        position: int,
    ) -> None: ...
    async def unpin_sidebar(self, user_id: str, space_id: str) -> None: ...
    async def set_space_alias(
        self,
        space_id: str,
        local_username: str,
        alias: str,
    ) -> None: ...
    async def get_space_alias(
        self,
        space_id: str,
        local_username: str,
    ) -> str | None: ...

    # ── Sidebar links (admin-configurable quick-links) ─────────────────
    async def list_links(self, space_id: str) -> list[dict]: ...
    async def upsert_link(
        self,
        *,
        link_id: str,
        space_id: str,
        label: str,
        url: str,
        position: int,
    ) -> None: ...
    async def delete_link(self, link_id: str) -> None: ...
    async def get_link(self, link_id: str) -> dict | None: ...


# ─── Concrete SQLite implementation ───────────────────────────────────────


class SqliteSpaceRepo:
    """SQLite-backed :class:`AbstractSpaceRepo`.

    ``key_manager`` is the household KEK used to wrap each space's Ed25519
    identity *private* seed at rest (``spaces.identity_private_key``). It is
    unavailable when the repos are built (the KEK is loaded later, in
    ``_on_startup``), so it can also be wired post-construction via
    :meth:`attach_key_manager`. The seed accessors (:meth:`set_space_seed` /
    :meth:`get_space_seed`) require it; every other method works without it.
    """

    def __init__(self, db: AsyncDatabase, *, key_manager=None) -> None:
        self._db = db
        self._kek = key_manager

    def attach_key_manager(self, key_manager) -> None:
        """Wire the household KEK after construction (see class docstring)."""
        self._kek = key_manager

    # ── Spaces ─────────────────────────────────────────────────────────

    async def save(self, space: Space) -> Space:
        cols = space.features.to_columns()
        await self._db.enqueue(
            """
            INSERT INTO spaces(
                id, name, description, emoji,
                owner_instance_id, owner_username, identity_public_key,
                config_sequence, roster_sequence, config_hlc,
                space_type, join_mode, join_code,
                retention_days, retention_exempt_json,
                feature_calendar, feature_todo, feature_location, location_mode,
                feature_stickies, feature_pages, feature_gallery, feature_bazaar,
                posts_access, pages_access, stickies_access,
                calendar_access, tasks_access,
                allow_subscribers,
                allow_subscriber_comment, allow_subscriber_react,
                delegated_admin_authority,
                allow_post_text, allow_post_image, allow_post_video,
                allow_post_transcript, allow_post_poll, allow_post_schedule,
                allow_post_file, allow_post_bazaar,
                allow_post_event, allow_post_location, allow_post_highlight_share,
                lat, lon, radius_km, bot_enabled, allow_here_mention,
                dissolved, archived, archived_reason, about_markdown, cover_hash, tz,
                min_age, category
            ) VALUES(
                -- 56 placeholders, one per column listed above.
                ?, ?, ?, ?,                   -- id, name, description, emoji
                ?, ?, ?,                      -- owner_instance_id, owner_username, identity_public_key
                ?, ?, ?,                      -- config_sequence, roster_sequence, config_hlc
                ?, ?, ?,                      -- space_type, join_mode, join_code
                ?, ?,                         -- retention_days, retention_exempt_json
                ?, ?, ?, ?,                   -- feature_calendar, feature_todo, feature_location, location_mode
                ?, ?, ?, ?,                   -- feature_stickies, feature_pages, feature_gallery, feature_bazaar
                ?, ?, ?,                      -- posts_access, pages_access, stickies_access
                ?, ?,                         -- calendar_access, tasks_access
                ?,                            -- allow_subscribers
                ?, ?,                         -- allow_subscriber_comment, allow_subscriber_react
                ?,                            -- delegated_admin_authority
                ?, ?, ?,                      -- allow_post_text, allow_post_image, allow_post_video
                ?, ?, ?,                      -- allow_post_transcript, allow_post_poll, allow_post_schedule
                ?, ?,                         -- allow_post_file, allow_post_bazaar
                ?, ?, ?,                      -- allow_post_event, allow_post_location, allow_post_highlight_share
                ?, ?, ?, ?, ?,                -- lat, lon, radius_km, bot_enabled, allow_here_mention
                ?, ?, ?, ?, ?, ?,             -- dissolved, archived, archived_reason, about_markdown, cover_hash, tz
                ?, ?                          -- min_age, category
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                emoji=excluded.emoji,
                config_sequence=excluded.config_sequence,
                roster_sequence=excluded.roster_sequence,
                config_hlc=excluded.config_hlc,
                space_type=excluded.space_type,
                join_mode=excluded.join_mode,
                join_code=excluded.join_code,
                retention_days=excluded.retention_days,
                retention_exempt_json=excluded.retention_exempt_json,
                feature_calendar=excluded.feature_calendar,
                feature_todo=excluded.feature_todo,
                feature_location=excluded.feature_location,
                location_mode=excluded.location_mode,
                feature_stickies=excluded.feature_stickies,
                feature_pages=excluded.feature_pages,
                feature_gallery=excluded.feature_gallery,
                feature_bazaar=excluded.feature_bazaar,
                posts_access=excluded.posts_access,
                pages_access=excluded.pages_access,
                stickies_access=excluded.stickies_access,
                calendar_access=excluded.calendar_access,
                tasks_access=excluded.tasks_access,
                allow_subscribers=excluded.allow_subscribers,
                allow_subscriber_comment=excluded.allow_subscriber_comment,
                allow_subscriber_react=excluded.allow_subscriber_react,
                delegated_admin_authority=excluded.delegated_admin_authority,
                allow_post_text=excluded.allow_post_text,
                allow_post_image=excluded.allow_post_image,
                allow_post_video=excluded.allow_post_video,
                allow_post_transcript=excluded.allow_post_transcript,
                allow_post_poll=excluded.allow_post_poll,
                allow_post_schedule=excluded.allow_post_schedule,
                allow_post_file=excluded.allow_post_file,
                allow_post_bazaar=excluded.allow_post_bazaar,
                allow_post_event=excluded.allow_post_event,
                allow_post_location=excluded.allow_post_location,
                allow_post_highlight_share=excluded.allow_post_highlight_share,
                lat=excluded.lat,
                lon=excluded.lon,
                radius_km=excluded.radius_km,
                bot_enabled=excluded.bot_enabled,
                allow_here_mention=excluded.allow_here_mention,
                dissolved=excluded.dissolved,
                archived=excluded.archived,
                archived_reason=excluded.archived_reason,
                about_markdown=excluded.about_markdown,
                cover_hash=excluded.cover_hash,
                tz=excluded.tz,
                min_age=excluded.min_age,
                category=excluded.category
            """,
            (
                space.id,
                space.name,
                space.description,
                space.emoji,
                space.owner_instance_id,
                space.owner_username,
                space.identity_public_key,
                space.config_sequence,
                space.roster_sequence,
                space.config_hlc,
                space.space_type.value,
                space.join_mode.value,
                space.join_code,
                space.retention_days,
                dump_json(list(space.retention_exempt_types)),
                cols["feature_calendar"],
                cols["feature_todo"],
                cols["feature_location"],
                cols["location_mode"],
                cols["feature_stickies"],
                cols["feature_pages"],
                cols["feature_gallery"],
                cols["feature_bazaar"],
                cols["posts_access"],
                cols["pages_access"],
                cols["stickies_access"],
                cols["calendar_access"],
                cols["tasks_access"],
                cols["allow_subscribers"],
                cols["allow_subscriber_comment"],
                cols["allow_subscriber_react"],
                cols["delegated_admin_authority"],
                cols["allow_post_text"],
                cols["allow_post_image"],
                cols["allow_post_video"],
                cols["allow_post_transcript"],
                cols["allow_post_poll"],
                cols["allow_post_schedule"],
                cols["allow_post_file"],
                cols["allow_post_bazaar"],
                cols["allow_post_event"],
                cols["allow_post_location"],
                cols["allow_post_highlight_share"],
                space.lat,
                space.lon,
                space.radius_km,
                int(space.bot_enabled),
                int(space.allow_here_mention),
                int(space.dissolved),
                int(space.archived),
                space.archived_reason,
                space.about_markdown,
                space.cover_hash,
                space.tz,
                int(space.min_age or 0),
                space.category,
            ),
        )
        return space

    async def set_cover_hash(
        self,
        space_id: str,
        cover_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE spaces SET cover_hash=? WHERE id=?",
            (cover_hash, space_id),
        )

    async def set_icon_hash(
        self,
        space_id: str,
        icon_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE spaces SET icon_hash=? WHERE id=?",
            (icon_hash, space_id),
        )

    async def set_tz(self, space_id: str, tz: str) -> None:
        """Set the space's IANA timezone anchor.

        Space admins call this to anchor a space to a wall clock that
        differs from the household tz — e.g. a federated multi-household
        space whose canonical wall clock is "Europe/Berlin" even though
        a co-host's local household runs in "America/New_York".
        """
        await self._db.enqueue(
            "UPDATE spaces SET tz=? WHERE id=?",
            (tz, space_id),
        )

    async def get(self, space_id: str) -> Space | None:
        row = await self._db.fetchone(
            "SELECT * FROM spaces WHERE id=?",
            (space_id,),
        )
        return _row_to_space(row_to_dict(row))

    async def set_space_seed(self, space_id: str, seed: bytes) -> None:
        """KEK-wrap the 32-byte Ed25519 ``seed`` and store it in
        ``spaces.identity_private_key``.

        The seed is the space-authority signing key — it MUST NOT federate.
        Stored ciphertext is bound to ``space_id`` as associated data so a
        wrapped seed can't be swapped between rows. Requires the KEK to have
        been wired (constructor or :meth:`attach_key_manager`).
        """
        if self._kek is None:
            raise RuntimeError("space seed persistence requires a key_manager")
        if len(seed) != 32:
            raise ValueError("Ed25519 seed must be 32 bytes")
        wrapped = self._kek.encrypt(seed, associated_data=space_id.encode("utf-8"))
        await self._db.enqueue(
            "UPDATE spaces SET identity_private_key=? WHERE id=?",
            (wrapped, space_id),
        )

    async def set_space_pubkey(self, space_id: str, public_key_hex: str) -> None:
        """Replace ``spaces.identity_public_key`` with ``public_key_hex``.

        A targeted update used only by the owned-space seed mint
        (:meth:`SpaceService.ensure_space_seed`) — the published public key
        is otherwise immutable on a normal ``save`` upsert, so a remote stub
        re-save can never clobber it.
        """
        await self._db.enqueue(
            "UPDATE spaces SET identity_public_key=? WHERE id=?",
            (public_key_hex, space_id),
        )

    async def get_space_seed(self, space_id: str) -> bytes | None:
        """Return the raw 32-byte Ed25519 seed for ``space_id``, or ``None``
        when the column is NULL (pre-upgrade owned space, or a non-owned
        space whose private key we never held).

        Decrypts the KEK-wrapped column with the same associated data
        (``space_id``) used to wrap it.
        """
        if self._kek is None:
            raise RuntimeError("space seed access requires a key_manager")
        row = await self._db.fetchone(
            "SELECT identity_private_key FROM spaces WHERE id=?",
            (space_id,),
        )
        if row is None:
            return None
        wrapped = row["identity_private_key"]
        if wrapped is None:
            return None
        return self._kek.decrypt(wrapped, associated_data=space_id.encode("utf-8"))

    async def set_host_identity_pk(self, space_id: str, pk_hex: str) -> None:
        """Record the hosting household's Ed25519 identity pubkey (hex).

        Only meaningful on a *stub* of a remote space whose host we are not
        paired with: the §25.6 receiver needs it to verify the host's
        per-chunk signatures, and a mesh-joined member has no
        ``remote_instances`` row to read one from (#648).

        Callers MUST have verified that ``derive_instance_id(pk)`` matches
        the space's authenticated host before storing — this method does
        not re-check. Public key only; nothing secret goes in this column,
        so unlike :meth:`set_space_seed` it is not KEK-wrapped.
        """
        await self._db.enqueue(
            "UPDATE spaces SET host_identity_pk=? WHERE id=?",
            (pk_hex, space_id),
        )

    async def get_host_identity_pk(self, space_id: str) -> str | None:
        """Return the host household's identity pubkey (hex), or ``None``.

        ``None`` for an owned space, for a stub whose host is a confirmed
        peer (the ``remote_instances`` row serves those), and for stubs
        seated before migration 0045.
        """
        row = await self._db.fetchone(
            "SELECT host_identity_pk FROM spaces WHERE id=?",
            (space_id,),
        )
        if row is None:
            return None
        return row["host_identity_pk"]

    async def list_by_type(self, space_type: SpaceType) -> list[Space]:
        rows = await self._db.fetchall(
            "SELECT * FROM spaces WHERE space_type=? AND dissolved=0 ORDER BY name",
            (space_type.value,),
        )
        return [s for s in (_row_to_space(d) for d in rows_to_dicts(rows)) if s]

    async def list_for_user(self, user_id: str) -> list[Space]:
        """Return every active space *user_id* is a member of (§23.48).

        Sorted by space name. Dissolved spaces are excluded.
        """
        rows = await self._db.fetchall(
            """
            SELECT s.* FROM spaces s
              JOIN space_members m ON m.space_id = s.id
             WHERE m.user_id = ? AND s.dissolved = 0
             ORDER BY s.name
            """,
            (user_id,),
        )
        return [s for s in (_row_to_space(d) for d in rows_to_dicts(rows)) if s]

    async def list_location_shared_spaces_for_user(self, user_id: str) -> list[Space]:
        """Return every space where this user has opted in to location
        sharing (``location_share_enabled = 1``) AND the space has the
        feature on (``feature_location = 1``).

        Used by :class:`SpaceLocationOutbound` to fan a household
        ``PresenceUpdated`` out to the spaces that should receive a
        space-bound payload (§23.8.6). Both gates must be ON — the
        space-level admin toggle and the per-member opt-in.
        """
        rows = await self._db.fetchall(
            """
            SELECT s.* FROM spaces s
              JOIN space_members m ON m.space_id = s.id
             WHERE m.user_id = ?
               AND m.location_share_enabled = 1
               AND s.feature_location = 1
               AND s.dissolved = 0
             ORDER BY s.id
            """,
            (user_id,),
        )
        return [s for s in (_row_to_space(d) for d in rows_to_dicts(rows)) if s]

    async def list_user_memberships_with_location_feature(
        self, user_id: str
    ) -> list[dict]:
        """Return every space where *user_id* is a member AND
        ``feature_location = 1``, sorted by space name.

        Each row carries the fields needed by
        ``GET /api/me/space-location-sharing``:
        ``space_id``, ``space_name``, ``space_emoji``,
        ``location_share_enabled``.
        """
        rows = await self._db.fetchall(
            """
            SELECT s.id         AS space_id,
                   s.name       AS space_name,
                   s.emoji      AS space_emoji,
                   m.location_share_enabled AS location_share_enabled
              FROM spaces s
              JOIN space_members m ON m.space_id = s.id
             WHERE m.user_id = ?
               AND s.feature_location = 1
               AND s.dissolved = 0
             ORDER BY s.name
            """,
            (user_id,),
        )
        return [
            {
                "space_id": r["space_id"],
                "space_name": r["space_name"],
                "space_emoji": r["space_emoji"],
                "location_share_enabled": bool(r["location_share_enabled"]),
            }
            for r in rows_to_dicts(rows)
        ]

    async def list_subscriptions_for_user(self, user_id: str) -> list[dict]:
        """Return ``[{space_id, subscribed_at}]`` for every space where
        *user_id* is a member with ``role='subscriber'``. Newest-joined
        first. Dissolved spaces excluded.

        Subscriptions are read-only memberships for public / global
        spaces — see :class:`SpaceService.subscribe_to_space` for the
        write path. Distinct from the
        ``preferences_json['followed_space_ids']`` dashboard pin list
        used by :mod:`corner_service`, which is a per-user UI pin
        over spaces the user is *already* a full member of.
        """
        rows = await self._db.fetchall(
            """
            SELECT m.space_id AS space_id, m.joined_at AS subscribed_at
              FROM space_members m
              JOIN spaces s ON s.id = m.space_id
             WHERE m.user_id = ?
               AND m.role = 'subscriber'
               AND s.dissolved = 0
             ORDER BY m.joined_at DESC
            """,
            (user_id,),
        )
        return [
            {"space_id": r["space_id"], "subscribed_at": r["subscribed_at"]}
            for r in rows
        ]

    async def list_subscribed_space_ids(self) -> list[str]:
        """Return every space id this HOUSEHOLD holds a subscription on.

        Household-wide (any local user with ``role='subscriber'``), unlike
        :meth:`list_subscriptions_for_user` which answers per user. The GFS
        seat is registered once per household, not per user, so the reconnect
        self-heal (``GfsSpaceMirrorService.resubscribe_all``) needs the
        household view. Dissolved spaces excluded.
        """
        rows = await self._db.fetchall(
            """
            SELECT DISTINCT m.space_id AS space_id
              FROM space_members m
              JOIN spaces s ON s.id = m.space_id
             WHERE m.role = 'subscriber'
               AND s.dissolved = 0
             ORDER BY m.space_id
            """,
        )
        return [r["space_id"] for r in rows]

    async def list_all(self) -> list[Space]:
        """Return every active space hosted on this instance (admin).

        Used by the household-admin "all spaces" panel so the admin can
        survey + dissolve / transfer any space on the household.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM spaces WHERE dissolved=0 ORDER BY name",
        )
        return [s for s in (_row_to_space(d) for d in rows_to_dicts(rows)) if s]

    async def mark_dissolved(self, space_id: str) -> None:
        await self._db.enqueue(
            "UPDATE spaces SET dissolved=1 WHERE id=?",
            (space_id,),
        )

    async def set_archived(
        self, space_id: str, archived: bool, reason: str | None = None
    ) -> None:
        """Soft archive flag + reason. Unlike :meth:`purge` this keeps all
        rows + media; the space stays readable but read-only.

        ``reason`` stamps *why* archived: ``'dissolved'`` (owner dissolved)
        or ``'removed'`` (this household removed). Omitting it (the default
        ``None``) clears the reason — so a normal admin archive carries NULL,
        and unarchiving (``set_archived(id, False)``) resets it to NULL too.
        """
        await self._db.enqueue(
            "UPDATE spaces SET archived=?, archived_reason=? WHERE id=?",
            (int(archived), reason, space_id),
        )

    async def purge(self, space_id: str) -> None:
        """Hard-delete the space and its entire content graph.

        Every space-scoped child table is declared ``REFERENCES
        spaces(id) ON DELETE CASCADE`` and the connection runs ``PRAGMA
        foreign_keys=ON`` (see ``db/database.py``), so dropping the
        parent row cascades the full graph — posts, comments, members,
        gallery albums/items, calendar, pages, tasks, stickies, content
        keys, the media-outbox rows, location pins — in one statement.
        Callers must collect any on-disk media filenames *before* calling
        this (the rows that point at them are gone afterwards).
        """
        await self._db.enqueue("DELETE FROM spaces WHERE id=?", (space_id,))

    async def update_age_gate(
        self,
        space_id: str,
        *,
        min_age: int | None = None,
    ) -> None:
        """§CP.F1: set the space's ``min_age`` child-protection gate.

        ``None`` means "don't change it" — callers pass the value they
        received in the federation payload.
        """
        if min_age is None:
            return
        await self._db.enqueue(
            "UPDATE spaces SET min_age=? WHERE id=?",
            (min_age, space_id),
        )

    async def increment_config_sequence(self, space_id: str) -> int:
        """Atomically bump ``spaces.config_sequence`` AND advance the config
        HLC, returning the new sequence.

        After the 0036 roster decouple this is the SOLE per-edit bumper of the
        config-LWW state, so it also ticks ``config_hlc`` in the same
        transaction: the HLC is read, ``tick(now_ms)``-ed off the current value
        (monotonic per node) and written back beside the incremented sequence.
        The advanced HLC rides on the row → picked up by the federation
        snapshot (``_space_metadata_for_federation``); callers consume only the
        returned int, so the 8 config-edit call sites are unchanged.

        ``AsyncDatabase.transact`` runs the read + UPDATE inside a single
        ``BEGIN IMMEDIATE`` transaction so concurrent callers always see
        strictly increasing sequence numbers AND a strictly increasing HLC,
        even on SQLite builds that predate the ``RETURNING`` clause.
        """
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        def _run(conn):
            row = conn.execute(
                "SELECT config_sequence, config_hlc FROM spaces WHERE id=?",
                (space_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"space {space_id!r} not found")
            new_hlc = HLC.parse(row[1]).tick(now_ms)
            conn.execute(
                "UPDATE spaces SET config_sequence = config_sequence + 1, "
                "config_hlc = ? WHERE id=?",
                (str(new_hlc), space_id),
            )
            return int(row[0]) + 1

        return await self._db.transact(_run)

    async def increment_roster_sequence(self, space_id: str) -> int:
        """Atomically bump ``spaces.roster_sequence`` and return the new value.

        Mirrors :meth:`increment_config_sequence` but on the dedicated roster
        counter, so roster gossip versions advance independently of config-LWW
        versions. Same ``BEGIN IMMEDIATE`` UPDATE+SELECT so concurrent callers
        always see strictly increasing values.
        """

        def _run(conn):
            cur = conn.execute(
                "UPDATE spaces SET roster_sequence = roster_sequence + 1 WHERE id=?",
                (space_id,),
            )
            if cur.rowcount == 0:
                raise KeyError(f"space {space_id!r} not found")
            row = conn.execute(
                "SELECT roster_sequence FROM spaces WHERE id=?",
                (space_id,),
            ).fetchone()
            return int(row[0])

        return await self._db.transact(_run)

    async def get_config_author(self, space_id: str) -> str | None:
        """Return the instance id that authored the last config edit we APPLIED.

        v_24 last-writer-wins tie-break key for concurrent same-sequence config
        edits from different admins (see migration 0032). ``None`` for a fresh
        space (never an applied edit) or an unknown space — NULL sorts below any
        real instance id, so an owner's first signed edit always wins the tie.
        """
        row = await self._db.fetchone(
            "SELECT config_author_instance FROM spaces WHERE id=?",
            (space_id,),
        )
        if row is None:
            return None
        return row["config_author_instance"]

    async def set_config_author(self, space_id: str, instance_id: str) -> None:
        """Record ``instance_id`` as the author of the last applied config edit.

        Written by the inbound apply path (and the local authoritative edit
        path) so a later equal-sequence edit can deterministically tie-break.
        """
        await self._db.enqueue(
            "UPDATE spaces SET config_author_instance=? WHERE id=?",
            (instance_id, space_id),
        )

    # ── Members ────────────────────────────────────────────────────────

    async def save_member(self, member: SpaceMember) -> SpaceMember:
        await self._db.enqueue(
            """
            INSERT INTO space_members(
                space_id, user_id, role, joined_at,
                history_visible_from, location_share_enabled,
                space_display_name, picture_hash
            ) VALUES(?, ?, ?, COALESCE(?, datetime('now')), ?, ?, ?, ?)
            ON CONFLICT(space_id, user_id) DO UPDATE SET
                role=excluded.role,
                history_visible_from=excluded.history_visible_from,
                location_share_enabled=excluded.location_share_enabled,
                space_display_name=excluded.space_display_name,
                picture_hash=excluded.picture_hash
            """,
            (
                member.space_id,
                member.user_id,
                member.role,
                member.joined_at,
                member.history_visible_from,
                int(member.location_share_enabled),
                member.space_display_name,
                member.picture_hash,
            ),
        )
        return member

    async def set_member_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        space_display_name: str | None = None,
        picture_hash: str | None | object = None,
    ) -> None:
        """Patch per-space profile fields without changing role / timestamps.

        ``picture_hash`` defaults to a sentinel (``None`` sentinel isn't
        usable because ``NULL`` is a valid "clear it" value). Callers pass
        the explicit new value.
        """
        await self._db.enqueue(
            """
            UPDATE space_members
               SET space_display_name=COALESCE(?, space_display_name),
                   picture_hash=?
             WHERE space_id=? AND user_id=?
            """,
            (space_display_name, picture_hash, space_id, user_id),
        )

    async def get_member(self, space_id: str, user_id: str) -> SpaceMember | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
        return _row_to_member(row_to_dict(row))

    async def list_members(self, space_id: str) -> list[SpaceMember]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_members WHERE space_id=? ORDER BY joined_at",
            (space_id,),
        )
        return [m for m in (_row_to_member(d) for d in rows_to_dicts(rows)) if m]

    async def delete_member(self, space_id: str, user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )

    async def set_role(
        self,
        space_id: str,
        user_id: str,
        role: str,
    ) -> None:
        if role not in ("owner", "admin", "member"):
            raise ValueError(f"invalid role {role!r}")
        await self._db.enqueue(
            "UPDATE space_members SET role=? WHERE space_id=? AND user_id=?",
            (role, space_id, user_id),
        )

    async def set_member_location_sharing(
        self,
        space_id: str,
        user_id: str,
        enabled: bool,
    ) -> bool:
        """Flip a member's ``location_share_enabled`` in this space.

        Returns ``True`` if the row existed and was updated; ``False``
        if no matching member row exists. Used by §23.8.8's
        member-self-service ``PATCH /spaces/{id}/members/me/location-sharing``
        endpoint and by the space admin UI.
        """
        existing = await self._db.fetchone(
            "SELECT 1 FROM space_members WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
        if existing is None:
            return False
        await self._db.enqueue(
            "UPDATE space_members SET location_share_enabled=?"
            " WHERE space_id=? AND user_id=?",
            (1 if enabled else 0, space_id, user_id),
        )
        return True

    async def list_local_member_user_ids(self, space_id: str) -> list[str]:
        """Return ``user_id`` values for space members whose home instance is ours.

        Uses the join with ``users`` because local users are the only ones
        that appear in that table.
        """
        rows = await self._db.fetchall(
            """
            SELECT m.user_id FROM space_members m
             JOIN users u ON u.user_id = m.user_id
            WHERE m.space_id=?
            """,
            (space_id,),
        )
        return [r["user_id"] for r in rows]

    # ── Member instances ───────────────────────────────────────────────

    async def add_space_instance(
        self,
        space_id: str,
        instance_id: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_instances(space_id, instance_id)
            VALUES(?, ?)
            ON CONFLICT(space_id, instance_id) DO UPDATE SET
                last_seen_at=datetime('now')
            """,
            (space_id, instance_id),
        )

    async def remove_space_instance(
        self,
        space_id: str,
        instance_id: str,
    ) -> None:
        await self._db.enqueue(
            "DELETE FROM space_instances WHERE space_id=? AND instance_id=?",
            (space_id, instance_id),
        )

    async def list_member_instances(self, space_id: str) -> list[str]:
        rows = await self._db.fetchall(
            "SELECT instance_id FROM space_instances WHERE space_id=?",
            (space_id,),
        )
        return [r["instance_id"] for r in rows]

    async def instance_in_any_space(self, instance_id: str) -> bool:
        """Whether *instance_id* still shares any space with us at all.

        The question a §D2b space-scoped seat's lifetime turns on: that
        ``remote_instances`` row exists solely because the two households
        shared a space, so when the last one goes the row (and its keys)
        has nothing left to authorize.
        """
        row = await self._db.fetchone(
            "SELECT 1 FROM space_instances WHERE instance_id=? LIMIT 1",
            (instance_id,),
        )
        return row is not None

    # ── Bans ───────────────────────────────────────────────────────────

    async def ban_member(
        self,
        space_id: str,
        user_id: str,
        banned_by: str,
        *,
        identity_pk: str | None = None,
        reason: str | None = None,
    ) -> None:
        # Atomic: insert the ban and drop the membership in the same batch.
        await self._db.enqueue(
            """
            INSERT INTO space_bans(space_id, user_id, identity_pk, banned_by, reason)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(space_id, user_id) DO UPDATE SET
                identity_pk=excluded.identity_pk,
                banned_by=excluded.banned_by,
                reason=excluded.reason
            """,
            (space_id, user_id, identity_pk, banned_by, reason),
        )
        await self.delete_member(space_id, user_id)

    async def unban_member(self, space_id: str, user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_bans WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )

    async def is_banned(self, space_id: str, user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM space_bans WHERE space_id=? AND user_id=?",
            (space_id, user_id),
        )
        return row is not None

    async def list_bans(self, space_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT * FROM space_bans WHERE space_id=? ORDER BY banned_at",
            (space_id,),
        )
        return rows_to_dicts(rows)

    # ── Moderation queue ──────────────────────────────────────────────

    async def save_moderation_item(
        self,
        item: SpaceModerationItem,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_moderation_queue(
                id, space_id, feature, action, submitted_by,
                payload_json, current_snapshot,
                submitted_at, expires_at, status,
                reviewed_by, reviewed_at, rejection_reason
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.space_id,
                item.feature,
                item.action,
                item.submitted_by,
                dump_json(item.payload),
                item.current_snapshot,
                _iso_ts(item.submitted_at),
                _iso_ts(item.expires_at),
                item.status.value,
                item.reviewed_by,
                _iso_ts(item.reviewed_at),
                item.rejection_reason,
            ),
        )

    async def list_moderation_queue(
        self,
        space_id: str,
        *,
        status: ModerationStatus | None = None,
        limit: int = 100,
    ) -> list[SpaceModerationItem]:
        if status is None:
            rows = await self._db.fetchall(
                "SELECT * FROM space_moderation_queue WHERE space_id=? "
                "ORDER BY submitted_at DESC LIMIT ?",
                (space_id, int(limit)),
            )
        else:
            rows = await self._db.fetchall(
                "SELECT * FROM space_moderation_queue "
                "WHERE space_id=? AND status=? "
                "ORDER BY submitted_at DESC LIMIT ?",
                (space_id, status.value, int(limit)),
            )
        return [
            item
            for item in (_row_to_moderation_item(d) for d in rows_to_dicts(rows))
            if item
        ]

    async def get_moderation_item(
        self,
        item_id: str,
    ) -> SpaceModerationItem | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_moderation_queue WHERE id=?",
            (item_id,),
        )
        return _row_to_moderation_item(row_to_dict(row))

    async def update_moderation_item_status(
        self,
        item_id: str,
        *,
        status: ModerationStatus,
        reviewed_by: str,
        rejection_reason: str | None = None,
    ) -> None:
        await self._db.enqueue(
            """
            UPDATE space_moderation_queue
               SET status=?, reviewed_by=?, reviewed_at=datetime('now'),
                   rejection_reason=COALESCE(?, rejection_reason)
             WHERE id=?
            """,
            (status.value, reviewed_by, rejection_reason, item_id),
        )

    # ── Invite tokens ──────────────────────────────────────────────────

    async def create_invite_token(
        self,
        space_id: str,
        created_by: str,
        *,
        uses: int = 1,
        expires_at: str | None = None,
        token: str | None = None,
        role: str = SpaceRole.MEMBER.value,
        gfs_id: str | None = None,
        gfs_token: str | None = None,
        gfs_url: str | None = None,
    ) -> str:
        """Mint one invite token and return it.

        ``expires_at`` is an optional UTC timestamp; ``None`` means the
        token never expires and only ``uses`` limits it. Every current
        writer passes the tz-aware ISO 8601 shape
        (``datetime.now(timezone.utc).isoformat()``), but
        :meth:`consume_invite_token` normalises with SQLite's
        ``datetime()`` so the naive ``"YYYY-MM-DD HH:MM:SS"`` shape is
        accepted too.

        ``role`` is the seat the redeemer lands in (``member`` /
        ``subscriber`` / ``admin``); the CHECK in migration 0053 is the
        on-disk authority and ``owner`` is not in it. ``token`` lets the
        caller supply the value it already put inside a published invite
        blob — the GFS publish has to happen before the row exists (see
        ``SpaceService.create_invite_link``), so the token string is
        minted there and handed down rather than generated here.
        ``gfs_*`` record the one connection server the blob was parked
        on, so a later revoke can take it down.
        """
        token = token or uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO space_invite_tokens(
                token, space_id, created_by, uses_remaining, expires_at,
                role, gfs_id, gfs_token, gfs_url, uses_total
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                space_id,
                created_by,
                uses,
                expires_at,
                role,
                gfs_id,
                gfs_token,
                gfs_url,
                # The minted total, kept because ``uses_remaining`` is
                # decremented in place and the admin list wants
                # "3 of 10 left".
                uses,
            ),
        )
        return token

    async def consume_invite_token(
        self,
        token: str,
        *,
        redeemer_user_id: str | None = None,
    ) -> dict | None:
        """Decrement a token's remaining uses and return its metadata.

        Returns ``None`` if the token does not exist, has expired, has
        already been fully consumed, or — when ``redeemer_user_id`` is
        given — that user is banned from the token's space.

        **``redeemer_user_id`` folds the §13.7 ban into the same atomic
        UPDATE.** The cross-instance redeem used to consume first and check
        the ban afterwards, with no refund: a banned household could burn a
        20-use invite link in twenty requests, and the differential answer
        ("denied" vs "exhausted") told it its own ban status one user at a
        time. As one statement the counter never moves for a banned
        redeemer, so there is nothing to burn and nothing to learn.

        Both sides of the expiry guard are wrapped in SQLite's
        ``datetime()``. ``expires_at`` holds tz-aware ISO 8601
        (``2026-09-18T14:52:14.331881+00:00``) while ``datetime('now')``
        yields the naive ``2026-09-18 15:52:14`` shape — and SQLite
        compares TEXT lexicographically, where ``"T"`` (0x54) sorts above
        ``" "`` (0x20). A raw comparison therefore reported an *expired*
        token as still valid for the whole UTC day it expired on.
        ``datetime()`` parses both shapes, converts a non-UTC offset to
        UTC, and returns the naive form, so the two sides are comparable.
        An unparseable value yields NULL, which fails the guard closed.
        """

        def _run(conn):
            cur = conn.execute(
                """
                UPDATE space_invite_tokens
                   SET uses_remaining = uses_remaining - 1
                 WHERE token=?
                   AND uses_remaining > 0
                   AND (
                        expires_at IS NULL
                        OR datetime(expires_at) > datetime('now')
                   )
                   AND (
                        ? IS NULL
                        OR NOT EXISTS (
                            SELECT 1 FROM space_bans
                             WHERE space_bans.space_id
                                   = space_invite_tokens.space_id
                               AND space_bans.user_id = ?
                        )
                   )
                """,
                (token, redeemer_user_id, redeemer_user_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT space_id, created_by, uses_remaining, expires_at, role
                  FROM space_invite_tokens WHERE token=?
                """,
                (token,),
            ).fetchone()
            if row is None:
                return None
            return {
                "space_id": row[0],
                "created_by": row[1],
                "uses_remaining": row[2],
                "expires_at": row[3],
                # The seat the issuer decided on at mint time. Every
                # redeem path (local accept, §D2 ACK, §D2b bootstrap)
                # reads it from here — never from the redeem request.
                "role": row[4] or SpaceRole.MEMBER.value,
            }

        return await self._db.transact(_run)

    async def list_live_invite_tokens(self, space_id: str) -> list[dict]:
        """Every still-redeemable invite token of ``space_id``, newest first.

        Expired and exhausted tokens are excluded: they are not links any
        more, and listing them would invite an owner to "revoke" rows that
        already grant nothing. The expiry comparison wraps both sides in
        ``datetime()`` for the same reason
        :meth:`consume_invite_token` does — the two stored shapes
        (``…T…+00:00`` and ``… …``) do not compare lexically.
        """
        rows = await self._db.fetchall(
            """
            SELECT token, space_id, created_by, uses_remaining, created_at,
                   expires_at, role, gfs_id, gfs_token, gfs_url, uses_total
              FROM space_invite_tokens
             WHERE space_id=?
               AND uses_remaining > 0
               AND (
                    expires_at IS NULL
                    OR datetime(expires_at) > datetime('now')
               )
             ORDER BY created_at DESC, token DESC
            """,
            (space_id,),
        )
        return rows_to_dicts(rows)

    async def delete_invite_token(self, space_id: str, token: str) -> dict | None:
        """Delete one invite token and return the row it deleted.

        Scoped to ``space_id`` so an admin of one space can never revoke
        another space's link by guessing a token. Returns ``None`` when
        there was nothing to delete — that is what makes revoke
        idempotent. The row comes back so the caller can take the blob
        down on the connection server named in its ``gfs_*`` columns.
        """

        def _run(conn):
            row = conn.execute(
                """
                SELECT token, space_id, created_by, uses_remaining, created_at,
                       expires_at, role, gfs_id, gfs_token, gfs_url,
                       uses_total
                  FROM space_invite_tokens
                 WHERE space_id=? AND token=?
                """,
                (space_id, token),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "DELETE FROM space_invite_tokens WHERE space_id=? AND token=?",
                (space_id, token),
            )
            return {
                "token": row[0],
                "space_id": row[1],
                "created_by": row[2],
                "uses_remaining": row[3],
                "created_at": row[4],
                "expires_at": row[5],
                "role": row[6] or SpaceRole.MEMBER.value,
                "gfs_id": row[7],
                "gfs_token": row[8],
                "gfs_url": row[9],
                "uses_total": row[10],
            }

        return await self._db.transact(_run)

    # ── Invitations ────────────────────────────────────────────────────

    async def save_invitation(
        self,
        space_id: str,
        invited_user_id: str,
        invited_by: str,
        *,
        ttl_days: int = 7,
    ) -> str:
        invitation_id = uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO space_invitations(
                id, space_id, invited_user_id, invited_by, expires_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (invitation_id, space_id, invited_user_id, invited_by, expires),
        )
        return invitation_id

    async def save_remote_invitation(
        self,
        space_id: str,
        *,
        invited_by: str,
        remote_instance_id: str,
        remote_user_id: str,
        invite_token: str,
        space_display_hint: str | None = None,
        ttl_minutes: int = 15,
    ) -> str:
        """§D1b — persist a cross-household private-space invitation.

        ``remote_instance_id`` + ``remote_user_id`` identify the peer-side
        counterparty. Semantics of the pair depend on whether this row
        is stored on the host (=invitee's identity) or on the invitee's
        household (=inviter's identity). Rows on both sides share the
        same ``invite_token`` so the accept envelope can round-trip.
        """
        invitation_id = uuid.uuid4().hex
        expires = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO space_invitations(
                id, space_id, invited_user_id, invited_by,
                remote_instance_id, remote_user_id, invite_token,
                space_display_hint, expires_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invitation_id,
                space_id,
                remote_user_id,
                invited_by,
                remote_instance_id,
                remote_user_id,
                invite_token,
                space_display_hint,
                expires,
            ),
        )
        return invitation_id

    async def get_invitation(self, invitation_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT * FROM space_invitations WHERE id=?",
            (invitation_id,),
        )
        return row_to_dict(row)

    async def get_invitation_by_token(self, token: str) -> dict | None:
        # ``invite_token`` is partial-UNIQUE in the schema (where
        # invite_token IS NOT NULL), so at most one row can match.
        row = await self._db.fetchone(
            "SELECT * FROM space_invitations WHERE invite_token=?",
            (token,),
        )
        return row_to_dict(row)

    async def list_pending_remote_invites_for(
        self,
        user_id: str,
    ) -> list[dict]:
        """Inbound cross-household invites still waiting on the user's
        accept/decline. ``remote_user_id`` is the invitee on both sides
        (set to the local user's user_id when the row lives on the
        invitee's household).
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_invitations
             WHERE remote_user_id=?
               AND remote_instance_id IS NOT NULL
               AND status='pending'
             ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return rows_to_dicts(rows)

    async def list_pending_local_invites_for(
        self,
        user_id: str,
    ) -> list[dict]:
        """Pending same-household invites where ``user_id`` is the
        invitee. Symmetric to :meth:`list_pending_remote_invites_for`
        but for the local-add flow Pascal asked for: an admin's
        "add member" no longer seats the user immediately — they get
        a row here and have to accept it. ``remote_instance_id IS
        NULL`` distinguishes local from cross-household invites; both
        live in the same table.
        """
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_invitations
             WHERE invited_user_id=?
               AND remote_instance_id IS NULL
               AND status='pending'
             ORDER BY created_at DESC
            """,
            (user_id,),
        )
        return rows_to_dicts(rows)

    async def is_user_remote_member(
        self,
        space_id: str,
        user_id: str,
    ) -> bool:
        row = await self._db.fetchone(
            """
            SELECT 1 FROM space_invitations
             WHERE space_id=? AND remote_user_id=?
               AND remote_instance_id IS NOT NULL
               AND remote_instance_id != ''
               AND status='accepted'
             LIMIT 1
            """,
            (space_id, user_id),
        )
        return row is not None

    async def update_invitation_status(
        self,
        invitation_id: str,
        status: str,
    ) -> None:
        if status not in ("pending", "accepted", "declined", "expired"):
            raise ValueError(f"invalid invitation status {status!r}")
        await self._db.enqueue(
            "UPDATE space_invitations SET status=? WHERE id=?",
            (status, invitation_id),
        )

    # ── Join requests ──────────────────────────────────────────────────

    async def save_join_request(
        self,
        space_id: str,
        user_id: str,
        *,
        message: str | None = None,
        ttl_days: int = 7,
        remote_applicant_instance_id: str | None = None,
        remote_applicant_pk: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """Persist a pending join request. For cross-household (§D2)
        requests pass ``remote_applicant_instance_id`` and optionally
        ``remote_applicant_pk``. The ``request_id`` arg lets the
        federation-inbound handler reuse the wire id so
        :data:`SPACE_JOIN_REQUEST_APPROVED` round-trips match.
        """
        rid = request_id or uuid.uuid4().hex
        expires = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
        await self._db.enqueue(
            """
            INSERT INTO space_join_requests(
                id, space_id, user_id, message, expires_at,
                remote_applicant_instance_id, remote_applicant_pk
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                rid,
                space_id,
                user_id,
                message,
                expires,
                remote_applicant_instance_id,
                remote_applicant_pk,
            ),
        )
        return rid

    async def list_pending_join_requests(
        self,
        space_id: str,
    ) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_join_requests
             WHERE space_id=? AND status='pending'
             ORDER BY requested_at
            """,
            (space_id,),
        )
        return rows_to_dicts(rows)

    async def list_pending_join_request_space_ids_for_user(
        self,
        user_id: str,
    ) -> list[str]:
        rows = await self._db.fetchall(
            """
            SELECT DISTINCT space_id FROM space_join_requests
             WHERE user_id=? AND status='pending'
            """,
            (user_id,),
        )
        return [r["space_id"] for r in rows]

    async def update_join_request_status(
        self,
        request_id: str,
        status: str,
        *,
        reviewed_by: str | None = None,
    ) -> None:
        if status not in ("pending", "approved", "denied", "expired", "withdrawn"):
            raise ValueError(f"invalid join request status {status!r}")
        await self._db.enqueue(
            """
            UPDATE space_join_requests
               SET status=?, reviewed_by=?, reviewed_at=datetime('now')
             WHERE id=?
            """,
            (status, reviewed_by, request_id),
        )

    async def list_expired_join_requests(self) -> list[dict]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM space_join_requests
             WHERE status='pending'
               AND datetime(expires_at) < datetime('now')
            """,
        )
        return rows_to_dicts(rows)

    # ── Sidebar + aliases ──────────────────────────────────────────────

    async def pin_sidebar(
        self,
        user_id: str,
        space_id: str,
        position: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO pinned_sidebar_spaces(user_id, space_id, position)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id, space_id) DO UPDATE SET position=excluded.position
            """,
            (user_id, space_id, position),
        )

    async def unpin_sidebar(self, user_id: str, space_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM pinned_sidebar_spaces WHERE user_id=? AND space_id=?",
            (user_id, space_id),
        )

    async def set_space_alias(
        self,
        space_id: str,
        local_username: str,
        alias: str,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_aliases(space_id, local_username, alias)
            VALUES(?, ?, ?)
            ON CONFLICT(space_id, local_username) DO UPDATE SET
                alias=excluded.alias,
                updated_at=datetime('now')
            """,
            (space_id, local_username, alias),
        )

    async def get_space_alias(
        self,
        space_id: str,
        local_username: str,
    ) -> str | None:
        row = await self._db.fetchone(
            "SELECT alias FROM space_aliases WHERE space_id=? AND local_username=?",
            (space_id, local_username),
        )
        return row["alias"] if row else None

    # ── Sidebar links ──────────────────────────────────────────────────

    async def list_links(self, space_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT id, label, url, position FROM space_links "
            "WHERE space_id=? ORDER BY position, label",
            (space_id,),
        )
        return [
            {
                "id": r["id"],
                "label": r["label"],
                "url": r["url"],
                "position": int(r["position"] or 0),
            }
            for r in rows
        ]

    async def upsert_link(
        self,
        *,
        link_id: str,
        space_id: str,
        label: str,
        url: str,
        position: int,
    ) -> None:
        await self._db.enqueue(
            """
            INSERT INTO space_links(id, space_id, label, url, position)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                label=excluded.label,
                url=excluded.url,
                position=excluded.position
            """,
            (link_id, space_id, label, url, position),
        )

    async def delete_link(self, link_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM space_links WHERE id=?",
            (link_id,),
        )

    async def get_link(self, link_id: str) -> dict | None:
        row = await self._db.fetchone(
            "SELECT id, space_id, label, url, position FROM space_links WHERE id=?",
            (link_id,),
        )
        if row is None:
            return None
        return {
            "id": row["id"],
            "space_id": row["space_id"],
            "label": row["label"],
            "url": row["url"],
            "position": int(row["position"] or 0),
        }


# ─── Row → domain mapping ─────────────────────────────────────────────────


def _row_to_space(row: dict | None) -> Space | None:
    if row is None:
        return None
    features = SpaceFeatures.from_row(row)
    exempt = tuple(load_json(row.get("retention_exempt_json"), []))
    return Space(
        id=row["id"],
        name=row["name"],
        description=row.get("description"),
        emoji=row.get("emoji"),
        owner_instance_id=row["owner_instance_id"],
        owner_username=row["owner_username"],
        identity_public_key=row["identity_public_key"],
        config_sequence=int(row.get("config_sequence") or 0),
        roster_sequence=int(row.get("roster_sequence") or 0),
        config_hlc=str(row.get("config_hlc") or "0-0"),
        features=features,
        space_type=SpaceType(row.get("space_type", "private")),
        join_mode=JoinMode(row.get("join_mode", "invite_only")),
        join_code=row.get("join_code"),
        retention_days=row.get("retention_days"),
        retention_exempt_types=exempt,
        lat=row.get("lat"),
        lon=row.get("lon"),
        radius_km=row.get("radius_km"),
        bot_enabled=bool_col(row.get("bot_enabled", 0)),
        allow_here_mention=bool_col(row.get("allow_here_mention", 0)),
        dissolved=bool_col(row.get("dissolved", 0)),
        archived=bool_col(row.get("archived", 0)),
        archived_reason=row.get("archived_reason"),
        about_markdown=row.get("about_markdown"),
        cover_hash=row.get("cover_hash"),
        icon_hash=row.get("icon_hash"),
        tz=row.get("tz") or "UTC",
        min_age=int(row.get("min_age") or 0),
        category=row.get("category"),
    )


def _row_to_member(row: dict | None) -> SpaceMember | None:
    if row is None:
        return None
    return SpaceMember(
        space_id=row["space_id"],
        user_id=row["user_id"],
        role=row.get("role", "member"),
        joined_at=row["joined_at"],
        history_visible_from=row.get("history_visible_from"),
        location_share_enabled=bool_col(row.get("location_share_enabled", 0)),
        space_display_name=row.get("space_display_name"),
        picture_hash=row.get("picture_hash"),
    )


def _iso_ts(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _row_to_moderation_item(row: dict | None) -> SpaceModerationItem | None:
    if row is None:
        return None
    try:
        status = ModerationStatus(row.get("status") or "pending")
    except ValueError:
        status = ModerationStatus.PENDING
    return SpaceModerationItem(
        id=row["id"],
        space_id=row["space_id"],
        feature=row.get("feature", ""),
        action=row.get("action", ""),
        submitted_by=row.get("submitted_by", ""),
        payload=load_json(row.get("payload_json"), default={}),
        current_snapshot=row.get("current_snapshot"),
        submitted_at=_parse_ts(row.get("submitted_at")) or datetime.now(timezone.utc),
        expires_at=_parse_ts(row.get("expires_at")) or datetime.now(timezone.utc),
        status=status,
        reviewed_by=row.get("reviewed_by"),
        reviewed_at=_parse_ts(row.get("reviewed_at")),
        rejection_reason=row.get("rejection_reason"),
    )
