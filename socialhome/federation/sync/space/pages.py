"""Sync pages on the §25.6 session — seal, carry, verify, store, check.

``docs/protocol/sync-pages.md`` Phase 2. No new message and no timer:

* **Seal (signer).** When this household holds the space's authority seed
  and acts as its host (the owner, or a delegated admin —
  :func:`~socialhome.federation.host_equivalence.serves_as_host`) and is
  about to end a stream with its sentinel, it seals a new page in the
  background — but only when the space changed since its stored page and
  that page is ≥ :data:`~socialhome.sync_page.PAGE_MIN_INTERVAL_S` old (or
  of another epoch, suite or cutoff day). One seal per space at a time; the
  sentinel never waits for it (it carries the stored page).
* **Carry (any provider).** The sentinel's ``page_sealed`` carries the
  newest page this household holds — sealed here or verified from another
  provider (the member-to-member relay) — sealed under the space content
  key like a chunk, and only to a requester at
  ``MIN_FOR_SYNC_PAGES`` whose BEGIN shows an older page (or none). Next to
  it the vector **delta** when the requester holds the signer's previous
  page, else the full vector.
* **Verify and store (member).** A received page must pass
  :func:`~socialhome.sync_page.verify_page` against the key and epoch this
  household pins, be newer than the one held, and come with a vector that
  hashes to its root. Two pages of one signer that cannot both be honest
  are logged at WARNING and flagged in the status (equivocation).
* **Compare (member).** On each newer page — at most once per 15 min per
  space — the member recomputes its own vector with the page's cutoff. A
  differing bucket in which it holds a row changed after the page is
  explained (it is ahead there). At most :data:`MAX_LISTING_BUCKETS` of the
  unexplained ones are asked of a household acting as host as **bucket
  listings** on the next round's BEGIN to it (``buckets``, :meth:`
  buckets_for` — never a BEGIN of its own; Phase 3 sends the same field to
  any member): the listings end that round's stream and are verified
  against the page's signed bucket hashes on its sentinel, and each listed
  row is classified missing or ahead against our rows read then. Only a
  proven missing row makes the space ``behind``.
* **Listings (provider).** A household holding the page lists a bucket as
  of the page only while its own rows of it still hash to the page; acting
  as host, it lists a changed bucket as it is now (``current``).

Storage: one ``space_sync_pages`` row per space (migration 0151). Compare
results, pending listings and conflict flags are memory only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import orjson

from ....canonical_json import canonical_str
from ....crypto import ed25519_public_key
from ....domain.federation_capabilities import FederationCapability
from ....domain.space_sync_page import StoredSyncPage, SyncPagePosition
from ....sync_page import (
    COMMITTED_RESOURCES,
    MAX_LISTING_BUCKETS,
    MAX_LISTING_ROWS,
    PAGE_MIN_INTERVAL_S,
    PAGE_SUITE_SHA256_B256,
    PageRefused,
    SyncPage,
    UnsupportedPageSuite,
    apply_delta,
    delta_from_bytes,
    delta_from_wire,
    delta_to_bytes,
    delta_to_wire,
    differing_buckets,
    is_conflict,
    is_superseded,
    listing_to_wire,
    page_time,
    parse_listing,
    parse_page,
    root_of,
    sign_page,
    vector_delta,
    vector_from_wire,
    vector_to_wire,
    verify_listing,
    verify_page,
)
from ...host_equivalence import serves_as_host
from .exporter import record_batches

if TYPE_CHECKING:
    from ....domain.space import Space
    from ....repositories.space_sync_page_repo import AbstractSpaceSyncPageRepo
    from .exporter import ResourceExporter
    from .page_state import BucketRows, PageStateHasher

log = logging.getLogger(__name__)

#: The sealed ``page_sealed`` field of a sentinel is never larger than this
#: (a ≤ 1 KiB page + an 8 KiB vector, base64 and AES-GCM overhead).
MAX_SEALED_PAGE_CHARS: int = 24 * 1024

#: Spaces whose compare result / conflict flag is remembered.
MAX_TRACKED: int = 4096

#: Rounds per space whose listing records are kept until their sentinel.
MAX_PENDING_LISTINGS: int = 8


class _Spaces(Protocol):
    async def get(self, space_id: str) -> Space | None: ...

    async def get_space_seed(self, space_id: str) -> bytes | None: ...

    async def list_local_member_user_ids(self, space_id: str) -> list[str]: ...

    async def list_members(self, space_id: str) -> list[Any]: ...


class _Seqs(Protocol):
    async def current_seq(self) -> int: ...

    async def applied_seq(self, space_id: str, instance_id: str) -> int | None: ...


class _Crypto(Protocol):
    async def encrypt_chunk(
        self, *, space_id: str, sync_id: str, plaintext: bytes
    ) -> tuple[int, str]: ...

    async def decrypt_chunk(
        self, *, space_id: str, epoch: int, sync_id: str, ciphertext: str
    ) -> bytes: ...


@dataclass(slots=True, frozen=True)
class _Held:
    page: SyncPage
    row: StoredSyncPage


@dataclass(slots=True, frozen=True)
class _Compare:
    """One compare of our copy with a page (memory only)."""

    root: bytes
    differing: int
    #: Differing buckets with no newer local row.
    unexplained: int
    #: Rows a listing proved missing; ``None`` while listings are pending.
    missing: int | None
    #: Unexplained buckets no listing settled (over the cap, not held, refused).
    unresolved: int


@dataclass(slots=True)
class _Wanted:
    """The bucket listings our next BEGIN to *host* asks for (memory only):
    the page they must hash to and what each round brought."""

    host: str
    root: bytes
    vector: bytes
    cutoff: str | None
    buckets: tuple[int, ...]
    #: sync_id → the ``__listing__`` records that session brought.
    received: dict[str, list[object]] = field(default_factory=dict)


def _classify(
    records: list[object],
    buckets: tuple[int, ...],
    vector: bytes,
    local: BucketRows,
) -> tuple[int, int]:
    """``(missing rows, unresolved buckets)`` of one round's listings
    against our rows (*local*, read after that round's rows were applied).
    Raises :class:`PageRefused` for a listing that is malformed, of a bucket
    not asked, repeated, or that does not hash to the signed bucket."""
    seen: set[int] = set()
    missing = 0
    unresolved = 0
    for raw in records:
        index, rows, current = parse_listing(raw)
        if index not in buckets or index in seen:
            raise PageRefused(f"a listing of bucket {index} not asked for")
        seen.add(index)
        if rows is None:
            unresolved += 1
            continue
        if not current:
            verify_listing(index, rows, vector)
        # ``current``: the host's rows as they are now (it changed the
        # bucket since the page) — trusted as its stream is: we only ever
        # ask a household acting as the space's host.
        mine = {(r.resource, r.row_id): r for r in local.rows.get(index, [])}
        for row in rows:
            have = mine.get((row.resource, row.row_id))
            if have is None:
                if not is_superseded(row.resource, row.row_id, local.gone):
                    missing += 1
            elif have.leaf != row.leaf and not (
                have.marker is not None
                and (row.marker is None or have.marker > row.marker)
            ):
                missing += 1
        # A row of ours not listed was created after the page: ahead.
    return missing, unresolved + len(buckets) - len(seen)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _cutoff_day(cutoff: str | None) -> str | None:
    return cutoff[:10] if cutoff else None


class SyncPageService:
    """Sync pages on the §25.6 session (see the module docstring)."""

    __slots__ = (
        "_checks",
        "_clock_ms",
        "_compared_at",
        "_comparing",
        "_conflicts",
        "_crypto",
        "_exporters",
        "_hosts",
        "_locks",
        "_monotonic",
        "_on_change",
        "_own_instance_id",
        "_repo",
        "_seqs",
        "_sleep",
        "_spaces",
        "_state",
        "_tasks",
        "_wanted",
    )

    def __init__(
        self,
        *,
        repo: AbstractSpaceSyncPageRepo,
        spaces: _Spaces,
        seqs: _Seqs,
        crypto: _Crypto,
        state: PageStateHasher,
        exporters: Mapping[str, ResourceExporter],
        own_instance_id: str,
        on_change: Callable[[str], Awaitable[None]] | None = None,
        clock_ms: Callable[[], int] = _now_ms,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._repo = repo
        self._spaces = spaces
        self._seqs = seqs
        self._crypto = crypto
        self._state = state
        self._exporters = exporters
        self._own_instance_id = own_instance_id
        #: Called with the space id when the page held here changed (the
        #: sync status recomputes).
        self._on_change = on_change
        self._clock_ms = clock_ms
        self._monotonic = monotonic
        self._sleep = sleep
        #: The households acting as a space's host (:meth:`attach_hosts`).
        self._hosts: Callable[[Space], Awaitable[list[str]]] | None = None
        #: space → the listings our next BEGIN to its host asks for.
        self._wanted: dict[str, _Wanted] = {}
        #: Spaces with a compare pending; space → when it last compared.
        self._comparing: set[str] = set()
        self._compared_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        #: space → the last compare of our copy with its page.
        self._checks: OrderedDict[str, _Compare] = OrderedDict()
        #: space → the (epoch, signer) whose equivocation we saw.
        self._conflicts: OrderedDict[str, tuple[int, str]] = OrderedDict()

    # ─── The page held here ───────────────────────────────────────────

    async def _held(self, space: Space) -> _Held | None:
        """The stored page of *space* when it is of the epoch we pin now.
        A malformed row (never written by us) is treated as none."""
        row = await self._repo.get(space.id)
        if row is None:
            return None
        try:
            page = parse_page(orjson.loads(row.page_json))
        except ValueError, orjson.JSONDecodeError:
            log.warning("sync page: the stored page of %s is unreadable", space.id)
            return None
        if page.epoch != int(space.authority_key_epoch or 0):
            return None
        return _Held(page=page, row=row)

    async def ref_for(self, space_id: str) -> dict | None:
        """``page`` for our ``SPACE_SYNC_BEGIN``: what we hold (signer,
        epoch, seq, issued_at, root), or ``None``."""
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        held = await self._held(space)
        return None if held is None else held.page.ref()

    # ─── Seal (signer) ────────────────────────────────────────────────

    async def _signing_seed(self, space: Space) -> bytes | None:
        """The seed when this household may sign the space's pages: it acts
        as host and holds the seed of the key the space pins."""
        if not await serves_as_host(
            self._spaces,  # type: ignore[arg-type]
            space,
            own_instance_id=self._own_instance_id,
        ):
            return None
        seed = await self._spaces.get_space_seed(space.id)
        if seed is None:
            return None
        if ed25519_public_key(seed).hex() != str(space.identity_public_key).lower():
            return None
        return seed

    def seal_soon(self, space_id: str) -> None:
        """Start a background seal of *space_id* when one is due (never
        waits; one at a time per space)."""
        lock = self._locks.setdefault(space_id, asyncio.Lock())
        if lock.locked():
            return
        task = asyncio.create_task(
            self.seal_if_due(space_id), name=f"sync-page-seal-{space_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def seal_if_due(self, space_id: str) -> SyncPage | None:
        """Seal and store a new page when this household signs the space
        and one is due; the new page, or ``None``. Fail-soft."""
        lock = self._locks.setdefault(space_id, asyncio.Lock())
        if lock.locked():
            return None
        async with lock:
            try:
                return await self._seal_if_due(space_id)
            except Exception:
                log.exception("sync page: sealing a page of %s failed", space_id)
                return None

    async def _seal_if_due(self, space_id: str) -> SyncPage | None:
        space = await self._spaces.get(space_id)
        if space is None:
            return None
        seed = await self._signing_seed(space)
        if seed is None:
            return None
        epoch = int(space.authority_key_epoch or 0)
        held = await self._held(space)
        own = (
            held
            if held is not None
            and held.page.signer == self._own_instance_id
            and held.page.page_suite == PAGE_SUITE_SHA256_B256
            else None
        )
        cutoff = await self._state.cutoff(space_id)
        if own is not None:
            if not await self._changed_since(space_id, own.page.seq):
                return None
            young = self._clock_ms() - own.page.issued_at < PAGE_MIN_INTERVAL_S * 1000
            if young and _cutoff_day(own.page.cutoff) == _cutoff_day(cutoff):
                return None
        elif (
            held is not None
            and self._clock_ms() - held.page.issued_at < PAGE_MIN_INTERVAL_S * 1000
        ):
            # Another signer's page (the owner's, held by a delegated admin
            # that also syncs as a member) is fresh: the space has a recent
            # page, so we do not add one of our own on top of every one of it.
            return None
        # Read before the export (as a stream's snapshot): a row changed while
        # we hash is stamped above it, so the next seal sees it as changed.
        seq = await self._seqs.current_seq()
        started = time.perf_counter()
        vector, rows = await self._state.vector(space_id, cutoff=cutoff)
        page = sign_page(
            space_id=space_id,
            epoch=epoch,
            signer=self._own_instance_id,
            seq=seq,
            issued_at=self._clock_ms(),
            cutoff=cutoff,
            rows=rows,
            vector=vector,
            prev_root=own.page.root if own is not None else None,
            space_seed=seed,
        )
        delta = (
            delta_to_bytes(vector_delta(own.row.vector, vector))
            if own is not None
            else None
        )
        await self._repo.put(
            StoredSyncPage(
                space_id=space_id,
                page_json=canonical_str(page.to_wire()),
                vector=vector,
                delta=delta,
            )
        )
        log.info(
            "sync page: sealed page %d of space %s (%d rows, %.2f s)",
            seq,
            space_id,
            rows,
            time.perf_counter() - started,
        )
        await self._changed(space_id)
        return page

    async def _changed_since(self, space_id: str, seq: int) -> bool:
        """Whether a committed row of the space was stamped above *seq* —
        the ``is_quiet`` probe of the committed resources; reads up to the
        first changed row."""
        for resource in COMMITTED_RESOURCES:
            exporter = self._exporters.get(resource)
            if exporter is None:
                continue
            batches = record_batches(exporter, space_id, since=seq)
            try:
                async for _batch in batches:
                    return True
            finally:
                await batches.aclose()
        return False

    # ─── Carry (any provider) ─────────────────────────────────────────

    async def sentinel_page(
        self,
        *,
        space_id: str,
        sync_id: str,
        requester_version: int,
        requester_ref: Mapping[str, Any] | None,
    ) -> dict | None:
        """The sentinel's ``page_sealed`` for one session, or ``None``: none
        held, an older requester, or it already holds this page or a newer
        one. Also starts a background seal when one is due here."""
        if requester_version < FederationCapability.MIN_FOR_SYNC_PAGES:
            return None
        self.seal_soon(space_id)
        try:
            space = await self._spaces.get(space_id)
            if space is None:
                return None
            held = await self._held(space)
            if held is None:
                return None
            page = held.page
            ref = requester_ref or None
            if ref is not None:
                if ref.get("root") == page.root:
                    return None
                ref_key = (
                    int(ref.get("epoch", 0)),
                    int(ref.get("issued_at", 0)),
                    int(ref.get("seq", 0)),
                )
                if ref_key >= page.order_key():
                    return None
            body: dict[str, Any] = {"page": page.to_wire()}
            if (
                ref is not None
                and held.row.delta is not None
                and page.prev_root is not None
                and ref.get("root") == page.prev_root
                and ref.get("signer") == page.signer
                and ref.get("epoch") == page.epoch
            ):
                body["delta"] = delta_to_wire(delta_from_bytes(held.row.delta))
            else:
                body["vector"] = vector_to_wire(held.row.vector)
            epoch, ciphertext = await self._crypto.encrypt_chunk(
                space_id=space_id, sync_id=sync_id, plaintext=orjson.dumps(body)
            )
            return {"epoch": epoch, "payload": ciphertext}
        except Exception:
            log.exception("sync page: attaching the page of %s failed", space_id)
            return None

    # ─── Verify and store (member) ────────────────────────────────────

    async def receive(
        self, *, space_id: str, sync_id: str, provider: str, sealed: object
    ) -> bool:
        """Take the ``page_sealed`` of a verified sentinel: decrypt, verify,
        store when newer. Returns whether a page was stored. A refused page
        is logged (WARNING when it fails verification) and the page held
        stays."""
        if sealed is None:
            await self._compare_if_unjudged(space_id)
            return False
        try:
            stored = await self._receive(space_id, sync_id, provider, sealed)
            if not stored:
                await self._compare_if_unjudged(space_id)
            return stored
        except (PageRefused, UnsupportedPageSuite) as exc:
            log.warning(
                "sync page: refused the page of space %s from %s: %s",
                space_id,
                provider,
                exc,
            )
            return False
        except Exception:
            log.exception(
                "sync page: taking the page of space %s from %s failed",
                space_id,
                provider,
            )
            return False

    async def _receive(
        self, space_id: str, sync_id: str, provider: str, sealed: object
    ) -> bool:
        if not isinstance(sealed, dict):
            raise PageRefused("page_sealed is not an object")
        epoch = sealed.get("epoch")
        ciphertext = sealed.get("payload")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or not isinstance(ciphertext, str)
            or len(ciphertext) > MAX_SEALED_PAGE_CHARS
        ):
            raise PageRefused("malformed page_sealed")
        space = await self._spaces.get(space_id)
        if space is None or not space.identity_public_key:
            return False
        try:
            plaintext = await self._crypto.decrypt_chunk(
                space_id=space_id, epoch=epoch, sync_id=sync_id, ciphertext=ciphertext
            )
        except Exception as exc:
            raise PageRefused(
                f"page_sealed does not open ({type(exc).__name__})"
            ) from exc
        try:
            body = orjson.loads(plaintext)
        except orjson.JSONDecodeError as exc:
            raise PageRefused("page_sealed is not JSON") from exc
        if not isinstance(body, dict):
            raise PageRefused("page_sealed is not an object")
        page = verify_page(
            body.get("page"),
            space_id=space_id,
            space_public_key=bytes.fromhex(str(space.identity_public_key)),
            pinned_epoch=int(space.authority_key_epoch or 0),
            now_ms=self._clock_ms(),
        )
        held = await self._held(space)
        if held is not None and is_conflict(held.page, page):
            self._note_conflict(space_id, held.page, page, provider)
        if held is not None and not page.is_newer_than(held.page):
            return False
        if (
            held is not None
            and held.page.signer == self._own_instance_id
            and page.signer != self._own_instance_id
        ):
            # We sign this space too (the owner and a delegated admin): our
            # own page and its delta chain stay; another signer's page is
            # never stored over it (it would break our chain and make our
            # next page look like equivocation to the members).
            return False
        if "vector" in body:
            vector = vector_from_wire(body["vector"])
        elif (
            "delta" in body
            and held is not None
            and held.page.signer == page.signer
            and held.page.root == page.prev_root
        ):
            vector = apply_delta(held.row.vector, delta_from_wire(body["delta"]))
        else:
            # A delta against a page we do not hold. Refused WITHOUT
            # dropping ours: an honest provider sends a delta only for the
            # page our BEGIN named, so this is a stale race or a relay trying
            # to roll us back (drop the newer page, then offer an older one).
            raise PageRefused("a delta without its previous page")
        if root_of(vector) != page.root:
            raise PageRefused("the vector does not hash to the page's root")
        await self._repo.put(
            StoredSyncPage(
                space_id=space_id,
                page_json=canonical_str(page.to_wire()),
                vector=vector,
            )
        )
        log.debug(
            "sync page: stored page %d of space %s from %s (signer %s)",
            page.seq,
            space_id,
            provider,
            page.signer,
        )
        await self._changed(space_id)
        self.compare_soon(space_id)
        return True

    def _note_conflict(
        self, space_id: str, held: SyncPage, incoming: SyncPage, provider: str
    ) -> None:
        log.warning(
            "sync page: EQUIVOCATION in space %s — signer %s signed two pages "
            "that cannot both be honest (epoch %d): held %s (seq %d) and %s "
            "(seq %d, relayed by %s); keeping the newer",
            space_id,
            incoming.signer,
            incoming.epoch,
            held.page_hash(),
            held.seq,
            incoming.page_hash(),
            incoming.seq,
            provider,
        )
        self._conflicts[space_id] = (incoming.epoch, incoming.signer)
        self._conflicts.move_to_end(space_id)
        while len(self._conflicts) > MAX_TRACKED:
            self._conflicts.popitem(last=False)

    async def _changed(self, space_id: str) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change(space_id)
        except Exception:
            log.exception("sync page: status update for %s failed", space_id)

    # ─── Compare (member) ─────────────────────────────────────────────

    def attach_hosts(self, hosts: Callable[[Space], Awaitable[list[str]]]) -> None:
        """``hosts(space)`` — the households acting as the space's host here:
        the one an unexplained difference is asked of (the signer when it
        is one of them)."""
        self._hosts = hosts

    async def _compare_if_unjudged(self, space_id: str) -> None:
        """A sentinel brought no newer page: compare the held page only if
        it has no verdict yet — compare results live in memory, so after a
        restart the stored page would otherwise wait for the next page.
        Never a second compare of a judged page. Fail-soft."""
        try:
            if space_id in self._comparing:
                return
            space = await self._spaces.get(space_id)
            held = await self._held(space) if space is not None else None
            if held is None or held.page.signer == self._own_instance_id:
                return
            check = self._checks.get(space_id)
            if check is not None and check.root == held.page.root:
                return  # judged already
            self.compare_soon(space_id)
        except Exception:
            log.exception("sync page: checking the verdict of %s failed", space_id)

    def compare_soon(self, space_id: str) -> None:
        """A newer page was stored: compare our copy with it — at once, or,
        when the last compare of the space is younger than the seal cadence
        (:data:`PAGE_MIN_INTERVAL_S`), once that much has passed, against
        whatever page is newest then. One pending compare per space."""
        if space_id in self._comparing:
            return
        self._comparing.add(space_id)
        task = asyncio.create_task(
            self._compare_when_due(space_id), name=f"sync-page-compare-{space_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _compare_when_due(self, space_id: str) -> None:
        try:
            last = self._compared_at.get(space_id)
            if last is not None:
                wait = PAGE_MIN_INTERVAL_S - (self._monotonic() - last)
                if wait > 0:
                    await self._sleep(wait)
            # From here a newer page schedules its own (spaced) compare.
            self._comparing.discard(space_id)
            await self.compare(space_id)
        finally:
            self._comparing.discard(space_id)

    async def compare(self, space_id: str) -> _Compare | None:
        """Compare our copy of the space with the held page (never a page we
        signed). Fail-soft; ``None`` when skipped.

        1. Our bucket vector against the page's: the differing buckets.
        2. A differing bucket in which we hold a row changed after the page
           is explained — we are ahead there (live events between pages).
           No wire cost.
        3. The unexplained ones, at most :data:`MAX_LISTING_BUCKETS`, are
           asked of a household acting as host as bucket listings
           (:meth:`take_listing` / :meth:`listing_done` classify the rows);
           the rest wait for the next page.
        """
        try:
            space = await self._spaces.get(space_id)
            if space is None:
                return None
            held = await self._held(space)
            if held is None or held.page.signer == self._own_instance_id:
                return None
            page = held.page
            self._compared_at[space_id] = self._monotonic()
            while len(self._compared_at) > MAX_TRACKED:
                self._compared_at.pop(next(iter(self._compared_at)))
            started = time.perf_counter()
            vector, _rows = await self._state.vector(space_id, cutoff=page.cutoff)
            differing = differing_buckets(vector, held.row.vector)
            unexplained: list[int] = []
            if differing:
                scan = await self._state.rows_in(
                    space_id, differing, cutoff=page.cutoff
                )
                newer_than = page_time(page)
                for index in differing:
                    rows = scan.rows.get(index, [])
                    if any(
                        r.marker is not None and r.marker > newer_than for r in rows
                    ):
                        continue  # ahead in this bucket
                    unexplained.append(index)
            asked = unexplained[:MAX_LISTING_BUCKETS]
            host = await self._host_for(space, page.signer) if asked else None
            self._wanted.pop(space_id, None)
            if host is not None:
                # Rides the next incremental BEGIN to that host (periodic or
                # Sync now) — never a BEGIN of its own.
                self._wanted[space_id] = _Wanted(
                    host=host,
                    root=page.root,
                    vector=held.row.vector,
                    cutoff=page.cutoff,
                    buckets=tuple(asked),
                )
                while len(self._wanted) > MAX_TRACKED:
                    self._wanted.pop(next(iter(self._wanted)))
            result = _Compare(
                root=page.root,
                differing=len(differing),
                unexplained=len(unexplained),
                missing=0 if not unexplained else None,
                unresolved=len(unexplained) - len(asked),
            )
            self._remember(space_id, result)
            log.info(
                "sync page: space %s against page %d: %d bucket(s) differ, %d "
                "unexplained (%.2f s)%s",
                space_id,
                page.seq,
                len(differing),
                len(unexplained),
                time.perf_counter() - started,
                f"; the next round to {host} lists {len(asked)}" if host else "",
            )
            await self._changed(space_id)
            return result
        except Exception:
            log.exception("sync page: comparing space %s failed", space_id)
            return None

    def _remember(self, space_id: str, result: _Compare) -> None:
        self._checks[space_id] = result
        self._checks.move_to_end(space_id)
        while len(self._checks) > MAX_TRACKED:
            self._checks.popitem(last=False)

    async def _host_for(self, space: Space, signer: str) -> str | None:
        if self._hosts is None:
            return None
        hosts = await self._hosts(space)
        if not hosts:
            return None
        return signer if signer in hosts else hosts[0]

    def buckets_for(self, space_id: str, to_instance_id: str) -> list[int] | None:
        """``buckets`` for our next BEGIN of *space_id* to *to_instance_id*:
        the unexplained buckets of the last compare, when it is the host
        they are asked of; else ``None``."""
        wanted = self._wanted.get(space_id)
        if wanted is None or wanted.host != to_instance_id:
            return None
        return list(wanted.buckets)

    async def take_listing(
        self, *, space_id: str, sync_id: str, provider: str, records: object
    ) -> None:
        """``__listing__`` chunk records from the household our BEGIN asked
        (bounded). Anything else is dropped."""
        wanted = self._wanted.get(space_id)
        if wanted is None or wanted.host != provider or not isinstance(records, list):
            log.info(
                "sync page: listing chunk of session %s from %s dropped (not asked)",
                sync_id,
                provider,
            )
            return
        got = wanted.received.setdefault(sync_id, [])
        got.extend(records[: MAX_LISTING_BUCKETS - len(got)])
        while len(wanted.received) > MAX_PENDING_LISTINGS:
            wanted.received.pop(next(iter(wanted.received)))

    async def listing_done(self, *, space_id: str, sync_id: str, provider: str) -> None:
        """The sentinel of a round that carried our listings: read our rows
        of those buckets NOW (the round's rows are applied — a row it just
        delivered is no longer missing), verify each listing against the
        page's signed bucket and classify. A round without listings (an
        older host, or our BEGIN did not ask) changes nothing."""
        wanted = self._wanted.get(space_id)
        if wanted is None or wanted.host != provider:
            return
        records = wanted.received.pop(sync_id, None)
        if not records:
            return
        self._wanted.pop(space_id, None)
        try:
            local = await self._state.rows_in(
                space_id, wanted.buckets, cutoff=wanted.cutoff
            )
            missing, unresolved = _classify(
                records, wanted.buckets, wanted.vector, local
            )
        except PageRefused as exc:
            log.warning(
                "sync page: refused the listings of space %s from %s: %s",
                space_id,
                provider,
                exc,
            )
            missing, unresolved = 0, len(wanted.buckets)
        log.info(
            "sync page: listings of space %s from %s: %d row(s) missing, %d "
            "bucket(s) unresolved",
            space_id,
            provider,
            missing,
            unresolved,
        )
        check = self._checks.get(space_id)
        if check is None or check.root != wanted.root:
            return  # a newer page came meanwhile; its compare decides
        self._remember(
            space_id,
            _Compare(
                root=check.root,
                differing=check.differing,
                unexplained=check.unexplained,
                missing=missing,
                unresolved=check.unresolved + unresolved,
            ),
        )
        await self._changed(space_id)

    # ─── Listings (provider) ──────────────────────────────────────────

    async def listing_records(
        self,
        *,
        space_id: str,
        page_ref: Mapping[str, Any] | None,
        buckets: list[int],
        requester_version: int,
    ) -> list[dict]:
        """The ``__listing__`` records of a ``sync_mode: "buckets"`` session:
        per asked bucket its rows (identity, leaf, change time) when our
        rows of it still hash to the page the requester holds — the same
        page we hold — else ``held: false``. One export of the space."""
        if requester_version < FederationCapability.MIN_FOR_SYNC_PAGES:
            return []
        space = await self._spaces.get(space_id)
        if space is None:
            return []
        held = await self._held(space)
        asked = buckets[:MAX_LISTING_BUCKETS]
        if held is None or not page_ref or page_ref.get("root") != held.page.root:
            return [listing_to_wire(i, None) for i in asked]
        scan = await self._state.rows_in(space_id, asked, cutoff=held.page.cutoff)
        out: list[dict] = []
        hosting = await serves_as_host(
            self._spaces,  # type: ignore[arg-type]
            space,
            own_instance_id=self._own_instance_id,
        )
        for index in asked:
            rows = scan.rows.get(index, [])
            if len(rows) > MAX_LISTING_ROWS:
                out.append(listing_to_wire(index, None))
                continue
            try:
                verify_listing(index, rows, held.row.vector)
                out.append(listing_to_wire(index, rows))
            except PageRefused:
                # Changed here since the page: as the space's host we list
                # the bucket as it is now (trusted like our stream); anyone
                # else cannot prove it.
                out.append(
                    listing_to_wire(index, rows, current=True)
                    if hosting
                    else listing_to_wire(index, None)
                )
        log.info(
            "sync page: listed %d of %d bucket(s) of space %s",
            sum(1 for r in out if r.get("held") or r.get("current")),
            len(out),
            space_id,
        )
        return out

    # ─── Status ───────────────────────────────────────────────────────

    async def position(self, space: Space, hosts: list[str]) -> SyncPagePosition | None:
        """What the newest page says about our copy of *space* (``None``:
        no page held). ``match``: nothing the page commits to is missing
        here — no unexplained differing bucket, or every listing asked
        showed no missing row; ``False`` only when a listing PROVED a
        missing row; ``None`` while not compared, a listing is pending, or
        a bucket stayed unresolved."""
        del hosts  # kept for the status service's signature
        held = await self._held(space)
        if held is None:
            return None
        page = held.page
        check = self._checks.get(space.id)
        if (
            (check is None or check.root != page.root)
            and page.signer != self._own_instance_id
            and space.id not in self._comparing
        ):
            # A held page without a verdict (verdicts are memory only — a
            # restart loses them): asking for the status starts its compare,
            # once. The status rechecks when it is done.
            self.compare_soon(space.id)
        match: bool | None = None
        missing: int | None = None
        differing: int | None = None
        if check is not None and check.root == page.root:
            differing = check.differing
            if check.missing:
                match, missing = False, check.missing
            elif check.unexplained == 0 or (
                check.missing == 0 and check.unresolved == 0
            ):
                match, missing = True, 0
        conflict = self._conflicts.get(space.id) == (page.epoch, page.signer)
        return SyncPagePosition(
            seq=page.seq,
            at=datetime.fromtimestamp(page.issued_at / 1000, UTC).isoformat(),
            signer_role="owner" if page.signer == space.owner_instance_id else "admin",
            match=match,
            missing=missing,
            differing_buckets=differing,
            conflict=conflict,
        )

    async def wait_idle(self) -> None:
        """Wait until the background seals and checks started so far end."""
        # Only the unfinished: a finished task leaves the set when its
        # done-callback runs, and gathering it again never yields.
        while pending := [t for t in self._tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Cancel the background seals and checks (shutdown)."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


__all__ = ["MAX_SEALED_PAGE_CHARS", "SyncPageService"]
