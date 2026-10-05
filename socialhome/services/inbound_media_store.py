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
never contribute chunks to one assembly.
"""

from __future__ import annotations

import hashlib
import logging
import pathlib
import re

import aiofiles.os

log = logging.getLogger(__name__)

#: A peer-supplied media file name / id that becomes one path component.
SAFE_MEDIA_NAME: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,199}")

#: The path prefix every stored local media reference carries.
MEDIA_REF_PREFIX: str = "api/media/"

#: Upper bound on ``chunk_count`` for one transfer (512 KiB chunks → 2 GiB).
MAX_MEDIA_CHUNKS: int = 4096


def is_safe_media_name(name: str) -> bool:
    """``True`` when ``name`` is usable as a single file name under media."""
    return bool(name) and SAFE_MEDIA_NAME.fullmatch(name) is not None


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


def partial_key(from_instance: str, transfer_id: str) -> str:
    """Directory name for one household's in-flight chunks of a transfer."""
    digest = hashlib.sha256(f"{from_instance}\0{transfer_id}".encode()).hexdigest()
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
