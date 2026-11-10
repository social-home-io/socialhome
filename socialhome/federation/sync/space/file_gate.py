"""Which files of a space row this household ships, and how many (v_86, v_92).

A receiver takes a file only from a household it accepts it from
(``docs/protocol/media.md``, "Who fills a file"):

* the space's **host** or a delegated admin (the roster authority) — every
  file of every row it streams, hashed or not (#1311);
* the row's **author** household — the files of its own people's rows;
* a **trusted** household (a live ``admin`` or ``moderator`` seat) — the
  HASHED files of other people's rows. The bytes are checked against the
  hash the receiver's row carries, so a helper can never change a picture,
  only deliver it.

A §25.6 catch-up (:class:`CatchUpFileGate` without fills) is the host's job
for other people's files: a helper ships only its own people's there, so k
helpers never re-send one file to a newcomer. A helper's
``SPACE_SYNC_RESUME`` replays only its own people's rows and ships only
their files (``resume.py``). Other people's files leave a trusted helper
only on a v_92 ``SPACE_MEDIA_WANTED`` (``services/space_media_wanted.py``),
for one viewed file whose bytes it proved hash to the asked-for content
name (``fills=True``).

Every file a household ships on a resume or a ``SPACE_MEDIA_WANTED`` is
charged to ONE budget per (requester, space) (:class:`SpaceFileBudget`):
:data:`BUDGET_MAX_FILES` files / :data:`BUDGET_MAX_BYTES` bytes per
:data:`BUDGET_WINDOW_S`. Rows are not this module's concern: a non-host
provider streams only its own people's rows (``_OwnPeopleOnly``).
"""

from __future__ import annotations

import pathlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import aiofiles.os

from ....domain.space import CONTENT_AUTHORITY_ROLES, SpaceMember
from ....services.inbound_media_store import media_basename, media_file_path

#: Files one household is sent per space per window (resume + wanted).
BUDGET_MAX_FILES: int = 50
#: … and bytes.
BUDGET_MAX_BYTES: int = 32 * 1024 * 1024
#: The window, in seconds — the resume's once-per-30-minutes window.
BUDGET_WINDOW_S: float = 30 * 60.0
#: (requester, space) windows remembered (oldest dropped).
BUDGET_TRACKED: int = 4096

_CONTENT_ROLES: frozenset[str] = frozenset(r.value for r in CONTENT_AUTHORITY_ROLES)


async def local_file_size(media_dir: pathlib.Path, url: str) -> int | None:
    """The size of the file ``url`` names in ``media_dir``; ``None`` when it
    is not here (or the name is not one safe component)."""
    path = media_file_path(media_dir, media_basename(url))
    if path is None:
        return None
    try:
        return (await aiofiles.os.stat(path)).st_size
    except OSError:
        return None


class SpaceFileBudget:
    """One upload budget per (requester, space), shared by every path that
    ships files on request (a resume's files, ``SPACE_MEDIA_WANTED``)."""

    __slots__ = ("_clock", "_windows")

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        #: (requester, space) → [window start, files, bytes].
        self._windows: OrderedDict[tuple[str, str], list[float]] = OrderedDict()

    def _window(self, requester: str, space_id: str) -> list[float] | None:
        window = self._windows.get((requester, space_id))
        if window is None or self._clock() - window[0] >= BUDGET_WINDOW_S:
            return None
        return window

    def exhausted(self, requester: str, space_id: str) -> bool:
        """Whether the window can take no file at all."""
        window = self._window(requester, space_id)
        return window is not None and (
            window[1] >= BUDGET_MAX_FILES or window[2] >= BUDGET_MAX_BYTES
        )

    def take(self, requester: str, space_id: str, size: int) -> bool:
        """Charge one file of *size* bytes, or ``False`` when it would pass
        the budget (nothing charged)."""
        key = (requester, space_id)
        window = self._window(requester, space_id)
        if window is None:
            window = [self._clock(), 0.0, 0.0]
            self._windows[key] = window
        self._windows.move_to_end(key)
        while len(self._windows) > BUDGET_TRACKED:
            self._windows.popitem(last=False)
        if window[1] + 1 > BUDGET_MAX_FILES or window[2] + size > BUDGET_MAX_BYTES:
            return False
        window[1] += 1
        window[2] += size
        return True


class _SpaceMembers(Protocol):
    async def list_members(self, space_id: str) -> list[SpaceMember]: ...

    async def list_local_member_user_ids(self, space_id: str) -> list[str]: ...


class CatchUpFileGate:
    """The file filter for one space."""

    __slots__ = ("_all", "_fills", "_own")

    def __init__(self, *, all_files: bool, own: frozenset[str], fills: bool) -> None:
        self._all = all_files
        self._own = own
        self._fills = fills

    def ship(self, owner: str | None, urls: Sequence[str]) -> list[str]:
        """The files of a row owned by ``owner`` (``None``: names nobody)
        this household ships."""
        if self._all or self._fills or (owner is not None and owner in self._own):
            return list(urls)
        return []


class CatchUpFileGates:
    """Builds the :class:`CatchUpFileGate` for a space."""

    __slots__ = ("_authority", "_spaces")

    def __init__(
        self,
        *,
        space_repo: _SpaceMembers,
        roster_authority: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._spaces = space_repo
        self._authority = roster_authority

    async def for_space(self, space_id: str, *, fills: bool = False) -> CatchUpFileGate:
        """The gate for *space_id*. Without *fills* (a §25.6 catch-up) a
        non-host household ships its own people's files only. *fills*
        (``SPACE_MEDIA_WANTED``: the caller proved each file's bytes hash to
        the asked-for content name) lets a trusted household ship other
        people's files too."""
        if await self._authority(space_id):
            return CatchUpFileGate(all_files=True, own=frozenset(), fills=False)
        own = frozenset(await self._spaces.list_local_member_user_ids(space_id))
        trusted = fills and any(
            m.user_id in own and str(m.role) in _CONTENT_ROLES
            for m in await self._spaces.list_members(space_id)
        )
        return CatchUpFileGate(all_files=False, own=own, fills=trusted)
