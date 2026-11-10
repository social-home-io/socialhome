"""Which files of a space row this household ships on catch-up (v_86).

A receiver takes a file only from a household it accepts it from
(``docs/protocol/media.md``, "Who fills a file"):

* the space's **host** or a delegated admin (the roster authority) — every
  file of every row it streams, hashed or not (#1311);
* the row's **author** household — the files of its own people's rows;
* a **trusted** household (a live ``admin`` or ``moderator`` seat) — the
  HASHED files (``sc-`` names) of other people's rows, at v_86. The bytes
  are checked against the hash the receiver's row carries, so a helper
  can never change a picture, only deliver it.

Anything else the receiver refuses, so a plain member household ships
only its own people's files, and a helper ships other people's hashed
files only to a v_86 requester, only on a ``SPACE_SYNC_RESUME`` or a v_92
``SPACE_MEDIA_WANTED`` (``services/space_media_wanted.py``) (a full
§25.6 catch-up is the host's job: it ships every file, and k helpers
re-sending the same bytes to a newcomer would only spend bandwidth), at
most :data:`FILL_MAX_FILES` files / :data:`FILL_MAX_BYTES` bytes per
resume (newest rows first, in the order the caller walks them). Rows are not this module's concern: a non-host
provider still streams only its own people's rows (``_OwnPeopleOnly``).
"""

from __future__ import annotations

import pathlib
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import aiofiles.os

from ....domain.federation_capabilities import FederationCapability
from ....domain.file_hashes import is_space_content_media_name
from ....domain.space import CONTENT_AUTHORITY_ROLES, SpaceMember
from ....services.inbound_media_store import media_basename, media_file_path

#: Other people's hashed files one helper ships per catch-up stream or
#: resume — the same bound a resume puts on all its files.
FILL_MAX_FILES: int = 50
#: … and bytes.
FILL_MAX_BYTES: int = 32 * 1024 * 1024

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


class _SpaceMembers(Protocol):
    async def list_members(self, space_id: str) -> list[SpaceMember]: ...

    async def list_local_member_user_ids(self, space_id: str) -> list[str]: ...


class _Supports(Protocol):
    async def __call__(self, instance_id: str, *, min_version: int) -> bool: ...


class CatchUpFileGate:
    """The file filter for one (space, requester) stream."""

    __slots__ = ("_all", "_bytes", "_files", "_fills", "_own", "_size")

    def __init__(
        self,
        *,
        all_files: bool,
        own: frozenset[str],
        fills: bool,
        size: Callable[[str], Awaitable[int | None]],
    ) -> None:
        self._all = all_files
        self._own = own
        self._fills = fills
        self._size = size
        self._files = 0
        self._bytes = 0

    async def ship(self, owner: str | None, urls: Sequence[str]) -> list[str]:
        """The files of a row owned by ``owner`` (``None``: names nobody)
        this household ships to the requester."""
        if self._all or (owner is not None and owner in self._own):
            return list(urls)
        if not self._fills:
            return []
        kept: list[str] = []
        for url in urls:
            if not is_space_content_media_name(media_basename(url)):
                continue  # an unhashed file: the host's fill only
            if self._files >= FILL_MAX_FILES:
                break
            size = await self._size(url)
            if size is None or self._bytes + size > FILL_MAX_BYTES:
                continue
            self._files += 1
            self._bytes += size
            kept.append(url)
        return kept


class CatchUpFileGates:
    """Builds the :class:`CatchUpFileGate` for a stream."""

    __slots__ = ("_authority", "_size", "_spaces", "_supports")

    def __init__(
        self,
        *,
        space_repo: _SpaceMembers,
        roster_authority: Callable[[str], Awaitable[bool]],
        supports: _Supports,
        size: Callable[[str], Awaitable[int | None]],
    ) -> None:
        self._spaces = space_repo
        self._authority = roster_authority
        self._supports = supports
        self._size = size

    async def for_requester(
        self, space_id: str, requester: str, *, fills: bool = True
    ) -> CatchUpFileGate:
        """The gate for one stream to ``requester``. ``fills=False`` (a full
        §25.6 catch-up — the host's job, which ships every file) leaves a
        helper only its own people's files, so k helpers never re-send the
        same bytes to a newcomer; helper fills ride the bounded resume."""
        if await self._authority(space_id):
            return CatchUpFileGate(
                all_files=True, own=frozenset(), fills=False, size=self._size
            )
        own = frozenset(await self._spaces.list_local_member_user_ids(space_id))
        trusted = any(
            m.user_id in own and str(m.role) in _CONTENT_ROLES
            for m in await self._spaces.list_members(space_id)
        )
        fills = (
            fills
            and trusted
            and await self._supports(
                requester, min_version=FederationCapability.MIN_FOR_TRUSTED_FILE_FILLS
            )
        )
        return CatchUpFileGate(all_files=False, own=own, fills=fills, size=self._size)
