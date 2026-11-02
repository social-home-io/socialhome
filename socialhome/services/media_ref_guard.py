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
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..domain.errors import InvalidMediaRefError
from ..media.cleanup import media_basename

if TYPE_CHECKING:
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo


class MediaRefGuard:
    """The one check every local write of a media reference passes."""

    __slots__ = ("_refs",)

    def __init__(self, refs: AbstractMediaReferenceRepo) -> None:
        self._refs = refs

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
            if not name or not await self._refs.may_reference(name, user_id):
                raise InvalidMediaRefError(field)


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
