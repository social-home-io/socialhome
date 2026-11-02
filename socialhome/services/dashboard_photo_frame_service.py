"""The wall's photo screensaver — which photos, and their stored files.

Gallery decision D10 (2026-11-02): after a while without a touch the
household wall (the browser dashboard and the ESPHome panels) shows
photos from the albums an admin picked. This service is its read model:

* **Off by default.** Nothing shows until an admin turns
  ``photo_frame`` on *and* picks albums
  (:attr:`~socialhome.domain.household_dashboard.DashboardSettings.frame_minutes`).
* **Only albums everyone at home sees.** Every read re-checks that an
  album is a live household album with the ``everyone`` audience
  (:meth:`AbstractGalleryRepo.list_frame_albums`) — a private album, a
  space album or one in the trash never shows, even if it was picked
  before it changed. Only live photos (never videos, never the trash).
* **The household view.** The wall belongs to the whole household, so
  there is no per-person filter (no viewer, no blocks) — the admin's
  album choice is the rule.
* **One round a day.** :meth:`photos` draws :data:`FRAME_PHOTOS_SHOWN`
  photos from the newest :data:`FRAME_PHOTOS_SCANNED` of the chosen
  albums, seeded by the household day and the album list — every wall
  shows the same round, and a panel's ``ETag`` holds all day.
* **Stored files only.** The photo is the stored WebP (≤ 2560 px, the
  gallery's one format, ``domain/media_constraints.py``); nothing new is
  ever stored. The panel's PNG is drawn from it on the fly
  (:mod:`socialhome.dashboard.photo_frame`).

Night and the privacy lock are the dashboard's to apply (both screens
hide the screensaver then); this service doesn't know the time of day.
"""

from __future__ import annotations

import logging
import pathlib
import random
from dataclasses import replace

import aiofiles
import aiofiles.os

from ..domain.household_dashboard import (
    FRAME_PHOTOS_SCANNED,
    FRAME_PHOTOS_SHOWN,
    DashboardPhoto,
    FrameAlbumChoice,
)
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from .dashboard_settings_service import DashboardSettingsService
from .gallery_service import DEFAULT_ALBUM_KEY, DEFAULT_ALBUM_RENAMED_KEY

log = logging.getLogger(__name__)

__all__ = ["DashboardPhotoFrameService"]

#: The largest stored photo the wall reads (a WebP ≤ 2560 px is a few
#: MB at most) — a bigger file is no gallery photo and is not served.
MAX_PHOTO_BYTES = 32 * 1024 * 1024


class DashboardPhotoFrameService:
    """Read model of the wall's photo screensaver."""

    __slots__ = ("_config", "_media_dir", "_repo", "_settings")

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        settings: DashboardSettingsService,
        instance_config: AbstractInstanceConfigRepo,
        *,
        media_dir: str | pathlib.Path,
    ) -> None:
        self._repo = repo
        self._settings = settings
        self._config = instance_config
        self._media_dir = pathlib.Path(media_dir)

    async def album_choices(self) -> list[FrameAlbumChoice]:
        """Every album an admin may pick (household, everyone at home),
        the default album flagged while nobody renamed it."""
        albums = await self._repo.list_frame_albums()
        default_id = await self._config.get(DEFAULT_ALBUM_KEY) or None
        renamed_id = await self._config.get(DEFAULT_ALBUM_RENAMED_KEY) or None
        return [
            replace(a, default_name=True)
            if a.id == default_id and not a.is_system and renamed_id != a.id
            else a
            for a in albums
        ]

    async def _shown_albums(self) -> list[FrameAlbumChoice]:
        """The picked albums that may show now — empty while it's off."""
        settings = await self._settings.get()
        if settings.frame_minutes is None:
            return []
        picked = set(settings.photo_frame_albums)
        return [a for a in await self.album_choices() if a.id in picked]

    async def photos(
        self, day: str, *, album_names: dict[str, str] | None = None
    ) -> tuple[DashboardPhoto, ...]:
        """Today's round: up to :data:`FRAME_PHOTOS_SHOWN` photos, the same
        all ``day`` (``YYYY-MM-DD``, the household's). ``album_names``
        names the system and default albums in the wall's language
        (``"system"`` / ``"default"``); others keep their own name."""
        albums = await self._shown_albums()
        if not albums:
            return ()
        names = {a.id: _album_name(a, album_names) for a in albums}
        found = await self._repo.list_frame_photos(
            [a.id for a in albums], limit=FRAME_PHOTOS_SCANNED
        )
        if len(found) > FRAME_PHOTOS_SHOWN:
            seed = "|".join([day, *sorted(names)])
            found = random.Random(seed).sample(found, FRAME_PHOTOS_SHOWN)
        else:
            found = sorted(found, key=lambda p: (p.day, p.id))
        return tuple(replace(p, album_name=names[p.album_id]) for p in found)

    async def photo(self, item_id: str) -> DashboardPhoto | None:
        """``item_id`` if the screensaver shows it now (on, its album
        picked and open to everyone at home, a live photo)."""
        albums = await self._shown_albums()
        if not albums:
            return None
        return await self._repo.get_frame_photo(item_id, [a.id for a in albums])

    async def read(self, photo: DashboardPhoto) -> bytes | None:
        """The stored WebP of ``photo`` (from :meth:`photo`) — ``None``
        when its file is gone or unreadable."""
        name = photo.filename
        if not name or "/" in name or "\\" in name or name.startswith("."):
            log.warning("photo screensaver: unusable file name for %s", photo.id)
            return None
        path = self._media_dir / name
        try:
            if (await aiofiles.os.stat(path)).st_size > MAX_PHOTO_BYTES:
                log.warning("photo screensaver: %s is too large to show", photo.id)
                return None
            async with aiofiles.open(path, "rb") as fh:
                body: bytes = await fh.read()
        except OSError:
            log.warning("photo screensaver: file of %s can't be read", photo.id)
            return None
        return body


def _album_name(album: FrameAlbumChoice, names: dict[str, str] | None) -> str:
    if names is not None:
        if album.is_system and "system" in names:
            return names["system"]
        if album.default_name and "default" in names:
            return names["default"]
    return album.name
