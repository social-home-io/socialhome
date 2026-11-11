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
   under from that household (``sd-``, direct) or through a GFS (``sg-``),
   or — a page's file (v_82) — its page-scoped copy (``sp-``). Page files are
   served as the page is NOW (cover and body); a file only an edit history
   or an open conflict side shows is not.
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

**Who answers.** Only a household the GFS may ask: for a listed space its
OWNER household, and only for a space it listed on THAT GFS (the GFS knows
both already); for a private space's channel a member household holding a
seat on that GFS. Any other request — a space this household doesn't own,
didn't list there, or a private space — is declined ``not_held`` on the same
path, before any key or crypto is touched, so a GFS can't probe who is a
member or which spaces a household owns. Over its hourly budget the owner
declines ``busy`` (the GFS may then ask the members).

A hint older than
:data:`~socialhome.space_media_hint.HINT_MAX_AGE_S` is refused. Only a file a
LIVE row of the space shows is sent (``live_in_space``): a deleted item's
picture never comes back.

A locator's addresses are remembered for :data:`MISMATCH_KEEP_S`: the same
locator sent again with another blob id is declined without a reseal.

Bounded per GFS AND in all (one GFS, or a reader flooding one, can't spend
this household's budget for the others): :data:`MAX_SERVES_PER_GFS` /
:data:`MAX_CONCURRENT_SERVES` answers at once (past it a request is dropped
before any work), :data:`HOLDER_BYTES_PER_HOUR_PER_GFS` /
:data:`HOLDER_BYTES_PER_HOUR` sent per hour (past it: declined); sealed
blobs kept a few seconds (:data:`SEALED_KEEP_S`, ≤ :data:`SEALED_KEEP_BYTES`)
so tickets for one blob from several cluster nodes seal it once.
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

from ..domain.gfs_space_media import (
    SCOPE_CHANNEL,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
)
from ..domain.gfs_space_media_on_demand import (
    MODE_CACHE,
    InvalidOnDemandWire,
    MediaRequestFrame,
)
from ..domain.space_media_ref import space_page_media_name
from ..space_media_hint import (
    InvalidSpaceMediaHint,
    SpaceMediaHint,
    SpaceMediaLocator,
    open_space_media_hint,
)
from ..space_media_seal import MAX_SPACE_MEDIA_BYTES
from ..task_drain import wait_tasks_done
from .inbound_media_store import (
    gfs_space_media_name,
    is_reserved_media_name,
    is_safe_media_name,
    media_file_path,
    space_direct_media_name,
)
from .space_media_gfs_uploader import KEEP_S, seal_and_address_sync

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ..domain.federation import GfsConnection
    from ..federation.federation_service import FederationService
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_channel_service import GfsChannelService
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_media_gfs_uploader import SpaceMediaGfsUploader

log = logging.getLogger(__name__)

#: Requests answered at once — per GFS, and in all; past either a new one
#: is dropped before any work (the GFS asks the next holder in time).
MAX_SERVES_PER_GFS: int = 2
MAX_CONCURRENT_SERVES: int = 4

#: Bytes this household sends on demand per (sliding) hour — per GFS, and
#: every GFS together; past either a request is declined.
HOLDER_BYTES_PER_HOUR_PER_GFS: int = 512 * 1024**2
HOLDER_BYTES_PER_HOUR: int = 1024**3

#: Decline reasons: the hint did not open / named nothing at this address
#: (the GFS tries another reader's hint); the file is not held here; held,
#: but this household is over its budget right now (a GFS before ``busy``
#: reads any unknown reason as ``not_held`` — fail-soft).
DECLINE_BAD_HINT = "bad_hint"
DECLINE_NOT_HELD = "not_held"
DECLINE_BUSY = "busy"

#: A cache request for one (GFS, blob) attaches the upload authorization at
#: most once in this window (one re-upload per blob).
REUPLOAD_COALESCE_S: float = 600.0

#: Sealed blobs kept for tickets that arrive together (several cluster
#: nodes asking for one blob): how long and how many bytes at most.
SEALED_KEEP_S: float = 30.0

#: A locator that resealed to another address is not resealed again for
#: this long (a key holder crafting hints costs one reseal each).
MISMATCH_KEEP_S: float = 600.0
SEALED_KEEP_BYTES: int = 32 * 1024 * 1024

_HOUR = 3600.0


def _monotonic() -> float:
    return time.monotonic()


class SpaceMediaGfsResponder:
    """Answer a GFS's ``space_media_request`` (module docstring)."""

    __slots__ = (
        "_channels",
        "_crypto",
        "_dir",
        "_federation",
        "_gfs",
        "_gfs_repo",
        "_in_flight",
        "_in_flight_by_gfs",
        "_media_refs",
        "_resealed",
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
        self._in_flight_by_gfs: dict[str, int] = {}
        #: (monotonic time, GFS, bytes) of what was sent in the last hour.
        self._sent: list[tuple[float, str, int]] = []
        #: (GFS, blob) → monotonic time until which no re-upload is attached.
        self._reuploaded: OrderedDict[tuple[str, str], float] = OrderedDict()
        #: Locator (scope, scope id, epoch, item, origin, file name) → (expiry,
        #: the addresses its files resealed to): the same locator asked for
        #: another blob id is declined without a reseal.
        self._resealed: OrderedDict[tuple, tuple[float, frozenset[str]]] = OrderedDict()
        #: v_51 channels: which channel seats this household holds, where.
        self._channels: GfsChannelService | None = None
        #: (scope, scope id, blob) → (monotonic expiry, sealed bytes).
        self._sealed: OrderedDict[tuple[str, str, str], tuple[float, bytes]] = (
            OrderedDict()
        )

    def attach_federation(self, federation: FederationService) -> None:
        self._federation = federation

    def attach_channels(self, channels: GfsChannelService) -> None:
        self._channels = channels

    async def handle(self, frame: dict, *, gfs_id: str) -> None:
        """One ``space_media_request`` from GFS *gfs_id* (the WS client's
        handler). Never raises; the work runs in the background."""
        try:
            req = MediaRequestFrame.from_frame(frame)
        except InvalidOnDemandWire as exc:
            log.debug("space media request from %s dropped: %s", gfs_id, exc)
            return
        if not self._has_room(gfs_id):
            # Dropped before any work (no task, no signature, no request):
            # a GFS flooding requests costs nothing; it asks someone else.
            log.info("space media request from %s: answers in flight — dropped", gfs_id)
            return
        self._enter(gfs_id)
        task = asyncio.create_task(self._answer(req, gfs_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every answer in progress (tests, the demo)."""
        await wait_tasks_done(lambda: self._tasks)

    async def stop(self) -> None:
        if not self._tasks:
            return
        _done, pending = await asyncio.wait(list(self._tasks), timeout=5.0)
        for task in pending:
            task.cancel()

    # ── Internals ────────────────────────────────────────────────────────

    async def _answer(self, req: MediaRequestFrame, gfs_id: str) -> None:
        try:
            conn = await self._gfs_repo.get(gfs_id)
            if conn is None or conn.status != "active" or not conn.inbox_url:
                return
            blob, space_id, reason = await self._find(req, conn)
            if blob is not None and not self._budget_allows(conn.id, len(blob)):
                # Busy, not "not held": the GFS may ask the space's members.
                blob, reason = None, DECLINE_BUSY
            if blob is None:
                await self._gfs.serve_space_media(
                    conn, req.ticket, None, decline_reason=reason
                )
                return
            upload_auth = await self._upload_auth(
                req.mode,
                req.scope,
                req.scope_id,
                req.blob_id,
                conn,
                blob,
                lambda claim: self._uploader.authorize(claim, space_id),
            )
            # Charged BEFORE the bytes go: a GFS that reads the body and then
            # answers 409 / 422 / 429 still spent this household's budget.
            charge = self._charge(conn.id, len(blob))
            status = await self._gfs.serve_space_media(
                conn, req.ticket, blob, upload_auth=upload_auth
            )
            if status is None:
                # Nothing left this household (no connection): refunded.
                self._refund(charge)
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
            self._leave(gfs_id)

    def _has_room(self, gfs_id: str) -> bool:
        return (
            self._in_flight < MAX_CONCURRENT_SERVES
            and self._in_flight_by_gfs.get(gfs_id, 0) < MAX_SERVES_PER_GFS
        )

    def _enter(self, gfs_id: str) -> None:
        self._in_flight += 1
        self._in_flight_by_gfs[gfs_id] = self._in_flight_by_gfs.get(gfs_id, 0) + 1

    def _leave(self, gfs_id: str) -> None:
        self._in_flight -= 1
        left = self._in_flight_by_gfs.get(gfs_id, 1) - 1
        if left > 0:
            self._in_flight_by_gfs[gfs_id] = left
        else:
            self._in_flight_by_gfs.pop(gfs_id, None)

    async def _find(
        self, req: MediaRequestFrame, conn: GfsConnection
    ) -> tuple[bytes | None, str, str]:
        """``(blob, space id, decline reason)``: the blob the request names,
        re-sealed from a file of this household that a live row of the space
        shows — or ``None`` and why (:data:`DECLINE_BAD_HINT`: the hint
        doesn't open or names nothing at this address; else
        :data:`DECLINE_NOT_HELD`)."""
        space_id = await self._space_of(req.scope, req.scope_id, conn)
        if space_id is None:
            # Not a household this GFS may ask: the same answer as a miss,
            # before anything is opened.
            return None, "", DECLINE_NOT_HELD
        blob, reason = await self._find_in(
            space_id, req.scope, req.scope_id, req.blob_id, req.hint
        )
        return blob, space_id, reason

    async def _find_in(
        self,
        space_id: str,
        scope: str,
        scope_id: str,
        blob_id: str,
        hint: SpaceMediaHint,
    ) -> tuple[bytes | None, str]:
        """The file-finding rules every answer shares (owner, seat holder,
        member): open *hint* with the space media key of its epoch, look
        only at the names this household computes (:meth:`_candidates`),
        each confined to the space and live in it, reseal, and keep it only
        at exactly *blob_id*."""
        current = await self._crypto.media_key(space_id, epoch=hint.epoch)
        if current is None:
            return None, DECLINE_BAD_HINT
        key = current[1]
        try:
            loc = open_space_media_hint(
                hint,
                media_key=key,
                scope=scope,
                scope_id=scope_id,
                blob_id=blob_id,
            )
        except InvalidTag, InvalidSpaceMediaHint:
            log.info("space media request: hint not for this space — declined")
            return None, DECLINE_BAD_HINT
        if not loc.fresh():
            log.info("space media request: a stale hint — declined")
            return None, DECLINE_BAD_HINT
        sealed_key = (scope, scope_id, blob_id)
        cached = self._sealed_get(sealed_key)
        if cached is not None:
            return cached, ""
        locator = (
            scope,
            scope_id,
            hint.epoch,
            loc.item_id,
            loc.origin,
            loc.filename,
        )
        known = self._resealed.get(locator)
        if known is not None and known[0] > _monotonic() and blob_id not in known[1]:
            # This locator's files reseal to other addresses: no new reseal
            # for a made-up blob id.
            return None, DECLINE_BAD_HINT
        held_any = False
        addresses: set[str] = set()
        # v_85 — a hashed file is held under its content name, which says
        # nothing of the author's file name: the item's content-addressed
        # files are candidates too (the sealed address picks the one).
        content_names = await self._media_refs.content_names_of_item(loc.item_id)
        for name in [*self._candidates(space_id, loc), *content_names]:
            if not await self._media_refs.confined_to_space(
                name, space_id
            ) or not await self._media_refs.live_in_space(name, space_id):
                continue
            data = await self._read(name)
            if data is None:
                continue
            held_any = True
            blob, address = await asyncio.to_thread(
                seal_and_address_sync,
                key,
                space_id,
                hint.epoch,
                loc.item_id,
                loc.origin,
                loc.filename,
                data,
                scope,
                scope_id,
            )
            addresses.add(address)
            if address == blob_id:
                self._remember_locator(locator, addresses)
                self._sealed_put(sealed_key, blob)
                return blob, ""
        if held_any:
            self._remember_locator(locator, addresses)
            return None, DECLINE_BAD_HINT
        return None, DECLINE_NOT_HELD

    def _remember_locator(self, locator: tuple, addresses: set[str]) -> None:
        self._resealed[locator] = (
            _monotonic() + MISMATCH_KEEP_S,
            frozenset(addresses),
        )
        self._resealed.move_to_end(locator)
        while len(self._resealed) > 4096:
            self._resealed.popitem(last=False)

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
        # v_82 — a member's page-scoped copy of a page's file (a page keeps
        # its host's names; the copy is bound to the space by its name).
        names.append(space_page_media_name(space_id, loc.filename))
        return names

    async def _space_of(
        self, scope: str, scope_id: str, conn: GfsConnection
    ) -> str | None:
        """The space this household may be asked for under the scope by
        *conn*'s GFS: a space it OWNS and LISTED on that GFS (the only
        household that GFS asks for it), or the space of a private channel it
        holds a seat for on that GFS. ``None`` otherwise — every such case
        the same way, before any crypto."""
        if scope == SCOPE_CHANNEL:
            spaces = await self._spaces.spaces_for_gfs_channel(scope_id)
            space_id = spaces[0] if len(spaces) == 1 else ""
            grant = (
                await self._channels.current_grant(space_id)
                if self._channels is not None and space_id
                else None
            )
            if (
                grant is None
                or grant.channel_id != scope_id
                or not self._gfs.known_instance_ids(conn) & set(grant.gfs_ids)
            ):
                return None
            return space_id
        own = self._federation.own_instance_id if self._federation else ""
        space = await self._spaces.get(scope_id)
        listed = await self._gfs_repo.list_publications_for_space(scope_id)
        if (
            space is None
            or not own
            or space.owner_instance_id != own
            or not any(
                p.gfs_connection_id == conn.id and p.status == "active" for p in listed
            )
        ):
            return None
        return scope_id

    async def _read(self, name: str) -> bytes | None:
        path = media_file_path(self._dir, name)
        if path is None or not await aiofiles.os.path.isfile(path):
            return None
        if (await aiofiles.os.stat(path)).st_size > MAX_SPACE_MEDIA_BYTES:
            return None
        async with aiofiles.open(path, "rb") as fh:
            return await fh.read()

    async def _upload_auth(
        self,
        mode: str,
        scope: str,
        scope_id: str,
        blob_id: str,
        conn: GfsConnection,
        blob: bytes,
        authorize: Callable[[SpaceMediaClaim], Awaitable[SpaceMediaUploadAuth | None]],
    ) -> str | None:
        """The upload authorization (from *authorize*) for a cache request —
        once per (GFS, blob) per :data:`REUPLOAD_COALESCE_S`, whoever asked
        (owner and member paths share the window); ``None`` streams only."""
        if mode != MODE_CACHE:
            return None
        now = _monotonic()
        key = (conn.id, blob_id)
        if self._reuploaded.get(key, 0.0) > now:
            return None
        wall = int(time.time())
        claim = SpaceMediaClaim(
            scope=scope,
            scope_id=scope_id,
            blob_id=blob_id,
            size=len(blob),
            expires_at=wall + KEEP_S,
            ts=wall,
        )
        auth = await authorize(claim)
        if auth is None:
            return None
        self._reuploaded[key] = now + REUPLOAD_COALESCE_S
        self._reuploaded.move_to_end(key)
        while len(self._reuploaded) > 4096:
            self._reuploaded.popitem(last=False)
        return auth.to_header()

    def _budget_allows(self, gfs_id: str, size: int) -> bool:
        cutoff = _monotonic() - _HOUR
        self._sent = [x for x in self._sent if x[0] > cutoff]
        total = sum(n for _, _, n in self._sent)
        mine = sum(n for _, g, n in self._sent if g == gfs_id)
        return (
            total + size <= HOLDER_BYTES_PER_HOUR
            and mine + size <= HOLDER_BYTES_PER_HOUR_PER_GFS
        )

    def _charge(self, gfs_id: str, size: int) -> tuple[float, str, int]:
        entry = (_monotonic(), gfs_id, size)
        self._sent.append(entry)
        return entry

    def _refund(self, entry: tuple[float, str, int]) -> None:
        try:
            self._sent.remove(entry)
        except ValueError:  # pragma: no cover - already aged out
            pass

    def _sealed_get(self, key: tuple[str, str, str]) -> bytes | None:
        now = _monotonic()
        for old in [k for k, (exp, _) in self._sealed.items() if exp <= now]:
            self._sealed.pop(old, None)
        hit = self._sealed.get(key)
        return hit[1] if hit is not None else None

    def _sealed_put(self, key: tuple[str, str, str], blob: bytes) -> None:
        self._sealed[key] = (_monotonic() + SEALED_KEEP_S, blob)
        while sum(len(b) for _, b in self._sealed.values()) > SEALED_KEEP_BYTES:
            self._sealed.popitem(last=False)
