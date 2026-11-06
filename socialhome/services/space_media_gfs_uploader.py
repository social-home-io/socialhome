"""Space files for households reached only through a GFS — the sender side.

A space member seated from an invite link (``InstanceSource.SPACE_SESSION``)
and a follower of a global space read the space through a GFS, whose
envelope relay is far too small for pictures. So when content with local
media is created in a space that has such readers, this household:

1. **plans** (:meth:`SpaceMediaGfsUploader.plan`): finds the GFSs those
   readers use — each link-joined v_80 member's ``relay_via`` connection,
   and (for followers) every GFS the space is relayed to — keeps the ones
   that proved the signed ``space_media`` capability, and describes each file
   (≤ 8 MiB): its deterministic sealed blob's address, suite, epoch and size
   (:mod:`socialhome.space_media_seal`). The caller puts that ``media_refs``
   descriptor inside the item's encrypted payload. No reader → no plan, no
   sealing.
2. **uploads** (:meth:`SpaceMediaGfsUploader.upload`) in the background: seals
   each file once more (deterministic: the same bytes, so no sealed copy is
   kept in memory meanwhile) and PUTs it once per target GFS, authorized
   exactly as PR 1's cache requires — the space-authority signature when
   this household holds the space seed (host or delegated admin), else the
   writer group key (strict mode), else our household's writer cert
   (trusted mode, the one identified kind — never to a server we are not
   registered on). Never ``from_instance``. A PRIVATE space reached through
   its v_51 opaque channel uploads under the channel scope instead
   (``PUT /gfs/channels/{channel_id}/media/{blob_id}``: the channel key for
   a seed holder, else the member's channel writer key or channel cert from
   its grant) — the GFS learns the channel id, never the space.

``200 already_stored`` is success (another uploader or an earlier try got
there first). A refusal (403 / 413 / 422) is logged and dropped; a transport
error, 429 or 5xx is retried at most :data:`MAX_ATTEMPTS` times with backoff.
The queue is in memory and bounded: a restart or a full queue loses pending
uploads (logged) — a reader then has a descriptor for a blob that is not
cached and shows the item without that picture until it is re-uploaded.

Cost per item: one upload per file per GFS (never per reader).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiofiles
import aiofiles.os

from ..crypto import b64url_decode, b64url_encode, sign_ed25519
from ..domain.federation import InstanceSource
from ..domain.federation_capabilities import FederationCapability
from ..domain.gfs_channel import CHANNEL_SUITE_ED25519
from ..domain.gfs_space_media import (
    ADDR_SUITE_SM1,
    AUTH_AUTHORITY,
    AUTH_CHANNEL,
    AUTH_CHANNEL_CERT,
    AUTH_WRITER_CERT,
    AUTH_WRITER_KEY,
    SCOPE_CHANNEL,
    SCOPE_SPACE,
    SPACE_MEDIA_SIG_SUITE_ED25519,
    SpaceMediaClaim,
    SpaceMediaUploadAuth,
    space_media_address,
)
from ..domain.space import SpaceType
from ..domain.space_media_ref import SpaceMediaRef, media_refs_to_wire
from ..domain.writer_cert import WRITER_SCOPE_WRITE, scope_permits
from ..domain.writer_key import WRITER_KEY_SUITE_ED25519
from ..gfs_channel import channel_pk_of, derive_channel_seed
from ..space_media_seal import (
    MAX_SPACE_MEDIA_BYTES,
    SPACE_MEDIA_SUITE_AESGCM256_PADDED,
    seal_space_media,
)
from ..writer_key import sign_with_writer_key
from .inbound_media_store import (
    MEDIA_REF_PREFIX,
    is_reserved_media_name,
    local_media_ref,
    media_file_path,
)
from .space_crypto_service import AUTHORITY_SIG_SUITE_ED25519

if TYPE_CHECKING:
    from ..domain.federation import GfsConnection
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_channel_service import GfsChannelService
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption
    from .space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: Files described per item (a post carries ≤ 10 pictures + a file + a card).
MAX_FILES_PER_ITEM: int = 16

#: Uploads waiting at once; past it a new one is dropped (WARNING).
MAX_QUEUED: int = 2048

#: Attempts per (blob, GFS) before giving up, and the first backoff.
MAX_ATTEMPTS: int = 4
RETRY_BASE_S: float = 30.0

#: How long the GFS is asked to keep a blob (it clamps to its own TTL).
KEEP_S: int = 7 * 86_400

#: (GFS, blob) pairs remembered as stored, so a repeat skips the PUT — each
#: until min(KEEP_S, that GFS's TTL), after which the GFS may have dropped
#: the blob and a repeat sends it again.
MAX_REMEMBERED: int = 4096

#: Descriptors remembered per (space, epoch, file, size, mtime), so one item
#: planned by two senders (the member broadcast and the seed holder's
#: follower relay) or re-streamed in a catch-up is sealed once.
MAX_DESCRIBED: int = 2048

_STATUS_STORED = frozenset({200, 201})

#: ``_put``'s answer when this household holds no authorization for the
#: space (no seed, writer key or cert) — nothing was sent; not a GFS status.
_NO_AUTHORIZATION = -1
_STATUS_RETRY = frozenset({408, 429})


def seal_file_sync(
    media_key: bytes, space_id: str, epoch: int, filename: str, data: bytes
) -> bytes:
    """:func:`seal_space_media` positionally — the worker-thread body."""
    return seal_space_media(
        media_key=media_key,
        space_id=space_id,
        epoch=epoch,
        filename=filename,
        data=data,
    )


def _monotonic() -> float:
    return time.monotonic()


def seal_and_address_sync(
    media_key: bytes,
    space_id: str,
    epoch: int,
    filename: str,
    data: bytes,
    scope: str,
    scope_id: str,
) -> tuple[bytes, str]:
    """The sealed blob and its scope-bound GFS address
    (:func:`~socialhome.domain.gfs_space_media.space_media_address`, suite
    ``sm1``) — both CPU work, run together in a worker thread."""
    blob = seal_file_sync(media_key, space_id, epoch, filename, data)
    return blob, space_media_address(scope, scope_id, blob)


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

    def wire(self) -> dict[str, dict]:
        """The ``media_refs`` descriptor."""
        return media_refs_to_wire(self.refs)


@dataclass(slots=True, frozen=True)
class _Job:
    ref: SpaceMediaRef
    conns: tuple[GfsConnection, ...]
    attempt: int = 1


class SpaceMediaGfsUploader:
    """Plan and upload a space item's files to the GFSs its readers use."""

    __slots__ = (
        "_certs",
        "_channels",
        "_crypto",
        "_described",
        "_dir",
        "_federation",
        "_federation_repo",
        "_gfs",
        "_identity_seed",
        "_media_refs",
        "_pending",
        "_queue",
        "_retries",
        "_spaces",
        "_stop",
        "_stored",
        "_task",
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
        writer_certs: SpaceWriterCertService | None = None,
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._federation_repo = federation_repo
        self._gfs = gfs_service
        #: Which files are only this space's content (``confined_to_space``).
        self._media_refs = media_refs
        self._dir = pathlib.Path(media_dir)
        self._certs = writer_certs
        self._federation: FederationService | None = None
        #: v_51 opaque channels — a private space's member grants.
        self._channels: GfsChannelService | None = None
        self._identity_seed: bytes = b""
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._stop = asyncio.Event()
        #: Spaces already warned about lacking any upload authorization.
        self._unauthorized: set[str] = set()
        self._task: asyncio.Task | None = None
        self._retries: set[asyncio.Task] = set()
        #: Jobs queued or being retried — :meth:`drain` waits for zero.
        self._pending = 0
        #: (GFS, blob) → monotonic time until which the GFS holds it.
        self._stored: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._described: OrderedDict[tuple, SpaceMediaRef] = OrderedDict()

    def attach_federation(self, federation: FederationService) -> None:
        self._federation = federation

    def attach_writer_certs(self, writer_certs: SpaceWriterCertService) -> None:
        self._certs = writer_certs

    def attach_channels(self, channels: GfsChannelService) -> None:
        """Wire the v_51 channel service: a private space with an opaque
        channel uploads under the channel scope."""
        self._channels = channels

    async def scope_of(self, space_id: str) -> tuple[str, str]:
        """``(scope, scope_id)`` *space_id*'s blobs are uploaded under: the
        opaque channel of a PRIVATE space that uses one (``private_gfs``),
        else the space itself (listed on the GFS)."""
        space = await self._spaces.get(space_id)
        if (
            space is not None
            and space.space_type is SpaceType.PRIVATE
            and space.features.private_gfs
        ):
            stored = await self._spaces.get_gfs_channel(space_id)
            if stored is not None:
                return SCOPE_CHANNEL, stored[0]
        return SCOPE_SPACE, space_id

    def attach_identity(self, *, own_identity_seed: bytes) -> None:
        self._identity_seed = own_identity_seed

    # ── Readers ──────────────────────────────────────────────────────────

    async def is_relay_reader(self, instance_id: str) -> bool:
        """Whether *instance_id* is a member household seated from an invite
        link that takes ``media_refs`` (v_80): its items carry the
        descriptor, and it gets no ``SPACE_MEDIA_BLOB`` push."""
        if self._federation is None:
            return False
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None or inst.source is not InstanceSource.SPACE_SESSION:
            return False
        return await self._federation.peer_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_SPACE_MEDIA_REFS
        )

    async def _reader_conn(self, instance_id: str) -> GfsConnection | None:
        if not await self.is_relay_reader(instance_id):
            return None
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None or not inst.relay_via:
            return None
        return await self._gfs.connection_for_url(inst.relay_via)

    async def _relay_reader_conns(self, space_id: str) -> list[GfsConnection]:
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
        self, space_id: str, media_urls: Iterable[str | None], *, followers: bool
    ) -> SpaceMediaPlan | None:
        """The plan for an item of *space_id* referencing *media_urls*:
        link-joined v_80 members' GFSs, plus (``followers``) the GFSs the
        space is relayed to. ``None`` when no capable GFS needs it or no
        file can be described — nothing is sealed then."""
        names = media_names(media_urls)
        if not names:
            return None
        conns = await self._relay_reader_conns(space_id)
        if followers:
            conns.extend(await self._gfs.space_media_follower_targets(space_id))
        return await self._plan(space_id, names, conns)

    async def plan_for_reader(
        self, space_id: str, media_urls: Iterable[str | None], instance_id: str
    ) -> SpaceMediaPlan | None:
        """The plan for ONE link-joined reader (a catch-up stream): only its
        GFS. ``None`` when it is no v_80 link-joined member."""
        names = media_names(media_urls)
        conn = await self._reader_conn(instance_id) if names else None
        if conn is None:
            return None
        return await self._plan(space_id, names, [conn])

    async def _plan(
        self, space_id: str, names: list[str], conns: list[GfsConnection]
    ) -> SpaceMediaPlan | None:
        scope, _scope_id = await self.scope_of(space_id)
        targets = await self._capable(conns, scope)
        if not targets:
            return None
        refs = await self._describe_names(space_id, names)
        if not refs:
            return None
        return SpaceMediaPlan(
            space_id=space_id, refs=tuple(refs), targets=tuple(targets)
        )

    async def _describe_names(
        self, space_id: str, names: list[str]
    ) -> list[SpaceMediaRef]:
        current = await self._crypto.media_key(space_id)
        if current is None:
            return []
        epoch, key = current
        scope, scope_id = await self.scope_of(space_id)
        refs: list[SpaceMediaRef] = []
        for name in names[:MAX_FILES_PER_ITEM]:
            if not await self._space_file(space_id, name):
                continue
            stamp = await self._stamp(name)
            if stamp is None:
                continue
            cache_key = (space_id, epoch, scope, scope_id, name, *stamp)
            cached = self._described.get(cache_key)
            if cached is not None:
                self._described.move_to_end(cache_key)
                refs.append(cached)
                continue
            data = await self._read(name)
            if data is None:
                continue
            blob, address = await asyncio.to_thread(
                seal_and_address_sync, key, space_id, epoch, name, data, scope, scope_id
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
            )
            self._described[cache_key] = ref
            while len(self._described) > MAX_DESCRIBED:
                self._described.popitem(last=False)
            refs.append(ref)
        return refs

    async def _space_file(self, space_id: str, name: str) -> bool:
        """Whether *name* may be sealed under *space_id*'s key: never a
        name another receiving path owns (DM / moment media), and only a
        file nothing but this space's content references — a member may
        name another space's, a private album's or a household file in a
        post or record of this space (review D)."""
        if is_reserved_media_name(name):
            return False
        return await self._media_refs.confined_to_space(name, space_id)

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

    # ── Uploading ────────────────────────────────────────────────────────

    def upload(self, plan: SpaceMediaPlan | None) -> None:
        """Queue *plan*'s blobs for upload (never blocks, never raises)."""
        if plan is None:
            return
        for ref in plan.refs:
            if self._queue.qsize() >= MAX_QUEUED:
                log.warning(
                    "space media upload queue is full — %s of space %s not "
                    "uploaded to its GFS",
                    ref.filename,
                    plan.space_id,
                )
                continue
            self._pending += 1
            self._queue.put_nowait(_Job(ref=ref, conns=plan.targets))

    async def drain(self) -> None:
        """Wait until every queued upload (and retry) finished — tests and
        the federation demo."""
        while self._pending > 0:
            await asyncio.sleep(0.01)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._retries):
            task.cancel()  # only sleeping before a re-queue: safe to cancel
        self._retries.clear()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _next_job(self) -> _Job | None:
        """The next queued job, or ``None`` once :meth:`stop` was called —
        whichever comes first (an upload in progress always finishes)."""
        get = asyncio.ensure_future(self._queue.get())
        stopped = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait({get, stopped}, return_when=asyncio.FIRST_COMPLETED)
        stopped.cancel()
        if get.done():
            return get.result()
        get.cancel()
        return None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            job = await self._next_job()
            if job is None:
                break
            try:
                await self._run(job)
            except Exception:  # pragma: no cover - defensive
                log.exception("space media upload of %s failed", job.ref.filename)
            finally:
                self._pending -= 1

    async def _run(self, job: _Job) -> None:
        ref = job.ref
        now = _monotonic()
        todo = [
            c for c in job.conns if self._stored.get((c.id, ref.blob_id), 0.0) <= now
        ]
        if not todo:
            return
        scope, scope_id = await self.scope_of(ref.space_id)
        blob = await self._reseal(ref, scope, scope_id)
        if blob is None:
            return
        retry: list[GfsConnection] = []
        for conn in todo:
            limit = await self._gfs.space_media_max_blob(conn, scope=scope)
            if limit is None or len(blob) > limit:
                log.info(
                    "space media %s (%d bytes) is over what GFS %s keeps — not sent",
                    ref.filename,
                    len(blob),
                    conn.id,
                )
                continue
            status = await self._put(conn, ref, blob, scope, scope_id)
            if status == _NO_AUTHORIZATION:
                return  # nothing to send anywhere; warned once per space
            if status in _STATUS_STORED:
                await self._remember(conn, ref.blob_id)
                log.info(
                    "space media %s: uploaded blob %s (%d bytes) to GFS %s (HTTP %d)",
                    ref.filename,
                    ref.blob_id[:12],
                    len(blob),
                    conn.id,
                    status,
                )
            elif status is None or status in _STATUS_RETRY or status >= 500:
                retry.append(conn)
            else:
                log.warning(
                    "GFS %s refused space media %s of space %s — HTTP %d",
                    conn.id,
                    ref.filename,
                    ref.space_id,
                    status,
                )
        if retry:
            self._retry(job, tuple(retry))

    def _retry(self, job: _Job, conns: tuple[GfsConnection, ...]) -> None:
        if job.attempt >= MAX_ATTEMPTS:
            log.warning(
                "space media %s of space %s: gave up on GFS %s after %d attempts",
                job.ref.filename,
                job.ref.space_id,
                ", ".join(c.id for c in conns),
                job.attempt,
            )
            return
        delay = RETRY_BASE_S * (2 ** (job.attempt - 1))
        nxt = _Job(ref=job.ref, conns=conns, attempt=job.attempt + 1)
        self._pending += 1
        task = asyncio.create_task(self._requeue(nxt, delay))
        self._retries.add(task)
        task.add_done_callback(self._retries.discard)

    async def _requeue(self, job: _Job, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            self._pending -= 1
            raise
        self._queue.put_nowait(job)

    async def _remember(self, conn: GfsConnection, blob_id: str) -> None:
        """Remember *blob_id* as held by *conn* until min(:data:`KEEP_S`,
        its TTL) — an ``already_stored`` answer does not refresh a blob's
        expiry, so the upload time is the bound."""
        ttl = await self._gfs.space_media_ttl_s(conn)
        keep = min(KEEP_S, ttl) if ttl else KEEP_S
        key = (conn.id, blob_id)
        self._stored[key] = _monotonic() + keep
        self._stored.move_to_end(key)
        while len(self._stored) > MAX_REMEMBERED:
            self._stored.popitem(last=False)

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
        ref: SpaceMediaRef,
        blob: bytes,
        scope: str,
        scope_id: str,
    ) -> int | None:
        now = int(time.time())
        claim = SpaceMediaClaim(
            scope=scope,
            scope_id=scope_id,
            blob_id=ref.blob_id,
            size=len(blob),
            expires_at=now + KEEP_S,
            ts=now,
        )
        auth = (
            await self._authorize_channel(claim, ref.space_id)
            if scope == SCOPE_CHANNEL
            else await self._authorize(claim)
        )
        if auth is None:
            if ref.space_id not in self._unauthorized:
                self._unauthorized.add(ref.space_id)
                log.warning(
                    "space media of space %s: this household holds no seed, "
                    "writer key or writer cert — its files are not uploaded",
                    ref.space_id,
                )
            return _NO_AUTHORIZATION
        return await self._gfs.put_space_media(
            conn,
            blob_id=ref.blob_id,
            blob=blob,
            auth_header=auth.to_header(),
            identified=auth.kind in (AUTH_WRITER_CERT, AUTH_CHANNEL_CERT),
        )

    async def _authorize_channel(
        self, claim: SpaceMediaClaim, space_id: str
    ) -> SpaceMediaUploadAuth | None:
        """The channel scope's authorization: the channel key (a seed holder
        whose seed derives the stored channel key), else the member's grant
        for the current epoch — its channel writer key (anonymous), else its
        channel cert (identified). Epochs on the wire are the grant's
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
        if grant.channel_cert is None or not self._identity_seed:
            return None
        msg = claim.writer_signing_bytes(kind=AUTH_CHANNEL_CERT, epoch=epoch)
        return SpaceMediaUploadAuth(
            claim=claim,
            kind=AUTH_CHANNEL_CERT,
            sig=b64url_encode(sign_ed25519(self._identity_seed, msg)),
            sig_suite=SPACE_MEDIA_SIG_SUITE_ED25519,
            epoch=epoch,
            channel_cert=grant.channel_cert,
        )

    async def _authorize(self, claim: SpaceMediaClaim) -> SpaceMediaUploadAuth | None:
        """The strongest anonymous authorization this household holds: the
        space authority (seed holder), else the writer group key, else our
        writer cert (identified)."""
        space_id = claim.scope_id
        try:
            seed = await self._spaces.get_space_seed(space_id)
        except RuntimeError:
            seed = None
        if seed is not None:
            sig = sign_ed25519(seed, claim.authority_signing_bytes())
            return SpaceMediaUploadAuth(
                claim=claim,
                kind=AUTH_AUTHORITY,
                sig=b64url_encode(sig),
                sig_suite=AUTHORITY_SIG_SUITE_ED25519,
            )
        if self._certs is None:
            return None
        epoch = await self._crypto.get_current_epoch(space_id)
        if epoch is None:
            return None
        writer_seed = await self._certs.own_writer_key(space_id, epoch)
        if writer_seed is not None:
            msg = claim.writer_signing_bytes(kind=AUTH_WRITER_KEY, epoch=epoch)
            return SpaceMediaUploadAuth(
                claim=claim,
                kind=AUTH_WRITER_KEY,
                sig=sign_with_writer_key(writer_seed, msg),
                sig_suite=WRITER_KEY_SUITE_ED25519,
                epoch=epoch,
            )
        cert = await self._certs.own_cert(space_id, epoch)
        if (
            cert is None
            or not self._identity_seed
            or not scope_permits(cert.scope, WRITER_SCOPE_WRITE)
        ):
            return None
        msg = claim.writer_signing_bytes(kind=AUTH_WRITER_CERT, epoch=epoch)
        return SpaceMediaUploadAuth(
            claim=claim,
            kind=AUTH_WRITER_CERT,
            sig=b64url_encode(sign_ed25519(self._identity_seed, msg)),
            sig_suite=SPACE_MEDIA_SIG_SUITE_ED25519,
            epoch=epoch,
            writer_cert=cert,
        )
