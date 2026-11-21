"""Sync pages — the space authority's signed commitment to a space's state.

``docs/protocol/sync-pages.md`` (Phase 2). A **page** says: "as of this
moment, the space's committed rows are exactly the ones whose leaves hash to
this root". A member keeps only the newest page and its 256-entry **bucket
vector**; with them it can tell whether it is behind even while the host is
offline (and, in Phase 3, check rows any member delivers).

Shape (suite :data:`PAGE_SUITE_SHA256_B256`)::

    rkey   = SHA-256("sh-sync-rkey:v1:" ‖ resource ‖ 0x00 ‖ row_id)
    bucket = rkey[0]
    leaf   = SHA-256("sh-sync-leaf:v1:" ‖ canonical_bytes(commit_form))
    bucket_hash[i] = SHA-256("sh-sync-bucket:v1:" ‖ i ‖ sorted leaves of i)
    root   = SHA-256("sh-sync-root:v1:" ‖ bucket_hash[0] ‖ … ‖ bucket_hash[255])

A row's **commit form** is ``{"r": resource, "v": projection}`` where the
projection keeps an allow-list of the §25.6 record's fields
(:data:`COMMIT_SPECS`): files replaced by their content SHA-256, item state
(reactions, votes, RSVPs, bids, answers), counts and receipt-local fields
dropped, lists sorted, floats as decimal strings, timestamps as one UTC form.
A field a later release adds to a record is never hashed until the commit
form is revised — a revision is a sibling page suite.

The page header is signed with the space authority seed
(:func:`~socialhome.authority_sig.sign_authority_event`, event type
:data:`~socialhome.authority_sig.AUTHORITY_EVENT_SPACE_SYNC_PAGE`); the
signature keeps its own ``authority_sig_suite``.

Pure: no I/O, no clock of its own (callers pass ``now_ms``). Depends only on
:mod:`socialhome.authority_sig`, :mod:`socialhome.canonical_json` and
:mod:`socialhome.crypto`.
"""

from __future__ import annotations

import binascii
import hashlib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from .authority_sig import (
    AUTHORITY_EVENT_SPACE_SYNC_PAGE,
    UnsupportedAuthoritySuite,
    sign_authority_event,
    strip_authority_sig_fields,
    verify_authority_event,
)
from .canonical_json import canonical_bytes, canonical_str
from .crypto import b64url_decode, b64url_encode

# ─── Suites ──────────────────────────────────────────────────────────────

#: SHA-256 leaves, 256 buckets, commit-form revision 1. A new commit form, a
#: different bucket count or another hash is a sibling suite.
PAGE_SUITE_SHA256_B256: str = "sha256-b256-c1"
SUPPORTED_PAGE_SUITES: frozenset[str] = frozenset({PAGE_SUITE_SHA256_B256})


class UnsupportedPageSuite(ValueError):
    """A page names a ``page_suite`` this build does not know. Refused —
    never defaulted (crypto-suite rule)."""


class PageRefused(ValueError):
    """A page failed verification (malformed, wrong space, epoch, signature,
    clock skew, or a vector that does not hash to its root)."""


# ─── Numbers ─────────────────────────────────────────────────────────────

BUCKETS: int = 256
HASH_BYTES: int = 32
VECTOR_BYTES: int = BUCKETS * HASH_BYTES

#: A signer seals at most this often (lazily, on a BEGIN that finds the
#: space changed — owner decision 2026-11-21).
PAGE_MIN_INTERVAL_S: int = 15 * 60
#: ``issued_at`` may be at most this far ahead of the receiver's clock.
MAX_PAGE_FUTURE_SKEW_MS: int = 300 * 1000
#: A page header as canonical JSON is never larger than this.
MAX_PAGE_BYTES: int = 1024
MAX_PAGE_INT: int = 2**63 - 1

_RKEY_PREFIX = b"sh-sync-rkey:v1:"
_LEAF_PREFIX = b"sh-sync-leaf:v1:"
_BUCKET_PREFIX = b"sh-sync-bucket:v1:"
_ROOT_PREFIX = b"sh-sync-root:v1:"

#: The signed header's fields (besides the two authority-signature fields).
PAGE_FIELDS: frozenset[str] = frozenset(
    {
        "space_id",
        "epoch",
        "signer",
        "seq",
        "issued_at",
        "cutoff",
        "rows",
        "root",
        "prev_root",
        "page_suite",
    }
)


# ─── Commit form ─────────────────────────────────────────────────────────


def _no_floats(value: Any) -> Any:
    """*value* with every float as its decimal string (``repr``: the
    shortest round-trip form, the same on every CPython)."""
    if value is None or type(value) in (str, int, bool):
        return value
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _no_floats(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_no_floats(v) for v in value]
    return value


def _sorted_list(values: Iterable[Any]) -> list[Any]:
    """A list in one order whatever order the household stored it in
    (strings by value, anything else by its canonical JSON)."""
    items = list(values)
    if all(type(v) is str for v in items):
        return sorted(items)
    return sorted(items, key=canonical_str)


def _stamp(value: Any) -> Any:
    """A timestamp in one form: UTC ISO 8601 with microseconds. A naive value
    is UTC (every stored stamp is). Anything unreadable stays as it is — it
    then only matches an identical value."""
    if not isinstance(value, str) or not value:
        return value
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        return value
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC).isoformat(timespec="microseconds")


#: Hashes a file reference (a local ``api/media/`` URL) to its content
#: SHA-256 (64 lowercase hex), or ``None`` when this household cannot.
FileHashOf = Callable[[str], str | None]


class _HostOnly(Exception):
    """The row cannot be committed here (a file without a known hash)."""


def _file_hash(url: Any, file_hash: FileHashOf) -> str | None:
    if url is None or url == "":
        return None
    if not isinstance(url, str):
        raise _HostOnly
    h = file_hash(url)
    if not h:
        raise _HostOnly
    return h


def _files(value: Any, file_hash: FileHashOf) -> Any:
    """A file field (one URL, or a list of them) as content hashes."""
    if isinstance(value, (list, tuple)):
        return sorted(h for v in value if (h := _file_hash(v, file_hash)) is not None)
    return _file_hash(value, file_hash)


@dataclass(slots=True, frozen=True)
class CommitSpec:
    """How one §25.6 resource's record becomes its commit form.

    ``fields`` — the allow-list; an absent field is committed as ``None``.
    ``files`` — top-level fields holding file references (hashed).
    ``nested_files`` — ``{field: sub_field}``: a dict field whose sub field
    is a file reference (``file_meta.url``, the link card thumbnail);
    ``nested_fields`` — the allow-list of that dict's keys.
    ``stamps`` — timestamp fields normalised by :func:`_stamp`.
    ``shape`` — per-field projections (poll options, schedule slots).
    ``skip`` — rows never committed (a gallery system album: local ids).
    """

    fields: tuple[str, ...]
    id_key: str = "id"
    files: tuple[str, ...] = ()
    nested_files: Mapping[str, str] = field(default_factory=dict)
    stamps: tuple[str, ...] = ("created_at",)
    nested_fields: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    shape: Mapping[str, Callable[[Any], Any]] = field(default_factory=dict)
    skip: Callable[[Mapping[str, Any]], bool] | None = None


def _poll_options(value: Any) -> Any:
    if not isinstance(value, list):
        return []
    return _sorted_list(
        {"id": o.get("id"), "text": o.get("text")} for o in value if isinstance(o, dict)
    )


def _schedule_slots(value: Any) -> Any:
    if not isinstance(value, list):
        return []
    keep = ("id", "slot_date", "start_time", "end_time", "position")
    return _sorted_list(
        {k: s.get(k) for k in keep} for s in value if isinstance(s, dict)
    )


def _poll_meta(value: Any) -> Any:
    if not isinstance(value, dict):
        return None
    keep = ("post_id", "question", "closes_at", "closed", "allow_multiple")
    return {k: value.get(k) for k in keep}


_TASK_FIELDS = (
    "id",
    "list_id",
    "space_id",
    "title",
    "status",
    "position",
    "created_by",
    "created_at",
    "description",
    "due_date",
    "assignees",
    "recurrence",
    "recurrence_parent_id",
    "archived_at",
    "priority",
    "labels",
)
_TASK_SPEC = CommitSpec(
    fields=_TASK_FIELDS, stamps=("created_at", "due_date", "archived_at")
)


def _tombstone(*fields: str, created_at: bool = True) -> CommitSpec:
    """A ``*_deleted`` record: its identity fields (+ ``created_at`` unless
    the record has none or it is receipt-local), never ``actor_user_id``
    (often empty on the live path)."""
    tail = ("created_at",) if created_at else ()
    return CommitSpec(fields=("id", *fields, *tail))


#: The committed resources and their commit forms (``docs/protocol/
#: sync-pages.md`` §2). ``bans``, ``members``, ``member_pictures`` and the
#: chat are host-only: not here, never committed.
COMMIT_SPECS: Mapping[str, CommitSpec] = MappingProxyType(
    {
        "posts_deleted": _tombstone("post_id", "author"),
        "posts": CommitSpec(
            # Owner decision 7: post fields the sync receiver drops
            # (location, bot_id, linked_*, no_link_preview, pinned,
            # moderated) stay uncommitted.
            fields=(
                "id",
                "space_id",
                "author",
                "type",
                "content",
                "created_at",
                "edited_at",
                "hidden_from_feed",
                "media_url",
                "image_urls",
                "file_meta",
                "link_preview",
            ),
            files=("media_url", "image_urls"),
            nested_files={"file_meta": "url", "link_preview": "thumbnail_url"},
            nested_fields={
                "file_meta": ("url", "mime_type", "original_name", "size_bytes"),
                "link_preview": (
                    "url",
                    "title",
                    "description",
                    "site_name",
                    "thumbnail_url",
                ),
            },
            stamps=("created_at", "edited_at"),
        ),
        "comments_deleted": _tombstone("comment_id", "post_id", "author"),
        "comments": CommitSpec(
            # ``edited_at`` is not kept by the sync receiver (a streamed
            # comment edit lands with no stamp), so it is left out; the
            # edited content is committed.
            fields=(
                "id",
                "post_id",
                "author",
                "type",
                "created_at",
                "parent_id",
                "content",
                "media_url",
                "deleted",
            ),
            files=("media_url",),
        ),
        "task_lists": CommitSpec(fields=("id", "space_id", "name", "created_by")),
        # Their records carry no ``created_at``.
        "task_lists_deleted": _tombstone("space_id", "created_by", created_at=False),
        "tasks_deleted": _tombstone(
            "space_id", "list_id", "created_by", created_at=False
        ),
        "tasks": _TASK_SPEC,
        "tasks_archived": _TASK_SPEC,
        "pages_deleted": _tombstone("space_id", "created_by"),
        "pages": CommitSpec(
            # ``sp-`` file names are the same on every household; locks and
            # delete requests are local workflow.
            fields=(
                "id",
                "title",
                "content",
                "cover_image_url",
                "created_by",
                "seq",
                "version_hash",
                "conflict",
            ),
            stamps=(),
        ),
        "stickies_deleted": _tombstone("author"),
        "stickies": CommitSpec(
            fields=(
                "id",
                "author",
                "content",
                "color",
                "position_x",
                "position_y",
                "created_at",
                "space_id",
            )
        ),
        # ``created_at`` is the receiver's clock here (calendar_repo).
        "calendar_deleted": _tombstone("created_by", created_at=False),
        "calendar": CommitSpec(
            # The series row; RSVPs are item state (rule 1).
            fields=(
                "id",
                "calendar_id",
                "summary",
                "start",
                "end",
                "created_by",
                "description",
                "all_day",
                "attendees",
                "rrule",
                "capacity",
                "rsvp_enabled",
                "cover_url",
                "location",
                "tz",
                "announce_in_feed",
            ),
            files=("cover_url",),
            stamps=("start", "end"),
        ),
        "gallery_albums_deleted": _tombstone("owner_user_id"),
        "gallery": CommitSpec(
            # Albums (without item_count / updated_at) and items, one
            # resource; system albums have local ids and are left out.
            fields=(
                "kind",
                "id",
                "space_id",
                "owner_user_id",
                "name",
                "description",
                "cover_item_id",
                "retention_exempt",
                "created_at",
                "album_id",
                "uploaded_by",
                "item_type",
                "url",
                "thumbnail_url",
                "width",
                "height",
                "duration_s",
                "caption",
                "taken_at",
                "sort_order",
                "source_post_id",
            ),
            files=("url", "thumbnail_url"),
            stamps=("created_at", "taken_at"),
            skip=lambda r: bool(r.get("is_system")),
        ),
        "gallery_items_deleted": _tombstone("uploaded_by", "album_id"),
        "polls": CommitSpec(
            # The definition only: votes and counts are item state.
            fields=("post_id", "meta", "options"),
            id_key="post_id",
            stamps=(),
            shape={"meta": _poll_meta, "options": _poll_options},
        ),
        "schedules": CommitSpec(
            fields=("post_id", "title", "deadline", "slots", "finalized_slot_id"),
            id_key="post_id",
            stamps=("deadline",),
            shape={"slots": _schedule_slots},
        ),
        "space_zones_deleted": _tombstone("created_by"),
        "space_zones": CommitSpec(
            fields=(
                "id",
                "space_id",
                "name",
                "latitude",
                "longitude",
                "radius_m",
                "created_by",
                "created_at",
                "color",
                "icon",
            )
        ),
        "bazaar": CommitSpec(
            # The listing without its bids (item state).
            fields=(
                "post_id",
                "space_id",
                "seller_user_id",
                "mode",
                "title",
                "description",
                "image_urls",
                "end_time",
                "currency",
                "status",
                "price",
                "start_price",
                "step_price",
                "winner_user_id",
                "winning_price",
                "sold_at",
                "created_at",
            ),
            id_key="post_id",
            files=("image_urls",),
            stamps=("created_at", "end_time", "sold_at"),
        ),
        "timetables_deleted": _tombstone("created_by"),
        "timetables": CommitSpec(
            fields=(
                "schema",
                "id",
                "name",
                "created_by",
                "created_at",
                "updated_by",
                "version",
                "week_start",
                "tz",
                "color",
                "days",
                "defaults",
                "entries",
                "overrides",
                "validity",
                "assignees",
            )
        ),
    }
)

#: The resources a page commits to.
COMMITTED_RESOURCES: frozenset[str] = frozenset(COMMIT_SPECS)


def row_id_of(resource: str, record: Mapping[str, Any]) -> str | None:
    """The id that names *record* within *resource* (``None``: none)."""
    spec = COMMIT_SPECS.get(resource)
    if spec is None:
        return None
    raw = record.get(spec.id_key)
    return str(raw) if isinstance(raw, (str, int)) and str(raw) else None


def file_refs(resource: str, record: Mapping[str, Any]) -> list[str]:
    """The file references of *record* that its commit form hashes."""
    spec = COMMIT_SPECS.get(resource)
    if spec is None:
        return []
    out: list[str] = []
    for name in spec.files:
        value = record.get(name)
        values = value if isinstance(value, (list, tuple)) else [value]
        out.extend(v for v in values if isinstance(v, str) and v)
    for name, sub in spec.nested_files.items():
        parent = record.get(name)
        if isinstance(parent, dict):
            value = parent.get(sub)
            if isinstance(value, str) and value:
                out.append(value)
    return out


def commit_form(
    resource: str, record: Mapping[str, Any], file_hash: FileHashOf
) -> dict[str, Any] | None:
    """The commit form of *record*, or ``None`` when the row is not committed
    here (an uncommitted resource, a row without an id, a skipped row, or a
    file this household cannot hash — rule 3: a mismatch costs
    availability, never safety)."""
    spec = COMMIT_SPECS.get(resource)
    if spec is None or row_id_of(resource, record) is None:
        return None
    if spec.skip is not None and spec.skip(record):
        return None
    projection: dict[str, Any] = {}
    try:
        for name in spec.fields:
            value = record.get(name)
            if name in spec.shape:
                value = spec.shape[name](value)
            elif name in spec.files:
                value = _files(value, file_hash)
            elif name in spec.nested_files:
                if isinstance(value, dict):
                    sub = spec.nested_files[name]
                    keep = spec.nested_fields.get(name, (sub,))
                    value = {k: value.get(k) for k in keep}
                    value[sub] = _file_hash(value.get(sub), file_hash)
                else:
                    value = None
            elif name in spec.stamps:
                value = _stamp(value)
            value = _no_floats(value)
            if isinstance(value, list):
                value = _sorted_list(value)
            projection[name] = value
    except _HostOnly:
        return None
    return {"r": resource, "v": projection}


def row_key(resource: str, row_id: str) -> bytes:
    """A row's identity hash; its first byte is its bucket."""
    return hashlib.sha256(
        _RKEY_PREFIX + resource.encode() + b"\x00" + row_id.encode()
    ).digest()


def leaf_of(form: Mapping[str, Any]) -> bytes:
    """The leaf of a commit form."""
    return hashlib.sha256(_LEAF_PREFIX + canonical_bytes(form)).digest()


def record_leaf(
    resource: str, record: Mapping[str, Any], file_hash: FileHashOf
) -> tuple[int, bytes] | None:
    """``(bucket, leaf)`` of a §25.6 record, or ``None`` (not committed)."""
    form = commit_form(resource, record, file_hash)
    if form is None:
        return None
    row_id = row_id_of(resource, record)
    assert row_id is not None  # commit_form checked it
    return row_key(resource, row_id)[0], leaf_of(form)


def bucket_hash(index: int, leaves: Iterable[bytes]) -> bytes:
    """The hash of bucket *index* over its leaves (in any order)."""
    return hashlib.sha256(
        _BUCKET_PREFIX + bytes([index]) + b"".join(sorted(leaves))
    ).digest()


def root_of(vector: bytes) -> bytes:
    """The root over a 256 × 32-byte bucket vector."""
    if len(vector) != VECTOR_BYTES:
        raise ValueError("a bucket vector is 256 × 32 bytes")
    return hashlib.sha256(_ROOT_PREFIX + vector).digest()


class VectorBuilder:
    """Collects leaves bucket by bucket; :meth:`finish` hashes the vector.
    Memory: 32 bytes per committed row (no rows, no records kept)."""

    __slots__ = ("_buckets", "rows")

    def __init__(self) -> None:
        self._buckets: list[list[bytes]] = [[] for _ in range(BUCKETS)]
        self.rows = 0

    def add(
        self, resource: str, record: Mapping[str, Any], file_hash: FileHashOf
    ) -> bool:
        found = record_leaf(resource, record, file_hash)
        if found is None:
            return False
        self._buckets[found[0]].append(found[1])
        self.rows += 1
        return True

    def finish(self) -> bytes:
        return b"".join(
            bucket_hash(i, leaves) for i, leaves in enumerate(self._buckets)
        )


def differing_buckets(a: bytes, b: bytes) -> list[int]:
    """The bucket indices where two vectors differ."""
    return [
        i
        for i in range(BUCKETS)
        if a[i * HASH_BYTES : (i + 1) * HASH_BYTES]
        != b[i * HASH_BYTES : (i + 1) * HASH_BYTES]
    ]


# ─── Vector delta ────────────────────────────────────────────────────────


def vector_delta(old: bytes, new: bytes) -> list[tuple[int, bytes]]:
    """The buckets of *new* that differ from *old*."""
    return [
        (i, new[i * HASH_BYTES : (i + 1) * HASH_BYTES])
        for i in differing_buckets(old, new)
    ]


def apply_delta(vector: bytes, delta: Sequence[tuple[int, bytes]]) -> bytes:
    """*vector* with *delta*'s buckets replaced."""
    out = bytearray(vector)
    for i, h in delta:
        out[i * HASH_BYTES : (i + 1) * HASH_BYTES] = h
    return bytes(out)


def delta_to_bytes(delta: Sequence[tuple[int, bytes]]) -> bytes:
    """A delta for storage: 33 bytes per bucket (index, hash)."""
    return b"".join(bytes([i]) + h for i, h in delta)


def delta_from_bytes(raw: bytes) -> list[tuple[int, bytes]]:
    step = HASH_BYTES + 1
    if len(raw) % step:
        raise ValueError("malformed stored delta")
    return [(raw[k], raw[k + 1 : k + step]) for k in range(0, len(raw), step)]


def delta_to_wire(delta: Sequence[tuple[int, bytes]]) -> list[list[Any]]:
    return [[i, b64url_encode(h)] for i, h in delta]


def delta_from_wire(raw: object) -> list[tuple[int, bytes]]:
    """A received delta, bounded and strict: ≤ 256 distinct buckets, each a
    32-byte hash. Raises :class:`PageRefused`."""
    if not isinstance(raw, list) or len(raw) > BUCKETS:
        raise PageRefused("malformed delta")
    seen: set[int] = set()
    out: list[tuple[int, bytes]] = []
    for item in raw:
        if not (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], int)
            and not isinstance(item[0], bool)
            and 0 <= item[0] < BUCKETS
            and item[0] not in seen
            and isinstance(item[1], str)
        ):
            raise PageRefused("malformed delta entry")
        h = _b64(item[1])
        if len(h) != HASH_BYTES:
            raise PageRefused("malformed delta hash")
        seen.add(item[0])
        out.append((item[0], h))
    return out


def vector_to_wire(vector: bytes) -> str:
    return b64url_encode(vector)


def vector_from_wire(raw: object) -> bytes:
    if not isinstance(raw, str):
        raise PageRefused("malformed vector")
    vector = _b64(raw)
    if len(vector) != VECTOR_BYTES:
        raise PageRefused("malformed vector")
    return vector


def _b64(raw: str) -> bytes:
    try:
        return b64url_decode(raw)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise PageRefused("malformed base64") from exc


# ─── The page ────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SyncPage:
    """A signed page header (the wire shape, ``to_wire`` / :func:`parse_page`).
    ``root`` / ``prev_root`` are raw 32-byte hashes here, b64url on the wire.
    ``cutoff`` — the retention cutoff the commitment used (naive UTC
    ``YYYY-MM-DD HH:MM:SS``, ``None``: keep forever)."""

    space_id: str
    epoch: int
    signer: str
    seq: int
    issued_at: int
    cutoff: str | None
    rows: int
    root: bytes
    prev_root: bytes | None
    page_suite: str
    authority_sig: str = ""
    authority_sig_suite: str = ""

    def body(self) -> dict[str, Any]:
        """The signed fields."""
        return {
            "space_id": self.space_id,
            "epoch": self.epoch,
            "signer": self.signer,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "cutoff": self.cutoff,
            "rows": self.rows,
            "root": b64url_encode(self.root),
            "prev_root": (
                b64url_encode(self.prev_root) if self.prev_root is not None else None
            ),
            "page_suite": self.page_suite,
        }

    def to_wire(self) -> dict[str, Any]:
        return {
            **self.body(),
            "authority_sig": self.authority_sig,
            "authority_sig_suite": self.authority_sig_suite,
        }

    def order_key(self) -> tuple[int, int, int]:
        """Pages order by ``(epoch, issued_at, seq)``."""
        return (self.epoch, self.issued_at, self.seq)

    def is_newer_than(self, other: SyncPage | None) -> bool:
        return other is None or self.order_key() > other.order_key()

    def page_hash(self) -> str:
        """A short id of the whole signed page (logs, conflict evidence)."""
        return hashlib.sha256(canonical_bytes(self.to_wire())).hexdigest()[:16]

    def ref(self) -> dict[str, Any]:
        """``page`` in a ``SPACE_SYNC_BEGIN``: what the requester holds —
        its order key (epoch, issued_at, seq), signer and root."""
        return {
            "signer": self.signer,
            "epoch": self.epoch,
            "seq": self.seq,
            "issued_at": self.issued_at,
            "root": b64url_encode(self.root),
        }


def _int(raw: object, name: str) -> int:
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or not 0 <= raw <= MAX_PAGE_INT
    ):
        raise PageRefused(f"malformed {name}")
    return raw


def _str(raw: object, name: str, *, limit: int = 128) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > limit:
        raise PageRefused(f"malformed {name}")
    return raw


def _hash_field(raw: object, name: str) -> bytes:
    if not isinstance(raw, str):
        raise PageRefused(f"malformed {name}")
    value = _b64(raw)
    if len(value) != HASH_BYTES:
        raise PageRefused(f"malformed {name}")
    return value


def parse_page(raw: object) -> SyncPage:
    """A received page header, strictly parsed (no signature check).
    Raises :class:`UnsupportedPageSuite` for an unknown suite (checked
    first, never defaulted) and :class:`PageRefused` for anything else."""
    if not isinstance(raw, dict):
        raise PageRefused("page is not an object")
    suite = raw.get("page_suite")
    if suite not in SUPPORTED_PAGE_SUITES:
        raise UnsupportedPageSuite(str(suite))
    if len(canonical_bytes(raw)) > MAX_PAGE_BYTES:
        raise PageRefused("page too large")
    if not PAGE_FIELDS <= raw.keys():
        raise PageRefused("page lacks a field")
    cutoff = raw.get("cutoff")
    if cutoff is not None:
        cutoff = _str(cutoff, "cutoff", limit=32)
    prev = raw.get("prev_root")
    return SyncPage(
        space_id=_str(raw.get("space_id"), "space_id"),
        epoch=_int(raw.get("epoch"), "epoch"),
        signer=_str(raw.get("signer"), "signer"),
        seq=_int(raw.get("seq"), "seq"),
        issued_at=_int(raw.get("issued_at"), "issued_at"),
        cutoff=cutoff,
        rows=_int(raw.get("rows"), "rows"),
        root=_hash_field(raw.get("root"), "root"),
        prev_root=None if prev is None else _hash_field(prev, "prev_root"),
        page_suite=str(suite),
        authority_sig=_str(raw.get("authority_sig"), "authority_sig", limit=256),
        authority_sig_suite=_str(
            raw.get("authority_sig_suite"), "authority_sig_suite", limit=64
        ),
    )


def sign_page(
    *,
    space_id: str,
    epoch: int,
    signer: str,
    seq: int,
    issued_at: int,
    cutoff: str | None,
    rows: int,
    vector: bytes,
    prev_root: bytes | None,
    space_seed: bytes,
) -> SyncPage:
    """Seal a page over *vector* with the space authority seed."""
    page = SyncPage(
        space_id=space_id,
        epoch=int(epoch),
        signer=signer,
        seq=int(seq),
        issued_at=int(issued_at),
        cutoff=cutoff,
        rows=int(rows),
        root=root_of(vector),
        prev_root=prev_root,
        page_suite=PAGE_SUITE_SHA256_B256,
    )
    sig = sign_authority_event(
        event_type=AUTHORITY_EVENT_SPACE_SYNC_PAGE,
        space_id=space_id,
        payload=page.body(),
        space_seed=space_seed,
    )
    return replace(
        page,
        authority_sig=sig["authority_sig"],
        authority_sig_suite=sig["authority_sig_suite"],
    )


def verify_page(
    raw: object,
    *,
    space_id: str,
    space_public_key: bytes,
    pinned_epoch: int,
    now_ms: int,
) -> SyncPage:
    """Parse and verify a received page (``docs/protocol/sync-pages.md`` §3):
    a known suite, this space, exactly the epoch we pin (a lower one is a
    retired key, a higher one waits for the rotation), a valid authority
    signature, and ``issued_at`` at most 300 s ahead of our clock.

    Raises :class:`UnsupportedPageSuite` or :class:`PageRefused`."""
    page = parse_page(raw)
    if page.space_id != space_id:
        raise PageRefused("page names another space")
    if page.epoch != int(pinned_epoch):
        raise PageRefused(f"page epoch {page.epoch} is not the pinned {pinned_epoch}")
    if page.issued_at > now_ms + MAX_PAGE_FUTURE_SKEW_MS:
        raise PageRefused("page issued in the future")
    assert isinstance(raw, dict)
    try:
        ok = verify_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SYNC_PAGE,
            space_id=space_id,
            payload=strip_authority_sig_fields(raw),
            authority_sig=page.authority_sig,
            authority_sig_suite=page.authority_sig_suite,
            space_public_key=space_public_key,
        )
    except UnsupportedAuthoritySuite as exc:
        raise PageRefused(f"unknown authority suite {exc}") from exc
    if not ok:
        raise PageRefused("bad authority signature")
    return page


def is_conflict(held: SyncPage, incoming: SyncPage) -> bool:
    """Equivocation: two valid pages of one signer and epoch that cannot
    both be honest — the same ``seq`` with different roots, or two
    different successors of one page (the same ``prev_root``)."""
    if held.signer != incoming.signer or held.epoch != incoming.epoch:
        return False
    if held.root == incoming.root:
        return False
    if held.seq == incoming.seq:
        return True
    return held.prev_root is not None and held.prev_root == incoming.prev_root


def parse_page_ref(raw: object) -> dict[str, Any] | None:
    """The ``page`` of a ``SPACE_SYNC_BEGIN`` (what the requester holds), or
    ``None`` when absent or malformed (it then gets the full vector)."""
    if not isinstance(raw, dict):
        return None
    try:
        return {
            "signer": _str(raw.get("signer"), "signer"),
            "epoch": _int(raw.get("epoch"), "epoch"),
            "seq": _int(raw.get("seq"), "seq"),
            "issued_at": _int(raw.get("issued_at"), "issued_at"),
            "root": _hash_field(raw.get("root"), "root"),
        }
    except PageRefused:
        return None


__all__ = [
    "BUCKETS",
    "COMMITTED_RESOURCES",
    "COMMIT_SPECS",
    "HASH_BYTES",
    "MAX_PAGE_FUTURE_SKEW_MS",
    "PAGE_MIN_INTERVAL_S",
    "PAGE_SUITE_SHA256_B256",
    "SUPPORTED_PAGE_SUITES",
    "VECTOR_BYTES",
    "CommitSpec",
    "FileHashOf",
    "PageRefused",
    "SyncPage",
    "UnsupportedPageSuite",
    "VectorBuilder",
    "apply_delta",
    "bucket_hash",
    "commit_form",
    "delta_from_bytes",
    "delta_from_wire",
    "delta_to_bytes",
    "delta_to_wire",
    "differing_buckets",
    "file_refs",
    "is_conflict",
    "leaf_of",
    "parse_page",
    "parse_page_ref",
    "record_leaf",
    "root_of",
    "row_id_of",
    "row_key",
    "sign_page",
    "vector_delta",
    "vector_from_wire",
    "vector_to_wire",
    "verify_page",
]
