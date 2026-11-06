"""Write-once helpers for media bytes another household sends us.

``SPACE_MEDIA_BLOB`` and ``DM_MEDIA_BLOB`` land peer-supplied bytes under
``media_dir`` by a peer-supplied name. Two rules keep that from reaching
files the sender was not sent for:

* **Strict names.** A name becomes a single path component, so it must
  match :data:`SAFE_MEDIA_NAME` — no separators, no leading dot, no NUL or
  whitespace. Every name the upload pipeline mints (``<uuid-hex>.<ext>``,
  ``<message-uuid>``) fits.
* **Write-once.** A file that already exists is never replaced. Media
  names are random, so the same name arriving twice is either a benign
  re-delivery of the same bytes or an attempt to swap somebody else's
  file; neither needs a write. :func:`publish_once` moves a fully written
  temp file into place only if nothing is there yet (a hard link, which
  fails atomically on an existing name, with a check-then-replace
  fallback for filesystems without hard links).

A third rule covers the *references* a received row carries
(``media_url``, ``image_urls``): :func:`local_media_ref` keeps only the
shape a local upload stores, ``api/media/<safe-name>``, and drops anything
else — so a row never points outside the media dir, and never at a
remote URL.

Partial chunk files live under ``media_dir/.partial/<key>`` where ``key``
(:func:`partial_key`) binds the sending household, so two households can
never contribute chunks to one assembly (and the chunk count, so two
attempts chunked differently never mix). Senders pipeline chunks, so they
land in any order: :func:`store_chunk` runs for every chunk and assembles
as soon as the set is complete, not when the ``final`` one lands.

**One media directory, two namespaces.** Space files and DM attachments
share ``media_dir``. A space blob may be sent by any household that can
name an unknown row, so the DM side must never take a file it did not
assemble from its own sender's chunks: a received DM attachment lives
under :func:`dm_media_name` — ``dm-`` plus a digest that binds the
sending household and the message id — and a ``SPACE_MEDIA_BLOB`` never
writes a :func:`is_reserved_media_name` (the ``dm-`` / ``gm-`` prefixes
other paths own, and the DM transients the media sweep leaves alone).

**Size caps.** A chunked file is bounded by what the receiving path
accepts (:data:`SPACE_MEDIA_MAX_BYTES`, :func:`dm_media_max_bytes`):
the declared ``file_size_bytes`` is checked on every chunk, and each
chunk must fit its share of that size (:func:`inbound_size_refusal`), so
the parts on disk never pass twice the cap and the assembly must match
the declared size exactly.

**No symlinks.** The ``.partial`` tree is only ever created by
:func:`store_chunk`; anything else found there — a symlink above all — is
removed without being followed (:func:`drop_parts`), never descended.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re
import stat
import uuid

import aiofiles
import aiofiles.os

from ..domain.errors import InvalidMediaRefError
from ..domain.media_constraints import (
    AUDIO_MAX_UPLOAD_BYTES,
    FILE_MAX_UPLOAD_BYTES,
    IMAGE_MAX_UPLOAD_BYTES,
    VIDEO_MAX_UPLOAD_BYTES,
)

log = logging.getLogger(__name__)

#: A peer-supplied media file name / id that becomes one path component.
SAFE_MEDIA_NAME: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,199}")

#: The path prefix every stored local media reference carries.
MEDIA_REF_PREFIX: str = "api/media/"

#: Upper bound on ``chunk_count`` for one transfer. The bytes are capped
#: separately per path (:data:`SPACE_MEDIA_MAX_BYTES`,
#: :func:`dm_media_max_bytes`).
MAX_MEDIA_CHUNKS: int = 4096

#: Prefix of a received DM attachment's file name (:func:`dm_media_name`).
DM_MEDIA_PREFIX: str = "dm-"

#: Name prefixes owned by one receiving path each — a ``SPACE_MEDIA_BLOB``
#: never writes them: ``dm-`` received DM attachments, ``gm-`` moment media
#: fetched from a GFS (``moment_public_media_inbound``).
RESERVED_MEDIA_PREFIXES: tuple[str, ...] = (DM_MEDIA_PREFIX, "gm-")

#: DM transients (``<msg>.preview.webp`` and the legacy ``.part<N>`` /
#: ``.assembled`` names). No ``media_url`` column holds them, so the media
#: sweep leaves them to ``dm_gc``; a space blob never takes such a name.
TRANSIENT_DM_MEDIA_RE: re.Pattern[str] = re.compile(
    r"(\.preview\.webp|\.part\d+|\.assembled)", re.IGNORECASE
)

#: The largest whole file a ``SPACE_MEDIA_BLOB`` may carry: a space post's
#: video is the biggest thing a space ships (``VIDEO_MAX_UPLOAD_BYTES``,
#: the upload cap every stored file passed — pictures, voice notes and
#: plain files have smaller caps).
SPACE_MEDIA_MAX_BYTES: int = VIDEO_MAX_UPLOAD_BYTES


def is_safe_media_name(name: str) -> bool:
    """``True`` when ``name`` is usable as a single file name under media."""
    return bool(name) and SAFE_MEDIA_NAME.fullmatch(name) is not None


def is_reserved_media_name(name: str) -> bool:
    """``True`` for a name a ``SPACE_MEDIA_BLOB`` must never write: one
    another receiving path owns (:data:`RESERVED_MEDIA_PREFIXES`) or a DM
    transient (:data:`TRANSIENT_DM_MEDIA_RE`)."""
    return name.lower().startswith(RESERVED_MEDIA_PREFIXES) or bool(
        TRANSIENT_DM_MEDIA_RE.search(name)
    )


def dm_media_name(from_instance: str, message_id: str, ext: str) -> str:
    """File name of a received DM attachment.

    Binds the household that sent the bytes and the message: only that
    household's own ``DM_MEDIA_BLOB`` chunks can assemble this name (a
    space blob never writes a ``dm-`` name), and the ``DM_MESSAGE`` adopts
    only the name of the household it came from — so a file another
    household planted, before or after the message, is never shown as the
    attachment. Fixed length, always :func:`is_safe_media_name`.
    """
    digest = hashlib.sha256(
        f"dm-media\0{from_instance}\0{message_id}".encode()
    ).hexdigest()
    return f"{DM_MEDIA_PREFIX}{digest[:40]}{ext}"


def dm_media_max_bytes(mime_type: str) -> int:
    """The largest whole file a ``DM_MEDIA_BLOB`` of ``mime_type`` may be.

    The sender's ``mime_type`` comes from its browser's file picker, so
    only a declared picture or voice note gets the tighter cap: either
    went through the upload's image / audio path or the plain-file path,
    so it is at most the largest of those caps. Anything else (a video,
    ``application/octet-stream`` from a picker that names no type) may be
    a video: ``VIDEO_MAX_UPLOAD_BYTES``.
    """
    major = mime_type.split("/", 1)[0].strip().lower()
    if major in ("image", "audio"):
        return max(
            IMAGE_MAX_UPLOAD_BYTES, AUDIO_MAX_UPLOAD_BYTES, FILE_MAX_UPLOAD_BYTES
        )
    return VIDEO_MAX_UPLOAD_BYTES


def inbound_size_refusal(
    *, total_size: int, chunk_len: int, chunk_count: int, max_bytes: int
) -> str | None:
    """Why one chunk of a ``total_size``-byte file is refused, or ``None``.

    * The whole file must not pass ``max_bytes`` (checked on every chunk,
      so the first one that lands already refuses an oversized file).
    * A chunk must fit its share: senders split into equal chunks with a
      shorter last one (``split_into_chunks``), so ``chunk_count - 1``
      full chunks are less than the whole file. A bigger chunk can't be
      part of an honest ``total_size`` file — and with it refused, the
      parts of one transfer on disk stay under twice ``total_size``.
    """
    if total_size > max_bytes:
        return f"declares {total_size} bytes, over the {max_bytes}-byte cap"
    if chunk_len > total_size or (
        chunk_count > 1 and (chunk_count - 1) * chunk_len >= total_size
    ):
        return (
            f"a {chunk_len}-byte chunk of {chunk_count} can't belong "
            f"to a {total_size}-byte file"
        )
    return None


def media_file_path(media_dir: pathlib.Path, name: str) -> pathlib.Path | None:
    """``media_dir / name`` when ``name`` is one safe component, else ``None``.

    For any path built from a peer-supplied id (``<message_id>.preview.webp``
    …): the name must pass :func:`is_safe_media_name` *and* the joined path
    must sit directly in ``media_dir`` — a pure check (no filesystem I/O),
    so an absolute name or a ``..`` component can never select a file
    outside the media directory even if the name rule is loosened later.
    """
    if not is_safe_media_name(name):
        return None
    path = media_dir / name
    if path.parent != media_dir or path.name != name:
        return None
    return path


def local_media_ref(value: object) -> str | None:
    """A peer-supplied media reference in the canonical local shape.

    Accepts ``api/media/<name>`` (a leading ``/`` and a ``?query`` are
    tolerated and dropped) where ``<name>`` is :func:`is_safe_media_name`;
    returns the normalised ``api/media/<name>``, or ``None`` for anything
    else — a bare name, another path, a full URL, a non-string.
    """
    if not isinstance(value, str):
        return None
    path = value.split("?", 1)[0].removeprefix("/")
    if not path.startswith(MEDIA_REF_PREFIX):
        return None
    name = path[len(MEDIA_REF_PREFIX) :]
    if not is_safe_media_name(name):
        return None
    return MEDIA_REF_PREFIX + name


def verbatim_local_media_ref(value: object) -> str | None:
    """``value`` unchanged when it is a local media reference, else ``None``.

    For peer fields rendered as an ``<img>`` / ``<video>`` source (moment
    and highlight media, event / page covers): a third-party URL there
    would load from every viewer's browser and leak their IP. Kept
    byte-for-byte rather than normalised so a hash or signature computed
    over the sender's own copy still matches.
    """
    return value if isinstance(value, str) and local_media_ref(value) else None


def require_local_media_ref(value: str, *, field: str = "media_url") -> str:
    """``value`` unchanged when it is a local media reference, else raise.

    The local-write twin of :func:`verbatim_local_media_ref`: a create /
    edit path for a field rendered as an ``<img>`` / ``<video>`` source
    refuses what a receiving household would drop, so a row the author
    sees always federates and never points at a third-party host.

    :raises InvalidMediaRefError: (422 ``INVALID_MEDIA_URL``) otherwise.
    """
    if verbatim_local_media_ref(value) is None:
        raise InvalidMediaRefError(field)
    return value


def optional_local_media_ref(
    value: str | None, *, field: str = "media_url"
) -> str | None:
    """:func:`require_local_media_ref` for an optional field: an empty /
    missing value is ``None`` ("no media"), anything else must be local."""
    return require_local_media_ref(value, field=field) if value else None


def require_local_media_refs(
    values: tuple[str, ...] | list[str], *, field: str = "image_urls"
) -> tuple[str, ...]:
    """:func:`require_local_media_ref` over every entry of a media list."""
    return tuple(require_local_media_ref(v, field=field) for v in values)


def local_media_refs(values: object, *, limit: int) -> tuple[str, ...]:
    """:func:`local_media_ref` over a peer-supplied list: the canonical
    references it holds, in order, at most ``limit``. A non-list is empty."""
    if not isinstance(values, list):
        return ()
    out: list[str] = []
    for v in values:
        ref = local_media_ref(v)
        if ref is not None:
            out.append(ref)
    return tuple(out[:limit])


def media_basename(url: str | None) -> str:
    """File name a media URL points at (``api/media/x.webp?sig=…`` → ``x.webp``).

    Same derivation the sender uses for the blob id
    (``SpaceMediaSyncService.enqueue_for_blob``).
    """
    if not url:
        return ""
    return url.rsplit("/", 1)[-1].split("?", 1)[0]


def partial_key(from_instance: str, transfer_id: str, *, chunk_count: int) -> str:
    """Directory name for one household's in-flight chunks of a transfer.

    Binds the sending household, the transfer and its ``chunk_count``: a
    retry re-chunks the file for the tier it goes out on (240 KiB over an
    older channel, 512 KiB over HTTPS), so parts of two attempts with
    different chunk sizes never meet in one directory.
    """
    digest = hashlib.sha256(
        f"{from_instance}\0{transfer_id}\0{chunk_count}".encode()
    ).hexdigest()
    return digest[:32]


def parse_chunk_meta(index: object, count: object) -> tuple[int, int] | None:
    """Validate ``(chunk_index, chunk_count)``; ``None`` when out of range."""
    try:
        i = int(index)  # type: ignore[call-overload]
        n = int(count)  # type: ignore[call-overload]
    except TypeError, ValueError:
        return None
    if not 1 <= n <= MAX_MEDIA_CHUNKS or not 0 <= i < n:
        return None
    return i, n


async def remove_quietly(path: pathlib.Path) -> None:
    """Delete ``path`` if it exists; never raises."""
    try:
        await aiofiles.os.remove(path)
    except OSError:
        pass


async def publish_once(tmp: pathlib.Path, target: pathlib.Path) -> bool:
    """Move the finished ``tmp`` to ``target`` unless ``target`` exists.

    Returns ``True`` when ``tmp`` became ``target``; ``False`` when a file
    was already there (``tmp`` is discarded either way). ``tmp`` must be on
    the same filesystem as ``target``.
    """
    try:
        await aiofiles.os.link(tmp, target)
    except FileExistsError:
        await remove_quietly(tmp)
        return False
    except OSError:
        # No hard links on this filesystem (some network / FUSE mounts):
        # fall back to check-then-replace. The window between the two is
        # a single coroutine's await, not a peer-controlled delay.
        if await aiofiles.os.path.exists(target):
            await remove_quietly(tmp)
            return False
        await aiofiles.os.replace(tmp, target)
        return True
    await remove_quietly(tmp)
    return True


def part_name(index: int) -> str:
    """File name of chunk ``index`` inside a transfer's part directory."""
    return f"{index:06d}"


async def present_parts(part_dir: pathlib.Path) -> frozenset[str]:
    """Names of the chunk parts that have landed in ``part_dir``.

    One directory listing per chunk: linear in the chunk count, where a
    per-part probe on every chunk would be quadratic. Temp files (a part
    being written, an assembly in progress) never count.
    """
    try:
        names = await aiofiles.os.listdir(part_dir)
    except FileNotFoundError:
        return frozenset()
    return frozenset(n for n in names if len(n) == 6 and n.isdigit())


async def drop_parts(part_dir: pathlib.Path) -> bool:
    """Remove a transfer's part directory and everything in it; never raises.

    Never follows a symlink: a symlinked ``part_dir`` is unlinked itself
    (its target is left alone), entries are unlinked without being
    followed, and a sub-directory is never descended into — so a folder
    holding one stays. Returns whether ``part_dir`` is gone.
    """
    try:
        st = await aiofiles.os.stat(part_dir, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        await remove_quietly(part_dir)
        return not (
            await aiofiles.os.path.islink(part_dir)
            or await aiofiles.os.path.exists(part_dir)
        )
    try:
        names = await aiofiles.os.listdir(part_dir)
    except OSError:
        return False
    for name in names:
        await remove_quietly(part_dir / name)
    try:
        await aiofiles.os.rmdir(part_dir)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


async def store_chunk(
    part_dir: pathlib.Path,
    *,
    chunk_index: int,
    chunk_count: int,
    data: bytes,
    target: pathlib.Path,
    expected_size: int,
) -> bool | None:
    """Land one chunk and assemble ``target`` once every chunk is here.

    Senders pipeline a file's chunks, so they land in any order: whichever
    chunk completes the set 0..``chunk_count``-1 assembles — never "on the
    ``final`` chunk". ``part_dir`` is the transfer's own directory (keyed
    by :func:`partial_key`, which binds ``chunk_count``). Returns

    * ``None`` — still waiting for a chunk, or the assembled bytes did not
      come to ``expected_size`` (``-1`` = not declared): nothing published,
      and on a mismatch the parts are dropped (a resend starts clean).
    * ``True`` — this call assembled and published ``target``.
    * ``False`` — ``target`` already exists (another chunk completed the
      set first, or a re-delivery); it is never replaced.

    A part is written to a temp name and renamed into place, so an
    assembly never reads a half-written part. Each assembly writes a temp
    file of its own inside ``part_dir`` (removed with it), and
    :func:`publish_once` lets exactly one of two concurrent assemblies win.
    """
    # Only this function creates ``.partial`` and its part folders; a
    # symlink at either level would point every write outside it. The
    # root is checked before ``makedirs`` so nothing is created behind it.
    if await aiofiles.os.path.islink(part_dir.parent):
        raise OSError(f"{part_dir.parent.name}: is a symlink — refusing")
    await aiofiles.os.makedirs(part_dir, exist_ok=True)
    if await aiofiles.os.path.islink(part_dir):
        raise OSError(f"{part_dir.name}: part folder is a symlink — refusing")
    tmp_part = part_dir / f"part.tmp-{uuid.uuid4().hex}"
    try:
        async with aiofiles.open(tmp_part, "wb") as fh:
            await fh.write(data)
        await aiofiles.os.replace(tmp_part, part_dir / part_name(chunk_index))
    except OSError:
        await remove_quietly(tmp_part)
        if await aiofiles.os.path.exists(target):
            # Lost the directory to a completed assembly's cleanup.
            await drop_parts(part_dir)
            return False
        raise
    names = [part_name(i) for i in range(chunk_count)]
    present = await present_parts(part_dir)
    if not present.issuperset(names):
        if await aiofiles.os.path.exists(target):
            # A duplicate landing after assembly: leave no orphan part.
            await drop_parts(part_dir)
            return False
        return None
    tmp = part_dir / f"assembling-{uuid.uuid4().hex}"
    written = 0
    try:
        async with aiofiles.open(tmp, "wb") as out:
            for name in names:
                async with aiofiles.open(part_dir / name, "rb") as inp:
                    chunk = await inp.read()
                written += len(chunk)
                await out.write(chunk)
    except FileNotFoundError:
        # A concurrent assembly published and removed the parts mid-read.
        await remove_quietly(tmp)
        return False if await aiofiles.os.path.exists(target) else None
    except OSError:
        await remove_quietly(tmp)
        raise
    if expected_size >= 0 and written != expected_size:
        log.warning(
            "%s: assembled %d bytes, sender declared %d — discarding the parts",
            target.name,
            written,
            expected_size,
        )
        await remove_quietly(tmp)
        await drop_parts(part_dir)
        return None
    published = await publish_once(tmp, target)
    await drop_parts(part_dir)
    return published


async def note_existing(
    target: pathlib.Path,
    *,
    incoming_size: int,
    what: str,
    from_instance: str,
) -> None:
    """Log a skipped write: DEBUG for a same-size re-delivery, else WARNING."""
    try:
        existing_size = (await aiofiles.os.stat(target)).st_size
    except OSError:
        existing_size = -1
    if existing_size == incoming_size:
        log.debug("%s: %s already present — skipping", what, target.name)
        return
    log.warning(
        "%s from %s: %s already exists with different content — not replacing",
        what,
        from_instance,
        target.name,
    )
