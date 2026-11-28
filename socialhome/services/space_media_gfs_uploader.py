"""Space files for households reached only through a GFS — the sender side.

A space member seated from an invite link (``InstanceSource.SPACE_SESSION``)
and a follower of a global space read the space through a GFS, whose
envelope relay is far too small for pictures. So when content with local
media is created in a space that has such readers, this household:

1. **plans** (:meth:`SpaceMediaGfsUploader.plan`, or
   :meth:`~SpaceMediaGfsUploader.plan_for_reader` for one §25.6 catch-up
   requester): finds the GFSs those readers use — each link-joined v_80
   member's ``relay_via`` connection, and (for followers) every GFS the
   space's opaque channel lives on — keeps the ones whose signed
   ``space_media`` capability lists the ``channel`` scope, and describes each file of the item
   (:func:`~socialhome.domain.space_media_ref.record_media_urls`, ≤ 8 MiB):
   its deterministic sealed blob's scope-bound address (``sm1``), suite,
   epoch, size, scope and ``addr_suite``. Only a file that is no reserved
   (DM / moment) name and that nothing but this space's content references
   (``MediaReferenceRepo.confined_to_space``) is ever described. The caller
   puts that ``media_refs`` descriptor inside the item's encrypted payload.
   No reader, no channel (since v_96 the GFS keeps space media only under a
   space's opaque channel), or no upload authorization here → no plan, no
   sealing.
2. **uploads** (:meth:`SpaceMediaGfsUploader.upload`) in the background: seals
   each file once more (deterministic: the same bytes, so no sealed copy is
   kept in memory meanwhile; the file is re-checked as the space's own) and
   PUTs it once per target GFS to the one scope-free path
   ``PUT /gfs/media/{blob_id}`` — the scope rides only inside the signed
   claim — authorized as the GFS requires: the claim names the space's
   opaque channel (v_51 private, v_96 global); the channel key signs for a
   seed holder, else the member's channel writer key from its grant. The
   GFS learns the channel id, never the space; nothing names the household.

**Files over one blob (v_97, ``sm1p``).** A file larger than 8 MiB (up to
the 200 MiB upload cap) is split into fixed 8 MiB pieces, each sealed like a
file under its piece name (:func:`~socialhome.space_media_seal
.piece_aad_name`). Its ONE descriptor entry names the file's address — which
commits to the SHA-256 of every sealed piece — its size and the whole
file's SHA-256; each piece's address is derived from the file address and
its index (:mod:`socialhome.domain.gfs_space_media`). It goes only to a GFS
proving ``space_media.pieces``; with none, its author is told
("too large to send").

**Uploads are durable (v_97).** Every blob — or piece — for every target
GFS is ONE row of the durable item queue (``space_item_outbox``,
:mod:`.space_item_outbox`), kind :data:`~socialhome.domain.space_item_outbox
.MEDIA_UPLOAD_ROW`: a small locator (the file, the item, the epoch, a
piece's index and the file's piece manifest), never the bytes — a row's
blob is sealed again when it goes (deterministic: the same bytes).
:meth:`SpaceMediaGfsUploader.deliver` makes one attempt: ``200
already_stored`` / ``201`` is delivered (remembered per (GFS, blob) until
min(:data:`KEEP_S`, that GFS's TTL)); a transport error, 408, 429 or 5xx
waits and goes again (the queue's backoff, no limit — a restart loses
nothing); a refusal (403 / 413 / 422) is shown to the author; a GFS that
no longer takes the space's media drops its copy. A file deleted or changed
meanwhile is not sent (the item no longer shows it).

Cost per item: one upload per file (or piece) per GFS (never per reader).

"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import pathlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import aiofiles
import aiofiles.os
import orjson

from ..crypto import b64url_decode, b64url_encode, sign_ed25519
from ..domain.federation import InstanceSource
from ..domain.federation_capabilities import FederationCapability
from ..domain.gfs_channel import CHANNEL_SUITE_ED25519
from ..domain.gfs_space_media import (
    ADDR_SUITE_SM1,
    ADDR_SUITE_SM1P,
    AUTH_CHANNEL,
    AUTH_WRITER_KEY,
    MAX_PIECED_FILE_BYTES,
    PIECE_BYTES,
    SCOPE_CHANNEL,
    PieceProof,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    piece_address,
    piece_count,
    pieced_file_address,
    space_media_address,
)
from ..domain.media_constraints import (
    MEDIA_REF_PREFIX,
    is_reserved_media_name,
    local_media_ref,
)
from ..domain.space_item_outbox import (
    MEDIA_UPLOAD_ROW,
    UNPLANNED_GFS,
    UNSENT_REFUSED,
    UNSENT_TOO_LARGE,
    QueuedSpaceItem,
)
from ..domain.space_media_ref import (
    MAX_PAGE_DESCRIPTOR_ENTRIES,
    SpaceMediaRef,
    media_refs_to_wire,
)
from ..gfs_channel import channel_pk_of, derive_channel_seed
from ..moment_public_media import is_blob_id
from ..space_media_seal import (
    MAX_SPACE_MEDIA_BYTES,
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    piece_aad_name,
    seal_space_media,
)
from ..writer_key import sign_with_writer_key
from .gfs_channel_service import channel_on
from .gfs_publish_retry import PublishOutcome
from .inbound_media_store import media_file_path

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..domain.space import Space
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_channel_service import GfsChannelService
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_item_outbox import SpaceItemOutbox

log = logging.getLogger(__name__)


class _DescribeFetcher(Protocol):
    async def ensure_local(self, filename: str, *, timeout_s: float) -> bool: ...

    def fetching(self, filename: str) -> bool: ...


#: Files described per item: the largest descriptor any item carries (a
#: page version with its open sides; a post shows ≤ 10 pictures + a file +
#: a card).
MAX_FILES_PER_ITEM: int = MAX_PAGE_DESCRIPTOR_ENTRIES

#: Piece manifests remembered per file address (a row carries its own; a
#: holder asked for a piece on demand reads it from here first).
MAX_MANIFESTS: int = 256

#: How long the GFS is asked to keep a blob (it clamps to its own TTL).
KEEP_S: int = 7 * 86_400

#: (GFS, blob) pairs remembered as stored, so a repeat skips the PUT — each
#: until min(KEEP_S, that GFS's TTL), after which the GFS may have dropped
#: the blob and a repeat sends it again.
MAX_REMEMBERED: int = 4096

#: Descriptors remembered per (space, epoch, file, size, mtime), so one item
#: planned by two senders (the member broadcast and the seed holder's
#: follower relay) or re-streamed in a catch-up is sealed once. A lazy
#: catch-up describes every file of a GFS space for its requester (v_97);
#: the address does not depend on who asks, so the next requester of the
#: same epoch costs a lookup. ~600 B an entry: ~10 MB at the bound.
MAX_DESCRIBED: int = 16_384

#: v_97 — lazy catch-up streams describing (sealing) at once on this
#: household; the rest wait their turn (one worker thread each).
MAX_DESCRIBING_STREAMS: int = 2

#: v_97 — a file this household holds only as a GFS descriptor (never
#: fetched: a video or file nobody viewed here) is fetched before it is
#: described again — at most this many such fetches per space at once (the
#: rest wait), each waited for at most :data:`DESCRIBE_FETCH_WAIT_S` (the
#: fetch goes on in the background). Owner 2026-11-27: no loss.
MAX_DESCRIBE_FETCHES_PER_SPACE: int = 2
DESCRIBE_FETCH_WAIT_S: float = 20.0

#: v_97 (review H3a) — a file a lazy catch-up could not fetch to describe
#: (no GFS held it) holds its record back like one still on its way, for
#: this many catch-up streams (counted once per stream, Fable I2); then
#: the record goes undescribed with a WARNING, so a lost blob never stalls a
#: requester's catch-up. At most MAX_DESCRIBE_MISSES_REMEMBERED (space,
#: file) counts are kept; a new miss past that goes undescribed at once (a
#: forgotten count would defer it forever).
MAX_DESCRIBE_MISS_STREAMS: int = 5
MAX_DESCRIBE_MISSES_REMEMBERED: int = 4096

_STATUS_STORED = frozenset({200, 201})

#: ``_put``'s answer when this household holds no authorization for the
#: space (no space seed, no channel key or grant) — nothing was sent; not a
#: GFS status.
_NO_AUTHORIZATION = -1
_STATUS_RETRY = frozenset({408, 429})


def seal_file_sync(
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
    data: bytes,
) -> bytes:
    """:func:`seal_space_media` positionally — the worker-thread body."""
    return seal_space_media(
        media_key=media_key,
        space_id=space_id,
        epoch=epoch,
        item_id=item_id,
        origin=origin,
        filename=filename,
        data=data,
    )


def _monotonic() -> float:
    return time.monotonic()


def seal_and_address_sync(
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
    data: bytes,
    scope: str,
    scope_id: str,
) -> tuple[bytes, str]:
    """The sealed blob and its scope-bound GFS address
    (:func:`~socialhome.domain.gfs_space_media.space_media_address`, suite
    ``sm1``) — both CPU work, run together in a worker thread."""
    blob = seal_file_sync(media_key, space_id, epoch, item_id, origin, filename, data)
    return blob, space_media_address(scope, scope_id, blob)


@dataclass(slots=True, frozen=True)
class PiecedFile:
    """A file sealed in pieces (v_97, ``sm1p``): its address (bound to the
    scope and every piece), the whole file's SHA-256, the SHA-256 of each
    sealed piece (the manifest a piece upload proves itself with) and its
    size. Service-local DTO."""

    address: str
    sha256: str
    manifest: tuple[str, ...]
    size: int


def describe_pieced_sync(
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
    path: str,
    scope: str,
    scope_id: str,
) -> PiecedFile | None:
    """Seal every piece of the file at *path* once (8 MiB at a time, never
    the whole file in memory) and describe it — ``None`` when its size is
    no pieced size. The worker-thread body."""
    size = os.path.getsize(path)
    try:
        count = piece_count(size)
    except ValueError:
        return None
    whole = hashlib.sha256()
    manifest: list[str] = []
    with open(path, "rb") as fh:
        for index in range(count):
            data = fh.read(PIECE_BYTES)
            whole.update(data)
            blob = seal_file_sync(
                media_key,
                space_id,
                epoch,
                item_id,
                origin,
                piece_aad_name(filename, index, count),
                data,
            )
            manifest.append(hashlib.sha256(blob).hexdigest())
    return PiecedFile(
        address=pieced_file_address(scope, scope_id, manifest),
        sha256=whole.hexdigest(),
        manifest=tuple(manifest),
        size=size,
    )


def seal_piece_sync(
    media_key: bytes,
    space_id: str,
    epoch: int,
    item_id: str,
    origin: str,
    filename: str,
    path: str,
    index: int,
    count: int,
) -> bytes:
    """Piece *index* of *count* of the file at *path*, sealed (the same
    bytes every time). The worker-thread body."""
    with open(path, "rb") as fh:
        fh.seek(index * PIECE_BYTES)
        data = fh.read(PIECE_BYTES)
    return seal_file_sync(
        media_key,
        space_id,
        epoch,
        item_id,
        origin,
        piece_aad_name(filename, index, count),
        data,
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def media_names(urls: Iterable[str | None]) -> list[str]:
    """The local file names *urls* reference (``api/media/<name>``), in
    order, without duplicates; anything else is ignored."""
    out: list[str] = []
    for url in urls:
        ref = local_media_ref(url)
        if ref is None:
            continue
        name = ref[len(MEDIA_REF_PREFIX) :]
        if name not in out:
            out.append(name)
    return out


@dataclass(slots=True, frozen=True)
class SpaceMediaPlan:
    """What one item's files need: the descriptor entries and the GFSs the
    blobs go to. Service-local DTO."""

    space_id: str
    refs: tuple[SpaceMediaRef, ...]
    targets: tuple[GfsConnection, ...]
    #: v_97 — described only, never uploaded (a catch-up's older files:
    #: a reader fetches on view, a miss on demand from a holder, v_83).
    lazy: bool = False
    #: v_97 — files still on their way here from the GFS (held only as a
    #: descriptor, fetch running): not described yet. A catch-up holds such
    #: a record back for its next stream.
    pending: tuple[str, ...] = ()

    def wire(self) -> dict[str, dict]:
        """The ``media_refs`` descriptor."""
        return media_refs_to_wire(self.refs)


class SpaceMediaGfsUploader:
    """Plan and upload a space item's files to the GFSs its readers use."""

    __slots__ = (
        "_acting_hosts",
        "_channels",
        "_crypto",
        "_describe_misses",
        "_described",
        "_describing",
        "_dir",
        "_federation",
        "_federation_repo",
        "_fetcher",
        "_fetching",
        "_gfs",
        "_legacy_window",
        "_manifests",
        "_media_refs",
        "_outbox",
        "_pieced",
        "_spaces",
        "_stored",
        "_stored_names",
        "_unauthorized",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        federation_repo: AbstractFederationRepo,
        gfs_service: GfsConnectionService,
        media_refs: AbstractMediaReferenceRepo,
        media_dir: pathlib.Path,
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._federation_repo = federation_repo
        self._gfs = gfs_service
        #: Which files are only this space's content (``confined_to_space``).
        self._media_refs = media_refs
        self._dir = pathlib.Path(media_dir)
        self._federation: FederationService | None = None
        self._acting_hosts: Callable[[Space], Awaitable[list[str]]] | None = None
        #: v_51 opaque channels — a private space's member grants.
        self._channels: GfsChannelService | None = None
        #: v_97 — the durable queue every upload waits in.
        self._outbox: SpaceItemOutbox | None = None
        #: v_97 — whether a space's 14-day legacy window is open.
        self._legacy_window: Callable[[str], Awaitable[bool]] | None = None
        #: Spaces already warned about lacking any upload authorization.
        self._unauthorized: set[str] = set()
        #: v_97 — file address → its piece manifest (:data:`MAX_MANIFESTS`).
        self._manifests: OrderedDict[str, tuple[str, ...]] = OrderedDict()
        #: v_97 — a holder's described pieced files, by what they were
        #: sealed for and the file's (size, mtime) (:data:`MAX_MANIFESTS`).
        self._pieced: OrderedDict[tuple, PiecedFile] = OrderedDict()
        #: (GFS, blob) → monotonic time until which the GFS holds it.
        self._stored: OrderedDict[tuple[str, str], float] = OrderedDict()
        #: (GFS connection id, local file name) → until when we know that
        #: GFS holds the file (uploaded by us) — what lets the media outbox
        #: drop a push to a reader there.
        self._stored_names: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._described: OrderedDict[tuple, SpaceMediaRef] = OrderedDict()
        #: v_97 — caps the lazy catch-up sealing at once
        #: (:data:`MAX_DESCRIBING_STREAMS`).
        self._describing = asyncio.Semaphore(MAX_DESCRIBING_STREAMS)
        #: v_97 — fetches a file before describing it (:meth:`attach_fetcher`)
        #: and their per-space cap.
        self._fetcher: _DescribeFetcher | None = None
        self._fetching: dict[str, asyncio.Semaphore] = {}
        #: v_97 — (space, file) → (lazy streams its fetch missed, the last
        #: stream counted) (:data:`MAX_DESCRIBE_MISS_STREAMS`).
        self._describe_misses: dict[tuple[str, str], tuple[int, object]] = {}

    def attach_federation(self, federation: FederationService) -> None:
        self._federation = federation

    def attach_acting_hosts(
        self, hosts: Callable[[Space], Awaitable[list[str]]]
    ) -> None:
        """The households acting as a space's host here (``acting_hosts``):
        each must be at v_97 for the space's files to come from the cache
        only (:meth:`cache_only`). Without it, the owner alone."""
        self._acting_hosts = hosts

    def attach_fetcher(self, fetcher: _DescribeFetcher) -> None:
        """Wire the GFS space media fetcher: a file this household holds
        only as a descriptor is fetched before it is described again (a
        host releasing an approved item, a catch-up, a re-sent held item)."""
        self._fetcher = fetcher

    def attach_outbox(self, outbox: SpaceItemOutbox) -> None:
        """v_97 — the durable queue uploads wait in (one row per blob or
        piece per GFS; :meth:`deliver` sends one)."""
        self._outbox = outbox

    def attach_legacy_window(self, is_open: Callable[[str], Awaitable[bool]]) -> None:
        """v_97 — the space's 14-day window: every household still gets
        the legacy events, so it keeps the push too."""
        self._legacy_window = is_open

    async def push_targets(self, space_id: str, targets: list[str]) -> list[str]:
        """Who still gets the household push of a file of *space_id* (v_97,
        owner 2026-11-27): the *targets* that do not read the space from
        the GFS cache only (:meth:`cache_only`). A mesh space's files go to
        everyone. The space's own conditions are read once per call
        (review M4)."""
        host = await self._cache_only_host(space_id)
        if host is None:
            return list(targets)
        return [t for t in targets if not await self._supports_items(t)]

    async def cache_only(self, space_id: str, instance_id: str) -> bool:
        """v_97 (owner 2026-11-27) — whether *instance_id* takes
        *space_id*'s files ONLY from the GFS space media cache: no
        ``SPACE_MEDIA_BLOB`` push and no ``SPACE_MEDIA_WANTED`` for it. True
        for a space whose channel is stored here, once its 14-day legacy
        window closed, when both the household and the space's host are at
        v_97 — a version and channel condition, never the space type alone:
        a v_96 host — any household acting as its host here (whose own
        files carry no descriptor) — or a space with
        no stored channel yet keeps the push. Every path that sends such a
        household an item with files describes them (``media_refs``)."""
        return await self._cache_only_host(space_id) is not None and (
            await self._supports_items(instance_id)
        )

    async def _cache_only_host(self, space_id: str) -> str | None:
        """The host of *space_id* when the space itself reads from the
        cache only (a stored channel, the legacy window closed, a v_97
        host), else ``None``."""
        if self._federation is None or await self.scope_of(space_id) is None:
            return None
        if self._legacy_window is not None and await self._legacy_window(space_id):
            return None
        space = await self._spaces.get(space_id)
        host = str(getattr(space, "owner_instance_id", "") or "")
        if space is None or not host:
            return None
        # Every household acting as its host here (owner reminder
        # 2026-11-28): the owner, and a delegated admin that invited us.
        hosts = await self._acting_hosts(space) if self._acting_hosts else [host]
        for iid in hosts:
            if not await self._supports_items(iid):
                return None
        return host

    async def _supports_items(self, instance_id: str) -> bool:
        if not instance_id:
            return False
        if instance_id == self._origin():
            return True
        assert self._federation is not None
        return await self._federation.space_member_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_SPACE_ITEMS
        )

    def attach_channels(self, channels: GfsChannelService) -> None:
        """Wire the v_51 channel service: a private space with an opaque
        channel uploads under the channel scope."""
        self._channels = channels

    async def can_upload(
        self, space_id: str, *, scoped: tuple[str, str] | None = None
    ) -> bool:
        """Whether this household holds an upload authorization for
        *space_id*'s channel right now — the very one :meth:`_put` would
        sign with (the channel key or this household's grant). A link-joined
        proposer describes a page's files only when it does. *scoped*: the
        :meth:`scope_of` answer when the caller read it already."""
        if scoped is None:
            scoped = await self.scope_of(space_id)
        if scoped is None:
            return False
        scope, scope_id = scoped
        now = int(time.time())
        claim = SpaceMediaClaim(
            scope=scope,
            scope_id=scope_id,
            blob_id="0" * 64,
            size=1,
            expires_at=now + KEEP_S,
            ts=now,
        )
        return await self._authorize_channel(claim, space_id) is not None

    async def scope_of(self, space_id: str) -> tuple[str, str] | None:
        """``(scope, scope_id)`` *space_id*'s blobs are uploaded under: the
        opaque channel of a space that uses one — a PRIVATE space with
        ``private_gfs``, or a GLOBAL space — else ``None``: since v_96 a GFS
        keeps no space-keyed media, so a space without a channel here
        uploads nothing (its readers get their files pushed)."""
        space = await self._spaces.get(space_id)
        if space is not None and channel_on(space):
            stored = await self._spaces.get_gfs_channel(space_id)
            if stored is not None:
                return SCOPE_CHANNEL, stored[0]
        return None

    # ── Readers ──────────────────────────────────────────────────────────

    async def is_relay_reader(self, instance_id: str) -> bool:
        """Whether *instance_id* is a member household seated from an invite
        link that takes ``media_refs`` (v_80): its items carry the
        descriptor, and the media outbox skips its ``SPACE_MEDIA_BLOB`` push
        only while its GFS is known to hold the file (:meth:`reads_from_gfs`)
        — it gets every other push, through the relay, like any member. Not
        one whose GFS keeps no space media cache while it reads binary relay
        frames (v_87): it has nothing to fetch from, so it only gets the
        push."""
        if self._federation is None:
            return False
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None or inst.source is not InstanceSource.SPACE_SESSION:
            return False
        if not await self._federation.peer_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_REFS
        ):
            log.debug("space-media: %s is no relay reader (below v_80)", instance_id)
            return False
        # v_87 — a reader whose GFS keeps NO space media cache (its last
        # signed capability block proved ``envelope_media`` and carried no
        # ``space_media``) has nothing to fetch from: it gets the push as
        # binary relay frames instead, when it opens them.
        if inst.relay_via and await self._federation.peer_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_RELAY_MEDIA
        ):
            conn = await self._gfs.connection_for_url(inst.relay_via)
            if conn is not None and self._gfs.space_media_cache_off(conn):
                log.debug(
                    "space-media: %s is no relay reader (its GFS keeps no cache)",
                    instance_id,
                )
                return False
        log.debug(
            "space-media: %s is a relay reader (relay %s)",
            instance_id,
            "set" if inst.relay_via else "none",
        )
        return True

    async def takes_relay_frames(self, instance_id: str) -> bool:
        """Whether *instance_id* — a household seated from an invite link —
        takes a media push as binary relay frames (v_87, its relay proved
        ``envelope_media``; :meth:`FederationService.relay_media_ok`). Such
        a household gets ``SPACE_MEDIA_BLOB`` through the outbox like any
        member, so a file meant for it alone needs no GFS cache."""
        if self._federation is None:
            return False
        inst = await self._federation_repo.get_instance(instance_id)
        return inst is not None and await self._federation.relay_media_ok(inst)

    async def _reader_conn(self, instance_id: str) -> GfsConnection | None:
        if not await self.is_relay_reader(instance_id):
            return None
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None or not inst.relay_via:
            return None
        return await self._gfs.connection_for_url(inst.relay_via)

    async def _relay_reader_conns(self, space_id: str) -> list[GfsConnection]:
        """The GFSs of the space's member households that read through one."""
        out: list[GfsConnection] = []
        for iid in await self._federation_repo.list_member_instance_ids(space_id):
            conn = await self._reader_conn(iid)
            if conn is not None:
                out.append(conn)
        return out

    async def _capable(
        self, conns: Iterable[GfsConnection], scope: str
    ) -> list[GfsConnection]:
        out: list[GfsConnection] = []
        seen: set[str] = set()
        for conn in conns:
            if conn.id in seen:
                continue
            seen.add(conn.id)
            if await self._gfs.space_media_max_blob(conn, scope=scope) is not None:
                out.append(conn)
        return out

    # ── Planning ─────────────────────────────────────────────────────────

    async def plan(
        self,
        space_id: str,
        media_urls: Iterable[str | None],
        *,
        item_id: str,
        followers: bool,
        author_user_id: str = "",
    ) -> SpaceMediaPlan | None:
        """The plan for item *item_id* of *space_id* referencing
        *media_urls*: link-joined v_80 members' GFSs, plus (``followers``)
        the GFSs the space's opaque channel is relayed on. ``None`` when the
        space runs no channel here, no capable GFS needs it or no file can
        be described — nothing is sealed then. *author_user_id* is told
        about a file no GFS here can take (v_97: over 8 MiB with no GFS
        keeping pieces, or over 200 MiB)."""
        names = media_names(media_urls)
        scoped = await self.scope_of(space_id) if names else None
        if scoped is None:
            return None
        conns = await self._media_targets(space_id, followers=followers)
        return await self._plan(
            space_id, names, conns, item_id, scoped=scoped, author=author_user_id
        )

    async def reads_space_from_cache(self, space_id: str, instance_id: str) -> bool:
        """v_97 — whether *instance_id* takes *space_id*'s files from the
        GFS space media cache only (:meth:`cache_only`): its items and
        catch-up records then carry the descriptor."""
        return await self.cache_only(space_id, instance_id)

    async def plan_for_reader(
        self,
        space_id: str,
        media_urls: Iterable[str | None],
        instance_id: str,
        *,
        item_id: str,
        stream: object | None = None,
    ) -> SpaceMediaPlan | None:
        """The plan for ONE catch-up reader. *stream* names the catch-up
        stream asking (a file no GFS holds counts one miss per stream,
        :data:`MAX_DESCRIBE_MISS_STREAMS`); ``None`` counts every call. v_97 (owner B-8): a household
        that reads a GFS space from the cache only gets a LAZY plan — each
        file described by its deterministic address from our copy, never
        uploaded (no bulk re-upload on a join): it fetches on view, and a
        blob the cache lacks comes on demand from a holder (v_83). A
        link-joined reader of any other space: its own GFS, uploaded.
        ``None`` otherwise."""
        names = media_names(media_urls)
        if not names:
            return None
        if await self.cache_only(space_id, instance_id):
            return await self._describe_lazily(
                space_id, names, item_id, stream if stream is not None else object()
            )
        conn = await self._reader_conn(instance_id)
        if conn is not None:
            return await self._plan(space_id, names, [conn], item_id)
        return None

    async def _describe_lazily(
        self, space_id: str, names: list[str], item_id: str, stream: object
    ) -> SpaceMediaPlan | None:
        """A described-only plan (:attr:`SpaceMediaPlan.lazy`): a file in
        pieces up to the 200 MiB cap (a GFS that keeps less refuses its
        pieces on demand, as it would an upload). At most
        :data:`MAX_DESCRIBING_STREAMS` describe at once (each seals in one
        worker thread); the rest wait."""
        pending: list[str] = []
        # Only the sealing takes a slot — a fetch to describe waits outside
        # it (review M5).
        refs = await self._describe_names(
            space_id,
            names,
            item_id,
            pieces=MAX_PIECED_FILE_BYTES,
            pending=pending,
            seal_gate=self._describing,
            stream=stream,
        )
        if not refs and not pending:
            return None
        return SpaceMediaPlan(
            space_id=space_id,
            refs=tuple(refs),
            targets=(),
            lazy=True,
            pending=tuple(pending),
        )

    async def _plan(
        self,
        space_id: str,
        names: list[str],
        conns: list[GfsConnection],
        item_id: str,
        *,
        scoped: tuple[str, str] | None = None,
        author: str = "",
    ) -> SpaceMediaPlan | None:
        # One read of the space's scope per plan (it was up to four).
        if scoped is None:
            scoped = await self.scope_of(space_id)
        if scoped is None:
            return None
        targets = await self._capable(conns, scoped[0])
        # A GFS whose capabilities are merely unknown right now (unreachable)
        # is no verdict: its rows wait in the durable queue (review F,
        # finding 4 — never filtered and lost).
        waiting = [
            c
            for c in conns
            if self._gfs.capability_unknown(c) and c.id not in {t.id for t in targets}
        ]
        if not targets and not waiting and conns:
            # A verdict: every GFS of the space answered and none keeps
            # space media — the file has no way to a cache-only household
            # (v_97); told for a live item with its author, never a
            # catch-up.
            if author and await self.can_upload(space_id, scoped=scoped):
                log.info(
                    "space media of space %s: no GFS of the space takes its"
                    " files — not sent",
                    space_id,
                )
                await self._tell_unsent(space_id, author, UNSENT_REFUSED)
            return None
        # No GFS planned yet (no connection), an unreachable one, or a grant
        # still on its way: the file is described all the same and its rows
        # wait in the durable queue (planned when due, sent once the GFS
        # answers or the grant lands — :meth:`deliver`).
        # The largest file in pieces a GFS of the plan keeps (half its
        # signed channel share; 0: none keeps pieces). Unknown: assume the
        # most — a GFS that turns out not to keep pieces drops its rows and
        # the author is told (``gone`` → replans → told).
        pieces = MAX_PIECED_FILE_BYTES if waiting or not conns else 0
        for conn in targets:
            pieces = max(pieces, await self._gfs.space_media_file_max(conn))
        refs = await self._describe_names(
            space_id, names, item_id, scoped=scoped, pieces=pieces, author=author
        )
        if not refs:
            return None
        return SpaceMediaPlan(
            space_id=space_id, refs=tuple(refs), targets=(*targets, *waiting)
        )

    async def _describe_names(
        self,
        space_id: str,
        names: list[str],
        item_id: str,
        *,
        cached_only: bool = False,
        scoped: tuple[str, str] | None = None,
        pieces: int = 0,
        author: str = "",
        pending: list[str] | None = None,
        seal_gate: asyncio.Semaphore | None = None,
        stream: object | None = None,
    ) -> list[SpaceMediaRef]:
        """Each file's descriptor entry, sealed for item *item_id* by this
        household — the household a reader authenticates as delivering the
        item, so both are in the blob's AAD (:mod:`~socialhome.space_media_seal`).
        Nothing before this household knows its own id. ``cached_only``:
        never seal — only entries described before (every uploaded blob
        was). ``pieces``: a GFS of the plan keeps pieces, so a file over one
        blob is described in pieces (v_97); otherwise — or past 200 MiB — it
        is not described and *author* is told. A file held here only as
        a GFS descriptor is fetched first (:meth:`_fetch_first`); one still
        on its way is added to *pending*, and so is one no GFS held, for
        :data:`MAX_DESCRIBE_MISS_STREAMS` catch-up *stream* s."""
        origin = self._federation.own_instance_id if self._federation else ""
        if not origin or not item_id:
            return []
        current = await self._crypto.media_key(space_id)
        if current is None:
            return []
        epoch, key = current
        if scoped is None:
            scoped = await self.scope_of(space_id)
        if scoped is None:
            return []
        scope, scope_id = scoped
        refs: list[SpaceMediaRef] = []
        for name in names[:MAX_FILES_PER_ITEM]:
            # A file described before costs a stat and a lookup (review M4:
            # the confinement check is ~25 table scans): it was confined
            # then, and every upload and on-demand serve checks again.
            stamp = await self._stamp(name)
            if stamp is not None:
                cache_key = (space_id, epoch, scope, scope_id, item_id, name, *stamp)
                cached = self._described.get(cache_key)
                if cached is not None:
                    self._described.move_to_end(cache_key)
                    refs.append(cached)
                    continue
            if cached_only or not await self._space_file(space_id, name):
                continue
            if stamp is None:
                stamp = await self._fetch_first(space_id, name)
                if (
                    stamp is None
                    and pending is not None
                    and (
                        self._still_fetching(name)
                        or (
                            self._fetcher is not None
                            and stream is not None
                            and self._defers_miss(space_id, name, stream)
                        )
                    )
                ):
                    pending.append(name)
                elif stamp is not None:
                    self._describe_misses.pop((space_id, name), None)
            if stamp is None:
                continue
            cache_key = (space_id, epoch, scope, scope_id, item_id, name, *stamp)
            if stamp[0] > MAX_SPACE_MEDIA_BYTES:
                async with seal_gate or contextlib.nullcontext():
                    ref = await self._describe_pieced(
                        space_id,
                        name,
                        item_id,
                        epoch,
                        key,
                        scope,
                        scope_id,
                        pieces=pieces,
                        author=author,
                    )
                if ref is not None:
                    self._described[cache_key] = ref
                    while len(self._described) > MAX_DESCRIBED:
                        self._described.popitem(last=False)
                    refs.append(ref)
                continue
            async with seal_gate or contextlib.nullcontext():
                data = await self._read(name)
                if data is None:
                    continue
                blob, address = await asyncio.to_thread(
                    seal_and_address_sync,
                    key,
                    space_id,
                    epoch,
                    item_id,
                    origin,
                    name,
                    data,
                    scope,
                    scope_id,
                )
            ref = SpaceMediaRef(
                filename=name,
                space_id=space_id,
                blob_id=address,
                suite=SPACE_MEDIA_SUITE_AESGCM256_PADDED,
                epoch=epoch,
                size=len(blob),
                scope=scope,
                addr_suite=ADDR_SUITE_SM1,
                item_id=item_id,
                origin=origin,
            )
            self._described[cache_key] = ref
            while len(self._described) > MAX_DESCRIBED:
                self._described.popitem(last=False)
            refs.append(ref)
        return refs

    async def _describe_pieced(
        self,
        space_id: str,
        name: str,
        item_id: str,
        epoch: int,
        key: bytes,
        scope: str,
        scope_id: str,
        *,
        pieces: int,
        author: str,
    ) -> SpaceMediaRef | None:
        """The ``sm1p`` entry of a file over one blob — ``None``, and its
        author told, when no GFS of the plan keeps pieces or it is past
        200 MiB (never sent through a GFS then)."""
        path = media_file_path(self._dir, name)
        stamp = await self._stamp(name)
        if path is None or stamp is None:
            return None
        if stamp[0] > min(pieces, MAX_PIECED_FILE_BYTES):
            log.info(
                "space media %s (%d bytes) of space %s: %s — not sent through a GFS",
                name,
                stamp[0],
                space_id,
                "over what a GFS of the space keeps for one file"
                if pieces
                else "no GFS of the space keeps pieces",
            )
            await self._tell_too_large(space_id, author)
            return None
        pieced = await asyncio.to_thread(
            describe_pieced_sync,
            key,
            space_id,
            epoch,
            item_id,
            self._origin(),
            name,
            str(path),
            scope,
            scope_id,
        )
        if pieced is None:
            return None
        self._remember_manifest(pieced.address, pieced.manifest)
        return SpaceMediaRef(
            filename=name,
            space_id=space_id,
            blob_id=pieced.address,
            suite=SPACE_MEDIA_SUITE_AESGCM256_PADDED,
            epoch=epoch,
            size=pieced.size,
            scope=scope,
            addr_suite=ADDR_SUITE_SM1P,
            item_id=item_id,
            origin=self._origin(),
            sha256=pieced.sha256,
        )

    def _origin(self) -> str:
        return self._federation.own_instance_id if self._federation else ""

    def _remember_manifest(self, address: str, manifest: tuple[str, ...]) -> None:
        self._manifests[address] = manifest
        self._manifests.move_to_end(address)
        while len(self._manifests) > MAX_MANIFESTS:
            self._manifests.popitem(last=False)

    async def manifest_of(
        self,
        space_id: str,
        name: str,
        *,
        epoch: int,
        item_id: str,
        origin: str,
        scope: str,
        scope_id: str,
        filename: str | None = None,
    ) -> PiecedFile | None:
        """The local file *name* described in pieces as sealed by *origin*
        for *item_id* under the author's file name *filename* (the AAD's —
        a holder keeps the file under its own name; default *name*) —
        remembered, else sealed once more. ``None`` without the epoch's key
        or a pieced size."""
        filename = filename or name
        path = media_file_path(self._dir, name)
        stamp = await self._stamp(name)
        current = await self._crypto.media_key(space_id, epoch=epoch)
        if path is None or stamp is None or current is None:
            return None
        key = (
            space_id,
            name,
            filename,
            epoch,
            item_id,
            origin,
            scope,
            scope_id,
            *stamp,
        )
        hit = self._pieced.get(key)
        if hit is not None:
            self._pieced.move_to_end(key)
            return hit
        pieced = await asyncio.to_thread(
            describe_pieced_sync,
            current[1],
            space_id,
            epoch,
            item_id,
            origin,
            filename,
            str(path),
            scope,
            scope_id,
        )
        if pieced is not None:
            self._remember_manifest(pieced.address, pieced.manifest)
            self._pieced[key] = pieced
            while len(self._pieced) > MAX_MANIFESTS:
                self._pieced.popitem(last=False)
        return pieced

    async def seal_piece(
        self,
        space_id: str,
        name: str,
        *,
        epoch: int,
        item_id: str,
        origin: str,
        index: int,
        count: int,
        filename: str | None = None,
    ) -> bytes | None:
        """Piece *index* of the local file *name*, sealed again under the
        author's file name *filename* (default *name*)."""
        path = media_file_path(self._dir, name)
        current = await self._crypto.media_key(space_id, epoch=epoch)
        if path is None or current is None or not await aiofiles.os.path.isfile(path):
            return None
        return await asyncio.to_thread(
            seal_piece_sync,
            current[1],
            space_id,
            epoch,
            item_id,
            origin,
            filename or name,
            str(path),
            index,
            count,
        )

    async def _tell_too_large(self, space_id: str, author: str) -> None:
        await self._tell_unsent(space_id, author, UNSENT_TOO_LARGE)

    async def _tell_unsent(self, space_id: str, author: str, error: str) -> None:
        """A file of *author*'s item that can't reach the GFS cache: shown
        as "Couldn't send" (no push carries it in a GFS space)."""
        if self._outbox is None:
            return
        try:
            await self._outbox.fail(
                space_id=space_id,
                event_type=MEDIA_UPLOAD_ROW,
                author_user_id=author,
                error=error,
            )
        except Exception:
            log.warning("space media: an unsent file was not recorded", exc_info=True)

    async def _space_file(self, space_id: str, name: str) -> bool:
        """Whether *name* may be sealed under *space_id*'s key: never a
        name another receiving path owns (DM / moment media), and only a
        file nothing but this space's content references — a member may
        name another space's, a private album's or a household file in a
        post or record of this space (review D)."""
        if is_reserved_media_name(name):
            return False
        return await self._media_refs.confined_to_space(name, space_id)

    async def _fetch_first(self, space_id: str, name: str) -> tuple[int, int] | None:
        """``(size, mtime_ns)`` of *name* once fetched from the GFS — a file
        this household holds only as a descriptor (v_97, owner: no loss).
        At most :data:`MAX_DESCRIBE_FETCHES_PER_SPACE` per space at once,
        each waited for at most :data:`DESCRIBE_FETCH_WAIT_S`; ``None``
        when it is not here by then (or no GFS holds it)."""
        if self._fetcher is None:
            return None
        gate = self._fetching.setdefault(
            space_id, asyncio.Semaphore(MAX_DESCRIBE_FETCHES_PER_SPACE)
        )
        async with gate:
            got = await self._fetcher.ensure_local(
                name, timeout_s=DESCRIBE_FETCH_WAIT_S
            )
        return await self._stamp(name) if got else None

    def _defers_miss(self, space_id: str, name: str, stream: object) -> bool:
        """Whether a lazy catch-up *stream* holds back a record whose file
        *name* no GFS held (review H3a, Fable I2): for the first
        :data:`MAX_DESCRIBE_MISS_STREAMS` streams that missed it (one count
        per stream, whoever asks — a lost blob is lost for everyone), then
        never — the record goes undescribed, with one WARNING. The count
        stays until the file turns up; with the table full a new miss goes
        undescribed at once, never deferred without a count."""
        key = (space_id, name)
        entry = self._describe_misses.get(key)
        if entry is None:
            if len(self._describe_misses) >= MAX_DESCRIBE_MISSES_REMEMBERED:
                log.warning(
                    "space media %s of space %s: no GFS held it and %d other "
                    "files are missing — its record goes undescribed",
                    name,
                    space_id,
                    len(self._describe_misses),
                )
                return False
            self._describe_misses[key] = (1, stream)
            return True
        count, last = entry
        if last is stream:
            return count <= MAX_DESCRIBE_MISS_STREAMS
        count += 1
        self._describe_misses[key] = (count, stream)
        if count > MAX_DESCRIBE_MISS_STREAMS:
            if count == MAX_DESCRIBE_MISS_STREAMS + 1:
                log.warning(
                    "space media %s of space %s: no GFS held it for %d catch-up "
                    "streams — its record goes undescribed",
                    name,
                    space_id,
                    MAX_DESCRIBE_MISS_STREAMS,
                )
            return False
        return True

    def _still_fetching(self, name: str) -> bool:
        return self._fetcher is not None and self._fetcher.fetching(name)

    async def _stamp(self, name: str) -> tuple[int, int] | None:
        """``(size, mtime_ns)`` of a local media file, or ``None``."""
        path = media_file_path(self._dir, name)
        if path is None:
            return None
        try:
            st = await aiofiles.os.stat(path)
        except OSError:
            return None
        return st.st_size, st.st_mtime_ns

    async def _read(self, name: str) -> bytes | None:
        path = media_file_path(self._dir, name)
        if path is None or not await aiofiles.os.path.isfile(path):
            return None
        if (await aiofiles.os.stat(path)).st_size > MAX_SPACE_MEDIA_BYTES:
            log.info(
                "space media %s is over %d MB — not sent through a GFS",
                name,
                MAX_SPACE_MEDIA_BYTES // (1024 * 1024),
            )
            return None
        async with aiofiles.open(path, "rb") as fh:
            return await fh.read()

    # ── Uploading (v_97: through the durable queue) ─────────────────────

    async def upload(
        self, plan: SpaceMediaPlan | None, *, author_user_id: str = ""
    ) -> None:
        """Queue *plan*'s blobs — one row per blob (or piece) per target GFS
        in the durable queue; a file another upload already stored on a GFS
        is skipped there. Never raises (a row that can't be stored is shown
        to *author_user_id* by the queue)."""
        if plan is None or plan.lazy or self._outbox is None:
            return
        for ref in plan.refs:
            try:
                await self._queue_ref(plan, ref, author_user_id)
            except Exception:
                log.warning(
                    "space media %s of space %s: upload not queued",
                    ref.filename,
                    plan.space_id,
                    exc_info=True,
                )

    async def _queue_ref(
        self, plan: SpaceMediaPlan, ref: SpaceMediaRef, author: str
    ) -> None:
        assert self._outbox is not None
        now = _monotonic()
        conns = list(plan.targets)
        if ref.pieced:
            conns = [
                c
                for c in conns
                if self._gfs.capability_unknown(c)
                or await self._gfs.space_media_file_max(c) >= ref.size
            ]
            manifest = self._manifests.get(ref.blob_id)
            if manifest is None:
                scoped = await self.scope_of(plan.space_id)
                pieced = (
                    await self.manifest_of(
                        plan.space_id,
                        ref.filename,
                        epoch=ref.epoch,
                        item_id=ref.item_id,
                        origin=ref.origin,
                        scope=scoped[0],
                        scope_id=scoped[1],
                    )
                    if scoped is not None
                    else None
                )
                if pieced is None or pieced.address != ref.blob_id:
                    log.warning(
                        "space media %s changed since it was described — not uploaded",
                        ref.filename,
                    )
                    return
                manifest = pieced.manifest
            parts = [(i, piece_address(ref.blob_id, i)) for i in range(len(manifest))]
        else:
            manifest = ()
            parts = [(-1, ref.blob_id)]
        rows: list[tuple[str, list[str]]] = []
        for index, address in parts:
            gfs_ids = [
                c.id for c in conns if self._stored.get((c.id, address), 0.0) <= now
            ]
            if gfs_ids:
                rows.append((_locator(ref, index, manifest), gfs_ids))
            elif not plan.targets:
                # No GFS planned yet: one row planned when due.
                rows.append((_locator(ref, index, manifest), [UNPLANNED_GFS]))
        # Every row of one file in one go: a file is never half queued.
        if rows:
            await self._outbox.queue_media(
                space_id=plan.space_id,
                epoch=ref.epoch,
                parts=rows,
                author_user_id=author,
            )

    async def deliver(self, row: QueuedSpaceItem) -> PublishOutcome:
        """One attempt of one queued blob (or piece) on ``row.gfs_id``."""
        parsed = _from_locator(row)
        if parsed is None:
            log.warning("space media: an unreadable upload row — dropped")
            return PublishOutcome.permanent()
        ref, index, manifest = parsed
        scoped = await self.scope_of(row.space_id)
        if scoped is None:
            return PublishOutcome.gone()
        scope, scope_id = scoped
        conn = await self._target(row.space_id, row.gfs_id)
        if conn is None:
            return PublishOutcome.gone()
        limit = await self._gfs.space_media_max_blob(conn, scope=scope)
        if limit is None and self._gfs.capability_unknown(conn):
            # Unreachable right now: no verdict — the row waits.
            return PublishOutcome.transient()
        if limit is None or (
            ref.pieced and not await self._gfs.space_media_pieces(conn)
        ):
            return PublishOutcome.gone()
        if not await self.can_upload(row.space_id, scoped=scoped):
            # No grant yet (it may come): nothing is sealed meanwhile.
            self._warn_unauthorized(row.space_id)
            return PublishOutcome.transient()
        if ref.pieced:
            blob_id = piece_address(ref.blob_id, index)
            blob = await self._reseal_piece(ref, index, manifest)
            proof: str | None = PieceProof(manifest=manifest, index=index).to_header()
        else:
            blob_id = ref.blob_id
            blob = await self._reseal(ref, scope, scope_id)
            proof = None
        if blob is None:
            # The file is gone or changed (the item no longer shows it):
            # nothing to send — the row is done.
            return PublishOutcome.delivered()
        if len(blob) > limit:
            log.info(
                "space media %s (%d bytes) is over what GFS %s keeps — not sent",
                ref.filename,
                len(blob),
                conn.id,
            )
            return PublishOutcome.permanent()
        status = await self._put(
            conn,
            space_id=ref.space_id,
            blob_id=blob_id,
            blob=blob,
            scope=scope,
            scope_id=scope_id,
            addr_suite=ref.addr_suite,
            proof=proof,
        )
        if status == _NO_AUTHORIZATION:
            return PublishOutcome.transient()  # a grant may come; warned once
        if status in _STATUS_STORED:
            await self._remember(conn, blob_id, filename=ref.filename)
            log.info(
                "space media %s: uploaded blob %s (%d bytes) to GFS %s (HTTP %d)",
                ref.filename,
                blob_id[:12],
                len(blob),
                conn.id,
                status,
            )
            return PublishOutcome.delivered()
        if status is None or status in _STATUS_RETRY or status >= 500:
            return PublishOutcome.transient()
        log.warning(
            "GFS %s refused space media %s of space %s — HTTP %d",
            conn.id,
            ref.filename,
            ref.space_id,
            status,
        )
        return PublishOutcome.permanent()

    def _warn_unauthorized(self, space_id: str) -> None:
        if space_id not in self._unauthorized:
            self._unauthorized.add(space_id)
            log.warning(
                "space media of space %s: this household holds no upload "
                "authorization — its files are not uploaded",
                space_id,
            )

    async def _target(self, space_id: str, gfs_id: str) -> GfsConnection | None:
        """The GFS *gfs_id* if it still reads this space's media."""
        conns = await self._media_targets(space_id, followers=True)
        return next((c for c in conns if c.id == gfs_id), None)

    async def _media_targets(
        self, space_id: str, *, followers: bool
    ) -> list[GfsConnection]:
        """Where a space's files go: v_97 — every GFS the space's channel
        lives on (each household of a GFS space reads the space there, so
        each takes the files there: owner 2026-11-27), plus the GFS of each
        link-joined member and, for *followers*, the GFSs the channel is
        relayed on."""
        conns = await self._relay_reader_conns(space_id)
        if self._channels is not None:
            conns.extend(await self._channels.plan(space_id))
        if followers:
            conns.extend(await self._gfs.space_media_follower_targets(space_id))
        return conns

    async def _reseal_piece(
        self, ref: SpaceMediaRef, index: int, manifest: tuple[str, ...]
    ) -> bytes | None:
        """Piece *index* sealed again — ``None`` (WARNING) when the file is
        no longer only the space's, or its bytes no longer give the piece
        the manifest names (the file changed)."""
        if not await self._space_file(ref.space_id, ref.filename):
            log.warning(
                "space media %s is no longer only space %s's — not uploaded",
                ref.filename,
                ref.space_id,
            )
            return None
        blob = await self.seal_piece(
            ref.space_id,
            ref.filename,
            epoch=ref.epoch,
            item_id=ref.item_id,
            origin=ref.origin,
            index=index,
            count=len(manifest),
        )
        if (
            blob is None
            or (await asyncio.to_thread(_sha256_hex, blob)) != manifest[index]
        ):
            log.warning(
                "space media %s piece %d changed since it was described — not uploaded",
                ref.filename,
                index,
            )
            return None
        return blob

    async def remember_stored(self, conn: GfsConnection, blob_id: str) -> None:
        """Note that *conn*'s GFS holds *blob_id* now (a re-upload on demand
        stored it), so an eager upload of it in the meantime is skipped."""
        await self._remember(conn, blob_id)

    async def authorize(
        self, claim: SpaceMediaClaim, space_id: str
    ) -> SpaceMediaUploadAuth | None:
        """The upload authorization this household holds for *claim* of
        *space_id* (a channel-scope claim), or ``None``."""
        if claim.scope != SCOPE_CHANNEL:
            return None
        return await self._authorize_channel(claim, space_id)

    async def _remember(
        self, conn: GfsConnection, blob_id: str, *, filename: str | None = None
    ) -> None:
        """Remember *blob_id* (and the local *filename* it seals) as held by
        *conn* until min(:data:`KEEP_S`, its TTL) — an ``already_stored``
        answer does not refresh a blob's expiry, so the upload time is the
        bound."""
        ttl = await self._gfs.space_media_ttl_s(conn)
        keep = min(KEEP_S, ttl) if ttl else KEEP_S
        until = _monotonic() + keep
        for cache, key in (
            (self._stored, (conn.id, blob_id)),
            (self._stored_names, (conn.id, filename) if filename else None),
        ):
            if key is None:
                continue
            cache[key] = until
            cache.move_to_end(key)
            while len(cache) > MAX_REMEMBERED:
                cache.popitem(last=False)

    async def reads_from_gfs(self, instance_id: str, filename: str) -> bool:
        """Whether *instance_id* takes *filename* from its GFS's media cache,
        so the media outbox may drop its push: a relay reader whose GFS we
        know holds the file (we uploaded it there, within the cache's TTL).
        A reader whose GFS never got the file keeps its push."""
        conn = await self._reader_conn(instance_id)
        if conn is None:
            return False
        return self._stored_names.get((conn.id, filename), 0.0) > _monotonic()

    async def stored_plan_for_reader(
        self,
        space_id: str,
        media_urls: Iterable[str | None],
        instance_id: str,
        *,
        item_id: str,
    ) -> SpaceMediaPlan | None:
        """The plan for ONE reader holding only the blobs its GFS is known
        to hold now (:meth:`stored_on_gfs`). Nothing is sealed: a blob we
        uploaded was described (and its entry kept) when it was planned, so
        a file with no kept entry cannot be on the GFS and costs nothing
        here. ``None`` when no such blob."""
        names = media_names(media_urls)
        conn = await self._reader_conn(instance_id) if names else None
        if conn is None:
            return None
        refs = await self._describe_names(space_id, names, item_id, cached_only=True)
        if not refs:
            return None
        return self.stored_on_gfs(
            SpaceMediaPlan(space_id=space_id, refs=tuple(refs), targets=(conn,))
        )

    def stored_on_gfs(self, plan: SpaceMediaPlan | None) -> SpaceMediaPlan | None:
        """*plan* narrowed to the refs whose exact blob (the address: file,
        epoch, item, origin) a target GFS is known to hold now — we uploaded
        it there, within the cache's TTL (:meth:`_remember`). ``None`` when
        none is. A file stored under an older epoch or another item is a
        different blob, so it is never described from here."""
        if plan is None:
            return None
        now = _monotonic()
        refs = tuple(
            ref
            for ref in plan.refs
            if any(
                self._stored.get((conn.id, ref.blob_id), 0.0) > now
                for conn in plan.targets
            )
        )
        if not refs:
            return None
        return SpaceMediaPlan(space_id=plan.space_id, refs=refs, targets=plan.targets)

    async def _reseal(
        self, ref: SpaceMediaRef, scope: str, scope_id: str
    ) -> bytes | None:
        """The blob the descriptor names, sealed again from the local file —
        ``None`` (WARNING) when the file or the epoch key is gone, or the
        bytes (or the space's scope) no longer give the described address."""
        if not await self._space_file(ref.space_id, ref.filename):
            log.warning(
                "space media %s is no longer only space %s's — not uploaded",
                ref.filename,
                ref.space_id,
            )
            return None
        current = await self._crypto.media_key(ref.space_id, epoch=ref.epoch)
        data = await self._read(ref.filename)
        if current is None or data is None:
            log.warning(
                "space media %s of space %s: file or key gone — not uploaded",
                ref.filename,
                ref.space_id,
            )
            return None
        blob, address = await asyncio.to_thread(
            seal_and_address_sync,
            current[1],
            ref.space_id,
            ref.epoch,
            ref.item_id,
            ref.origin,
            ref.filename,
            data,
            scope,
            scope_id,
        )
        if scope != ref.scope or address != ref.blob_id:
            log.warning(
                "space media %s changed since it was described — not uploaded",
                ref.filename,
            )
            return None
        return blob

    async def _put(
        self,
        conn: GfsConnection,
        *,
        space_id: str,
        blob_id: str,
        blob: bytes,
        scope: str,
        scope_id: str,
        addr_suite: str = ADDR_SUITE_SM1,
        proof: str | None = None,
    ) -> int | None:
        now = int(time.time())
        claim = SpaceMediaClaim(
            scope=scope,
            scope_id=scope_id,
            blob_id=blob_id,
            size=len(blob),
            expires_at=now + KEEP_S,
            ts=now,
            addr_suite=addr_suite,
        )
        auth = await self.authorize(claim, space_id)
        if auth is None:
            self._warn_unauthorized(space_id)
            return _NO_AUTHORIZATION
        return await self._gfs.put_space_media(
            conn,
            blob_id=blob_id,
            blob=blob,
            auth_header=auth.to_header(),
            piece_proof=proof,
        )

    async def _authorize_channel(
        self, claim: SpaceMediaClaim, space_id: str
    ) -> SpaceMediaUploadAuth | None:
        """The channel scope's authorization: the channel key (a seed holder
        whose seed derives the stored channel key), else the member's grant
        for the current epoch — its channel writer key. Epochs on the wire
        are the grant's
        shifted channel epoch, as on every channel request."""
        try:
            seed = await self._spaces.get_space_seed(space_id)
        except RuntimeError:
            seed = None
        stored = await self._spaces.get_gfs_channel(space_id)
        if seed is not None and stored is not None:
            channel_id, channel_pk = stored
            channel_seed = derive_channel_seed(seed, space_id, channel_id)
            if (
                channel_id == claim.scope_id
                and channel_pk_of(channel_seed) == channel_pk
            ):
                return SpaceMediaUploadAuth(
                    claim=claim,
                    kind=AUTH_CHANNEL,
                    sig=b64url_encode(
                        sign_ed25519(channel_seed, claim.channel_signing_bytes())
                    ),
                    sig_suite=CHANNEL_SUITE_ED25519,
                )
        grant = (
            await self._channels.current_grant(space_id)
            if self._channels is not None
            else None
        )
        if grant is None or grant.channel_id != claim.scope_id:
            return None
        epoch = grant.channel_epoch
        if grant.writer_key is not None:
            msg = claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=epoch)
            return SpaceMediaUploadAuth(
                claim=claim,
                kind=AUTH_WRITER_KEY,
                sig=sign_with_writer_key(
                    b64url_decode(grant.writer_key.writer_seed), msg
                ),
                sig_suite=CHANNEL_SUITE_ED25519,
                epoch=epoch,
            )
        return None


def _locator(ref: SpaceMediaRef, index: int, manifest: tuple[str, ...]) -> str:
    """The queue row's payload for one blob (``index`` -1) or one piece of
    *ref*: what :meth:`SpaceMediaGfsUploader.deliver` seals it again from —
    never the bytes."""
    return orjson.dumps(
        {
            "f": ref.filename,
            "i": ref.item_id,
            "o": ref.origin,
            "e": ref.epoch,
            "b": ref.blob_id,
            "a": ref.addr_suite,
            "s": ref.size,
            "c": ref.scope,
            "p": index,
            "m": list(manifest),
        }
    ).decode()


def _from_locator(
    row: QueuedSpaceItem,
) -> tuple[SpaceMediaRef, int, tuple[str, ...]] | None:
    """``(ref, piece index, manifest)`` of a queue row, or ``None``."""
    try:
        body = orjson.loads(row.payload or "")
    except orjson.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    manifest = body.get("m")
    index = body.get("p")
    if (
        not isinstance(manifest, list)
        or not all(is_blob_id(h) for h in manifest)
        or not isinstance(index, int)
        or isinstance(index, bool)
    ):
        return None
    suite = body.get("a")
    pieced = suite == ADDR_SUITE_SM1P
    if pieced != (0 <= index < len(manifest)) or (not pieced and index != -1):
        return None
    try:
        ref = SpaceMediaRef(
            filename=str(body["f"]),
            space_id=row.space_id,
            blob_id=str(body["b"]),
            suite=SPACE_MEDIA_SUITE_AESGCM256_PADDED,
            epoch=int(body["e"]),
            size=int(body["s"]),
            scope=str(body["c"]),
            addr_suite=str(suite),
            item_id=str(body["i"]),
            origin=str(body["o"]),
        )
    except KeyError, TypeError, ValueError:
        return None
    return ref, index, tuple(manifest)
