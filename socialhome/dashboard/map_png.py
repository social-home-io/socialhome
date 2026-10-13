"""The wall-panel map picture, gathered and kept: ``GET /api/display/v1/map.png``.

:class:`MapPictures` turns a :class:`~.map_scene.MapScene` into a PNG
(:func:`~.map_render.render_map`) and keeps the last few:

* **Tiles** come from the dashboard's tile source — the main app's
  :class:`~socialhome.services.map_tile_service.MapTileService`, the same
  upstream, cache and polite ``User-Agent`` as the tablet's ``/tiles``
  and the app's ``/api/map/tiles``; at most :data:`TILE_FETCHES` at once.
  A tile that can't be had leaves the board colour (logged at WARNING
  once until the tiles come back); a picture with no tile at all is
  :class:`MapTilesUnavailable` (``503``). A picture drawn with gaps is
  kept only :data:`RETRY_INCOMPLETE_S` before it is drawn again.
* **Faces** — shown people's pictures (the read model's ``avatar``, the
  same rule as ``/avatar/…``) and the bundled Twemoji.
* **Painting** runs in a worker thread, one picture at a time; a panel
  polling every 30 s gets the kept picture (or a ``304``) — a picture is
  only drawn when what it shows changes.

The cache holds :data:`CACHE_ENTRIES` pictures (and :data:`CACHE_BYTES`
at most), keyed by ``(scene key, width, height, scheme)``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from ..services.map_tile_service import TileCoordinateError, TileUnavailableError
from .avatars import AvatarImages
from .emoji import SOURCE_SIZE, EmojiImages
from .map_frame import TileSpot
from .map_render import Rendered, plan_tiles, render_map, scene_emoji
from .map_scene import MapPalette, MapScene, scene_etag

if TYPE_CHECKING:
    from .keys import DashboardTiles

log = logging.getLogger(__name__)

__all__ = [
    "CACHE_BYTES",
    "CACHE_ENTRIES",
    "DEFAULT_SIZE",
    "MAX_SIZE",
    "MIN_SIZE",
    "RETRY_INCOMPLETE_S",
    "SCHEMES",
    "TILE_DEADLINE_S",
    "TILE_FETCHES",
    "MapPicture",
    "MapPictures",
    "MapTilesUnavailable",
    "clamp_size",
]

#: The esphome-dashboard firmware's map area on a 1280 × 800 panel with the
#: rail (content 1104 px − the 336 px list − the 20 px gap, 560 px tall).
DEFAULT_SIZE: Final = (748, 560)
MIN_SIZE: Final = (200, 200)
MAX_SIZE: Final = (1920, 1200)
SCHEMES: Final = frozenset({"light", "dark"})
#: Tiles fetched at once for one picture.
TILE_FETCHES: Final = 4
#: A picture drawn with missing tiles is drawn again after this long.
RETRY_INCOMPLETE_S: Final = 60
#: The longest one picture waits for its tiles; the rest are left out.
TILE_DEADLINE_S: Final = 10
CACHE_ENTRIES: Final = 8
CACHE_BYTES: Final = 24 * 1024 * 1024
#: Face pictures are fetched at this size and scaled while painting.
PICTURE_SIZE: Final = 96

PictureSource = Callable[[str], Awaitable[tuple[bytes, str] | None]]


class MapTilesUnavailable(RuntimeError):
    """Not one tile of the picture could be had (``503``)."""


@dataclass(slots=True, frozen=True)
class MapPicture:
    """A painted map: ``complete`` when no tile was missing."""

    etag: str
    png: bytes
    complete: bool
    at: float


def clamp_size(w: int | None, h: int | None) -> tuple[int, int]:
    """A panel's ``?w=&h=`` held to :data:`MIN_SIZE` … :data:`MAX_SIZE`
    (``None`` → :data:`DEFAULT_SIZE`)."""
    width = DEFAULT_SIZE[0] if w is None else min(max(w, MIN_SIZE[0]), MAX_SIZE[0])
    height = DEFAULT_SIZE[1] if h is None else min(max(h, MIN_SIZE[1]), MAX_SIZE[1])
    return width, height


class MapPictures:
    """Paints and keeps wall-panel map pictures."""

    __slots__ = (
        "_avatars",
        "_cache",
        "_cache_bytes",
        "_clock",
        "_emoji",
        "_locks",
        "_paint_gate",
        "_pictures",
        "_tiles",
        "_tiles_failing",
        "_unavailable",
    )

    def __init__(
        self,
        *,
        tiles: DashboardTiles,
        emoji: EmojiImages,
        avatars: AvatarImages,
        pictures: PictureSource,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tiles = tiles
        self._emoji = emoji
        self._avatars = avatars
        self._pictures = pictures
        self._clock = clock
        self._cache: OrderedDict[tuple[str, int, int, str], MapPicture] = OrderedDict()
        self._cache_bytes = 0
        self._locks: dict[tuple[str, int, int, str], asyncio.Lock] = {}
        self._paint_gate = asyncio.Lock()
        self._unavailable: dict[tuple[str, int, int, str], float] = {}
        self._tiles_failing = False

    @property
    def source_tag(self) -> str:
        """Which tile source draws the ground (part of every scene key)."""
        return self._tiles.source_tag

    @property
    def cache_entries(self) -> int:
        return len(self._cache)

    def cached(self, key: str, w: int, h: int, scheme: str) -> MapPicture | None:
        """The kept picture — unless it has gaps and is due again."""
        hit = self._cache.get((key, w, h, scheme))
        if hit is None:
            return None
        if not hit.complete and self._clock() - hit.at >= RETRY_INCOMPLETE_S:
            return None
        self._cache.move_to_end((key, w, h, scheme))
        return hit

    async def render(
        self,
        scene: MapScene,
        key: str,
        *,
        w: int,
        h: int,
        scheme: str,
        palette: MapPalette,
    ) -> MapPicture:
        """Paint ``scene`` (whose :func:`~.map_scene.scene_key` is ``key``)
        at ``w`` × ``h`` — or hand back the kept picture another request
        painted meanwhile. Raises :class:`MapTilesUnavailable` (also for
        :data:`RETRY_INCOMPLETE_S` after a picture found no tile at all)."""
        slot = (key, w, h, scheme)
        # One painting per picture at a time (single flight); different
        # pictures gather their tiles side by side.
        lock = self._locks.setdefault(slot, asyncio.Lock())
        try:
            async with lock:
                hit = self.cached(key, w, h, scheme)
                if hit is not None:
                    return hit
                failed = self._unavailable.get(slot)
                if failed is not None and self._clock() - failed < RETRY_INCOMPLETE_S:
                    raise MapTilesUnavailable("no map tile could be loaded")
                grid = plan_tiles(scene, w, h)
                tiles = await self._fetch_tiles(grid)
                if grid and not tiles:
                    self._warn(len(grid))
                    self._unavailable[slot] = self._clock()
                    raise MapTilesUnavailable("no map tile could be loaded")
                self._unavailable.pop(slot, None)
                pictures, faces_missing = await self._faces(scene)
                emoji = await self._emoji_pngs(scene)
                out = await self._paint(
                    scene,
                    w=w,
                    h=h,
                    palette=palette,
                    dark=scheme == "dark",
                    tiles=tiles,
                    pictures=pictures,
                    emoji=emoji,
                )
                if out.missing:
                    self._warn(len(out.missing))
                else:
                    self._tiles_failing = False
                missing = (
                    tuple(f"{z}/{x}/{y}" for z, x, y in out.missing) + faces_missing
                )
                picture = MapPicture(
                    etag=scene_etag(key, w, h, scheme, missing=missing),
                    png=out.png,
                    complete=not missing,
                    at=self._clock(),
                )
                self._store(slot, picture)
                return picture
        finally:
            if not lock.locked() and self._locks.get(slot) is lock:
                del self._locks[slot]

    async def _paint(self, scene: MapScene, **kw: Any) -> Rendered:
        """:func:`~.map_render.render_map` in a worker thread, one at a
        time. A cancelled request still waits for its painting to end, so
        two never run at once (memory; the shared fonts)."""
        async with self._paint_gate:
            job = asyncio.ensure_future(asyncio.to_thread(render_map, scene, **kw))
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                await asyncio.wait({job})
                raise

    def _warn(self, missing: int) -> None:
        """Once per outage — the tile service logs each upstream failure."""
        if not self._tiles_failing:
            log.warning(
                "household dashboard: map picture drawn without %d tiles "
                "(tile upstream unavailable)",
                missing,
            )
        self._tiles_failing = True

    async def _fetch_tiles(
        self, grid: tuple[TileSpot, ...]
    ) -> dict[tuple[int, int, int], bytes]:
        """The grid's tiles, :data:`TILE_FETCHES` at a time; whatever
        hasn't come within :data:`TILE_DEADLINE_S` is left out (a dead
        upstream answers no faster than its timeout)."""
        gate = asyncio.Semaphore(TILE_FETCHES)
        keys = list(dict.fromkeys((t.z, t.x, t.y) for t in grid))

        async def one(key: tuple[int, int, int]) -> bytes | None:
            async with gate:
                try:
                    return (await self._tiles.fetch(*key)).body
                except TileCoordinateError, TileUnavailableError:
                    return None

        jobs = {asyncio.ensure_future(one(k)): k for k in keys}
        if not jobs:
            return {}
        done, pending = await asyncio.wait(jobs, timeout=TILE_DEADLINE_S)
        for job in pending:
            job.cancel()
        if pending:
            await asyncio.wait(pending)
        return {
            jobs[job]: body
            for job in done
            if not job.cancelled() and (body := job.result()) is not None
        }

    async def _faces(self, scene: MapScene) -> tuple[dict[str, bytes], tuple[str, ...]]:
        """Each shown face's picture as a square PNG, and the faces whose
        picture couldn't be read (``face:<id>`` — drawn with initials,
        the picture is painted again later)."""
        wanted = list(
            dict.fromkeys(
                f.user_id for b in scene.bubbles for f in b.faces if f.picture
            )
        )
        out: dict[str, bytes] = {}
        missing: list[str] = []
        for user_id in wanted:
            found = await self._pictures(user_id)
            if found is None:
                missing.append(f"face:{user_id}")
                continue
            body, picture_hash = found
            tag = hashlib.sha256(picture_hash.encode()).hexdigest()[:32]
            try:
                out[user_id] = await self._avatars.png(body, tag, PICTURE_SIZE)
            except Exception:
                log.warning(
                    "household dashboard: map face transcode failed", exc_info=True
                )
                missing.append(f"face:{user_id}")
        return out, tuple(missing)

    async def _emoji_pngs(self, scene: MapScene) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for code in sorted(scene_emoji(scene)):
            resolved = await self._emoji.resolve(code)
            out[code] = await self._emoji.png(resolved, SOURCE_SIZE)
        return out

    def _store(self, key: tuple[str, int, int, str], picture: MapPicture) -> None:
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= len(old.png)
        self._cache[key] = picture
        self._cache_bytes += len(picture.png)
        while len(self._cache) > CACHE_ENTRIES or (
            self._cache_bytes > CACHE_BYTES and len(self._cache) > 1
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_bytes -= len(evicted.png)
