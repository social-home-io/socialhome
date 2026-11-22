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
  ``MIN_FOR_SYNC_PAGES`` whose BEGIN shows an older page (or none) — with
  its full bucket vector (no delta: final review I4).
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
from ....domain.file_hashes import (
    FILES_FIELD,
    FILES_SUITE_FIELD,
    file_hashes_to_wire,
    parse_file_hashes,
    space_content_media_name,
)
from ....domain.media_constraints import space_media_ref
from ....domain.space_media_ref import MEDIA_REFS_FIELD
from ....domain.space_sync_page import StoredSyncPage, SyncPagePosition
from ....services.inbound_media_store import media_basename
from ....sync_page import (
    COMMIT_SPECS,
    COMMITTED_RESOURCES,
    COUNTERPARTS,
    HASH_BYTES,
    MAX_FETCH_BYTES,
    MAX_FETCH_ROWS,
    MAX_HINT_LEAVES,
    MAX_LISTING_BUCKETS,
    MAX_LISTING_ROWS,
    MAX_PAGE_FUTURE_SKEW_MS,
    PAGE_MIN_INTERVAL_S,
    PAGE_SUITE_SHA256_B256,
    SUPERSEDED_BY,
    ListingRow,
    PageRefused,
    SyncPage,
    UnsupportedPageSuite,
    bucket_hash,
    differing_buckets,
    fetched_record,
    fetched_row_to_wire,
    file_refs,
    hint_to_wire,
    is_conflict,
    is_superseded,
    listing_to_wire,
    page_time,
    parse_fetched_row,
    parse_listing,
    parse_page,
    parse_page_ref,
    record_leaf,
    root_of,
    row_id_of,
    row_key,
    sign_page,
    vector_from_wire,
    vector_to_wire,
    verify_listing,
    verify_page,
)
from ...host_equivalence import serves_as_host
from .exporter import RESOURCE_ORDER, record_batches
from .page_state import BucketRows
from .stream_health import HELD_BACK_LIMIT, HeldBack
from .trusted_helpers import helper_offset, member_deliverers

if TYPE_CHECKING:
    from ....domain.space import Space
    from ....repositories.space_sync_page_repo import AbstractSpaceSyncPageRepo
    from .exporter import ResourceExporter
    from .page_state import PageStateHasher

log = logging.getLogger(__name__)

#: The sealed ``page_sealed`` field of a sentinel is never larger than this
#: (a ≤ 1 KiB page + an 8 KiB vector, base64 and AES-GCM overhead).
MAX_SEALED_PAGE_CHARS: int = 24 * 1024

#: Spaces whose compare result / conflict flag is remembered.
MAX_TRACKED: int = 4096

#: A signer holding another signer's page does not seal over it while that
#: page's catch-up still has buckets to fetch — for at most this long after
#: its compare (final review I3: four 30-min rounds, 96 buckets of asks).
CATCH_UP_HOLD_S: float = 2 * 3600.0

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
    #: Phase 3: the unexplained buckets not yet proven complete here — what a
    #: verified fetch from a member still asks for while no host answers.
    todo: frozenset[int] = frozenset()


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
    #: Phase 3: a verified fetch from a member household (``host`` is the
    #: deliverer): the BEGIN also asks for the rows (``rows: true``) minus
    #: those the ``have`` hint names; ``page_at`` — the page's signed
    #: ``issued_at`` (a row of ours changed later is ahead).
    fetch: bool = False
    have: str | None = None
    page_at: str | None = None
    #: Whether WE are the space's owner (back after a long absence): it
    #: only ever takes rows it lacks, never another version of its own.
    owner: bool = False
    #: sync_id → the ``__rows__`` records that session brought (bounded).
    rows: dict[str, list[object]] = field(default_factory=dict)
    row_bytes: dict[str, int] = field(default_factory=dict)


def _lacks(
    local: BucketRows,
    wanted: _Wanted,
    horizon: str,
    index: int,
    resource: str,
    row_id: str,
    leaf: bytes,
) -> bool:
    """Phase 3: whether we lack the verified row ``(resource, row_id)`` with
    *leaf* — the identity from the delivered record itself (its leaf commits
    to it), judged against OUR rows only (a member's listing labels and
    change times are not signed):

    * we hold no row of that identity, and it is not gone here for a later
      reason (deleted, archived) → we lack it;
    * we hold that very version → we do not;
    * we are the space's OWNER (``wanted.owner``): we never replace a
      version we hold — the page an admin sealed while we were away may
      predate our own edits, and what we apply we then spread as the host;
    * else we lack it unless we changed our copy after the page's signed
      ``issued_at`` — a change time past *horizon* (:func:`_horizon`, a
      clock far ahead) never counts as newer.

    One stored row can sit under two identities (:data:`COUNTERPARTS`: a
    task is ``tasks:T`` live, ``tasks_archived:T`` archived): holding its
    counterpart is holding the row, judged by the same two rules (review
    N2). A tombstone is no counterpart — delete wins."""
    mine = next(
        (
            r
            for r in local.rows.get(index, [])
            if r.resource == resource and r.row_id == row_id
        ),
        None,
    )
    if mine is None:
        if is_superseded(resource, row_id, local.gone):
            return False
        for other in COUNTERPARTS.get(resource, ()):
            if (other, row_id) in local.held_any:
                marker = local.held_any[(other, row_id)]
                return not (wanted.owner or _newer(marker, wanted, horizon))
        return True
    if mine.leaf == leaf or wanted.owner:
        return False
    return not _newer(mine.marker, wanted, horizon)


def _newer(marker: str | None, wanted: _Wanted, horizon: str) -> bool:
    """Whether a row of ours changed at *marker* is newer than the page
    (after its signed ``issued_at``, not past *horizon*)."""
    return (
        marker is not None
        and wanted.page_at is not None
        and wanted.page_at < marker <= horizon
    )


def _fetched_listings(
    records: list[object], buckets: tuple[int, ...], vector: bytes
) -> dict[int, list[ListingRow] | None]:
    """Phase 3: a member's listings, per bucket — ``None`` when not held. A
    ``current`` listing (a host's rows as they are now) proves nothing from a
    member and counts as not held. Raises :class:`PageRefused` for one that
    is malformed, of a bucket not asked, repeated, or that does not hash to
    the signed bucket (the whole answer is refused)."""
    out: dict[int, list[ListingRow] | None] = {}
    for raw in records:
        index, rows, current = parse_listing(raw)
        if index not in buckets or index in out:
            raise PageRefused(f"a listing of bucket {index} not asked for")
        if rows is not None and current:
            rows = None
        elif rows is not None:
            verify_listing(index, rows, vector)
        out[index] = rows
    return out


def _fit_hint(
    window: list[int], rows: Mapping[int, list[ListingRow]]
) -> tuple[list[int], list[bytes]]:
    """The buckets of *window* whose leaves fit one ``have`` hint
    (:data:`MAX_HINT_LEAVES`), in order, and those leaves. The first bucket
    always goes; if it alone is too big its hint is cut (the deliverer then
    sends a few rows we hold, which we skip)."""
    buckets: list[int] = []
    leaves: list[bytes] = []
    for index in window:
        mine = [r.leaf for r in rows.get(index, [])]
        if buckets and len(leaves) + len(mine) > MAX_HINT_LEAVES:
            break
        buckets.append(index)
        leaves.extend(mine)
    return buckets, leaves[:MAX_HINT_LEAVES]


def _verified_row(
    resource: str, record: Mapping[str, Any], leaves: frozenset[bytes]
) -> tuple[str, str, bytes, dict[str, Any]] | None:
    """``(resource, row_id, leaf, record to apply)`` when the committed part
    of a delivered *record* hashes to one of *leaves* (its bucket's verified
    listing) — else ``None``. The identity comes from the record itself (the
    leaf commits to it), never from the listing's labels. The record applied
    is that committed part (:func:`~socialhome.sync_page.fetched_record`)
    plus the content hashes of its files, which the leaf covers.

    The leaf commits to a file's CONTENT, not to how the row names it: a
    reference not in the canonical local shape (``api/media/<name>``, not a
    name another receiving path owns — :func:`space_media_ref`) is refused,
    or a deliverer could keep the leaf and have the receiver drop the file
    (review N1)."""
    kept = fetched_record(resource, record)
    if any(space_media_ref(url) != url for url in file_refs(resource, kept)):
        return None
    files = parse_file_hashes(record)

    def file_hash(url: str) -> str | None:
        found = files.get(media_basename(url))
        return found.h if found is not None else None

    leaf = record_leaf(resource, kept, file_hash)
    row_id = row_id_of(resource, kept)
    if leaf is None or row_id is None or leaf[1] not in leaves:
        return None
    named = {
        name: files[name]
        for url in file_refs(resource, kept)
        if (name := media_basename(url)) in files
    }
    return resource, row_id, leaf[1], {**kept, **file_hashes_to_wire(named)}


#: A delivered row that verified: ``(bucket, resource, row_id, leaf,
#: record to apply)``.
_Verified = tuple[int, str, str, bytes, dict[str, Any]]


def _verify_rows(
    rows: list[object],
    listed: Mapping[int, list[ListingRow] | None],
    wanted: _Wanted,
) -> tuple[list[_Verified], int]:
    """``(rows that verified, rows refused)`` of a member's answer — CPU
    only (leaf hashing), run in a worker thread: each row's committed part
    hashes to a leaf of its bucket's verified listing."""
    leaves = {
        index: frozenset(r.leaf for r in listed_rows)
        for index, listed_rows in listed.items()
        if listed_rows is not None
    }
    verified: list[_Verified] = []
    refused = 0
    for raw in rows:
        try:
            index, resource, record = parse_fetched_row(raw)
        except PageRefused:
            refused += 1
            continue
        if index not in wanted.buckets:
            refused += 1
            continue
        if resource == "posts" and record.get("linked_event_id"):
            continue  # a feed card: every household mints its own
        row = _verified_row(resource, record, leaves.get(index, frozenset()))
        if row is None:
            refused += 1
            continue
        verified.append((index, *row))
    return verified, refused


#: A verified row handed to the receiver: ``(bucket, resource, leaf,
#: record)``.
_Applied = tuple[int, str, bytes, dict[str, Any]]


def _to_apply(
    verified: list[_Verified], wanted: _Wanted, local: BucketRows, horizon: str
) -> list[_Applied]:
    """The verified rows we lack (:func:`_lacks`), each record with what the page does not commit carried over from OUR copy
    of the row (review N3: a pin, a moderation mark, reactions, local
    stamps — the leaf does not cover them, so the deliverer's are never
    applied and ours must not be reset either). CPU only."""
    out: list[_Applied] = []
    seen: set[bytes] = set()
    for index, resource, row_id, leaf, record in verified:
        if leaf in seen:
            continue  # delivered twice
        seen.add(leaf)
        if not _lacks(local, wanted, horizon, index, resource, row_id, leaf):
            continue
        own = next(
            (
                found
                for name in (resource, *COUNTERPARTS.get(resource, ()))
                if (found := local.own.get((name, row_id))) is not None
            ),
            None,
        )
        if own is not None:
            committed = COMMIT_SPECS[resource].fields
            record = {
                **{
                    k: v
                    for k, v in own.items()
                    if k not in committed and k not in _NOT_CARRIED
                },
                **record,
            }
        out.append((index, resource, leaf, record))
    return out


#: Fields of our own copy never carried onto a verified row: its file
#: hashes and GFS descriptors describe OUR files (the verified row brings
#: its own hashes).
_NOT_CARRIED: frozenset[str] = frozenset(
    {FILES_FIELD, FILES_SUITE_FIELD, MEDIA_REFS_FIELD}
)


def _still_missing(
    listed: Mapping[int, list[ListingRow] | None],
    verified: list[_Verified],
    wanted: _Wanted,
    local: BucketRows,
    horizon: str,
    applied: list[_Applied],
    held: set[bytes],
) -> tuple[dict[int, int], dict[int, tuple[bytes, int]], dict[int, tuple[bytes, int]]]:
    """Per listed bucket, how many of its signed leaves are still not
    accounted for — judged on OUR rows as they are after the apply (review
    N1: what the receiver silently refused is still missing). A leaf is
    accounted for when we hold it, or a delivered row with that leaf
    verified and its OWN identity shows we do not need it (ours is newer,
    deleted here, the owner's). The listing's labels never count. CPU
    only.

    Also the buckets whose every missing leaf was a row handed to the
    receiver that did not land, ``index -> (our bucket hash now, rows
    missing)``: refused by its own rules (R3 — asking again brings the same
    row), and those with a row only *held* back (its parent not here yet;
    S1)."""
    settled = {
        leaf
        for index, resource, row_id, leaf, _record in verified
        if not _lacks(local, wanted, horizon, index, resource, row_id, leaf)
    }
    handed = {leaf for _index, _resource, leaf, _record in applied}
    still: dict[int, int] = {}
    refused: dict[int, tuple[bytes, int]] = {}
    holding: dict[int, tuple[bytes, int]] = {}
    for index, listed_rows in listed.items():
        if listed_rows is None:
            continue
        mine = [r.leaf for r in local.rows.get(index, [])]
        missing = {r.leaf for r in listed_rows} - set(mine) - settled
        still[index] = len(missing)
        if missing and missing <= handed:
            entry = (bucket_hash(index, mine), len(missing))
            (holding if missing & held else refused)[index] = entry
    return still, refused, holding


def _wanted_ids(rows: list[object]) -> frozenset[tuple[str, str]]:
    """The identities a member's answer names (and their counterparts) —
    whose own records the requester keeps (:func:`_to_apply`). Unverified:
    at worst we keep a few of our own records for nothing."""
    out: set[tuple[str, str]] = set()
    for raw in rows:
        try:
            _index, resource, record = parse_fetched_row(raw)
        except PageRefused:
            continue
        row_id = row_id_of(resource, record)
        if row_id is None:
            continue
        for name in (resource, *COUNTERPARTS.get(resource, ())):
            out.add((name, row_id))
    return frozenset(out)


def _known_files(space_id: str, applied: list[_Applied]) -> dict[str, str]:
    """``content name -> SHA-256`` of the files the applied rows name: the
    receiver stores a hashed file under its content name, whose bytes —
    once they arrive — hash to it; until then the re-read takes the hash
    from here."""
    known: dict[str, str] = {}
    for *_, record in applied:
        for name, fh in parse_file_hashes(record).items():
            known[space_content_media_name(space_id, fh.h, name)] = fh.h
    return known


def _host_listings(
    asked: list[int], scan: BucketRows, vector: bytes, hosting: bool
) -> list[dict]:
    """The Phase 2 listings of the asked buckets — CPU only, run in a worker
    thread: proven ones as of the page; a bucket changed here since the page
    listed as it is now (``current``) when we act as the space's host
    (trusted like our stream), else ``held: false``."""
    out: list[dict] = []
    for index in asked:
        rows = scan.rows.get(index, [])
        if len(rows) > MAX_LISTING_ROWS:
            out.append(listing_to_wire(index, None))
            continue
        try:
            verify_listing(index, rows, vector)
            out.append(listing_to_wire(index, rows))
        except PageRefused:
            out.append(
                listing_to_wire(index, rows, current=True)
                if hosting
                else listing_to_wire(index, None)
            )
    return out


def _fetch_answer(
    asked: list[int],
    scan: BucketRows,
    vector: bytes,
    max_record_bytes: int | None,
) -> tuple[list[dict], list[dict]]:
    """A deliverer's answer from its export of the asked buckets — CPU only,
    run in a worker thread: per bucket its listing when our rows of it hash
    to the page's signed bucket (else ``held: false``), then the proven
    buckets' rows, bounded (:data:`MAX_FETCH_ROWS`,
    :data:`MAX_FETCH_BYTES`, *max_record_bytes* each)."""
    out: list[dict] = []
    fetched: list[dict] = []
    used = 0
    for index in asked:
        listed = scan.rows.get(index, [])
        proven = len(listed) <= MAX_LISTING_ROWS
        if proven:
            try:
                verify_listing(index, listed, vector)
            except PageRefused:
                proven = False  # changed here since the page
        out.append(listing_to_wire(index, listed if proven else None))
        if not proven or scan.records is None:
            continue
        for resource, record in scan.records.get(index, []):
            wire = fetched_row_to_wire(index, resource, record)
            size = len(orjson.dumps(wire))
            if max_record_bytes is not None and size > max_record_bytes:
                continue  # over the requester's frame cap
            if len(fetched) >= MAX_FETCH_ROWS or used + size > MAX_FETCH_BYTES:
                break
            fetched.append(wire)
            used += size
    return out, fetched


#: Verified rows apply in stream order (a list before its tasks, a post
#: before its comments and poll).
_RESOURCE_RANK: dict[str, int] = {r: k for k, r in enumerate(RESOURCE_ORDER)}

#: Phase 3: ``(space_id, deliverer) ->`` one incremental BEGIN to that
#: member household now (the scheduler's ``sync_with``): the fetch rides it.
RoundRequest = Callable[[str, str], Awaitable[None]]

#: Phase 3: ``(space_id, deliverer, resource, record) ->`` what of the
#: verified row was held back only for now (a parent not held yet) or
#: failed — the receiver's normal merge rules.
ApplyVerified = Callable[[str, str, str, dict], Awaitable[HeldBack]]

#: A round asks at most this many member households, one after another.
MAX_FETCH_ASKS: int = 3

#: An ask unanswered this long is given up (the next round asks again).
FETCH_ANSWER_WAIT_S: float = 300.0

#: Spaces whose stuck buckets (:class:`_Stuck`) are remembered, oldest
#: dropped first — at most 256 buckets each, ~100 bytes a bucket.
MAX_STUCK_SPACES: int = 256


@dataclass(slots=True)
class _Stuck:
    """Re-review R3 (memory only): buckets of the page *root* whose every
    missing row a member delivered, verified, and our receiver refused by
    its own rules (an id held in another space, newer words kept, …).
    Asking again would bring the same rows; such a bucket is not asked of
    members again while OUR bucket is unchanged — ``index -> (our bucket
    hash then, rows missing)``. The status keeps them missing.

    A bucket whose rows were only HELD BACK (a parent not here yet) is
    counted in ``holds``, answer after answer; at :data:`HELD_BACK_LIMIT`
    in a row it is stuck too — its parent is outside the page and will
    not come from a member. A row landing starts every count over."""

    root: bytes
    buckets: dict[int, tuple[bytes, int]] = field(default_factory=dict)
    holds: dict[int, int] = field(default_factory=dict)
    #: Final review I1: per bucket, our bucket hash then and the member
    #: households that answered ``held: false`` for it …
    nays: dict[int, tuple[bytes, frozenset[str]]] = field(default_factory=dict)
    #: … and the buckets a whole round answered so (``index -> our bucket
    #: hash then``): no member can prove them (each changed the bucket since
    #: the page too — an edit or a close with no change time), so they are
    #: not asked again while ours is unchanged. Unresolved, never missing.
    dry: dict[int, bytes] = field(default_factory=dict)


class _DeliverySeats(Protocol):
    async def list_instances_with_roles(
        self, space_id: str, roles: frozenset[str]
    ) -> list[str]: ...


class _DeliveryVersions(Protocol):
    async def space_member_supports(
        self, instance_id: str, *, min_version: int
    ) -> bool: ...


def _classify(
    records: list[object],
    buckets: tuple[int, ...],
    vector: bytes,
    local: BucketRows,
    horizon: str,
) -> tuple[int, int, frozenset[int]]:
    """``(missing rows, unresolved buckets, buckets settled)`` of one round's
    listings against our rows (*local*, read after that round's rows were
    applied); a bucket is settled when its listing shows no row missing.
    A local change time past *horizon* (:func:`_horizon`) never makes our
    version the newer one. Raises :class:`PageRefused` for a listing that
    is malformed, of a bucket not asked, repeated, or that does not hash to
    the signed bucket."""
    seen: set[int] = set()
    missing = 0
    unresolved = 0
    settled: set[int] = set()
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
        before = missing
        for row in rows:
            have = mine.get((row.resource, row.row_id))
            if have is None:
                if not is_superseded(row.resource, row.row_id, local.gone):
                    missing += 1
            elif have.leaf != row.leaf and not (
                have.marker is not None
                and have.marker <= horizon
                and (row.marker is None or have.marker > row.marker)
            ):
                missing += 1
        # A row of ours not listed was created after the page: ahead.
        if missing == before:
            settled.add(index)
    return missing, unresolved + len(buckets) - len(seen), frozenset(settled)


def _partners(resource: str) -> tuple[str, ...]:
    """The other identities one stored row moves between: a tombstone's or
    an archived copy's live row (:data:`SUPERSEDED_BY`), a live task's
    archived copy (:data:`COUNTERPARTS`)."""
    live = SUPERSEDED_BY.get(resource)
    return tuple(
        dict.fromkeys(((live,) if live else ()) + COUNTERPARTS.get(resource, ()))
    )


def _moved_since(differing: list[int], scan: BucketRows, vector: bytes) -> set[int]:
    """Final review I1: the differing buckets a MOVE we hold explains
    exactly — a delete (its tombstone), an archive or un-archive (the row
    under its other identity), which carries no change time of its own (a
    tombstone keeps the row's ``created_at``). CPU only.

    A moved row of ours (its other identity in a differing bucket too) is
    not in the page when our bucket WITHOUT it hashes to the page's signed
    bucket: ours is then the page's plus the move, nothing of it is missing
    — and an old tombstone (in the page) never explains anything. Tried:
    every such row of the bucket at once, then each alone.

    The bucket of the row's other identity is NOT explained: the page's
    leaf there is unknown and that bucket may hold another signed row we
    lack (re-review: crediting it hid a missing row behind a delete). It is
    asked like any other, and settles as dry when nobody can prove it."""
    differ = set(differing)
    out: set[int] = set()
    for index in differing:
        rows = scan.rows.get(index, [])
        moved = [
            k
            for k, r in enumerate(rows)
            if any(row_key(p, r.row_id)[0] in differ for p in _partners(r.resource))
        ]
        if not moved:
            continue
        signed = vector[index * HASH_BYTES : (index + 1) * HASH_BYTES]
        tries = [moved] + ([[k] for k in moved] if len(moved) > 1 else [])
        for drop in tries:
            kept = [r.leaf for k, r in enumerate(rows) if k not in drop]
            if bucket_hash(index, kept) == signed:
                out.add(index)
                break
    return out


def _now_ms() -> int:
    return int(time.time() * 1000)


def _root_of_ref(ref: Mapping[str, Any] | None) -> bytes | None:
    parsed = parse_page_ref(ref)
    return None if parsed is None else parsed["root"]


def _horizon(now_ms: int) -> str:
    """The latest change time we believe, in the
    :func:`~socialhome.sync_page.change_time` form: now plus the page skew.
    A row stamped later (a writer's clock far ahead, or on purpose) never
    explains a differing bucket — or one writer could make every member
    read "up to date"."""
    return datetime.fromtimestamp(
        (now_ms + MAX_PAGE_FUTURE_SKEW_MS) / 1000, UTC
    ).isoformat(timespec="microseconds")


def _cutoff_day(cutoff: str | None) -> str | None:
    return cutoff[:10] if cutoff else None


class SyncPageService:
    """Sync pages on the §25.6 session (see the module docstring)."""

    __slots__ = (
        "_apply_verified",
        "_asked",
        "_checks",
        "_clock_ms",
        "_compared_at",
        "_comparing",
        "_conflicts",
        "_crypto",
        "_exporters",
        "_fetching",
        "_hosts",
        "_locks",
        "_monotonic",
        "_on_change",
        "_own_instance_id",
        "_probes",
        "_repo",
        "_request_round",
        "_round_left",
        "_round_size",
        "_rounds",
        "_seats",
        "_seqs",
        "_silent",
        "_sleep",
        "_spaces",
        "_state",
        "_stuck",
        "_tasks",
        "_versions",
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
        #: sync_id → the ``page`` of our BEGIN under it, when that BEGIN
        #: asked bucket listings (:meth:`attach_asks`).
        self._asked: Callable[[str], Mapping[str, Any] | None] | None = None
        #: space → the listings our next BEGIN to its host asks for.
        self._wanted: dict[str, _Wanted] = {}
        #: Spaces with a compare pending; space → when it last compared.
        self._comparing: set[str] = set()
        self._compared_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: space → ``(page seq, change counter then, changed)``: the last
        #: answer of the seal's quiet probe (final review minor 9).
        self._probes: OrderedDict[str, tuple[int, int, bool]] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        #: space → the last compare of our copy with its page.
        self._checks: OrderedDict[str, _Compare] = OrderedDict()
        #: space → the (epoch, signer) whose equivocation we saw.
        self._conflicts: OrderedDict[str, tuple[int, str]] = OrderedDict()
        #: Phase 3 verified fetch (:meth:`attach_member_fetch`); ``None``:
        #: not wired — only the hosts are asked.
        self._seats: _DeliverySeats | None = None
        self._versions: _DeliveryVersions | None = None
        self._silent: Callable[[str], bool] | None = None
        self._request_round: RoundRequest | None = None
        self._apply_verified: ApplyVerified | None = None
        #: space → rounds so far (the rotation step); the round's deliverers
        #: still to ask; when its ask went out (monotonic).
        self._rounds: dict[str, int] = {}
        self._round_left: dict[str, list[str]] = {}
        #: space → how many member households its current round asks (a
        #: bucket all of them answer ``held: false`` for is dry — I1).
        self._round_size: dict[str, int] = {}
        self._fetching: dict[str, float] = {}
        #: space → the buckets a member fetch cannot complete (R3).
        self._stuck: OrderedDict[str, _Stuck] = OrderedDict()

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

    async def _held_root(self, space_id: str) -> bytes | None:
        """The root of the page held for *space_id* now, or ``None``."""
        space = await self._spaces.get(space_id)
        held = await self._held(space) if space is not None else None
        return None if held is None else held.page.root

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
        try:
            async with lock:
                try:
                    return await self._seal_if_due(space_id)
                except Exception:
                    log.exception("sync page: sealing a page of %s failed", space_id)
                    return None
        finally:
            if self._locks.get(space_id) is lock:
                del self._locks[space_id]

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
        elif held is not None and (
            self._clock_ms() - held.page.issued_at < PAGE_MIN_INTERVAL_S * 1000
            or self._catching_up(space_id, held.page.root)
        ):
            # Another signer's page (the owner's, held by a delegated admin
            # that also syncs as a member) is fresh: the space has a recent
            # page, so we do not add one of our own on top of every one of it.
            # Nor while we still catch up what it proves we lack (final
            # review I3): our page would replace it, end the catch-up and
            # commit the incomplete state.
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
        if not await self._repo.put(
            StoredSyncPage(
                space_id=space_id,
                page_json=canonical_str(page.to_wire()),
                vector=vector,
            ),
            key=str(space.identity_public_key),
        ):
            return None  # re-pinned or a newer page stored meanwhile
        log.info(
            "sync page: sealed page %d of space %s (%d rows, %.2f s)",
            seq,
            space_id,
            rows,
            time.perf_counter() - started,
        )
        await self._changed(space_id)
        return page

    def _catching_up(self, space_id: str, root: bytes) -> bool:
        """Whether the catch-up from members to the held page *root*
        (another signer's) still has buckets to fetch (the verified fetch
        wired) — for at most
        :data:`CATCH_UP_HOLD_S` after its compare (no member household to
        ask, or none answering, must not stop our seals for good)."""
        if self._request_round is None:
            return False  # no verified fetch wired: nothing to wait for
        check = self._checks.get(space_id)
        at = self._compared_at.get(space_id)
        return (
            check is not None
            and check.root == root
            and bool(check.todo)
            and at is not None
            and self._monotonic() - at < CATCH_UP_HOLD_S
        )

    async def _changed_since(self, space_id: str, seq: int) -> bool:
        """Whether a committed row of the space was stamped above *seq* —
        the ``is_quiet`` probe of the committed resources; reads up to the
        first changed row. Memoised (final review minor 9): every BEGIN to
        the signer asks, so a "changed" stays changed for that page, and a
        "quiet" holds while the change counter has not moved."""
        now_seq = await self._seqs.current_seq()
        memo = self._probes.get(space_id)
        if memo is not None and memo[0] == seq and (memo[2] or memo[1] == now_seq):
            return memo[2]
        changed = False
        for resource in COMMITTED_RESOURCES:
            exporter = self._exporters.get(resource)
            if exporter is None:
                continue
            batches = record_batches(exporter, space_id, since=seq)
            try:
                async for _batch in batches:
                    changed = True
                    break
            finally:
                await batches.aclose()
            if changed:
                break
        self._probes[space_id] = (seq, now_seq, changed)
        self._probes.move_to_end(space_id)
        while len(self._probes) > MAX_TRACKED:
            self._probes.popitem(last=False)
        return changed

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
            # Always the whole vector (final review I4: a delta against the
            # requester's page fit too rarely to earn its column and rules).
            body = {"page": page.to_wire(), "vector": vector_to_wire(held.row.vector)}
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
            and space.owner_instance_id != self._own_instance_id
        ):
            # A delegated admin signs this space too: its own page and its
            # prev_root chain stay; another signer's page is never stored over it
            # (it would break its chain and make its next page look like
            # equivocation to the members). The OWNER does take a newer page
            # its delegated admin signed (same epoch — verified above) while
            # the owner was offline: it then compares and catches up from
            # members (Phase 3). Its next page starts a chain of its own
            # (``prev_root`` null, the full vector), which is no conflict.
            return False
        if "vector" not in body:
            # Refused WITHOUT dropping ours: dropping it would let a relay
            # roll us back (drop the newer page, then offer an older one).
            raise PageRefused("a page without its vector")
        vector = vector_from_wire(body["vector"])
        if root_of(vector) != page.root:
            raise PageRefused("the vector does not hash to the page's root")
        if not await self._repo.put(
            StoredSyncPage(
                space_id=space_id,
                page_json=canonical_str(page.to_wire()),
                vector=vector,
            ),
            key=str(space.identity_public_key),
        ):
            return False  # re-pinned or a newer page stored meanwhile
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

    def attach_asks(self, asked: Callable[[str], Mapping[str, Any] | None]) -> None:
        """``asked(sync_id)`` — the ``page`` (wire form) our BEGIN under that
        id carried when it asked bucket listings (``None``: it asked none —
        the requester's pending-request record). A listing is
        taken only for the page the current ask is about: a round that
        carried an older ask (a newer page came meanwhile) brings listings
        made for that older page."""
        self._asked = asked

    async def _compare_if_unjudged(self, space_id: str) -> None:
        """A sentinel brought no newer page: compare the held page only if
        it has no verdict yet — compare results live in memory, so after a
        restart the stored page would otherwise wait for the next page.
        Never a second compare of a judged page. Fail-soft."""
        try:
            if space_id in self._comparing or await self._judged(space_id):
                return
            self.compare_soon(space_id)
        except Exception:
            log.exception("sync page: checking the verdict of %s failed", space_id)

    async def _judged(self, space_id: str) -> bool:
        """The page held now already has a verdict (or is our own)."""
        space = await self._spaces.get(space_id)
        held = await self._held(space) if space is not None else None
        if held is None or held.page.signer == self._own_instance_id:
            return True
        check = self._checks.get(space_id)
        return check is not None and check.root == held.page.root

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
            if await self._judged(space_id):
                return  # compared while we waited: nothing new to check
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
                horizon = _horizon(self._clock_ms())
                moved = await asyncio.to_thread(
                    _moved_since, differing, scan, held.row.vector
                )
                for index in differing:
                    if index in moved:
                        continue  # ours = the page's + a delete / move of ours
                    rows = scan.rows.get(index, [])
                    if any(
                        r.marker is not None and newer_than < r.marker <= horizon
                        for r in rows
                    ):
                        continue  # ahead in this bucket
                    unexplained.append(index)
            asked = unexplained[:MAX_LISTING_BUCKETS]
            # Phase 3: no host answers (silent, or we are the owner) — the
            # rows and their proof come from a member household instead.
            from_members = bool(asked) and await self._no_host_answers(space_id)
            # Buckets a member fetch of this page cannot complete (R3):
            # still missing, never asked of members again.
            stuck, dry = (
                self._stuck_now(space_id, page.root, vector, unexplained)
                if from_members
                else ({}, set())
            )
            host = (
                await self._host_for(space, page.signer)
                if asked and not from_members
                else None
            )
            replaced = self._wanted.pop(space_id, None)
            if replaced is not None and replaced.fetch:
                # Its answer is now dropped (the ask is gone): no ask is in
                # flight any more, so this compare's round may ask at once.
                self._fetching.pop(space_id, None)
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
            todo = frozenset(unexplained) - stuck.keys() - dry
            result = _Compare(
                root=page.root,
                differing=len(differing),
                unexplained=len(unexplained),
                missing=sum(stuck.values()) if not todo else None,
                unresolved=len(dry) if not todo else len(unexplained) - len(asked),
                todo=todo,
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
            if from_members and todo:
                await self._fetch_round(space_id)
            return result
        except Exception:
            log.exception("sync page: comparing space %s failed", space_id)
            return None

    def _stuck_now(
        self, space_id: str, root: bytes, vector: bytes, unexplained: list[int]
    ) -> tuple[dict[int, int], set[int]]:
        """The unexplained buckets still stuck for page *root* (R3) — ``index
        -> rows missing`` — and still dry (I1): ours unchanged since the
        fetch that could not complete them. A changed bucket of ours is
        asked again."""
        held = self._stuck.get(space_id)
        if held is None or held.root != root:
            return {}, set()
        out: dict[int, int] = {}
        dry: set[int] = set()
        for index in unexplained:
            entry = held.buckets.get(index)
            ours = vector[index * HASH_BYTES : (index + 1) * HASH_BYTES]
            if entry is not None and entry[0] == ours:
                out[index] = entry[1]
            elif held.dry.get(index) == ours:
                dry.add(index)
        return out, dry

    def _stuck_for(self, space_id: str, root: bytes) -> _Stuck:
        """The space's :class:`_Stuck` record for page *root* (a new one for
        a newer page), most recent last, bounded."""
        held = self._stuck.get(space_id)
        if held is None or held.root != root:
            held = _Stuck(root=root)
        self._stuck[space_id] = held
        self._stuck.move_to_end(space_id)
        while len(self._stuck) > MAX_STUCK_SPACES:
            self._stuck.popitem(last=False)
        return held

    def _note_nays(
        self,
        space_id: str,
        root: bytes,
        provider: str,
        nays: Mapping[int, bytes],
        ours_now: Mapping[int, bytes],
    ) -> set[int]:
        """Final review I1: *provider* answered ``held: false`` for the
        buckets of *nays* (``index -> our bucket hash now``). A bucket every
        member household of the round answered so is dry. Every bucket dry
        for page *root* now."""
        held = self._stuck_for(space_id, root)
        need = max(1, self._round_size.get(space_id, 1))
        for index, ours in nays.items():
            prev = held.nays.get(index)
            who = (prev[1] if prev is not None and prev[0] == ours else frozenset()) | {
                provider
            }
            if len(who) >= need:
                held.dry[index] = ours
                held.nays.pop(index, None)
            else:
                held.nays[index] = (ours, who)
        # A dry bucket that changed here since (rows came meanwhile) is to
        # fetch again (*ours_now*: our hashes of the buckets just read).
        for index, ours in ours_now.items():
            if held.dry.get(index, ours) != ours:
                del held.dry[index]
        return set(held.dry)

    def _note_stuck(
        self,
        space_id: str,
        root: bytes,
        found: Mapping[int, tuple[bytes, int]],
        holding: Mapping[int, tuple[bytes, int]] | None = None,
    ) -> dict[int, tuple[bytes, int]]:
        """Remember *found* (refused by rule) as stuck, count one more hold
        for each bucket of *holding* and remember those at
        :data:`HELD_BACK_LIMIT` as stuck too. The buckets stuck now."""
        held = self._stuck_for(space_id, root)
        out = dict(found)
        for index, entry in (holding or {}).items():
            held.holds[index] = held.holds.get(index, 0) + 1
            if held.holds[index] >= HELD_BACK_LIMIT:
                out[index] = entry
        for index in out:
            held.holds.pop(index, None)
        held.buckets.update(out)
        return out

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

    def buckets_for(
        self,
        space_id: str,
        to_instance_id: str,
        page_ref: Mapping[str, Any] | None,
    ) -> list[int] | None:
        """``buckets`` for our next BEGIN of *space_id* to *to_instance_id*:
        the unexplained buckets of the last compare, when it is the host
        they are asked of; else ``None``."""
        wanted = self._wanted.get(space_id)
        if (
            wanted is None
            or wanted.host != to_instance_id
            or _root_of_ref(page_ref) != wanted.root
        ):
            # A newer page is held and its compare is still waiting: the
            # host would list buckets for a page we no longer hold, and we
            # would drop them — no export for nothing.
            return None
        return list(wanted.buckets)

    def have_for(
        self,
        space_id: str,
        to_instance_id: str,
        page_ref: Mapping[str, Any] | None,
    ) -> str | None:
        """Phase 3: the ``have`` hint of our next BEGIN of *space_id* to
        *to_instance_id* when its ``buckets`` are a verified fetch from that
        member household (it then also asks for the rows, ``rows: true``);
        else ``None``."""
        wanted = self._wanted.get(space_id)
        if (
            wanted is None
            or not wanted.fetch
            or wanted.host != to_instance_id
            or _root_of_ref(page_ref) != wanted.root
        ):
            return None
        return wanted.have

    async def take_listing(
        self, *, space_id: str, sync_id: str, provider: str, records: object
    ) -> None:
        """``__listing__`` chunk records from the household our BEGIN asked
        (bounded). Anything else is dropped."""
        wanted = self._wanted.get(space_id)
        if (
            wanted is None
            or wanted.host != provider
            or not isinstance(records, list)
            or self._asked is None
            or _root_of_ref(self._asked(sync_id)) != wanted.root
        ):
            log.info(
                "sync page: listing chunk of session %s from %s dropped (not "
                "asked, or asked for an older page)",
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
        rows = wanted.rows.pop(sync_id, [])
        wanted.row_bytes.pop(sync_id, None)
        if not records:
            if (
                wanted.fetch
                and self._asked is not None
                and _root_of_ref(self._asked(sync_id)) == wanted.root
            ):
                # Our fetch rode this BEGIN and the member answered with no
                # listing (it holds no page, or not ours): an answer all the
                # same — the round goes on to its next member at once.
                self._wanted.pop(space_id, None)
                self._fetching.pop(space_id, None)
                self._spawn(self._fetch_next(space_id), f"sync-page-fetch-{space_id}")
            return
        self._wanted.pop(space_id, None)
        if wanted.fetch:
            await self._fetch_done(space_id, provider, wanted, records, rows)
            return
        if await self._held_root(space_id) != wanted.root:
            # The same round's sentinel stored a newer page: these listings
            # are for the page it replaced — dropped before any export (final
            # review minor 9); the newer page's compare decides.
            return
        try:
            local = await self._state.rows_in(
                space_id, wanted.buckets, cutoff=wanted.cutoff
            )
            missing, unresolved, settled = _classify(
                records,
                wanted.buckets,
                wanted.vector,
                local,
                _horizon(self._clock_ms()),
            )
        except PageRefused as exc:
            log.warning(
                "sync page: refused the listings of space %s from %s: %s",
                space_id,
                provider,
                exc,
            )
            missing, unresolved, settled = 0, len(wanted.buckets), frozenset()
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
                # Final review I2: a bucket the listing settled is complete —
                # a host gone silent later must not ask a member for it.
                todo=check.todo - settled,
            ),
        )
        await self._changed(space_id)

    # ─── Verified fetch from any member (requester, Phase 3) ──────────

    def attach_member_fetch(
        self,
        *,
        seats: _DeliverySeats,
        versions: _DeliveryVersions,
        silent: Callable[[str], bool],
        request: RoundRequest,
        apply: ApplyVerified,
    ) -> None:
        """Wire the verified fetch (``docs/protocol/sync-pages.md`` Phase 3):
        ``seats`` / ``versions`` — what the deliverer picker reads
        (:func:`~.trusted_helpers.member_deliverers`); ``silent(space_id)``
        — whether the households acting as the space's host left this round
        unanswered (``HostContact.is_silent``); ``request(space_id,
        deliverer)`` — one incremental BEGIN to that member now, which
        carries the fetch (:meth:`buckets_for` / :meth:`have_for`);
        ``apply(space_id, deliverer, resource, record)`` — apply one verified
        row through the normal receiver (whether it landed)."""
        self._seats = seats
        self._versions = versions
        self._silent = silent
        self._request_round = request
        self._apply_verified = apply

    def _host_silent(self, space_id: str) -> bool:
        if self._silent is None:
            return False
        try:
            return bool(self._silent(space_id))
        except Exception:
            log.exception("sync page: reading the host silence of %s", space_id)
            return False

    async def _no_host_answers(self, space_id: str) -> bool:
        """Whether member households deliver: the households acting as the
        space's host are silent — or we ARE its owner, with no host above
        us: back after a long absence, a newer page our delegated admin
        signed meanwhile proves what we lack."""
        if self._request_round is None:
            return False
        space = await self._spaces.get(space_id)
        if space is not None and space.owner_instance_id == self._own_instance_id:
            return True
        return self._host_silent(space_id)

    def _spawn(self, coro: Awaitable[None], name: str) -> None:
        task = asyncio.ensure_future(coro)
        task.set_name(name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def host_silent(self, space_id: str) -> None:
        """The scheduler found *space_id*'s acting hosts silent this round:
        catch up from a member household what the newest page proves we
        lack (compare first when the held page has no verdict yet)."""
        self._spawn(self._on_host_silent(space_id), f"sync-page-silent-{space_id}")

    async def _on_host_silent(self, space_id: str) -> None:
        try:
            space = await self._spaces.get(space_id)
            held = await self._held(space) if space is not None else None
            if held is None or held.page.signer == self._own_instance_id:
                return
            check = self._checks.get(space_id)
            if check is None or check.root != held.page.root:
                self.compare_soon(space_id)  # it then goes to a member
                return
            if check.todo:
                await self._fetch_round(space_id)
        except Exception:
            log.exception("sync page: catching up %s from a member failed", space_id)

    async def _fetch_round(self, space_id: str) -> None:
        """One round of asks: the member households at v_95 (never a host,
        never us, never a follower), sorted and rotated from a stable
        per-household offset plus one step per round, at most
        :data:`MAX_FETCH_ASKS`, one at a time while no host answers and a
        bucket is still to fetch. One ask in flight per space."""
        asked_at = self._fetching.get(space_id)
        if asked_at is not None and self._monotonic() - asked_at < FETCH_ANSWER_WAIT_S:
            return  # an ask is in flight; its answer goes on with the round
        if (
            self._seats is None
            or self._versions is None
            or not await self._no_host_answers(space_id)
        ):
            return
        space = await self._spaces.get(space_id)
        if space is None:
            return
        hosts = await self._hosts(space) if self._hosts is not None else []
        rnd = self._rounds.get(space_id, 0)
        self._rounds[space_id] = rnd + 1
        while len(self._rounds) > MAX_TRACKED:
            self._rounds.pop(next(iter(self._rounds)))
        deliverers = await member_deliverers(
            self._seats,
            self._versions,
            space_id,
            exclude={*hosts, space.owner_instance_id, self._own_instance_id},
            offset=helper_offset(self._own_instance_id, space_id) + rnd,
        )
        if not deliverers:
            log.info(
                "sync page: no host answers for %s and no member household at "
                "v_%d can deliver verified rows — waiting for the host",
                space_id,
                FederationCapability.MIN_FOR_SYNC_PAGES,
            )
            return
        self._round_left[space_id] = deliverers[:MAX_FETCH_ASKS]
        self._round_size[space_id] = len(self._round_left[space_id])
        await self._fetch_next(space_id)

    async def _fetch_next(self, space_id: str) -> None:
        """Ask the round's next deliverer for the buckets still to fetch:
        the fetch rides the next incremental BEGIN to it (``buckets``,
        ``rows: true``, ``have``), asked for now."""
        self._fetching.pop(space_id, None)
        left = self._round_left.get(space_id) or []
        while left:
            if not await self._no_host_answers(space_id):
                left.clear()  # a host answers again: it is the source
                return
            deliverer = left.pop(0)
            space = await self._spaces.get(space_id)
            held = await self._held(space) if space is not None else None
            check = self._checks.get(space_id)
            if (
                space is None
                or held is None
                or check is None
                or check.root != held.page.root
                or not check.todo
                or self._request_round is None
            ):
                left.clear()
                return
            todo = sorted(check.todo)
            start = (self._rounds.get(space_id, 1) - 1) * MAX_LISTING_BUCKETS
            start %= len(todo)
            window = (todo[start:] + todo[:start])[:MAX_LISTING_BUCKETS]
            scan = await self._state.rows_in(space_id, window, cutoff=held.page.cutoff)
            buckets, leaves = _fit_hint(window, scan.rows)
            self._wanted[space_id] = _Wanted(
                host=deliverer,
                root=held.page.root,
                vector=held.row.vector,
                cutoff=held.page.cutoff,
                buckets=tuple(buckets),
                fetch=True,
                have=hint_to_wire(leaves),
                page_at=page_time(held.page),
                owner=space.owner_instance_id == self._own_instance_id,
            )
            while len(self._wanted) > MAX_TRACKED:
                self._wanted.pop(next(iter(self._wanted)))
            self._fetching[space_id] = self._monotonic()
            log.info(
                "sync page: no host answers for %s — asking member household "
                "%s for %d bucket(s) of page %d, verified",
                space_id,
                deliverer,
                len(buckets),
                held.page.seq,
            )
            try:
                await self._request_round(space_id, deliverer)
            except Exception:
                log.exception("sync page: asking %s for rows failed", deliverer)
            return

    async def take_rows(
        self, *, space_id: str, sync_id: str, provider: str, records: object
    ) -> None:
        """``__rows__`` chunk records of a verified fetch our BEGIN asked,
        from the household it asked, for the page it asked about — kept
        (bounded: :data:`MAX_FETCH_ROWS` rows, :data:`MAX_FETCH_BYTES`)
        until its sentinel; anything else is dropped."""
        wanted = self._wanted.get(space_id)
        if (
            wanted is None
            or not wanted.fetch
            or wanted.host != provider
            or not isinstance(records, list)
            or self._asked is None
            or _root_of_ref(self._asked(sync_id)) != wanted.root
        ):
            log.info("sync page: unasked rows from %s dropped", provider)
            return
        got = wanted.rows.setdefault(sync_id, [])
        used = wanted.row_bytes.get(sync_id, 0)
        for raw in records:
            size = len(orjson.dumps(raw))
            if len(got) >= MAX_FETCH_ROWS or used + size > MAX_FETCH_BYTES:
                log.info(
                    "sync page: rows of space %s from %s over the cap — the "
                    "rest is asked of the next member",
                    space_id,
                    provider,
                )
                break
            got.append(raw)
            used += size
        wanted.row_bytes[sync_id] = used
        while len(wanted.rows) > MAX_PENDING_LISTINGS:
            stale = next(iter(wanted.rows))
            wanted.rows.pop(stale)
            wanted.row_bytes.pop(stale, None)

    async def _fetch_done(
        self,
        space_id: str,
        provider: str,
        wanted: _Wanted,
        records: list[object],
        rows: list[object],
    ) -> None:
        """A verified fetch's sentinel: verify the listings against the
        page's signed buckets, each delivered row by its own leaf, apply the
        rows we lack through the normal receiver, narrow the verdict, and
        ask the round's next member for what is still missing."""
        self._fetching.pop(space_id, None)  # answered
        try:
            try:
                listed = _fetched_listings(records, wanted.buckets, wanted.vector)
            except PageRefused as exc:
                log.warning(
                    "sync page: refused the answer of %s for space %s: %s",
                    provider,
                    space_id,
                    exc,
                )
                listed = {}
            # Our rows now (the round's own stream already applied), with
            # our own records of the rows delivered (review N3).
            local = await self._state.rows_in(
                space_id,
                wanted.buckets,
                cutoff=wanted.cutoff,
                keep_ids=_wanted_ids(rows) if listed else frozenset(),
            )
            horizon = _horizon(self._clock_ms())
            verified, applied, held = (
                await self._apply_rows(
                    space_id, provider, wanted, listed, rows, local, horizon
                )
                if listed
                else ([], [], set())
            )
            check = self._checks.get(space_id)
            if check is None or check.root != wanted.root:
                return  # a newer page came meanwhile; its compare decides
            # Completeness by LEAVES only, on our rows as they are NOW
            # (security review 1, re-review N1): what the receiver refused
            # without saying so is still missing.
            if applied:
                local = await self._state.rows_in(
                    space_id,
                    wanted.buckets,
                    cutoff=wanted.cutoff,
                    known=_known_files(space_id, applied),
                )
            # A refusal by rule marks a bucket stuck at once (R3); a row
            # held back for its parent only after HELD_BACK_LIMIT answers
            # in a row (S1) — its parent may still come.
            still, refused, holding = await asyncio.to_thread(
                _still_missing,
                listed,
                verified,
                wanted,
                local,
                horizon,
                applied,
                held,
            )
            landed = sum(
                1
                for index, _resource, leaf, _record in applied
                if any(leaf == row.leaf for row in local.rows.get(index, []))
            )
            revived: frozenset[int] = frozenset()
            if landed:
                # A row landed: a row refused before may land now (its
                # parent came) — those buckets are to fetch again (S1).
                gone = self._stuck.get(space_id)
                if gone is not None and gone.root == wanted.root:
                    revived = frozenset(gone.buckets)
                    gone.buckets.clear()
                    gone.holds.clear()
            stuck = (
                self._note_stuck(space_id, wanted.root, refused, holding)
                if refused or holding
                else {}
            )
            # I1: a bucket the whole round answers ``held: false`` for is
            # proven by nobody — not asked again while ours is unchanged.
            ours_now = {
                i: bucket_hash(i, [r.leaf for r in local.rows.get(i, [])])
                for i in wanted.buckets
            }
            dry = self._note_nays(
                space_id,
                wanted.root,
                provider,
                {i: ours_now[i] for i, rows_of in listed.items() if rows_of is None},
                ours_now,
            )
            complete = frozenset(i for i, n in still.items() if not n)
            todo = (check.todo | revived) - complete - stuck.keys() - dry
            short = sum(1 for i, n in still.items() if n and i not in stuck)
            # The verdict counts the buckets stuck from earlier answers too:
            # a stuck bucket is missing rows, never "up to date" (S1).
            earlier = self._stuck.get(space_id)
            stuck_before = (
                sum(n for i, (_ours, n) in earlier.buckets.items() if i not in still)
                if earlier is not None and earlier.root == wanted.root
                else 0
            )
            self._remember(
                space_id,
                _Compare(
                    root=check.root,
                    differing=check.differing,
                    unexplained=check.unexplained,
                    missing=sum(still.values()) + stuck_before,
                    unresolved=len(todo) - short + len(dry),
                    todo=todo,
                ),
            )
            log.info(
                "sync page: verified catch-up of space %s from %s: %d row(s) "
                "applied, %d bucket(s) complete, %d still to fetch%s",
                space_id,
                provider,
                landed,
                len(complete),
                len(todo),
                (
                    f", {len(stuck)} bucket(s) refused here by the receiver "
                    "(missing, not asked again for this page)"
                    if stuck
                    else ""
                ),
            )
            await self._changed(space_id)
            if todo:
                self._spawn(self._fetch_next(space_id), f"sync-page-fetch-{space_id}")
            else:
                self._round_left.pop(space_id, None)
        except Exception:
            log.exception("sync page: a verified fetch of %s failed", space_id)

    async def _apply_rows(
        self,
        space_id: str,
        provider: str,
        wanted: _Wanted,
        listed: Mapping[int, list[ListingRow] | None],
        rows: list[object],
        local: BucketRows,
        horizon: str,
    ) -> tuple[list[_Verified], list[_Applied], set[bytes]]:
        """Verify and apply the delivered rows: ``(rows that verified, rows
        handed to the receiver)``. Whether one landed is judged after, from
        our own rows (:func:`_still_missing`) — never from the receiver's
        answer, which does not see every silent refusal.

        Nothing the deliverer says about a row is trusted but what the page
        signs: the row's own committed part must hash to a leaf of its
        bucket's verified listing (the listing's labels and change times
        play no part), and whether we lack it is decided from the row's own
        identity against OUR rows (:func:`_lacks`). Anything else is refused
        (one WARNING per answer). The hashing runs in a worker thread."""
        verified, refused = await asyncio.to_thread(_verify_rows, rows, listed, wanted)
        if refused:
            log.warning(
                "sync page: refused %d row(s) of space %s from %s — not a row "
                "of a verified listing (tampered, stale or unasked)",
                refused,
                space_id,
                provider,
            )
        if self._apply_verified is None:
            return verified, [], set()
        apply = await asyncio.to_thread(_to_apply, verified, wanted, local, horizon)
        apply.sort(key=lambda v: _RESOURCE_RANK.get(v[1], len(_RESOURCE_RANK)))
        held: set[bytes] = set()  # held back for now, or failed: no rule
        for _index, resource, leaf, record in apply:
            try:
                outcome = await self._apply_verified(
                    space_id, provider, resource, record
                )
            except Exception:
                log.exception(
                    "sync page: applying a verified %s row of %s failed",
                    resource,
                    space_id,
                )
                held.add(leaf)
                continue
            if outcome.count or outcome.failed:
                held.add(leaf)
        return verified, apply, held

    # ─── Listings (provider) ──────────────────────────────────────────

    async def listing_records(
        self,
        *,
        space_id: str,
        page_ref: Mapping[str, Any] | None,
        buckets: list[int],
        requester_version: int,
    ) -> list[dict]:
        """The ``__listing__`` records for the ``buckets`` a BEGIN named:
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
        hosting = await serves_as_host(
            self._spaces,  # type: ignore[arg-type]
            space,
            own_instance_id=self._own_instance_id,
        )
        # The proofs (bucket hashes over every listed leaf) run in a worker
        # thread, like the export's hashing.
        out = await asyncio.to_thread(
            _host_listings, asked, scan, held.row.vector, hosting
        )
        log.info(
            "sync page: listed %d of %d bucket(s) of space %s",
            sum(1 for r in out if r.get("held") or r.get("current")),
            len(out),
            space_id,
        )
        return out

    async def fetch_records(
        self,
        *,
        space_id: str,
        page_ref: Mapping[str, Any] | None,
        buckets: list[int],
        requester_version: int,
        have: frozenset[bytes] | None,
        max_record_bytes: int | None,
    ) -> tuple[list[dict], list[dict]]:
        """A verified fetch (Phase 3; any member household serves it): per
        asked bucket the ``__listing__`` record when our rows of it still
        hash to the page the requester holds (the same page we hold), and
        then its rows the *have* hint does not name, as ``__rows__``
        records — at most :data:`MAX_FETCH_ROWS` rows and
        :data:`MAX_FETCH_BYTES`, each at most *max_record_bytes* (a
        relay-seated requester's frame cap). A bucket changed here since the
        page is ``held: false`` (never ``current``, never a row we cannot
        prove)."""
        if requester_version < FederationCapability.MIN_FOR_SYNC_PAGES:
            return [], []
        space = await self._spaces.get(space_id)
        if space is None:
            return [], []
        held = await self._held(space)
        asked = buckets[:MAX_LISTING_BUCKETS]
        if held is None or not page_ref or page_ref.get("root") != held.page.root:
            return [listing_to_wire(i, None) for i in asked], []
        scan = await self._state.rows_in(
            space_id,
            asked,
            cutoff=held.page.cutoff,
            keep_records=True,
            skip=have or frozenset(),
        )
        # The export's hashing already ran in worker threads (rows_in); the
        # proofs and the answer's sizing do too.
        out, fetched = await asyncio.to_thread(
            _fetch_answer, asked, scan, held.row.vector, max_record_bytes
        )
        log.info(
            "sync page: listed %d of %d bucket(s) of space %s for a verified "
            "fetch, %d row(s)",
            sum(1 for r in out if r.get("held")),
            len(out),
            space_id,
            len(fetched),
        )
        return out, fetched

    # ─── Status ───────────────────────────────────────────────────────

    async def position(self, space: Space) -> SyncPagePosition | None:
        """What the newest page says about our copy of *space* (``None``:
        no page held). ``match``: nothing the page commits to is missing
        here — no unexplained differing bucket, or every listing asked
        showed no missing row; ``False`` only when a listing PROVED a
        missing row; ``None`` while not compared, a listing is pending, or
        a bucket stayed unresolved."""
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
