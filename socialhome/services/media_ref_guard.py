"""Who may name a stored media file in a write.

A post, comment, cover, DM, moment, highlight frame, listing or task
attachment carries ``api/media/<file>`` references, and every read of it
signs those references again for that write's audience. A reference is
therefore a capability: whoever may write it decides who may fetch the
file. The URL shape alone (``require_local_media_ref``) does not stop a
person from naming a file they learned elsewhere — a private gallery
photo (gallery D7), someone else's DM attachment, a space's photo in the
household feed.

The rule, in one place: a person may name a file only when they can open
it today — no row references it yet (their own fresh upload), or at least
one row that does is one they can see — and a private album's file only
its uploader may name (privacy follows the upload; a chosen member shares
nothing out). A refusal answers exactly like a malformed reference (422
``INVALID_MEDIA_URL``), so it tells nobody that a file exists.

:class:`MediaRefGuard` holds the rule (over
:meth:`AbstractMediaReferenceRepo.may_reference`); the write services take
it through :class:`MediaRefGuardedMixin` (``attach_media_guard``, wired in
``app.py``) and call :meth:`MediaRefGuardedMixin._require_openable_media`
at every local write sink.

A file one write of a space names that another place references too
(another space, the household feed, a DM) gets a copy of its own for that
space (v_97, coordinator 2A): :meth:`MediaRefGuard.space_copies` — a new
name, the same bytes (a hard link where the file system allows), so the
space's GFS cache may seal it under the space's key
(``confined_to_space``) and every path treats it like any of the space's
files. The cache holds it once per space it is named in, as it would two
uploads.
"""

from __future__ import annotations

import logging
import pathlib
import uuid
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..domain.errors import InvalidMediaRefError
from ..domain.media_constraints import SPACE_PAGE_MEDIA_PREFIX
from ..media.cleanup import media_basename
from .inbound_media_store import media_file_path

if TYPE_CHECKING:
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo


log = logging.getLogger(__name__)

#: Bytes copied at a time when a hard link is not possible.
_COPY_CHUNK = 1024 * 1024


class MediaRefGuard:
    """The one check every local write of a media reference passes."""

    __slots__ = ("_dir", "_refs")

    def __init__(
        self,
        refs: AbstractMediaReferenceRepo,
        *,
        media_dir: pathlib.Path | None = None,
    ) -> None:
        self._refs = refs
        self._dir = pathlib.Path(media_dir) if media_dir is not None else None

    async def space_copies(
        self, refs: Iterable[str | None], *, space_id: str
    ) -> Callable[[str | None], str | None]:
        """A rename for a write of space *space_id* naming *refs*: each file
        another place references too — any row names it, yet not only this
        space's — becomes a copy local to the space (new name, same bytes);
        a fresh upload, a file only this space names and a missing file
        keep their names. Call it after :meth:`require_openable`."""
        names: dict[str, str] = {}
        if self._dir is not None:
            for ref in refs:
                name = media_basename(ref) if ref else None
                if not name or name in names:
                    continue
                if await self._refs.is_referenced(
                    name
                ) and not await self._refs.confined_to_space(name, space_id):
                    copy = await self._copy(name)
                    if copy is not None:
                        names[name] = copy

        def rename(value: str | None) -> str | None:
            if not value:
                return value
            path, sep, query = value.partition("?")
            head, slash, base = path.rpartition("/")
            local = names.get(base)
            return value if local is None else f"{head}{slash}{local}{sep}{query}"

        return rename

    async def _copy(self, name: str) -> str | None:
        """A new name holding *name*'s bytes, or ``None`` (no such file)."""
        assert self._dir is not None
        src = media_file_path(self._dir, name)
        if src is None or not await aiofiles.os.path.isfile(src):
            return None
        dot = name.rfind(".")
        copy = f"{uuid.uuid4().hex}{name[dot:] if dot > 0 else ''}"
        dst = self._dir / copy
        try:
            await aiofiles.os.link(src, dst)
        except OSError:
            async with aiofiles.open(src, "rb") as fin, aiofiles.open(dst, "wb") as out:
                while chunk := await fin.read(_COPY_CHUNK):
                    await out.write(chunk)
        log.info("media %s: a copy %s local to its new space", name, copy)
        return copy

    async def require_openable(
        self, refs: Iterable[str | None], *, user_id: str, field: str = "media_url"
    ) -> None:
        """Raise :class:`InvalidMediaRefError` (422 ``INVALID_MEDIA_URL``)
        unless *user_id* may name every file of *refs* (empty entries are
        skipped)."""
        for ref in refs:
            if not ref:
                continue
            name = media_basename(ref)
            if (
                not name
                # A space page's page-scoped copy (v_82) is never named by a
                # write: a page names its host's file, mapped on read.
                or name.startswith(SPACE_PAGE_MEDIA_PREFIX)
                or not await self._refs.may_reference(name, user_id)
            ):
                raise InvalidMediaRefError(field)

    async def may_open(self, name: str, user_id: str) -> bool:
        """Whether *user_id* may open the stored file *name* (a bearer or
        ingress viewer, not a signed URL): the same rule as naming it — a
        row they can see references it, or nothing does."""
        return await self._refs.may_reference(name, user_id)


class MediaRefGuardedMixin:
    """Behaviour-only mixin for a service that writes media references:
    ``attach_media_guard`` once at startup, then
    :meth:`_require_openable_media` at each write. A consumer with
    ``__slots__`` declares ``_media_guard``. Without a guard attached (a
    unit test's bare service) nothing is checked; ``app.py`` always
    attaches it."""

    __slots__ = ()

    def attach_media_guard(self, guard: MediaRefGuard) -> None:
        # The consumer declares the slot (a behaviour-only mixin owns none).
        setattr(self, "_media_guard", guard)  # noqa: B010

    async def _require_openable_media(
        self, refs: Iterable[str | None], *, user_id: str, field: str = "media_url"
    ) -> None:
        guard: MediaRefGuard | None = getattr(self, "_media_guard", None)
        if guard is not None:
            await guard.require_openable(refs, user_id=user_id, field=field)

    async def _space_media_copies(
        self, refs: Iterable[str | None], *, space_id: str
    ) -> Callable[[str | None], str | None]:
        """:meth:`MediaRefGuard.space_copies` — unchanged references
        without a guard attached."""
        guard: MediaRefGuard | None = getattr(self, "_media_guard", None)
        if guard is None:
            return lambda value: value
        return await guard.space_copies(refs, space_id=space_id)
