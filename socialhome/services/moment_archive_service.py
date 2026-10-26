"""Your moments stay in your archive (decision 6a, owner sign-off 6).

A moment written on this household is never pruned at its ``expires_at``
here (:meth:`SqliteMomentRepo.prune_expired` skips it): it stays in its
author's archive, text and media. Other households prune their copies as
before — nothing on the wire changes.

So the archive can't fill the disk, the household admin sets a cap per
person on archived media (``instance_config`` key :data:`ARCHIVE_CAP_KEY`,
bytes; default :data:`MOMENT_ARCHIVE_DEFAULT_CAP_BYTES`; ``0`` = no
limit). The retention tick (:meth:`enforce_caps`) drops the oldest
archived files of anyone past it: the moment's text stays, its media
reference is cleared and the file removed once nothing else references it
(:func:`unlink_unreferenced`). A moment other people can still see keeps
its picture. Nobody is notified; the SPA says it up front ("Oldest
pictures go first when you pass …").

Usage counts every own moment's file (live and archived) plus a video's
poster, measured with one ``stat`` batch in a worker thread per call.

**Saved moments (decision 6b)** count toward the *saver's* cap: the file of
every moment a person saved that wasn't written here (a housemate's moment
counts toward its author's archive instead). A file several people saved
counts for each of them. Past the cap one oldest-first order applies per
person — an own moment by when it was written, a saved one by when it was
saved — and a saved moment loses its media the same way as an own one (its
text stays), only past its expiry and only when nobody else here saved it:
one person's cap never takes another person's picture.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from collections.abc import Iterable
from datetime import UTC, datetime

from ..domain.moment import (
    MOMENT_ARCHIVE_DEFAULT_CAP_BYTES,
    MOMENT_ARCHIVE_MAX_DROPS_PER_TICK,
    Moment,
    MomentArchiveUsage,
    SavedMoment,
)
from ..media.cleanup import media_basename, unlink_unreferenced
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from ..repositories.moment_repo import AbstractMomentRepo

log = logging.getLogger(__name__)

#: ``instance_config`` key holding the per-person archive cap in bytes.
ARCHIVE_CAP_KEY = "moments.archive_cap_bytes"

#: A transcoded video's poster is its ``.webp`` sibling (one UUID stem,
#: see ``routes.media_status.video_poster_path``).
_VIDEO_EXT = ".webm"
_POSTER_EXT = ".webp"


def _files_of(moment: Moment) -> list[str]:
    """The media file names one moment holds: its file, and a video's poster."""
    name = media_basename(moment.media_url)
    if name is None:
        return []
    if name.endswith(_VIDEO_EXT):
        return [name, name[: -len(_VIDEO_EXT)] + _POSTER_EXT]
    return [name]


def _has_expired(moment: Moment, now: datetime) -> bool:
    try:
        expires = datetime.fromisoformat(moment.expires_at)
    except TypeError, ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= now


def _kept_since(value: str) -> datetime:
    """Sort key for when a file entered someone's archive (naive = UTC;
    junk sorts first so it is looked at, never stuck)."""
    try:
        when = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


class _Kept:
    """One file a person keeps: an own moment or a saved one."""

    __slots__ = ("moment", "person", "saved", "since")

    def __init__(self, person: str, moment: Moment, *, saved: SavedMoment | None):
        self.person = person
        self.moment = moment
        self.saved = saved is not None
        self.since = _kept_since(saved.saved_at if saved else moment.created_at)


class MomentArchiveService:
    """Per-person archive usage, the household cap, and its enforcement."""

    __slots__ = ("_config", "_media_dir", "_media_refs", "_moments")

    def __init__(
        self,
        moment_repo: AbstractMomentRepo,
        config_repo: AbstractInstanceConfigRepo,
        *,
        media_dir: pathlib.Path | None,
        media_refs: AbstractMediaReferenceRepo | None = None,
    ) -> None:
        self._moments = moment_repo
        self._config = config_repo
        self._media_dir = media_dir
        self._media_refs = media_refs

    # ── The cap ────────────────────────────────────────────────────────

    async def cap_bytes(self) -> int:
        """The household's per-person cap; ``0`` = no limit."""
        raw = await self._config.get(ARCHIVE_CAP_KEY)
        if raw is None:
            return MOMENT_ARCHIVE_DEFAULT_CAP_BYTES
        try:
            value = int(raw)
        except ValueError:
            log.warning("moment archive cap %r is not a number — using default", raw)
            return MOMENT_ARCHIVE_DEFAULT_CAP_BYTES
        return max(0, value)

    async def set_cap_bytes(self, value: object, *, actor_is_admin: bool) -> int:
        """Set the cap (admins only). ``value`` is a whole number of bytes
        ≥ 0; ``0`` turns the limit off."""
        if not actor_is_admin:
            raise PermissionError("Only household admins can set the archive limit.")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("cap_bytes must be a whole number of bytes, 0 or more.")
        await self._config.set(ARCHIVE_CAP_KEY, str(value))
        return value

    # ── Usage ──────────────────────────────────────────────────────────

    async def usage_for(self, author_user_id: str) -> MomentArchiveUsage:
        """How much media *author_user_id* keeps here: their own moments'
        files and the files of moments they saved."""
        cap = await self.cap_bytes()
        if self._media_dir is None:
            return MomentArchiveUsage(used_bytes=0, cap_bytes=cap)
        rows = await self._moments.list_own_media(author_user_id)
        rows += [s.moment for s in await self._moments.list_saved_media(author_user_id)]
        sizes = await self._sizes(rows)
        return MomentArchiveUsage(used_bytes=sum(sizes.values()), cap_bytes=cap)

    # ── Enforcement (retention tick) ───────────────────────────────────

    async def enforce_caps(
        self, *, max_drops: int = MOMENT_ARCHIVE_MAX_DROPS_PER_TICK
    ) -> int:
        """Drop the oldest kept media of everyone past the cap — own
        moments and saved ones in one oldest-first order per person.

        At most ``max_drops`` moments lose their media per call; the next
        tick continues. Only a moment past its expiry loses its file — a
        live one other people still see keeps it, even over the cap — and a
        saved one only when nobody else here saved it. Returns how many
        moments lost their media."""
        cap = await self.cap_bytes()
        if cap <= 0 or self._media_dir is None or max_drops <= 0:
            return 0
        kept = [
            _Kept(m.author_user_id, m, saved=None)
            for m in await self._moments.list_own_media()
        ] + [
            _Kept(s.user_id, s.moment, saved=s)
            for s in await self._moments.list_saved_media()
        ]
        if not kept:
            return 0
        kept.sort(key=lambda k: (k.person, k.since))
        sizes = await self._sizes(list({k.moment.id: k.moment for k in kept}.values()))
        totals: dict[str, int] = {}
        for k in kept:
            totals[k.person] = totals.get(k.person, 0) + sizes[k.moment.id]
        now = datetime.now(UTC)
        dropped = 0
        for k in kept:
            m = k.moment
            if dropped >= max_drops:
                break
            if totals[k.person] <= cap or not _has_expired(m, now):
                # Under the cap, or a live moment others still see.
                continue
            cleared = (
                await self._moments.clear_saved_media(m.id, k.person)
                if k.saved
                else await self._moments.clear_media(m.id)
            )
            if not cleared:
                continue
            await unlink_unreferenced(self._media_dir, self._media_refs, _files_of(m))
            totals[k.person] -= sizes[m.id]
            dropped += 1
        if dropped:
            log.info("momentum: archive cap dropped media of %d moments", dropped)
        return dropped

    # ── Internal ───────────────────────────────────────────────────────

    async def _sizes(self, rows: list[Moment]) -> dict[str, int]:
        """Bytes per moment id (file + a video's poster; missing = 0)."""
        media_dir = self._media_dir
        if media_dir is None or not rows:
            return {m.id: 0 for m in rows}
        files = [(m.id, _files_of(m)) for m in rows]
        return await asyncio.to_thread(self._sizes_sync, media_dir, files)

    @staticmethod
    def _sizes_sync(
        media_dir: pathlib.Path, files: Iterable[tuple[str, list[str]]]
    ) -> dict[str, int]:
        """Blocking ``stat`` batch — runs in a worker thread."""
        out: dict[str, int] = {}
        for moment_id, names in files:
            total = 0
            for name in names:
                try:
                    total += os.stat(media_dir / name).st_size
                except OSError:
                    continue
            out[moment_id] = total
        return out


__all__ = ["ARCHIVE_CAP_KEY", "MomentArchiveService"]
