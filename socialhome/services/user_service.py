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
import secrets
from dataclasses import replace
from datetime import datetime, timezone

from ..crypto import derive_user_id
from ..domain.events import (
    UserDeprovisioned,
    UserProfileUpdated,
    UserProvisioned,
    UserStatusChanged,
)
from ..domain.user import RESERVED_USERNAMES, User, UserStatus
from ..infrastructure.event_bus import EventBus
from ..media.image_processor import ImageProcessor
from ..repositories.profile_picture_repo import (
    AbstractProfilePictureRepo,
    compute_picture_hash,
)
from ..repositories.user_repo import AbstractUserRepo


_USERNAME_MAX_LENGTH = 32

#: Largest side of a stored profile picture (square WebP). Matches the
#: user-facing spec "≤ 400 px" caveat.
PROFILE_PICTURE_MAX_DIMENSION = 256

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


class UserService:
    """Provision, update, and query local users."""

    __slots__ = ("_repo", "_bus", "_own_instance_pk", "_pictures")

    def __init__(
        self,
        repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_public_key: bytes,
        profile_picture_repo: AbstractProfilePictureRepo | None = None,
    ) -> None:
        self._repo = repo
        self._bus = bus
        self._own_instance_pk = own_instance_public_key
        self._pictures = profile_picture_repo

    def attach_profile_picture_repo(
        self,
        repo: AbstractProfilePictureRepo,
    ) -> None:
        """Wire the picture repo post-construction (tests may build a
        bare :class:`UserService` first and attach later)."""
        self._pictures = repo

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
        user_id = derive_user_id(self._own_instance_pk, username)

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
        )
        await self._repo.save(user)
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

    async def patch_preferences(self, username: str, patch: dict) -> User:
        """Shallow-merge ``patch`` into ``users.preferences_json``.

        Unknown keys are allowed — the frontend owns the schema. ``None``
        values explicitly remove a key.
        """
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        try:
            prefs = json.loads(user.preferences_json or "{}")
        except json.JSONDecodeError:
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
        emoji: str | None = None,
        text: str | None = None,
        expires_at: str | None = None,
    ) -> User:
        """Set or clear a user's presence status."""
        user = await self._repo.get(username)
        if user is None:
            raise KeyError(f"user {username!r} not found")
        if emoji is None and text is None:
            status = UserStatus()
        else:
            status = UserStatus(emoji=emoji, text=text, expires_at=expires_at)
        new_user = replace(user, status=status)
        await self._repo.save(new_user)
        await self._bus.publish(
            UserStatusChanged(
                user_id=user.user_id,
                status=status if (emoji or text) else None,
            ),
        )
        return new_user

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
        await self._repo.revoke_api_token(token_id)

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

    async def unblock(self, blocker_username: str, blocked_user_id: str) -> None:
        blocker = await self._repo.get(blocker_username)
        if blocker is None:
            raise KeyError(f"user {blocker_username!r} not found")
        await self._repo.unblock(blocker.user_id, blocked_user_id)

    async def is_blocked(
        self,
        blocker_user_id: str,
        candidate_user_id: str,
    ) -> bool:
        return await self._repo.is_blocked(blocker_user_id, candidate_user_id)

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
