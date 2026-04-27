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
from ..domain.space import (
    JoinMode,
    ModerationStatus,
    Space,
    SpaceFeatures,
    SpaceMember,
    SpaceModerationItem,
    SpaceType,
)
from .base import bool_col, dump_json, load_json, row_to_dict, rows_to_dicts


@runtime_checkable
class AbstractSpaceRepo(Protocol):
    # ── Spaces ─────────────────────────────────────────────────────────
    async def save(self, space: Space) -> Space: ...
    async def get(self, space_id: str) -> Space | None: ...
    async def set_cover_hash(
        self,
        space_id: str,
        cover_hash: str | None,
    ) -> None: ...
    async def list_by_type(self, space_type: SpaceType) -> list[Space]: ...
    async def list_for_user(self, user_id: str) -> list[Space]: ...
    async def list_subscriptions_for_user(self, user_id: str) -> list[dict]: ...
    async def list_all(self) -> list[Space]: ...
    async def mark_dissolved(self, space_id: str) -> None: ...
    async def increment_config_sequence(self, space_id: str) -> int: ...
    async def update_age_gate(
        self,
        space_id: str,
        *,
        min_age: int | None = None,
        target_audience: str | None = None,
    ) -> None: ...

    # ── Members ────────────────────────────────────────────────────────
    async def save_member(self, member: SpaceMember) -> SpaceMember: ...
    async def get_member(self, space_id: str, user_id: str) -> SpaceMember | None: ...
    async def list_members(self, space_id: str) -> list[SpaceMember]: ...
    async def delete_member(self, space_id: str, user_id: str) -> None: ...
    async def set_role(self, space_id: str, user_id: str, role: str) -> None: ...
    async def set_member_profile(
        self,
        space_id: str,
        user_id: str,
        *,
        space_display_name: str | None = None,
        picture_hash: str | None | object = None,
    ) -> None: ...
    async def list_local_member_user_ids(self, space_id: str) -> list[str]: ...

    # ── Instances that mirror this space ───────────────────────────────
    async def add_space_instance(self, space_id: str, instance_id: str) -> None: ...
    async def remove_space_instance(self, space_id: str, instance_id: str) -> None: ...
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
    ) -> str: ...
    async def consume_invite_token(self, token: str) -> dict | None: ...

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
    async def update_invitation_status(
        self,
        invitation_id: str,
        status: str,
    ) -> None: ...

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
    """SQLite-backed :class:`AbstractSpaceRepo`."""

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    # ── Spaces ─────────────────────────────────────────────────────────

    async def save(self, space: Space) -> Space:
        cols = space.features.to_columns()
        await self._db.enqueue(
            """
            INSERT INTO spaces(
                id, name, description, emoji,
                owner_instance_id, owner_username, identity_public_key,
                config_sequence, space_type, join_mode, join_code,
                retention_days, retention_exempt_json,
                feature_calendar, feature_todo, feature_location,
                feature_stickies, feature_pages,
                posts_access, pages_access, stickies_access,
                calendar_access, tasks_access,
                allow_post_text, allow_post_image, allow_post_video,
                allow_post_transcript, allow_post_poll, allow_post_schedule,
                allow_post_file, allow_post_bazaar,
                lat, lon, radius_km, bot_enabled, allow_here_mention,
                dissolved, about_markdown, cover_hash
            ) VALUES(
                ?,?,?,?,
                ?,?,?,
                ?,?,?,?,
                ?,?,
                ?,?,?,
                ?,?,
                ?,?,?,
                ?,?,?,
                ?,?,
                ?,?,?,
                ?,?,?,
                ?,?,
                ?,?,?,?,?,
                ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                emoji=excluded.emoji,
                config_sequence=excluded.config_sequence,
                space_type=excluded.space_type,
                join_mode=excluded.join_mode,
                join_code=excluded.join_code,
                retention_days=excluded.retention_days,
                retention_exempt_json=excluded.retention_exempt_json,
                feature_calendar=excluded.feature_calendar,
                feature_todo=excluded.feature_todo,
                feature_location=excluded.feature_location,
                feature_stickies=excluded.feature_stickies,
                feature_pages=excluded.feature_pages,
                posts_access=excluded.posts_access,
                pages_access=excluded.pages_access,
                stickies_access=excluded.stickies_access,
                calendar_access=excluded.calendar_access,
                tasks_access=excluded.tasks_access,
                allow_post_text=excluded.allow_post_text,
                allow_post_image=excluded.allow_post_image,
                allow_post_video=excluded.allow_post_video,
                allow_post_transcript=excluded.allow_post_transcript,
                allow_post_poll=excluded.allow_post_poll,
                allow_post_schedule=excluded.allow_post_schedule,
                allow_post_file=excluded.allow_post_file,
                allow_post_bazaar=excluded.allow_post_bazaar,
                lat=excluded.lat,
                lon=excluded.lon,
                radius_km=excluded.radius_km,
                bot_enabled=excluded.bot_enabled,
                allow_here_mention=excluded.allow_here_mention,
                dissolved=excluded.dissolved,
                about_markdown=excluded.about_markdown,
                cover_hash=excluded.cover_hash
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
                space.space_type.value,
                space.join_mode.value,
                space.join_code,
                space.retention_days,
                dump_json(list(space.retention_exempt_types)),
                cols["feature_calendar"],
                cols["feature_todo"],
                cols["feature_location"],
                cols["feature_stickies"],
                cols["feature_pages"],
                cols["posts_access"],
                cols["pages_access"],
                cols["stickies_access"],
                cols["calendar_access"],
                cols["tasks_access"],
                cols["allow_post_text"],
                cols["allow_post_image"],
                cols["allow_post_video"],
                cols["allow_post_transcript"],
                cols["allow_post_poll"],
                cols["allow_post_schedule"],
                cols["allow_post_file"],
                cols["allow_post_bazaar"],
                space.lat,
                space.lon,
                space.radius_km,
                int(space.bot_enabled),
                int(space.allow_here_mention),
                int(space.dissolved),
                space.about_markdown,
                space.cover_hash,
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

    async def get(self, space_id: str) -> Space | None:
        row = await self._db.fetchone(
            "SELECT * FROM spaces WHERE id=?",
            (space_id,),
        )
        return _row_to_space(row_to_dict(row))

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

    async def update_age_gate(
        self,
        space_id: str,
        *,
        min_age: int | None = None,
        target_audience: str | None = None,
    ) -> None:
        """§CP.F1: set ``min_age`` and/or ``target_audience``.

        Both fields are nullable — ``None`` means "don't change this
        field"; callers pass the fields they received in the federation
        payload.
        """
        if min_age is None and target_audience is None:
            return
        if min_age is not None and target_audience is not None:
            await self._db.enqueue(
                "UPDATE spaces SET min_age=?, target_audience=? WHERE id=?",
                (min_age, target_audience, space_id),
            )
        elif min_age is not None:
            await self._db.enqueue(
                "UPDATE spaces SET min_age=? WHERE id=?",
                (min_age, space_id),
            )
        else:
            await self._db.enqueue(
                "UPDATE spaces SET target_audience=? WHERE id=?",
                (target_audience, space_id),
            )

    async def increment_config_sequence(self, space_id: str) -> int:
        """Atomically bump ``spaces.config_sequence`` and return the new value.

        ``AsyncDatabase.transact`` runs the UPDATE + SELECT inside a single
        ``BEGIN IMMEDIATE`` transaction so concurrent callers always see
        strictly increasing sequence numbers, even on SQLite builds that
        predate the ``RETURNING`` clause.
        """

        def _run(conn):
            cur = conn.execute(
                "UPDATE spaces SET config_sequence = config_sequence + 1 WHERE id=?",
                (space_id,),
            )
            if cur.rowcount == 0:
                raise KeyError(f"space {space_id!r} not found")
            row = conn.execute(
                "SELECT config_sequence FROM spaces WHERE id=?",
                (space_id,),
            ).fetchone()
            return int(row[0])

        return await self._db.transact(_run)

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
    ) -> str:
        token = uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO space_invite_tokens(
                token, space_id, created_by, uses_remaining, expires_at
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (token, space_id, created_by, uses, expires_at),
        )
        return token

    async def consume_invite_token(self, token: str) -> dict | None:
        """Decrement a token's remaining uses and return its metadata.

        Returns ``None`` if the token does not exist, has expired, or has
        already been fully consumed. When it has uses left, decrements the
        counter atomically and returns the row as a dict.
        """

        def _run(conn):
            cur = conn.execute(
                """
                UPDATE space_invite_tokens
                   SET uses_remaining = uses_remaining - 1
                 WHERE token=?
                   AND uses_remaining > 0
                   AND (expires_at IS NULL OR expires_at > datetime('now'))
                """,
                (token,),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute(
                """
                SELECT space_id, created_by, uses_remaining, expires_at
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
        row = await self._db.fetchone(
            "SELECT * FROM space_invitations WHERE invite_token=?"
            " ORDER BY created_at DESC LIMIT 1",
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
             WHERE status='pending' AND expires_at < datetime('now')
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
        about_markdown=row.get("about_markdown"),
        cover_hash=row.get("cover_hash"),
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
