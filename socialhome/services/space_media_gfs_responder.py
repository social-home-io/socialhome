"""Space media on demand, the holder side (v_83).

A GFS keeps space blobs only as a short-lived cache. When a reader misses
one, the GFS asks ONE household that holds the file over its socket
(``space_media_request``, :class:`~socialhome.domain.gfs_space_media_on_demand
.MediaRequestFrame`): send the same deterministic ciphertext to the ticket
it names, to be piped to the waiting reader(s) and — ``mode: "cache"`` —
kept in its cache again. This service answers that:

1. **opens the reader's hint** (:mod:`socialhome.space_media_hint`) with the
   space media key of the epoch it names — the GFS can't read it, and it
   names no reader — to learn the item, the sealing household and the file
   name the descriptor used;
2. **finds the file** among the names THIS household computes for it: the
   name itself when this household sealed it, else the name it got the file
   under from that household (``sd-``, direct) or through a GFS (``sg-``).
   A name another receiving path owns (DM / moment) is never one; each
   candidate must be referenced only by this space's content
   (``confined_to_space``) — a household is never made to send a file the
   space doesn't reference;
3. **re-seals it** (deterministic: byte-identical to the blob readers'
   descriptors name) and sends it only when it sits at exactly the address
   asked — which a reader can know only from a real descriptor of that file;
4. **serves or declines** (``PUT /gfs/media/serve/{ticket}`` or
   ``…/decline``), attaching this household's normal upload authorization
   (:meth:`.space_media_gfs_uploader.SpaceMediaGfsUploader.authorize`) for a
   cache request — at most once per (GFS, blob) per
   :data:`REUPLOAD_COALESCE_S`: one re-upload per blob however many readers
   asked; later requests in the window stream only.

Bounded: :data:`MAX_CONCURRENT_SERVES` at once, :data:`HOLDER_BYTES_PER_HOUR`
sent per hour across every GFS (past either: decline), sealed blobs kept a
few seconds (:data:`SEALED_KEEP_S`, ≤ :data:`SEALED_KEEP_BYTES`) so tickets
for one blob from several cluster nodes seal it once.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os
from cryptography.exceptions import InvalidTag

from ..domain.gfs_space_media import SCOPE_CHANNEL, SpaceMediaClaim
from ..domain.gfs_space_media_on_demand import (
    MODE_CACHE,
    InvalidOnDemandWire,
    MediaRequestFrame,
)
from ..space_media_hint import (
    InvalidSpaceMediaHint,
    SpaceMediaLocator,
    open_space_media_hint,
)
from ..space_media_seal import MAX_SPACE_MEDIA_BYTES
from .inbound_media_store import (
    gfs_space_media_name,
    is_reserved_media_name,
    is_safe_media_name,
    media_file_path,
    space_direct_media_name,
)
from .space_media_gfs_uploader import KEEP_S, seal_and_address_sync

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..federation.federation_service import FederationService
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_media_gfs_uploader import SpaceMediaGfsUploader

log = logging.getLogger(__name__)

#: Requests served at once; past it a new one is declined.
MAX_CONCURRENT_SERVES: int = 2

#: Bytes this household sends on demand per (sliding) hour, every GFS
#: together; past it a request is declined.
HOLDER_BYTES_PER_HOUR: int = 1024**3

#: A cache request for one (GFS, blob) attaches the upload authorization at
#: most once in this window (one re-upload per blob).
REUPLOAD_COALESCE_S: float = 600.0

#: Sealed blobs kept for tickets that arrive together (several cluster
#: nodes asking for one blob): how long and how many bytes at most.
SEALED_KEEP_S: float = 30.0
SEALED_KEEP_BYTES: int = 32 * 1024 * 1024

_HOUR = 3600.0


def _monotonic() -> float:
    return time.monotonic()


class SpaceMediaGfsResponder:
    """Answer a GFS's ``space_media_request`` (module docstring)."""

    __slots__ = (
        "_crypto",
        "_dir",
        "_federation",
        "_gfs",
        "_gfs_repo",
        "_in_flight",
        "_media_refs",
        "_reuploaded",
        "_sealed",
        "_sent",
        "_spaces",
        "_tasks",
        "_uploader",
    )

    def __init__(
        self,
        *,
        gfs_repo: AbstractGfsConnectionRepo,
        gfs_service: GfsConnectionService,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        media_refs: AbstractMediaReferenceRepo,
        uploader: SpaceMediaGfsUploader,
        media_dir: pathlib.Path,
    ) -> None:
        self._gfs_repo = gfs_repo
        self._gfs = gfs_service
        self._spaces = space_repo
        self._crypto = space_crypto
        self._media_refs = media_refs
        self._uploader = uploader
        self._dir = pathlib.Path(media_dir)
        #: Knows this household's own id (the sealer of its own files).
        self._federation: FederationService | None = None
        self._tasks: set[asyncio.Task] = set()
        self._in_flight = 0
        #: (monotonic time, bytes) of what was sent in the last hour.
        self._sent: list[tuple[float, int]] = []
        #: (GFS, blob) → monotonic time until which no re-upload is attached.
        self._reuploaded: OrderedDict[tuple[str, str], float] = OrderedDict()
        #: blob → (monotonic expiry, sealed bytes).
        self._sealed: OrderedDict[str, tuple[float, bytes]] = OrderedDict()

    def attach_federation(self, federation: FederationService) -> None:
        self._federation = federation

    async def handle(self, frame: dict, *, gfs_id: str) -> None:
        """One ``space_media_request`` from GFS *gfs_id* (the WS client's
        handler). Never raises; the work runs in the background."""
        try:
            req = MediaRequestFrame.from_frame(frame)
        except InvalidOnDemandWire as exc:
            log.debug("space media request from %s dropped: %s", gfs_id, exc)
            return
        task = asyncio.create_task(self._answer(req, gfs_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every answer in progress (tests, the demo)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def stop(self) -> None:
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(list(self._tasks), timeout=5.0)
        for task in pending:
            task.cancel()

    # ── Internals ────────────────────────────────────────────────────────

    async def _answer(self, req: MediaRequestFrame, gfs_id: str) -> None:
        conn = await self._gfs_repo.get(gfs_id)
        if conn is None or conn.status != "active" or not conn.inbox_url:
            return
        if self._in_flight >= MAX_CONCURRENT_SERVES:
            log.info(
                "space media request: %d answers in flight — declined", self._in_flight
            )
            await self._gfs.serve_space_media(conn, req.ticket, None)
            return
        self._in_flight += 1
        try:
            blob, space_id = await self._find(req)
            if blob is None or not self._budget_allows(len(blob)):
                await self._gfs.serve_space_media(conn, req.ticket, None)
                return
            upload_auth = await self._upload_auth(req, conn, blob, space_id)
            self._charge(len(blob))
            status = await self._gfs.serve_space_media(
                conn, req.ticket, blob, upload_auth=upload_auth
            )
            log.info(
                "space media %s: %s on demand to GFS %s (%d bytes, HTTP %s)",
                req.blob_id[:12],
                "re-uploaded" if upload_auth else "streamed",
                conn.id,
                len(blob),
                status,
            )
            if upload_auth and status == 200:
                await self._uploader.remember_stored(conn, req.blob_id)
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "space media request for %s failed", req.blob_id[:12], exc_info=True
            )
        finally:
            self._in_flight -= 1

    async def _find(self, req: MediaRequestFrame) -> tuple[bytes | None, str]:
        """The blob the request names, re-sealed from a file of this
        household that the space references — or ``None``."""
        space_id = await self._space_of(req.scope, req.scope_id)
        if space_id is None:
            return None, ""
        cached = self._sealed_get(req.blob_id)
        if cached is not None:
            return cached, space_id
        current = await self._crypto.media_key(space_id, epoch=req.hint.epoch)
        if current is None:
            return None, space_id
        key = current[1]
        try:
            loc = open_space_media_hint(
                req.hint,
                media_key=key,
                scope=req.scope,
                scope_id=req.scope_id,
                blob_id=req.blob_id,
            )
        except InvalidTag, InvalidSpaceMediaHint:
            log.info("space media request: hint not for this space — declined")
            return None, space_id
        for name in self._candidates(space_id, loc):
            if not await self._media_refs.confined_to_space(name, space_id):
                continue
            data = await self._read(name)
            if data is None:
                continue
            blob, address = await asyncio.to_thread(
                seal_and_address_sync,
                key,
                space_id,
                req.hint.epoch,
                loc.item_id,
                loc.origin,
                loc.filename,
                data,
                req.scope,
                req.scope_id,
            )
            if address == req.blob_id:
                self._sealed_put(req.blob_id, blob)
                return blob, space_id
        return None, space_id

    def _candidates(self, space_id: str, loc: SpaceMediaLocator) -> list[str]:
        """The local names this household may hold the file under — computed
        here, never taken from the wire (except the sealer's own name, when
        this household is the sealer, which must be no reserved name)."""
        names: list[str] = []
        own = self._federation.own_instance_id if self._federation else ""
        if (
            own
            and loc.origin == own
            and is_safe_media_name(loc.filename)
            and not is_reserved_media_name(loc.filename)
        ):
            names.append(loc.filename)
        names.append(space_direct_media_name(space_id, loc.origin, loc.filename))
        names.append(
            gfs_space_media_name(space_id, loc.origin, loc.item_id, loc.filename)
        )
        return names

    async def _space_of(self, scope: str, scope_id: str) -> str | None:
        """The space this household knows the scope as (it must be in it)."""
        if scope == SCOPE_CHANNEL:
            spaces = await self._spaces.spaces_for_gfs_channel(scope_id)
            return spaces[0] if len(spaces) == 1 else None
        return scope_id if await self._spaces.get(scope_id) is not None else None

    async def _read(self, name: str) -> bytes | None:
        path = media_file_path(self._dir, name)
        if path is None or not await aiofiles.os.path.isfile(path):
            return None
        if (await aiofiles.os.stat(path)).st_size > MAX_SPACE_MEDIA_BYTES:
            return None
        async with aiofiles.open(path, "rb") as fh:
            return await fh.read()

    async def _upload_auth(
        self, req: MediaRequestFrame, conn: GfsConnection, blob: bytes, space_id: str
    ) -> str | None:
        """The upload authorization for a cache request — once per (GFS,
        blob) per :data:`REUPLOAD_COALESCE_S`; ``None`` streams only."""
        if req.mode != MODE_CACHE:
            return None
        now = _monotonic()
        key = (conn.id, req.blob_id)
        if self._reuploaded.get(key, 0.0) > now:
            return None
        wall = int(time.time())
        claim = SpaceMediaClaim(
            scope=req.scope,
            scope_id=req.scope_id,
            blob_id=req.blob_id,
            size=len(blob),
            expires_at=wall + KEEP_S,
            ts=wall,
        )
        auth = await self._uploader.authorize(claim, space_id)
        if auth is None:
            return None
        self._reuploaded[key] = now + REUPLOAD_COALESCE_S
        self._reuploaded.move_to_end(key)
        while len(self._reuploaded) > 4096:
            self._reuploaded.popitem(last=False)
        return auth.to_header()

    def _budget_allows(self, size: int) -> bool:
        cutoff = _monotonic() - _HOUR
        self._sent = [(t, n) for t, n in self._sent if t > cutoff]
        return sum(n for _, n in self._sent) + size <= HOLDER_BYTES_PER_HOUR

    def _charge(self, size: int) -> None:
        self._sent.append((_monotonic(), size))

    def _sealed_get(self, blob_id: str) -> bytes | None:
        now = _monotonic()
        for bid in [b for b, (exp, _) in self._sealed.items() if exp <= now]:
            self._sealed.pop(bid, None)
        hit = self._sealed.get(blob_id)
        return hit[1] if hit is not None else None

    def _sealed_put(self, blob_id: str, blob: bytes) -> None:
        self._sealed[blob_id] = (_monotonic() + SEALED_KEEP_S, blob)
        while sum(len(b) for _, b in self._sealed.values()) > SEALED_KEEP_BYTES:
            self._sealed.popitem(last=False)
