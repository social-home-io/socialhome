"""Where a space file lives in a GFS media cache — the ``media_refs`` descriptor.

A space item (post, calendar event, gallery item) that reaches a household
only through a GFS carries, INSIDE its encrypted payload::

    "media_refs": {"<local filename>": {"blob_id", "suite", "epoch", "size",
                                         "scope", "addr_suite"}}

one entry per file of the item: the address of the sealed blob
(:mod:`socialhome.space_media_seal`), its suite, the content-key epoch it is
sealed under, the blob's byte size, and the GFS scope it was uploaded under
(``space`` or ``channel``) with the address construction (``addr_suite``,
:func:`~socialhome.domain.gfs_space_media.space_media_address`). The address
is bound to the scope: a reader checks it with the scope id it knows ITSELF
(the space id, or its own channel id for a private space) — the descriptor
never names a scope id. No GFS URL — every reader fetches from the GFS it is
connected to. A receiver keeps each entry as a
``space_media_refs`` row (:class:`SpaceMediaRef`) so the file can be fetched
on arrival or when first viewed.

Pure: no I/O.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..moment_public_media import MAX_BLOB_BYTES, is_blob_id
from ..space_media_seal import UnsupportedSpaceMediaSuite, require_space_media_suite
from .gfs_space_media import MEDIA_SCOPES, SUPPORTED_ADDR_SUITES

if TYPE_CHECKING:
    from .post import Post

log = logging.getLogger(__name__)

#: The payload / record field carrying the descriptor.
MEDIA_REFS_FIELD: str = "media_refs"

#: Entries one descriptor may carry (a post shows ≤ 10 pictures, a file and
#: a link card): past it the descriptor is refused whole, never walked.
MAX_DESCRIPTOR_ENTRIES: int = 16

#: The resources (§25.6 names) whose items carry ``media_refs``: posts
#: (pictures, file, link card), calendar events (the cover), gallery
#: items (thumbnail + full file) and pages (v_81: the cover and the
#: pictures the body shows).
MEDIA_REF_RESOURCES: frozenset[str] = frozenset(
    {"posts", "calendar", "gallery", "pages"}
)

#: A local file a page's markdown body shows: ``api/media/<file>`` — an
#: optional leading ``/`` and a ``?query`` belong to the match — never the
#: same path inside another URL or path (``https://host/api/media/x`` links
#: to someone else's server; the SPA never loads it as a picture here).
_PAGE_BODY_MEDIA_RE = re.compile(
    r"(?<![\w.~:/%-])/?api/media/[A-Za-z0-9._-]+(?:\?[^)\s\"'<>]*)?"
)

_ENTRY_KEYS: frozenset[str] = frozenset(
    {"blob_id", "suite", "epoch", "size", "scope", "addr_suite"}
)
_MAX_EPOCH: int = 2**53


@dataclass(slots=True, frozen=True)
class SpaceMediaRef:
    """One descriptor entry: the file *filename* of an item of *space_id*
    (the name the item's author gave it — what the blob's AAD names) is the
    blob *blob_id* in a GFS media cache. As a ``space_media_refs`` row, it is
    stored here as *local_name*
    (:func:`~socialhome.services.inbound_media_store.gfs_space_media_name`)."""

    filename: str
    space_id: str
    blob_id: str
    suite: str
    epoch: int
    size: int
    #: The GFS scope the blob was uploaded under (``space`` / ``channel``)
    #: and how its address binds that scope.
    scope: str
    addr_suite: str
    #: The item that introduced the file here (a post, calendar event or
    #: gallery item id) and the household that delivered it — the row is
    #: bound to them: a descriptor of the same file from another item or
    #: space is refused (write-once). Never on the wire.
    item_id: str = ""
    origin: str = ""
    #: The reader's own name for the file (a row only; never on the wire).
    local_name: str = ""
    created_at: str | None = None

    def to_wire(self) -> dict:
        return {
            "blob_id": self.blob_id,
            "suite": self.suite,
            "epoch": self.epoch,
            "size": self.size,
            "scope": self.scope,
            "addr_suite": self.addr_suite,
        }


@dataclass(slots=True, frozen=True)
class SpaceMediaLocalization:
    """What a received item's files are called here: the author's file name
    → this household's name for the copy fetched from a GFS (*names*), and
    the descriptor rows to keep once the item is stored (*refs*). A file not
    in *names* keeps its reference (its bytes come another way, or not at
    all). Pure — the rewrite is applied to the item before it is saved."""

    names: Mapping[str, str] = field(default_factory=dict)
    refs: tuple[SpaceMediaRef, ...] = ()

    def url(self, value: str | None) -> str | None:
        """*value* with its file renamed when *names* has it (path prefix
        and query kept), else unchanged."""
        if not value or not self.names:
            return value
        path, sep, query = value.partition("?")
        head, slash, base = path.rpartition("/")
        local = self.names.get(base)
        if local is None:
            return value
        return f"{head}{slash}{local}{sep}{query}"

    def post(self, post: Post) -> Post:
        """*post* with every file :func:`post_media_urls` lists renamed."""
        if not self.names:
            return post
        preview = post.link_preview
        file_meta = post.file_meta
        return replace(
            post,
            media_url=self.url(post.media_url),
            image_urls=tuple(self.url(u) or u for u in post.image_urls or ()),
            link_preview=(
                replace(preview, thumbnail_url=self.url(preview.thumbnail_url))
                if preview is not None
                else None
            ),
            file_meta=(
                replace(file_meta, url=self.url(file_meta.url) or file_meta.url)
                if file_meta is not None
                else None
            ),
        )


def media_refs_to_wire(refs: Iterable[SpaceMediaRef]) -> dict[str, dict]:
    """The descriptor for *refs*, keyed by local file name."""
    return {ref.filename: ref.to_wire() for ref in refs}


def _is_int(value: object, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def _str(value: object) -> list[str]:
    return [value] if isinstance(value, str) and value else []


def page_body_media_refs(text: object) -> list[str]:
    """Every local file reference in a page's markdown body, as written
    (query included), in order — repeats included."""
    if not isinstance(text, str) or not text:
        return []
    return [m.group(0) for m in _PAGE_BODY_MEDIA_RE.finditer(text)]


def rewrite_page_body_media(text: str, rename: Callable[[str], str]) -> str:
    """*text* with every :func:`page_body_media_refs` reference replaced by
    ``rename(reference)``; nothing else is touched."""
    return _PAGE_BODY_MEDIA_RE.sub(lambda m: rename(m.group(0)), text)


def page_media_urls(cover: object, content: object) -> list[str]:
    """A page's files: its cover, then each picture its body shows
    (without a query), each reference once."""
    urls = _str(cover)
    for ref in page_body_media_refs(content):
        url = ref.split("?", 1)[0]
        if url not in urls:
            urls.append(url)
    return urls


def distinct_media_files(urls: list[str], limit: int) -> list[str]:
    """The first *limit* files of *urls*, each once however it is spelled."""
    out: list[str] = []
    seen: set[str] = set()
    for url in urls:
        name = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if name in seen:
            continue
        if len(out) == limit:
            break
        seen.add(name)
        out.append(url)
    return out


def record_media_urls(resource: str, record: dict[str, Any]) -> list[str]:
    """The local file references an item of *resource* shows, in order —
    THE one rule for which files an item has: the sender describes exactly
    these, every receiver allows exactly these (live, follower, §25.6)."""
    if resource == "posts":
        urls = _str(record.get("media_url"))
        images = record.get("image_urls")
        if isinstance(images, (list, tuple)):
            for url in images:
                urls.extend(_str(url))
        file_meta = record.get("file_meta")
        if isinstance(file_meta, dict):
            urls.extend(_str(file_meta.get("url")))
        preview = record.get("link_preview")
        if isinstance(preview, dict):
            urls.extend(_str(preview.get("thumbnail_url")))
        return urls
    if resource == "calendar":
        return _str(record.get("cover_url"))
    if resource == "gallery" and record.get("kind", "item") == "item":
        return _str(record.get("thumbnail_url")) + _str(record.get("url"))
    if resource == "pages":
        # A page may show more pictures than one descriptor carries: its
        # first ones are described (a receiver refuses a longer one whole).
        return distinct_media_files(
            page_media_urls(record.get("cover_image_url"), record.get("content")),
            MAX_DESCRIPTOR_ENTRIES,
        )
    return []


def post_media_urls(post: Post) -> list[str]:
    """:func:`record_media_urls` for a :class:`~.post.Post`."""
    preview = post.link_preview
    file_meta = post.file_meta
    return record_media_urls(
        "posts",
        {
            "media_url": post.media_url,
            "image_urls": list(post.image_urls or ()),
            "file_meta": {"url": file_meta.url} if file_meta is not None else None,
            "link_preview": (
                {"thumbnail_url": preview.thumbnail_url}
                if preview is not None
                else None
            ),
        },
    )


def parse_media_refs(
    raw: object,
    *,
    space_id: str,
    allowed: Collection[str],
    item_id: str = "",
    origin: str = "",
) -> list[SpaceMediaRef]:
    """The well-formed entries of a received descriptor.

    *allowed* is the set of local file names the item itself references
    (:func:`record_media_urls`, already validated as safe, non-reserved
    media names by the caller): an entry for any other name is dropped — the
    rest are kept — so a descriptor can never point a fetch at a file the
    item does not show. A descriptor of more than
    :data:`MAX_DESCRIPTOR_ENTRIES` entries is refused whole. An entry with
    an unknown suite is refused with a WARNING (never a default); any other
    malformed entry is skipped.
    """
    if not isinstance(raw, dict) or not allowed or len(raw) > MAX_DESCRIPTOR_ENTRIES:
        return []
    out: list[SpaceMediaRef] = []
    for name, entry in raw.items():
        if name not in allowed or not isinstance(entry, dict):
            continue
        if set(entry) != _ENTRY_KEYS:
            continue
        try:
            suite = require_space_media_suite(entry.get("suite"))
        except UnsupportedSpaceMediaSuite as exc:
            log.warning("space media descriptor for %s refused: %s", name, exc)
            continue
        addr_suite = entry.get("addr_suite")
        if addr_suite not in SUPPORTED_ADDR_SUITES:
            log.warning(
                "space media descriptor for %s refused: address suite %r",
                name,
                addr_suite,
            )
            continue
        scope = entry.get("scope")
        if scope not in MEDIA_SCOPES:
            continue
        blob_id = entry.get("blob_id")
        epoch = entry.get("epoch")
        size = entry.get("size")
        if not is_blob_id(blob_id) or not _is_int(epoch, 0, _MAX_EPOCH):
            continue
        if not _is_int(size, 1, MAX_BLOB_BYTES):
            continue
        out.append(
            SpaceMediaRef(
                filename=name,
                space_id=space_id,
                blob_id=str(blob_id),
                suite=suite,
                epoch=int(epoch),  # type: ignore[arg-type]
                size=int(size),  # type: ignore[arg-type]
                scope=str(scope),
                addr_suite=str(addr_suite),
                item_id=item_id,
                origin=origin,
            )
        )
    return out


__all__ = [
    "MAX_DESCRIPTOR_ENTRIES",
    "MEDIA_REFS_FIELD",
    "MEDIA_REF_RESOURCES",
    "SpaceMediaLocalization",
    "SpaceMediaRef",
    "media_refs_to_wire",
    "parse_media_refs",
    "post_media_urls",
    "record_media_urls",
]
