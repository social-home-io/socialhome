"""User repository — persistence for :class:`~socialhome.domain.user.User`,
:class:`~socialhome.domain.user.RemoteUser`, API tokens and user blocks.

The :class:`AbstractUserRepo` protocol is what services depend on;
:class:`SqliteUserRepo` is the production implementation. Tests substitute
in-memory fakes that implement the same surface.

Only the methods that are actually required by domain + services in v1 are
exposed. Additional spec methods (remote-user status, presence, preferences
patching, …) are added here as the services that need them come online.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.move_errors import StaleMoveLink
from ..domain.user import RemoteUser, User, UserStatus
from .base import bool_col, row_to_dict, rows_to_dicts


# ─── Abstract interface ───────────────────────────────────────────────────


@runtime_checkable
class AbstractUserRepo(Protocol):
    # Local users ---------------------------------------------------------
    async def get(self, username: str) -> User | None: ...
    async def get_by_user_id(self, user_id: str) -> User | None: ...
    async def get_by_external_id(self, external_id: str) -> User | None: ...
    async def get_by_handle(self, handle: str) -> User | None: ...
    async def save(self, user: User) -> User: ...
    async def list_active(self) -> list[User]: ...
    async def list_all(self) -> list[User]: ...
    async def list_by_ids(self, user_ids: set[str]) -> list[User]: ...
    async def set_admin(self, username: str, is_admin: bool) -> None: ...
    async def set_last_seen(self, user_id: str, at: str) -> None: ...
    async def set_tz(self, username: str, tz: str) -> None: ...
    async def set_user_identity_key(
        self,
        username: str,
        *,
        public_key_hex: str,
        private_key_wrapped: str,
    ) -> None: ...
    async def get_user_identity_keypair(
        self,
        username: str,
    ) -> tuple[bytes, bytes] | None: ...
    async def get_user_identity_anchor(self, username: str) -> str | None: ...
    async def soft_delete(self, username: str, grace_days: int = 30) -> None: ...
    async def rename_username(self, old: str, new: str) -> None: ...
    async def set_handle(self, username: str, handle: str) -> None: ...

    # Remote users --------------------------------------------------------
    async def get_remote(self, user_id: str) -> RemoteUser | None: ...
    async def get_remote_by_member(
        self,
        instance_id: str,
        remote_username: str,
    ) -> RemoteUser | None: ...
    async def upsert_remote(self, remote: RemoteUser) -> None: ...
    async def set_remote_user_identity_key(
        self,
        user_id: str,
        *,
        public_key_hex: str,
        identity_anchor: str | None = None,
    ) -> None: ...
    async def get_remote_user_identity_pubkey(self, user_id: str) -> bytes | None: ...
    async def list_remote_for_instance(self, instance_id: str) -> list[RemoteUser]: ...
    async def list_all_known_remote(self) -> list[RemoteUser]: ...
    async def get_instance_for_user(self, user_id: str) -> str | None: ...
    async def mark_remote_deprovisioned(
        self,
        user_id: str,
        *,
        at: str | None = None,
    ) -> None: ...
    async def record_user_move(
        self,
        *,
        old_user_id: str,
        new_user_id: str,
        new_instance_id: str,
        issued_at: str,
        move_link_json: str,
    ) -> None: ...
    async def get_move_link(self, old_user_id: str) -> str | None: ...
    async def resolve_current_identity(
        self, user_id: str
    ) -> tuple[str, str] | None: ...

    # API tokens ----------------------------------------------------------
    async def list_api_tokens(self, user_id: str) -> list[dict]: ...
    async def list_all_api_tokens(self) -> list[dict]: ...
    async def create_api_token(
        self,
        user_id: str,
        token_hash: str,
        label: str,
        *,
        expires_at: str | None = None,
    ) -> str: ...
    async def revoke_api_token(self, token_id: str) -> None: ...
    async def get_user_by_token_hash(self, token_hash: str) -> User | None: ...

    # Blocks --------------------------------------------------------------
    async def block(self, blocker_user_id: str, blocked_user_id: str) -> None: ...
    async def unblock(self, blocker_user_id: str, blocked_user_id: str) -> None: ...
    async def is_blocked(self, blocker_user_id: str, blocked_user_id: str) -> bool: ...
    async def list_blocked(
        self,
        blocker_user_id: str,
    ) -> list[tuple[str, str]]: ...

    # Follows -------------------------------------------------------------
    async def follow(self, follower_user_id: str, followed_user_id: str) -> None: ...
    async def unfollow(self, follower_user_id: str, followed_user_id: str) -> None: ...
    async def is_following(
        self, follower_user_id: str, followed_user_id: str
    ) -> bool: ...
    async def list_following(
        self,
        follower_user_id: str,
    ) -> list[tuple[str, str]]: ...

    # Profile picture (hash only — bytes live in ProfilePictureRepo) ------
    async def set_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None: ...
    async def set_remote_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None: ...


# ─── Concrete SQLite implementation ───────────────────────────────────────


class SqliteUserRepo:
    """SQLite-backed :class:`AbstractUserRepo` implementation.

    ``key_manager`` is the household KEK used to unwrap each user's Ed25519
    identity *private* seed at rest (``users.user_identity_private_key``). It
    is unavailable when the repos are built (the KEK is loaded later, in
    ``_on_startup``), so it can also be wired post-construction via
    :meth:`attach_key_manager` — mirroring :class:`SqliteSpaceRepo`. Only
    :meth:`get_user_identity_keypair` requires it; every other method works
    without it.
    """

    def __init__(self, db: AsyncDatabase, *, key_manager=None) -> None:
        self._db = db
        self._kek = key_manager

    def attach_key_manager(self, key_manager) -> None:
        """Wire the household KEK after construction (see class docstring)."""
        self._kek = key_manager

    # ── Local users ─────────────────────────────────────────────────────

    async def get(self, username: str) -> User | None:
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE username=?",
            (username,),
        )
        return _row_to_user(row_to_dict(row))

    async def get_by_user_id(self, user_id: str) -> User | None:
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,),
        )
        return _row_to_user(row_to_dict(row))

    async def get_by_external_id(self, external_id: str) -> User | None:
        """Look up an HA-synced user by their stable ``external_id``.

        Scoped to ``source='ha'`` — ``external_id`` is the HA ``user_id``
        and only meaningful for HA-mirrored rows. Used by the HA bootstrap
        to find the local row to follow when the HA person was renamed
        (the username drifts, the ``external_id`` does not).
        """
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE external_id=? AND source='ha'",
            (external_id,),
        )
        return _row_to_user(row_to_dict(row))

    async def get_by_handle(self, handle: str) -> User | None:
        """Look up a local user by their public ``handle``, case-insensitively.

        Backs the :meth:`UserService.set_handle` uniqueness pre-check; matches
        the ``idx_users_handle_nocase`` UNIQUE index collation (migration 0043).
        """
        row = await self._db.fetchone(
            "SELECT * FROM users WHERE handle=? COLLATE NOCASE",
            (handle,),
        )
        return _row_to_user(row_to_dict(row))

    async def save(self, user: User) -> User:
        """Upsert a local user row.

        Uses ``username`` (the primary key) to resolve conflicts — callers
        keep the same username to update an existing row.
        """
        await self._db.enqueue(
            """
            INSERT INTO users(
                username, user_id, display_name, is_admin, picture_hash, state,
                bio, locale, theme, emoji_skin_tone_default,
                status_emoji, status_text, status_expires_at,
                public_key, public_key_version, is_new_member,
                preferences_json, tz, email, phone, date_of_birth,
                declared_age, is_minor, child_protection_enabled,
                deleted_at, grace_until, created_at, source, external_id,
                identity_anchor, handle
            ) VALUES(
                ?,?,?,?,?,?,
                ?,?,?,?,
                ?,?,?,
                ?,?,?,
                ?,?,?,?,?,
                ?,?,?,
                ?,?,COALESCE(?, datetime('now')), ?, ?,
                ?,?
            )
            ON CONFLICT(username) DO UPDATE SET
                display_name=excluded.display_name,
                is_admin=excluded.is_admin,
                picture_hash=excluded.picture_hash,
                state=excluded.state,
                bio=excluded.bio,
                locale=excluded.locale,
                theme=excluded.theme,
                emoji_skin_tone_default=excluded.emoji_skin_tone_default,
                status_emoji=excluded.status_emoji,
                status_text=excluded.status_text,
                status_expires_at=excluded.status_expires_at,
                public_key=excluded.public_key,
                public_key_version=excluded.public_key_version,
                is_new_member=excluded.is_new_member,
                preferences_json=excluded.preferences_json,
                tz=excluded.tz,
                email=excluded.email,
                phone=excluded.phone,
                date_of_birth=excluded.date_of_birth,
                declared_age=excluded.declared_age,
                is_minor=excluded.is_minor,
                child_protection_enabled=excluded.child_protection_enabled,
                deleted_at=excluded.deleted_at,
                grace_until=excluded.grace_until,
                source=excluded.source,
                external_id=excluded.external_id,
                identity_anchor=excluded.identity_anchor,
                handle=excluded.handle
            """,
            (
                user.username,
                user.user_id,
                user.display_name,
                int(user.is_admin),
                user.picture_hash,
                user.state,
                user.bio,
                user.locale,
                user.theme,
                user.emoji_skin_tone_default,
                user.status.emoji,
                user.status.text,
                user.status.expires_at,
                user.public_key,
                user.public_key_version,
                int(user.is_new_member),
                user.preferences_json,
                user.tz,
                user.email,
                user.phone,
                user.date_of_birth,
                user.declared_age,
                int(user.is_minor),
                int(user.child_protection_enabled),
                user.deleted_at,
                user.grace_until,
                user.created_at,
                user.source,
                user.external_id,
                user.identity_anchor,
                user.handle,
            ),
        )
        return user

    async def list_active(self) -> list[User]:
        rows = await self._db.fetchall(
            "SELECT * FROM users WHERE state='active' AND deleted_at IS NULL "
            "ORDER BY username",
        )
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def list_all(self) -> list[User]:
        rows = await self._db.fetchall("SELECT * FROM users ORDER BY username")
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def list_by_ids(self, user_ids: set[str]) -> list[User]:
        if not user_ids:
            return []
        placeholders = ",".join("?" for _ in user_ids)
        rows = await self._db.fetchall(
            f"SELECT * FROM users WHERE user_id IN ({placeholders})",
            tuple(user_ids),
        )
        return [u for u in (_row_to_user(d) for d in rows_to_dicts(rows)) if u]

    async def set_admin(self, username: str, is_admin: bool) -> None:
        await self._db.enqueue(
            "UPDATE users SET is_admin=? WHERE username=?",
            (int(is_admin), username),
        )

    async def set_last_seen(self, user_id: str, at: str) -> None:
        """Persist the user's most recent active-session timestamp."""
        await self._db.enqueue(
            "UPDATE users SET last_seen_at=? WHERE user_id=?",
            (at, user_id),
        )

    async def set_tz(self, username: str, tz: str) -> None:
        """Persist the user's IANA timezone.

        Called from the SPA cold-start probe when the user logs in and
        the column is still at its ``'UTC'`` default — the SPA POSTs
        the browser-detected zone so personal calendar events anchor
        to the user's local wall clock. The settings page writes here
        too when the user picks a different zone manually.
        """
        await self._db.enqueue(
            "UPDATE users SET tz=? WHERE username=?",
            (tz, username),
        )

    async def set_user_identity_key(
        self,
        username: str,
        *,
        public_key_hex: str,
        private_key_wrapped: str,
    ) -> None:
        """Persist a user's KEK-wrapped Ed25519 identity keypair (§Phase 1).

        ``public_key_hex`` is the hex-encoded 32-byte public key (only the
        public half ever federates); ``private_key_wrapped`` is the seed
        sealed under the instance KEK via :class:`KeyManager`.
        """
        await self._db.enqueue(
            "UPDATE users SET user_identity_public_key=?, "
            "user_identity_private_key=? WHERE username=?",
            (public_key_hex, private_key_wrapped, username),
        )

    async def get_user_identity_keypair(
        self,
        username: str,
    ) -> tuple[bytes, bytes] | None:
        """Return ``(public_key_bytes, private_seed_bytes)`` for ``username``.

        The public key is decoded from the hex ``user_identity_public_key``
        column; the private seed is the raw 32-byte Ed25519 seed recovered by
        KEK-unwrapping ``user_identity_private_key`` (wrapped without
        associated data by :meth:`UserService.provision`). Returns ``None``
        when the user has no minted identity key yet (either column NULL) or
        the username is unknown — never a partial pair. Requires the KEK to
        have been wired (constructor or :meth:`attach_key_manager`).
        """
        if self._kek is None:
            raise RuntimeError("user identity key access requires a key_manager")
        row = await self._db.fetchone(
            "SELECT user_identity_public_key, user_identity_private_key "
            "FROM users WHERE username=?",
            (username,),
        )
        if row is None:
            return None
        public_hex = row["user_identity_public_key"]
        wrapped = row["user_identity_private_key"]
        if public_hex is None or wrapped is None:
            return None
        public_key = bytes.fromhex(public_hex)
        private_seed = self._kek.decrypt(wrapped)
        return public_key, private_seed

    async def get_user_identity_anchor(self, username: str) -> str | None:
        """Return the local user's immutable ``identity_anchor`` (uuid).

        The anchor is the derivation input for ``user_id`` for new users
        (``derive_user_id(own_instance_pk, identity_anchor)``); migration 0041
        backfilled it to the username for pre-existing rows. Returns ``None``
        when the username is unknown or the column is NULL (an early-boot row
        the backfill hasn't reached). No KEK needed — the anchor is a public,
        non-secret value carried on the wire in the v_26 user binding.
        """
        row = await self._db.fetchone(
            "SELECT identity_anchor FROM users WHERE username=?",
            (username,),
        )
        if row is None:
            return None
        anchor = row["identity_anchor"]
        return str(anchor) if anchor is not None else None

    async def soft_delete(self, username: str, grace_days: int = 30) -> None:
        now = datetime.now(timezone.utc).isoformat()
        grace = datetime.now(timezone.utc).timestamp() + grace_days * 86400
        grace_iso = datetime.fromtimestamp(grace, tz=timezone.utc).isoformat()
        await self._db.enqueue(
            "UPDATE users SET state='inactive', deleted_at=?, grace_until=? "
            "WHERE username=?",
            (now, grace_iso, username),
        )

    async def rename_username(self, old: str, new: str) -> None:
        """Atomically rename a local user's ``username``.

        ``users.username`` is the parent key for six child FKs (presence,
        post_drafts, space_aliases, conversation_members, calendars,
        platform_tokens) which all carry ``ON UPDATE CASCADE`` (migration
        0042), so the single ``UPDATE users`` propagates to them. Two homes
        of the username are *not* FK-linked and are updated explicitly in
        the same transaction:

        * ``platform_users.username`` — the standalone-login row (itself the
          parent of ``platform_tokens`` via ``ON UPDATE CASCADE``); a no-op
          UPDATE when the user has no standalone-login row (HA-synced users).
        * ``post_comments.author`` — a plain username text column, not an FK.
        * ``spaces.owner_username`` — a non-FK column (a space owner may be
          remote, so migration 0042 deliberately doesn't cascade it). Scoped
          to spaces *we* own (``owner_instance_id`` = our self instance) so a
          remote-owned space whose remote owner happens to share this username
          is left untouched; otherwise the old name strands owner-authority
          lookups (``_actor_or_raise(space.owner_username)`` → ``KeyError``).

        ``user_id`` / ``identity_anchor`` are immutable and untouched.
        """

        def _run(conn):
            conn.execute(
                "UPDATE users SET username=? WHERE username=?",
                (new, old),
            )
            conn.execute(
                "UPDATE platform_users SET username=? WHERE username=?",
                (new, old),
            )
            conn.execute(
                "UPDATE post_comments SET author=? WHERE author=?",
                (new, old),
            )
            conn.execute(
                "UPDATE spaces SET owner_username=? "
                "WHERE owner_username=? AND owner_instance_id=("
                "  SELECT instance_id FROM instance_identity WHERE id='self')",
                (new, old),
            )

        await self._db.transact(_run)

    async def set_handle(self, username: str, handle: str) -> None:
        """Set a local user's public ``handle``.

        The ``idx_users_handle_nocase`` UNIQUE index (migration 0043) enforces
        per-household, case-insensitive uniqueness. The service does a
        :meth:`get_by_handle` pre-check, but a concurrent writer could still
        collide — surface that as the same :class:`ValueError` the service
        raises rather than letting a raw ``IntegrityError`` become a 500.
        """
        try:
            await self._db.enqueue(
                "UPDATE users SET handle=? WHERE username=?",
                (handle, username),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"handle {handle!r} is already taken") from exc

    # ── Remote users ────────────────────────────────────────────────────

    async def get_remote(self, user_id: str) -> RemoteUser | None:
        row = await self._db.fetchone(
            "SELECT * FROM remote_users WHERE user_id=?",
            (user_id,),
        )
        return _row_to_remote_user(row_to_dict(row))

    async def get_remote_by_member(
        self,
        instance_id: str,
        remote_username: str,
    ) -> RemoteUser | None:
        """Look up a remote user by their conversation-member key.

        ``RemoteConversationMember`` rows carry ``(instance_id,
        remote_username)`` rather than the cryptographic ``user_id``,
        so the DM list / members endpoints need this side index to
        enrich a roster row with the peer's ``display_name``,
        ``picture_hash``, and globally-unique ``user_id``.
        """
        row = await self._db.fetchone(
            "SELECT * FROM remote_users WHERE instance_id=? AND remote_username=?",
            (instance_id, remote_username),
        )
        return _row_to_remote_user(row_to_dict(row))

    async def upsert_remote(self, remote: RemoteUser) -> None:
        # ``deprovisioned_at`` is reset on upsert so a USER_UPDATED that
        # arrives after a previous USER_REMOVED re-provisions the row.
        # The owning instance is authoritative for their own users —
        # if they re-publish the profile, we trust they want the user
        # visible again (e.g. peer-user-visibility flipped from hidden
        # back to visible).
        # Two conflict targets, not one. ``user_id`` is the PK, but
        # ``(instance_id, remote_username)`` is UNIQUE too — and a peer's
        # user_id can legitimately CHANGE under a stable natural key: that is
        # exactly what migration 0049 does when it repairs a synthetic
        # ``uid-<username>`` admin id to the derived one. With only
        # ``ON CONFLICT(user_id)`` the re-broadcast hit the natural-key
        # UNIQUE and raised forever, so the cached row could never learn the
        # repaired id. The second clause rebinds the cached row onto the new
        # id instead. SQLite applies at most one DO UPDATE per row, trying
        # the clauses in order.
        # ``handle`` is unsigned display metadata mirrored from the peer's
        # USER_UPDATED — an older peer omits it, in which case ``remote.handle``
        # is None and we must NOT clobber a previously-cached handle. The
        # ``COALESCE(excluded.handle, handle)`` keeps the stored value sticky
        # across a non-handle profile edit (bio/display_name/picture).
        await self._db.enqueue(
            """
            INSERT INTO remote_users(
                user_id, instance_id, remote_username, display_name, alias,
                visible_to, picture_hash, bio, status_json,
                public_key, public_key_version, handle, synced_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,COALESCE(?, datetime('now')))
            ON CONFLICT(user_id) DO UPDATE SET
                instance_id=excluded.instance_id,
                remote_username=excluded.remote_username,
                display_name=excluded.display_name,
                alias=excluded.alias,
                visible_to=excluded.visible_to,
                picture_hash=excluded.picture_hash,
                bio=excluded.bio,
                status_json=excluded.status_json,
                public_key=excluded.public_key,
                public_key_version=excluded.public_key_version,
                handle=COALESCE(excluded.handle, handle),
                synced_at=excluded.synced_at,
                deprovisioned_at=NULL
            ON CONFLICT(instance_id, remote_username) DO UPDATE SET
                user_id=excluded.user_id,
                display_name=excluded.display_name,
                alias=excluded.alias,
                visible_to=excluded.visible_to,
                picture_hash=excluded.picture_hash,
                bio=excluded.bio,
                status_json=excluded.status_json,
                public_key=excluded.public_key,
                public_key_version=excluded.public_key_version,
                handle=COALESCE(excluded.handle, handle),
                synced_at=excluded.synced_at,
                deprovisioned_at=NULL
            """,
            (
                remote.user_id,
                remote.instance_id,
                remote.remote_username,
                remote.display_name,
                remote.alias,
                remote.visible_to,
                remote.picture_hash,
                remote.bio,
                remote.status_json,
                remote.public_key,
                remote.public_key_version,
                remote.handle,
                remote.synced_at,
            ),
        )

    async def set_remote_user_identity_key(
        self,
        user_id: str,
        *,
        public_key_hex: str,
        identity_anchor: str | None = None,
    ) -> None:
        """Persist a remote user's *verified* per-user identity public key.

        Called by the inbound USERS_SYNC / USER_UPDATED handler **only after**
        :func:`socialhome.crypto.verify_user_identity_assertion` has validated
        the binding against the sender instance's pinned key — an unverified
        key is never stored. Kept separate from :meth:`upsert_remote` (which
        does not touch this column) so a legacy profile upsert can't clobber a
        previously-verified key, and a failed verification leaves the existing
        key intact. ``public_key_hex`` is the hex-encoded 32-byte Ed25519
        user public key.

        ``identity_anchor`` (proto v_26) is the immutable uuid the verifier
        used to derive ``user_id``; it is persisted alongside the key so a
        later rename can be tracked by anchor rather than username. ``None``
        (a v_25 binding, or no anchor on the wire) leaves the column unchanged
        so a v_26 anchor already on file isn't clobbered by a later v_25
        re-publish.
        """
        if identity_anchor is None:
            await self._db.enqueue(
                "UPDATE remote_users SET user_identity_public_key=? WHERE user_id=?",
                (public_key_hex, user_id),
            )
            return
        await self._db.enqueue(
            "UPDATE remote_users "
            "SET user_identity_public_key=?, identity_anchor=? WHERE user_id=?",
            (public_key_hex, identity_anchor, user_id),
        )

    async def get_remote_user_identity_pubkey(self, user_id: str) -> bytes | None:
        """Return a remote user's stored per-user identity pubkey ``P`` as bytes.

        Decodes the hex ``remote_users.user_identity_public_key`` — the same
        portable Ed25519 key the inbound USERS_SYNC / USER_UPDATED path stored
        after verifying its binding. The move-out verifier reads it as the
        receiver-stored ``P`` for binding #3. Returns ``None`` when the row is
        unknown, carries no verified key yet, or the stored value is malformed
        hex (fail-soft so the caller degrades rather than crashes).
        """
        row = await self._db.fetchone(
            "SELECT user_identity_public_key FROM remote_users WHERE user_id=?",
            (user_id,),
        )
        if row is None:
            return None
        hex_key = row["user_identity_public_key"]
        if not hex_key:
            return None
        try:
            return bytes.fromhex(hex_key)
        except ValueError:
            return None

    async def list_remote_for_instance(self, instance_id: str) -> list[RemoteUser]:
        """List remote users from an instance, excluding deprovisioned rows."""
        rows = await self._db.fetchall(
            "SELECT * FROM remote_users "
            "WHERE instance_id=? AND deprovisioned_at IS NULL "
            "ORDER BY remote_username",
            (instance_id,),
        )
        return [r for r in (_row_to_remote_user(d) for d in rows_to_dicts(rows)) if r]

    async def list_all_known_remote(self) -> list[RemoteUser]:
        """Return all non-deprovisioned remote users across every instance.

        Used by :meth:`AppFederationService.list_contacts` to build the
        pairing-scoped roster: members of paired households, the same set
        DMs and ``/friends`` expose.  The per-peer hide-list is applied
        upstream at pairing time (hidden members never reach this table).
        Personal-block filtering is applied by the caller.
        """
        rows = await self._db.fetchall(
            "SELECT * FROM remote_users "
            "WHERE deprovisioned_at IS NULL "
            "ORDER BY instance_id, remote_username",
        )
        return [r for r in (_row_to_remote_user(d) for d in rows_to_dicts(rows)) if r]

    async def mark_remote_deprovisioned(
        self,
        user_id: str,
        *,
        at: str | None = None,
    ) -> None:
        """Flag a remote user as gone. The row is kept so historical
        references (posts, comments) still render the display name.
        """
        await self._db.enqueue(
            "UPDATE remote_users SET deprovisioned_at=COALESCE(?, datetime('now')) "
            "WHERE user_id=?",
            (at, user_id),
        )

    async def get_instance_for_user(self, user_id: str) -> str | None:
        """Return the home ``instance_id`` of a ``user_id`` — local or remote.

        Checks the local ``instance_identity`` table first (our own users),
        then ``remote_users``. Returns ``None`` if the ``user_id`` is unknown.
        """
        own = await self._db.fetchone(
            "SELECT i.instance_id FROM users u "
            "JOIN instance_identity i ON i.id='self' "
            "WHERE u.user_id=?",
            (user_id,),
        )
        if own is not None:
            return own["instance_id"]
        row = await self._db.fetchone(
            "SELECT instance_id FROM remote_users WHERE user_id=?",
            (user_id,),
        )
        return row["instance_id"] if row else None

    # ── Move-out redirect (MO-1) ────────────────────────────────────────

    async def record_user_move(
        self,
        *,
        old_user_id: str,
        new_user_id: str,
        new_instance_id: str,
        issued_at: str,
        move_link_json: str,
    ) -> None:
        """Record a move-out redirect on the moved user's OLD ``remote_users``
        row, pointing at their NEW household-scoped identity.

        Monotonic on ``issued_at`` (a tz-aware ISO-8601 ``…+00:00`` string,
        which sorts lexically): if a redirect is already on file whose
        ``move_issued_at`` is ``>=`` the incoming one, the call raises
        :class:`StaleMoveLink` rather than overwriting newer state — so a
        replayed older move-link cannot resurrect a stale identity. The guard
        is keyed by the stable per-user row (``old_user_id`` is the immutable
        pubkey-derived id); the caller is responsible for having verified the
        link's signature against that user's identity key before recording.
        """
        existing = await self._db.fetchone(
            "SELECT move_issued_at FROM remote_users WHERE user_id=?",
            (old_user_id,),
        )
        if existing is not None:
            prior = existing["move_issued_at"]
            if prior is not None and issued_at <= prior:
                raise StaleMoveLink(
                    f"move link for {old_user_id} issued_at {issued_at!r} "
                    f"is not newer than recorded {prior!r}"
                )
        await self._db.enqueue(
            "UPDATE remote_users SET moved_to_user_id=?, moved_to_instance_id=?, "
            "move_issued_at=?, move_link=? WHERE user_id=?",
            (new_user_id, new_instance_id, issued_at, move_link_json, old_user_id),
        )

    async def get_move_link(self, old_user_id: str) -> str | None:
        """Return the stored ``move_link`` JSON for a moved user, or ``None``
        when the row is unknown or carries no recorded move."""
        row = await self._db.fetchone(
            "SELECT move_link FROM remote_users WHERE user_id=?",
            (old_user_id,),
        )
        if row is None:
            return None
        link = row["move_link"]
        return str(link) if link is not None else None

    async def resolve_current_identity(self, user_id: str) -> tuple[str, str] | None:
        """Follow the move-redirect chain to the current identity.

        Walks ``moved_to_user_id`` from ``user_id`` to the tip, returning the
        ``(current_user_id, current_instance_id)`` of the row that has no
        further redirect. An unmoved row resolves to itself. Returns ``None``
        when the starting ``user_id`` is unknown, or when a redirect cycle is
        detected (defended with a ``seen`` set so a malformed chain can't
        loop forever).
        """
        seen: set[str] = set()
        current = user_id
        while True:
            if current in seen:
                return None
            seen.add(current)
            row = await self._db.fetchone(
                "SELECT instance_id, moved_to_user_id FROM remote_users "
                "WHERE user_id=?",
                (current,),
            )
            if row is None:
                return None
            moved_to = row["moved_to_user_id"]
            if moved_to is None:
                return (current, row["instance_id"])
            current = moved_to

    # ── API tokens ──────────────────────────────────────────────────────

    async def list_api_tokens(self, user_id: str) -> list[dict]:
        rows = await self._db.fetchall(
            "SELECT token_id, label, created_at, expires_at, last_used_at, revoked_at "
            "FROM api_tokens WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )
        return rows_to_dicts(rows)

    async def list_all_api_tokens(self) -> list[dict]:
        """§A7 — admin-wide token list. Every user's active tokens in
        one query so the admin sessions panel can render a household-
        scope session view rather than only the caller's tokens.
        """
        rows = await self._db.fetchall(
            """
            SELECT t.token_id, t.label, t.created_at, t.expires_at,
                   t.last_used_at, t.revoked_at,
                   t.user_id, u.username, u.display_name
              FROM api_tokens AS t
              JOIN users AS u ON u.user_id = t.user_id
             ORDER BY t.created_at DESC
            """,
        )
        return rows_to_dicts(rows)

    async def create_api_token(
        self,
        user_id: str,
        token_hash: str,
        label: str,
        *,
        expires_at: str | None = None,
    ) -> str:
        token_id = uuid.uuid4().hex
        await self._db.enqueue(
            """
            INSERT INTO api_tokens(token_id, user_id, label, token_hash, expires_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (token_id, user_id, label, token_hash, expires_at),
        )
        return token_id

    async def revoke_api_token(self, token_id: str) -> None:
        await self._db.enqueue(
            "UPDATE api_tokens SET revoked_at=COALESCE(revoked_at, datetime('now')) "
            "WHERE token_id=?",
            (token_id,),
        )

    async def get_user_by_token_hash(self, token_hash: str) -> User | None:
        row = await self._db.fetchone(
            """
            SELECT u.* FROM users u
             JOIN api_tokens t ON t.user_id = u.user_id
            WHERE t.token_hash=? AND t.revoked_at IS NULL
              AND (t.expires_at IS NULL OR t.expires_at > datetime('now'))
            """,
            (token_hash,),
        )
        if row is None:
            return None
        # Bump last_used_at so operator UI can show active tokens.
        await self._db.enqueue(
            "UPDATE api_tokens SET last_used_at=datetime('now') WHERE token_hash=?",
            (token_hash,),
        )
        return _row_to_user(row_to_dict(row))

    # ── Blocks ──────────────────────────────────────────────────────────

    async def block(self, blocker_user_id: str, blocked_user_id: str) -> None:
        if blocker_user_id == blocked_user_id:
            raise ValueError("Cannot block yourself")
        await self._db.enqueue(
            "INSERT OR IGNORE INTO user_blocks(blocker_user_id, blocked_user_id) "
            "VALUES(?, ?)",
            (blocker_user_id, blocked_user_id),
        )

    async def unblock(self, blocker_user_id: str, blocked_user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM user_blocks WHERE blocker_user_id=? AND blocked_user_id=?",
            (blocker_user_id, blocked_user_id),
        )

    async def is_blocked(self, blocker_user_id: str, blocked_user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM user_blocks WHERE blocker_user_id=? AND blocked_user_id=?",
            (blocker_user_id, blocked_user_id),
        )
        return row is not None

    async def list_blocked(
        self,
        blocker_user_id: str,
    ) -> list[tuple[str, str]]:
        """Return ``[(blocked_user_id, blocked_at), ...]`` for the blocker.

        Newest blocks first so the Settings → Privacy → Blocked accounts
        panel can show recent additions at the top without re-sorting.
        """
        rows = await self._db.fetchall(
            "SELECT blocked_user_id, blocked_at FROM user_blocks "
            "WHERE blocker_user_id=? ORDER BY blocked_at DESC",
            (blocker_user_id,),
        )
        return [(r["blocked_user_id"], r["blocked_at"]) for r in rows]

    # ── Follows (§Momentum) ────────────────────────────────────────────

    async def follow(self, follower_user_id: str, followed_user_id: str) -> None:
        if follower_user_id == followed_user_id:
            raise ValueError("Cannot follow yourself")
        await self._db.enqueue(
            "INSERT OR IGNORE INTO user_follows(follower_user_id, followed_user_id) "
            "VALUES(?, ?)",
            (follower_user_id, followed_user_id),
        )

    async def unfollow(self, follower_user_id: str, followed_user_id: str) -> None:
        await self._db.enqueue(
            "DELETE FROM user_follows WHERE follower_user_id=? AND followed_user_id=?",
            (follower_user_id, followed_user_id),
        )

    async def is_following(self, follower_user_id: str, followed_user_id: str) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM user_follows "
            "WHERE follower_user_id=? AND followed_user_id=?",
            (follower_user_id, followed_user_id),
        )
        return row is not None

    async def list_following(
        self,
        follower_user_id: str,
    ) -> list[tuple[str, str]]:
        """Return ``[(followed_user_id, created_at), ...]``, newest first.

        Drives the Momentum follows-management panel so the user sees
        recent follows at the top without resorting.
        """
        rows = await self._db.fetchall(
            "SELECT followed_user_id, created_at FROM user_follows "
            "WHERE follower_user_id=? ORDER BY created_at DESC",
            (follower_user_id,),
        )
        return [(r["followed_user_id"], r["created_at"]) for r in rows]

    async def set_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE users SET picture_hash=? WHERE user_id=?",
            (picture_hash, user_id),
        )

    async def set_remote_picture_hash(
        self,
        user_id: str,
        picture_hash: str | None,
    ) -> None:
        await self._db.enqueue(
            "UPDATE remote_users SET picture_hash=? WHERE user_id=?",
            (picture_hash, user_id),
        )


# ─── Row ↔ domain mapping ─────────────────────────────────────────────────


def _row_to_user(row: dict | None) -> User | None:
    if row is None:
        return None
    return User(
        user_id=row["user_id"],
        username=row["username"],
        display_name=row["display_name"],
        is_admin=bool_col(row.get("is_admin", 0)),
        picture_hash=row.get("picture_hash"),
        state=row.get("state", "active"),
        bio=row.get("bio"),
        locale=row.get("locale"),
        theme=row.get("theme", "auto"),
        emoji_skin_tone_default=row.get("emoji_skin_tone_default"),
        status=UserStatus(
            emoji=row.get("status_emoji"),
            text=row.get("status_text"),
            expires_at=row.get("status_expires_at"),
        ),
        public_key=row.get("public_key"),
        public_key_version=int(row.get("public_key_version") or 0),
        is_new_member=bool_col(row.get("is_new_member", 1)),
        deleted_at=row.get("deleted_at"),
        grace_until=row.get("grace_until"),
        email=row.get("email"),
        phone=row.get("phone"),
        date_of_birth=row.get("date_of_birth"),
        declared_age=row.get("declared_age"),
        is_minor=bool_col(row.get("is_minor", 0)),
        child_protection_enabled=bool_col(row.get("child_protection_enabled", 0)),
        preferences_json=row.get("preferences_json", "{}"),
        tz=row.get("tz") or "UTC",
        created_at=row.get("created_at"),
        last_seen_at=row.get("last_seen_at"),
        source=row.get("source", "manual"),
        external_id=row.get("external_id"),
        identity_anchor=row.get("identity_anchor"),
        handle=row.get("handle"),
    )


def _row_to_remote_user(row: dict | None) -> RemoteUser | None:
    if row is None:
        return None
    return RemoteUser(
        user_id=row["user_id"],
        instance_id=row["instance_id"],
        remote_username=row["remote_username"],
        display_name=row["display_name"],
        alias=row.get("alias"),
        visible_to=row.get("visible_to", '"all"'),
        picture_hash=row.get("picture_hash"),
        bio=row.get("bio"),
        status_json=row.get("status_json"),
        public_key=row.get("public_key"),
        public_key_version=int(row.get("public_key_version") or 0),
        synced_at=row.get("synced_at"),
        handle=row.get("handle"),
        deprovisioned_at=row.get("deprovisioned_at"),
    )
