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
        """How much media *author_user_id*'s own moments hold here."""
        cap = await self.cap_bytes()
        if self._media_dir is None:
            return MomentArchiveUsage(used_bytes=0, cap_bytes=cap)
        rows = await self._moments.list_own_media(author_user_id)
        sizes = await self._sizes(rows)
        return MomentArchiveUsage(used_bytes=sum(sizes.values()), cap_bytes=cap)

    # ── Enforcement (retention tick) ───────────────────────────────────

    async def enforce_caps(
        self, *, max_drops: int = MOMENT_ARCHIVE_MAX_DROPS_PER_TICK
    ) -> int:
        """Drop the oldest archived media of everyone past the cap.

        At most ``max_drops`` moments lose their media per call; the next
        tick continues. Only a moment past its expiry loses its file — a
        live one other people still see keeps it, even over the cap.
        Returns how many moments lost their media."""
        cap = await self.cap_bytes()
        if cap <= 0 or self._media_dir is None or max_drops <= 0:
            return 0
        rows = await self._moments.list_own_media()  # by author, oldest first
        if not rows:
            return 0
        sizes = await self._sizes(rows)
        totals: dict[str, int] = {}
        for m in rows:
            totals[m.author_user_id] = totals.get(m.author_user_id, 0) + sizes[m.id]
        now = datetime.now(UTC)
        dropped = 0
        for m in rows:
            if dropped >= max_drops:
                break
            if totals[m.author_user_id] <= cap or not _has_expired(m, now):
                # Under the cap, or this (and every newer) moment is live.
                continue
            if not await self._moments.clear_media(m.id):
                continue
            await unlink_unreferenced(self._media_dir, self._media_refs, _files_of(m))
            totals[m.author_user_id] -= sizes[m.id]
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
