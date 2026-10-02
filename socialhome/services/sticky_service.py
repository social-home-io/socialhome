"""Sticky-note service — household board + per-space boards (§19).

Every read/mutation takes an explicit ``space_id`` scope (``None`` = the
household board). A sticky id outside that scope is "not found"
(:class:`KeyError` → 404): the household routes must never reach a
space's note by id, and a member of space A must never reach space B's
notes (§24.11 scoping, mirroring :class:`SpaceTaskService`).

Field rules (hex-only colour, board-clamped coordinates, sanitised and
capped content) come from :mod:`socialhome.domain.sticky`; a value that
breaks them is refused (:class:`ValueError` → 422) before any write.

Write access to a space board is gated by :meth:`StickyService.require_writer`
— read-only subscribers and archived spaces are refused
(:class:`SpacePermissionError` → 403). Membership + the per-space
``stickies`` feature toggle are checked by the route before the call.

A space board's writes also pass the space's ``stickies`` access level
(§4.3, :class:`ContentAccessMixin`) for the acting user: under
``ADMIN_ONLY`` only the owner / admins create, edit, recolour, move or
delete notes. A position-only update is a LAYOUT write. The household
board has no access level.

Each mutation publishes :class:`StickyCreated` / :class:`StickyUpdated` /
:class:`StickyDeleted`; :class:`RealtimeService` fans the WS frame out and
:class:`StickyFederationOutbound` federates space-scoped ones to member
households.
"""

from __future__ import annotations

import builtins
from typing import Any

from ..domain.events import StickyCreated, StickyDeleted, StickyUpdated
from ..domain.space import ContentAction, SpacePermissionError
from ..domain.sticky import (
    DEFAULT_STICKY_COLOR,
    MAX_STICKY_CONTENT_LENGTH,
    STICKY_BOARD_HEIGHT,
    STICKY_BOARD_WIDTH,
    Sticky,
    normalize_sticky_color,
    parse_sticky_coord,
    sanitize_sticky_content,
)
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.sticky_repo import AbstractStickyRepo
from .bus_publisher import BusPublisherMixin
from .content_access import ContentAccessMixin
from .space_service import SpaceService


def _as_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"sticky {field} must be a string")
    return value


def _content(value: Any) -> str:
    text = sanitize_sticky_content(_as_text(value, "content"))
    if not text:
        raise ValueError("sticky content must not be empty")
    if len(text) > MAX_STICKY_CONTENT_LENGTH:
        raise ValueError(
            f"sticky content must be at most {MAX_STICKY_CONTENT_LENGTH} characters"
        )
    return text


def _color(value: Any) -> str:
    color = normalize_sticky_color(value)
    if color is None:
        raise ValueError("sticky color must be a hex colour (#RGB or #RRGGBB)")
    return color


def _coord(value: Any, field: str, limit: float) -> float:
    out = parse_sticky_coord(value, limit=limit)
    if out is None:
        raise ValueError(f"sticky {field} must be a finite number")
    return out


class StickyService(BusPublisherMixin, ContentAccessMixin):
    """Scoped sticky-note operations for the household and space boards."""

    __slots__ = ("_repo", "_bus", "_spaces")

    def __init__(
        self,
        sticky_repo: AbstractStickyRepo,
        bus=None,
        *,
        space_repo: AbstractSpaceRepo,
    ) -> None:
        self._repo = sticky_repo
        self._bus = bus
        self._spaces = space_repo

    # ── Writer gate ──────────────────────────────────────────────────────

    async def require_writer(self, space_id: str, user_id: str) -> None:
        """Raise unless ``user_id`` may write this space's sticky board.

        * unknown / dissolved space → :class:`KeyError` (404);
        * archived space (read-only) → :class:`SpacePermissionError`;
        * not a member, or a read-only subscriber →
          :class:`SpacePermissionError` (403), via the uniform
          :meth:`SpaceService.assert_writable_member` rule.
        """
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            raise KeyError(f"space {space_id!r} not found")
        if space.archived:
            raise SpacePermissionError(
                "space is archived (read-only) — unarchive it to make changes",
            )
        member = await self._spaces.get_member(space_id, user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        SpaceService.assert_writable_member(
            member, action="edit sticky notes", space=space
        )

    # ── Reads ────────────────────────────────────────────────────────────

    async def list(self, *, space_id: str | None) -> builtins.list[Sticky]:
        return await self._repo.list(space_id=space_id)

    async def _get_or_404(self, sticky_id: str, space_id: str | None) -> Sticky:
        sticky = await self._repo.get_scoped(sticky_id, space_id=space_id)
        if sticky is None:
            raise KeyError(f"sticky {sticky_id!r} not found")
        return sticky

    # ── Mutations ────────────────────────────────────────────────────────

    async def create(
        self,
        *,
        author: str,
        content: Any,
        space_id: str | None,
        color: Any = DEFAULT_STICKY_COLOR,
        position_x: Any = 0.0,
        position_y: Any = 0.0,
    ) -> Sticky:
        if space_id is not None:
            await self._gate(space_id, author, "stickies", ContentAction.CREATE, True)
        sticky = await self._repo.add(
            author=author,
            content=_content(content),
            color=_color(color),
            position_x=_coord(position_x, "position_x", STICKY_BOARD_WIDTH),
            position_y=_coord(position_y, "position_y", STICKY_BOARD_HEIGHT),
            space_id=space_id,
        )
        await self._emit(
            StickyCreated(
                sticky_id=sticky.id,
                space_id=space_id,
                author=sticky.author,
                content=sticky.content,
                color=sticky.color,
                position_x=sticky.position_x,
                position_y=sticky.position_y,
                actor_user_id=author,
            )
        )
        return sticky

    async def update(
        self,
        sticky_id: str,
        *,
        space_id: str | None,
        actor_user_id: str,
        content: Any = None,
        color: Any = None,
        position_x: Any = None,
        position_y: Any = None,
    ) -> Sticky:
        """Apply the given fields to a sticky inside ``space_id``.

        :class:`KeyError` when the id isn't in that scope; every field is
        validated before the first write so a bad request changes nothing.
        """
        sticky = await self._get_or_404(sticky_id, space_id)
        if space_id is not None:
            layout_only = content is None and color is None
            await self._gate(
                space_id,
                actor_user_id,
                "stickies",
                ContentAction.LAYOUT if layout_only else ContentAction.EDIT,
                sticky.author == actor_user_id,
            )
        new_content = _content(content) if content is not None else None
        new_color = _color(color) if color is not None else None
        move = position_x is not None or position_y is not None
        x = (
            _coord(position_x, "position_x", STICKY_BOARD_WIDTH)
            if position_x is not None
            else sticky.position_x
        )
        y = (
            _coord(position_y, "position_y", STICKY_BOARD_HEIGHT)
            if position_y is not None
            else sticky.position_y
        )

        if new_content is not None:
            await self._repo.update_content(sticky.id, new_content, space_id=space_id)
        if move:
            await self._repo.update_position(sticky.id, x, y, space_id=space_id)
        if new_color is not None:
            await self._repo.update_color(sticky.id, new_color, space_id=space_id)

        updated = await self._get_or_404(sticky.id, space_id)
        await self._emit(
            StickyUpdated(
                sticky_id=updated.id,
                space_id=space_id,
                content=updated.content,
                color=updated.color,
                position_x=updated.position_x,
                position_y=updated.position_y,
                actor_user_id=actor_user_id,
            )
        )
        return updated

    async def delete(
        self, sticky_id: str, *, space_id: str | None, actor_user_id: str
    ) -> None:
        """Delete a sticky inside ``space_id`` — :class:`KeyError` otherwise."""
        if space_id is not None:
            sticky = await self._get_or_404(sticky_id, space_id)
            await self._gate(
                space_id,
                actor_user_id,
                "stickies",
                ContentAction.DELETE,
                sticky.author == actor_user_id,
            )
        if not await self._repo.delete(sticky_id, space_id=space_id):
            raise KeyError(f"sticky {sticky_id!r} not found")
        await self._emit(
            StickyDeleted(
                sticky_id=sticky_id, space_id=space_id, actor_user_id=actor_user_id
            )
        )
