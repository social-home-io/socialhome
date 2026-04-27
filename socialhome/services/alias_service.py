"""Personal user aliases — write path + bulk resolver (§4.1.6).

Two services in one module:

* :class:`AliasService` — the mutation surface (PUT/DELETE the
  current viewer's alias for another user). Routes call this; it
  validates the inputs and refuses to alias yourself or a stranger.
* :class:`AliasResolver` — bulk read path. Member-list, feed, and
  DM render code calls :meth:`AliasResolver.resolve_users` with the
  viewer's ``user_id`` and the target ``user_ids`` it's about to
  display, gets back ``{target_user_id: alias}``, and feeds those
  into :meth:`DisplayableUser.from_local_user` /
  :meth:`from_remote_user`.

Why split: the resolver is dependency-light (one repo) so we can
inject it into many services without dragging the alias mutation
surface along. The mutation service needs the user repo to validate
that the target exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..repositories.alias_repo import MAX_ALIAS_LENGTH, AbstractAliasRepo

if TYPE_CHECKING:
    from ..repositories.user_repo import AbstractUserRepo


class AliasError(Exception):
    """Base class for alias-domain errors."""


class AliasNotFoundError(AliasError):
    """Target user_id doesn't exist in either users or remote_users."""


class AliasInvalidError(AliasError):
    """Alias is empty, too long, or names the viewer themselves."""


class AliasResolver:
    """Bulk lookup of viewer-private aliases for a set of target users.

    Stateless apart from the injected repo. Safe to construct once
    per app; injected wherever ``DisplayableUser`` instances are
    built.
    """

    __slots__ = ("_repo",)

    def __init__(self, alias_repo: AbstractAliasRepo) -> None:
        self._repo = alias_repo

    async def resolve_users(
        self,
        viewer_user_id: str,
        target_user_ids: Iterable[str],
    ) -> dict[str, str]:
        """Return ``{target_user_id: alias}`` for any matches.

        Returns ``{}`` when the viewer is anonymous (no
        ``viewer_user_id``) so unauthenticated render paths don't
        accidentally surface someone else's aliases.
        """
        if not viewer_user_id:
            return {}
        return await self._repo.get_user_aliases(
            viewer_user_id,
            target_user_ids,
        )


class AliasService:
    """Set/clear/list the current viewer's personal aliases."""

    __slots__ = ("_alias_repo", "_user_repo")

    def __init__(
        self,
        alias_repo: AbstractAliasRepo,
        user_repo: "AbstractUserRepo",
    ) -> None:
        self._alias_repo = alias_repo
        self._user_repo = user_repo

    async def set_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
        alias: str,
    ) -> None:
        """Set ``viewer_user_id``'s personal alias for ``target_user_id``.

        Validates:
        * ``alias`` is non-empty after strip and within
          :data:`MAX_ALIAS_LENGTH` (matches the DB CHECK).
        * Viewer cannot alias themselves — they have other tools for
          that (display name, space-display-name).
        * Target user must exist locally or remotely; aliasing a
          ghost user_id would clutter the table with rows that can
          never resolve to a name.
        """
        cleaned = (alias or "").strip()
        if not cleaned or len(cleaned) > MAX_ALIAS_LENGTH:
            raise AliasInvalidError(
                f"alias must be 1..{MAX_ALIAS_LENGTH} characters",
            )
        if not viewer_user_id or not target_user_id:
            raise AliasInvalidError("missing viewer or target user_id")
        if viewer_user_id == target_user_id:
            raise AliasInvalidError("cannot alias yourself")
        if not await self._target_exists(target_user_id):
            raise AliasNotFoundError(target_user_id)
        await self._alias_repo.set_user_alias(
            viewer_user_id=viewer_user_id,
            target_user_id=target_user_id,
            alias=cleaned,
        )

    async def clear_user_alias(
        self,
        *,
        viewer_user_id: str,
        target_user_id: str,
    ) -> None:
        if not viewer_user_id or not target_user_id:
            return
        await self._alias_repo.clear_user_alias(
            viewer_user_id=viewer_user_id,
            target_user_id=target_user_id,
        )

    async def list_aliases(self, viewer_user_id: str) -> dict[str, str]:
        if not viewer_user_id:
            return {}
        return await self._alias_repo.list_user_aliases(viewer_user_id)

    async def _target_exists(self, target_user_id: str) -> bool:
        local = await self._user_repo.get_by_user_id(target_user_id)
        if local is not None:
            return True
        remote = await self._user_repo.get_remote(target_user_id)
        return remote is not None
