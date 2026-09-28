"""User service — account lifecycle, preferences, API tokens.

Wraps :class:`AbstractUserRepo` with the business rules the route layer
needs to invoke: provisioning, soft-delete, preference patching, API
token lifecycle, block/unblock.

Every public method is ``async`` and raises plain domain exceptions
(``ValueError``, ``KeyError``, ``PermissionError``) — the route layer
maps those to HTTP codes via ``_map_exc`` (§5.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import orjson

from ..crypto import derive_user_id, generate_identity_keypair
from ..domain.events import (
    UserBlocked,
    UserDeprovisioned,
    UserFollowed,
    UserProfileUpdated,
    UserProvisioned,
    UserStatusChanged,
    UserUnblocked,
    UserUnfollowed,
)
from ..domain.user import (
    RESERVED_USERNAMES,
    User,
    UserStatus,
    clean_status_emoji,
    clean_status_text,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import (
    AbstractProfilePictureRepo,
    compute_picture_hash,
)
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

_USERNAME_MAX_LENGTH = 32

#: Largest side of a stored profile picture (square WebP). 384 px keeps
#: avatars sharp at 3× retina (128 dp) so the UI never has to upscale —
#: covers watch faces and TV apps too. The previous 256 px was a
#: 2018-era target that was already getting upscaled in some surfaces.
PROFILE_PICTURE_MAX_DIMENSION = 384

#: Display-name / bio length caps applied by :meth:`patch_profile`.
DISPLAY_NAME_MAX_LENGTH = 64
BIO_MAX_LENGTH = 300


class _Unset:
    """Sentinel so partial-update kwargs can distinguish "unset" from
    an explicit ``None`` that clears a column."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "_UNSET"


_UNSET = _Unset()

#: Relative "clear after" choices the status editor offers.
STATUS_CLEAR_AFTER_CHOICES: dict[str, timedelta] = {
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}
#: Furthest an explicit ``clear_after`` instant may lie in the future.
STATUS_MAX_LIFETIME = timedelta(days=7)


def _resolve_clear_after(value: object, *, now: datetime, tz: str) -> str | None:
    """Turn a ``clear_after`` choice into a UTC ISO-8601 ``expires_at``."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pick when the status should clear.")
    value = value.strip()
    if value in STATUS_CLEAR_AFTER_CHOICES:
        deadline = now + STATUS_CLEAR_AFTER_CHOICES[value]
    elif value == "today":
        try:
            zone = ZoneInfo(tz or "UTC")
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
        local = now.astimezone(zone)
        # Midnight at the start of the user's next local day. Built from
        # the date (not ``+ timedelta(days=1)`` on the wall clock) so a
        # DST switch today doesn't shift it by an hour.
        next_day = local.date() + timedelta(days=1)
        deadline = datetime(
            next_day.year, next_day.month, next_day.day, tzinfo=zone
        ).astimezone(timezone.utc)
    else:
        try:
            deadline = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "Clear after must be 30m, 1h, 4h, today or an ISO-8601 time."
            ) from exc
        if deadline.tzinfo is None:
            raise ValueError("The clear-after time needs a timezone offset.")
        if deadline <= now:
            raise ValueError("The clear-after time must be in the future.")
        if deadline - now > STATUS_MAX_LIFETIME:
            raise ValueError("A status can be set to clear at most 7 days ahead.")
    return deadline.astimezone(timezone.utc).isoformat(timespec="seconds")


class UserService:
    """Provision, update, and query local users."""

    __slots__ = ("_repo", "_bus", "_own_instance_pk", "_pictures", "_key_manager")

    def __init__(
        self,
        repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_public_key: bytes,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
        key_manager: KeyManager | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._own_instance_pk = own_instance_public_key
        self._pictures = profile_picture_repo
        self._key_manager = key_manager

    def attach_profile_picture_repo(
        self,
        repo: AbstractProfilePictureRepo,
    ) -> None:
        """Wire the picture repo post-construction (tests may build a
        bare :class:`UserService` first and attach later)."""
        self._pictures = repo

    def attach_key_manager(self, key_manager: KeyManager) -> None:
        """Wire the instance KEK post-construction.

        The KEK only exists after ``_on_startup`` (the DB isn't open at
        ``create_app`` time), so a :class:`UserService` built earlier
        attaches it later — mirrors ``SpaceRepo.attach_key_manager``.
        Once attached, :meth:`provision` mints a per-user identity key.
        """
        self._key_manager = key_manager

    # ── Provisioning ────────────────────────────────────────────────────

    async def provision(
        self,
        *,
        username: str,
        display_name: str,
        is_admin: bool = False,
        email: str | None = None,
        picture_url: str | None = None,  # noqa: ARG002 — deprecated, ignored
        source: str = "manual",
    ) -> User:
        """Create a new local user or reactivate a soft-deleted one.

        Idempotent by ``username``: if the row already exists and is
        ``active``, this is a no-op returning the existing row; if the
        row is ``inactive`` (soft-deleted) it is reactivated with the
        new display-name / admin flag.

        ``source`` distinguishes manually-provisioned users (standalone
        mode, explicit admin creates) from HA-synced rows. The HA
        Users admin panel passes ``source='ha'`` so the UI knows
        which rows can be deprovisioned via the HA flow.

        The legacy ``picture_url`` parameter is accepted for backwards
        compatibility but ignored — pictures are now stored as WebP
        bytes via :meth:`set_picture` (§23 profile).
        """
        _validate_username(username)
        if source not in ("manual", "ha"):
            raise ValueError(f"invalid source {source!r}")
        display = display_name.strip() or username
        # New users derive their cryptographic user_id from an immutable
        # uuid4 identity_anchor rather than the (mutable) username, so a
        # future rename leaves user_id stable (§v_26). Existing rows keep
        # their username-derived id via the migration-0041 backfill — the
        # reactivate branch below never re-derives.
        #
        # identity_anchor is derived ONCE at provision and FROZEN — never
        # mutate; user_id depends on it.
        identity_anchor = uuid.uuid4().hex
        user_id = derive_user_id(self._own_instance_pk, identity_anchor)

        existing = await self._repo.get(username)
        if existing is not None:
            if existing.state == "active":
                return existing
            reactivated = replace(
                existing,
                state="active",
                deleted_at=None,
                grace_until=None,
                display_name=display,
                is_admin=is_admin or existing.is_admin,
                email=email or existing.email,
                source=source,
            )
            await self._repo.save(reactivated)
            return reactivated

        user = User(
            user_id=user_id,
            username=username,
            display_name=display,
            is_admin=is_admin,
            email=email,
            created_at=datetime.now(timezone.utc).isoformat(),
            source=source,
            identity_anchor=identity_anchor,
            handle=username,
        )
        await self._repo.save(user)
        # Mint a KEK-wrapped Ed25519 identity key for the new user so they
        # have one immediately, not at the next startup backfill. Only the
        # public half ever federates. Skipped when no KEK is attached (a
        # bare service in early-boot wiring) — the startup backfill catches
        # those rows once the KEK exists.
        if self._key_manager is not None:
            kp = generate_identity_keypair()
            await self._repo.set_user_identity_key(
                username,
                public_key_hex=kp.public_key.hex(),
                private_key_wrapped=self._key_manager.encrypt(kp.private_key),
            )
        await self._bus.publish(
            UserProvisioned(
                user_id=user.user_id,
                username=user.username,
                is_admin=user.is_admin,
            ),
        )
        return user

    async def deprovision(self, username: str, *, grace_days: int = 30) -> None:
        """Soft-delete a user. Row is retained for ``grace_days`` before
        background cleanup removes it (spec §23.56).
        """
        existing = await self._repo.get(username)
        if existing is None:
            raise KeyError(f"user {username!r} not found")
        await self._repo.soft_delete(username, grace_days=grace_days)
        await self._bus.publish(
            UserDeprovisioned(user_id=existing.user_id, username=username),
        )

    async def deprovision_ha_user(self, username: str) -> None:
        """Opt an HA-synced user out of Social Home.

        Soft-deletes the row immediately (no grace period — the admin
        can re-provision later from the HA users list) and publishes
        :class:`UserDeprovisioned`. Raises :class:`KeyError` if the user
        isn't known, and :class:`PermissionError` if the row was created
        manually rather than via HA sync (those should go through the
        regular deprovision flow).
        """
        existing = await self._repo.get(username)
        if existing is None:
            raise KeyError(f"user {username!r} not found")
        if existing.source != "ha":
            raise PermissionError(
                f"user {username!r} is not HA-synced (source={{existing.source!r}})",
            )
        await self._repo.soft_delete(username, grace_days=0)
        await self._bus.publish(
            UserDeprovisioned(user_id=existing.user_id, username=username),
        )

    async def set_admin(self, username: str, is_admin: bool) -> User:
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        await self._repo.set_admin(username, is_admin)
        return replace(user, is_admin=is_admin)

    async def rename_username(self, username: str, new_username: str) -> None:
        """Rename a local user's ``username`` (the mutable login label).

        Only ``manual``-source users can be renamed — an ``ha``-synced row's
        username is owned by Home Assistant, so renaming it would drift from
        the directory it mirrors (raises :class:`PermissionError`). The
        ``user_id`` / ``identity_anchor`` are immutable and unaffected, so the
        cryptographic identity survives the rename (§v_26).

        The repo applies the rename atomically (``UPDATE users`` cascades to
        the FK children; ``platform_users`` + ``post_comments.author`` are
        updated explicitly). On success a :class:`UserProfileUpdated` is
        published so the rename federates to paired peers like any other
        profile edit. Raises :class:`KeyError` if the user is unknown and
        :class:`ValueError` if the new name is invalid, reserved, or taken.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if user.source != "manual":
            raise PermissionError(
                f"username {username!r} is controlled by Home Assistant",
            )
        new_username = new_username.strip()
        if new_username == username:
            return
        _validate_username(new_username)
        if await self._repo.get(new_username) is not None:
            raise ValueError(f"username {new_username!r} already taken")
        await self._repo.rename_username(username, new_username)
        await self._bus.publish(
            UserProfileUpdated(
                user_id=user.user_id,
                username=new_username,
                display_name=user.display_name,
                bio=user.bio,
                picture_hash=user.picture_hash,
                picture_webp=None,
            ),
        )

    async def apply_ha_username(self, external_id: str, new_username: str) -> None:
        """Follow a Home-Assistant-side person rename onto the local row.

        HA owns an ``ha``-source user's username; this is the HA-authoritative
        counterpart to :meth:`rename_username` (which guards against renaming
        HA rows). The local row is matched by its stable ``external_id`` (the
        HA ``user_id``) — never by username, since the username is precisely
        what may have drifted. When the stored ``username`` differs from the
        HA person's current name the row is renamed (cascading via the repo)
        and a :class:`UserProfileUpdated` is published so the rename federates.

        No ``source`` guard (unlike :meth:`rename_username`): HA is the source
        of truth here. The new name must still pass :func:`_validate_username`
        — HA display names can be arbitrary, so an invalid name is logged at
        WARNING and the old username is kept rather than crashing the boot.

        No-ops (no event) when the external_id is unknown locally or the name
        is unchanged, so it is safe to call on every boot.
        """
        new_username = new_username.strip()
        user = await self._repo.get_by_external_id(external_id)
        if user is None:
            return
        if user.username == new_username:
            return
        try:
            _validate_username(new_username)
        except ValueError as exc:
            log.warning(
                "apply_ha_username: HA name %r for external_id %r is invalid"
                " (%s) — keeping %r",
                new_username,
                external_id,
                exc,
                user.username,
            )
            return
        if await self._repo.get(new_username) is not None:
            log.warning(
                "apply_ha_username: HA name %r for external_id %r is already"
                " taken — keeping %r",
                new_username,
                external_id,
                user.username,
            )
            return
        await self._repo.rename_username(user.username, new_username)
        await self._bus.publish(
            UserProfileUpdated(
                user_id=user.user_id,
                username=new_username,
                display_name=user.display_name,
                bio=user.bio,
                picture_hash=user.picture_hash,
                picture_webp=None,
            ),
        )

    async def set_handle(self, username: str, new_handle: str) -> None:
        """Change a local user's public ``handle`` (§public-handle).

        The handle is an SH-local *public* name, distinct from the login
        ``username``. Unlike :meth:`rename_username` there is **no** ``source``
        guard — every local user, including ``source='ha'`` rows (whose login
        username is HA-controlled), may set their own public handle. Uniqueness
        is per-household and case-insensitive (``idx_users_handle_nocase``).

        A no-op (no event) when the handle is unchanged. Raises
        :class:`KeyError` if the user is unknown and :class:`ValueError` if the
        new handle is invalid, reserved, or already taken by another user. On
        success a :class:`UserProfileUpdated` carrying the new handle is
        published so the change federates via USER_UPDATED like any other
        profile edit.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        new_handle = new_handle.strip()
        if new_handle == user.handle:
            return
        _validate_username(new_handle)
        existing = await self._repo.get_by_handle(new_handle)
        if existing is not None and existing.username != username:
            raise ValueError(f"handle {new_handle!r} is already taken")
        await self._repo.set_handle(username, new_handle)
        await self._bus.publish(
            UserProfileUpdated(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                bio=user.bio,
                picture_hash=user.picture_hash,
                picture_webp=None,
                handle=new_handle,
            ),
        )

    # ── Profile (display_name + bio + picture) ──────────────────────────

    async def patch_profile(
        self,
        username: str,
        *,
        display_name: str | _Unset = _UNSET,
        bio: str | None | _Unset = _UNSET,
    ) -> User:
        """Partial update of display_name + bio.

        Picture mutations go through :meth:`set_picture` /
        :meth:`clear_picture` (bytes + hash).
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")

        next_display = user.display_name
        if not isinstance(display_name, _Unset):
            cleaned = (display_name or "").strip()
            if not cleaned:
                raise ValueError("display_name cannot be empty")
            if len(cleaned) > DISPLAY_NAME_MAX_LENGTH:
                raise ValueError(
                    f"display_name must be ≤ {DISPLAY_NAME_MAX_LENGTH} chars",
                )
            next_display = cleaned

        next_bio = user.bio
        if not isinstance(bio, _Unset):
            if bio is None or not bio.strip():
                next_bio = None
            else:
                cleaned_bio = bio.strip()
                if len(cleaned_bio) > BIO_MAX_LENGTH:
                    raise ValueError(
                        f"bio must be ≤ {BIO_MAX_LENGTH} chars",
                    )
                next_bio = cleaned_bio

        updated = replace(user, display_name=next_display, bio=next_bio)
        await self._repo.save(updated)
        await self._bus.publish(
            UserProfileUpdated(
                user_id=updated.user_id,
                username=updated.username,
                display_name=updated.display_name,
                bio=updated.bio,
                picture_hash=updated.picture_hash,
                picture_webp=None,  # unchanged — no blob to ship
            )
        )
        return updated

    async def set_picture(
        self,
        user_id: str,
        raw_bytes: bytes,
    ) -> User:
        """Accept any image, convert via :class:`ImageProcessor` to a
        square-bounded WebP at :data:`PROFILE_PICTURE_MAX_DIMENSION`,
        store into :class:`AbstractProfilePictureRepo`, stamp the new
        hash onto ``users.picture_hash``, publish
        :class:`UserProfileUpdated`.
        """
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        user = await self._repo.get_by_user_id(user_id)
        if user is None:
            raise KeyError(f"user_id {user_id!r} not found")
        webp_bytes = await ImageProcessor().generate_thumbnail(
            raw_bytes,
            size=PROFILE_PICTURE_MAX_DIMENSION,
        )
        hash_ = compute_picture_hash(webp_bytes)
        await self._pictures.set_user_picture(
            user_id,
            bytes_webp=webp_bytes,
            hash=hash_,
            width=PROFILE_PICTURE_MAX_DIMENSION,
            height=PROFILE_PICTURE_MAX_DIMENSION,
        )
        await self._repo.set_picture_hash(user_id, hash_)
        refreshed = await self._repo.get_by_user_id(user_id)
        assert refreshed is not None
        await self._bus.publish(
            UserProfileUpdated(
                user_id=refreshed.user_id,
                username=refreshed.username,
                display_name=refreshed.display_name,
                bio=refreshed.bio,
                picture_hash=hash_,
                picture_webp=webp_bytes,
            )
        )
        return refreshed

    async def clear_picture(self, user_id: str) -> User:
        if self._pictures is None:
            raise RuntimeError("profile picture repo not attached")
        user = await self._repo.get_by_user_id(user_id)
        if user is None:
            raise KeyError(f"user_id {user_id!r} not found")
        await self._pictures.clear_user_picture(user_id)
        await self._repo.set_picture_hash(user_id, None)
        await self._bus.publish(
            UserProfileUpdated(
                user_id=user.user_id,
                username=user.username,
                display_name=user.display_name,
                bio=user.bio,
                picture_hash=None,
                picture_webp=None,
            )
        )
        return replace(user, picture_hash=None)

    async def get_picture(
        self,
        user_id: str,
    ) -> tuple[bytes, str] | None:
        if self._pictures is None:
            return None
        return await self._pictures.get_user_picture(user_id)

    # ── Preferences ─────────────────────────────────────────────────────

    async def set_tz(self, username: str, tz: str) -> User:
        """Persist the user's IANA timezone.

        Called from the SPA cold-start probe when the user logs in
        and ``users.tz`` is still at the default — the SPA POSTs the
        browser-detected zone so future personal calendar events
        anchor to the user's local wall clock without an extra prompt.
        Validates against the IANA database; an unknown name raises
        :class:`ValueError`.
        """
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone {tz!r}") from exc
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if user.tz == tz:
            return user
        await self._repo.set_tz(username, tz)
        return replace(user, tz=tz)

    async def patch_preferences(self, username: str, patch: dict) -> User:
        """Shallow-merge ``patch`` into ``users.preferences_json``.

        Unknown keys are allowed — the frontend owns the schema. ``None``
        values explicitly remove a key.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        try:
            prefs = orjson.loads(user.preferences_json or "{}")
        except orjson.JSONDecodeError:
            prefs = {}
        for key, value in patch.items():
            if value is None:
                prefs.pop(key, None)
            else:
                prefs[key] = value
        new_user = replace(
            user,
            preferences_json=json.dumps(prefs, sort_keys=True, separators=(",", ":")),
        )
        await self._repo.save(new_user)
        return new_user

    async def set_status(
        self,
        username: str,
        *,
        emoji: object = None,
        text: object = None,
        clear_after: object = None,
        now: datetime | None = None,
    ) -> User:
        """Set or clear a user's status (emoji + one line of text).

        ``emoji`` / ``text`` are validated by :func:`clean_status_emoji` /
        :func:`clean_status_text`; both blank clears the status.
        ``clear_after`` is ``None`` (keep until changed), one of
        :data:`STATUS_CLEAR_AFTER_CHOICES` (``"today"`` = the end of the
        user's local day in ``users.tz``), or an ISO-8601 instant in the
        future and at most :data:`STATUS_MAX_LIFETIME` away. Invalid
        input raises :class:`ValueError`.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        now = now or datetime.now(timezone.utc)
        clean_emoji = clean_status_emoji(emoji)
        clean_text = clean_status_text(text)
        if clean_emoji is None and clean_text is None:
            status = UserStatus()
        else:
            status = UserStatus(
                emoji=clean_emoji,
                text=clean_text,
                expires_at=_resolve_clear_after(clear_after, now=now, tz=user.tz),
            )
        if status == user.status:
            return user
        new_user = replace(user, status=status)
        await self._repo.save(new_user)
        await self._bus.publish(
            UserStatusChanged(
                user_id=user.user_id,
                status=status if status.is_set else None,
            ),
        )
        return new_user

    async def clear_expired_statuses(self, *, now: datetime | None = None) -> int:
        """Clear every local status whose ``expires_at`` has passed.

        Driven by :class:`UserStatusExpiryScheduler`; going through
        :meth:`set_status` publishes ``UserStatusChanged`` so open tabs
        and paired households drop the status too. Returns the count.
        """
        now = now or datetime.now(timezone.utc)
        cleared = 0
        for user in await self._repo.list_with_expired_status(
            now.isoformat(timespec="seconds")
        ):
            if user.status.is_expired(now):
                await self.set_status(user.username, now=now)
                cleared += 1
        return cleared

    async def clear_onboarding(self, username: str) -> None:
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if user.is_new_member:
            await self._repo.save(replace(user, is_new_member=False))

    # ── API tokens ──────────────────────────────────────────────────────

    async def create_api_token(
        self,
        username: str,
        *,
        label: str,
        expires_at: str | None = None,
    ) -> tuple[str, str]:
        """Create an API token. Returns ``(token_id, raw_token)``.

        The raw token is shown to the user exactly once. Only the SHA-256
        hash is persisted.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if not label.strip():
            raise ValueError("token label must not be empty")
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token_id = await self._repo.create_api_token(
            user.user_id,
            token_hash,
            label.strip(),
            expires_at=expires_at,
        )
        return token_id, raw_token

    async def revoke_api_token(self, token_id: str) -> None:
        """Revoke any user's token — the admin path (``/api/admin/tokens``)."""
        await self._repo.revoke_api_token(token_id)

    async def revoke_own_api_token(self, username: str, token_id: str) -> None:
        """Revoke ``token_id`` only if it belongs to ``username``.

        The self-service path (``DELETE /api/me/tokens/{id}``). An id owned
        by someone else is a no-op, so a member can't sign another member
        out by guessing or harvesting a token id.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        await self._repo.revoke_api_token_for_user(user.user_id, token_id)

    async def list_api_tokens(self, username: str) -> list[dict]:
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        return await self._repo.list_api_tokens(user.user_id)

    # ── Blocks ──────────────────────────────────────────────────────────

    async def block(self, blocker_username: str, blocked_user_id: str) -> None:
        blocker = await self._repo.get(blocker_username)
        if blocker is None:
            raise KeyError(f"user {blocker_username!r} not found")
        if blocker.user_id == blocked_user_id:
            raise ValueError("Cannot block yourself")
        await self._repo.block(blocker.user_id, blocked_user_id)
        await self._bus.publish(
            UserBlocked(
                blocker_user_id=blocker.user_id,
                blocked_user_id=blocked_user_id,
            )
        )

    async def unblock(self, blocker_username: str, blocked_user_id: str) -> None:
        blocker = await self._repo.get(blocker_username)
        if blocker is None:
            raise KeyError(f"user {blocker_username!r} not found")
        await self._repo.unblock(blocker.user_id, blocked_user_id)
        await self._bus.publish(
            UserUnblocked(
                blocker_user_id=blocker.user_id,
                blocked_user_id=blocked_user_id,
            )
        )

    async def is_blocked(
        self,
        blocker_user_id: str,
        candidate_user_id: str,
    ) -> bool:
        return await self._repo.is_blocked(blocker_user_id, candidate_user_id)

    async def list_blocked(self, blocker_username: str) -> list[dict]:
        """Return [{user_id, blocked_at}, …] for the blocker, newest first.

        Resolves the username → user_id once so callers can pass the
        authenticated session's username straight from the route.
        """
        blocker = await self._repo.get(blocker_username)
        if blocker is None:
            raise KeyError(f"user {blocker_username!r} not found")
        rows = await self._repo.list_blocked(blocker.user_id)
        return [{"user_id": uid, "blocked_at": at} for uid, at in rows]

    # ── Follows (§Momentum) ────────────────────────────────────────────

    async def follow(self, follower_username: str, followed_user_id: str) -> None:
        follower = await self._repo.get(follower_username)
        if follower is None:
            raise KeyError(f"user {follower_username!r} not found")
        if follower.user_id == followed_user_id:
            raise ValueError("Cannot follow yourself")
        await self._repo.follow(follower.user_id, followed_user_id)
        await self._bus.publish(
            UserFollowed(
                follower_user_id=follower.user_id,
                followed_user_id=followed_user_id,
            )
        )

    async def unfollow(self, follower_username: str, followed_user_id: str) -> None:
        follower = await self._repo.get(follower_username)
        if follower is None:
            raise KeyError(f"user {follower_username!r} not found")
        await self._repo.unfollow(follower.user_id, followed_user_id)
        await self._bus.publish(
            UserUnfollowed(
                follower_user_id=follower.user_id,
                followed_user_id=followed_user_id,
            )
        )

    async def list_following(self, follower_username: str) -> list[dict]:
        """Return ``[{user_id, created_at}, …]`` for the follower."""
        follower = await self._repo.get(follower_username)
        if follower is None:
            raise KeyError(f"user {follower_username!r} not found")
        rows = await self._repo.list_following(follower.user_id)
        return [{"user_id": uid, "created_at": at} for uid, at in rows]

    # ── Queries ─────────────────────────────────────────────────────────

    async def get(self, username: str) -> User | None:
        return await self._repo.get(username)

    async def get_by_user_id(self, user_id: str) -> User | None:
        return await self._repo.get_by_user_id(user_id)

    async def list_active(self) -> list[User]:
        return await self._repo.list_active()


# ─── Helpers ──────────────────────────────────────────────────────────────


def _validate_username(username: str) -> None:
    if not username:
        raise ValueError("username must not be empty")
    if len(username) > _USERNAME_MAX_LENGTH:
        raise ValueError(f"username must be at most {_USERNAME_MAX_LENGTH} characters")
    if username in RESERVED_USERNAMES:
        raise ValueError(f"username {username!r} is reserved")
