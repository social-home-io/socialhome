"""Fail-soft removal of a stored media file given its ``api/media/`` URL.

Several services own a media file 1:1 with a DB row (a DM message blob,
a gallery upload, …) and must delete the file when the row goes away —
otherwise blobs accumulate on disk forever. They all need the same
small, defensive operation: map the stored ``api/media/<filename>`` URL
to ``media_dir/<basename>`` and remove it without ever letting disk
state block the row deletion.

This helper is deliberately conservative — it only resolves the URL's
*basename* under ``media_dir`` (no path traversal) and swallows a
missing file / FS error.

A row's file name is not proof that the row owns the file: a blob can be
shared (feed-post media mirrored into a gallery system album), and a row
received from another household may name a file that belongs to another
row. Per-row delete paths therefore call :func:`unlink_unreferenced`,
which removes a file only once no remaining row references it.
:func:`unlink_media` is the raw primitive underneath.
"""

from __future__ import annotations

import logging
import pathlib
from collections.abc import Iterable
from typing import TYPE_CHECKING

import aiofiles.os

if TYPE_CHECKING:
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo

log = logging.getLogger(__name__)

#: Above this many files in one call, read the whole reference set once.
BATCH_REFERENCE_THRESHOLD: int = 8


def media_basename(media_url: str | None) -> str | None:
    """Resolve a stored ``api/media/<file>`` URL to its bare filename.

    Tolerates a leading ``/`` and a ``?query``. Returns ``None`` for an
    empty/``.``/``..`` value. Splitting on ``/`` guarantees the result has
    no path separator, so ``media_dir / basename`` can't escape media_dir.
    """
    if not media_url:
        return None
    name = media_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if not name or name in (".", ".."):
        return None
    return name


async def unlink_media(media_dir: pathlib.Path, media_url: str | None) -> bool:
    """Best-effort delete of the local file backing ``media_url``.

    ``media_url`` is the stored ``api/media/<filename>`` form (a leading
    ``/`` and a ``?query`` are tolerated). Returns ``True`` iff a file
    was removed. Never raises: a missing file, an unresolvable URL, or a
    filesystem error is logged at debug and swallowed.
    """
    filename = media_basename(media_url)
    if filename is None:
        return False
    path = media_dir / filename
    try:
        await aiofiles.os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:  # pragma: no cover — defensive
        log.debug("unlink_media: failed to remove %s: %s", path, exc)
        return False


async def unlink_unreferenced(
    media_dir: pathlib.Path | None,
    refs: "AbstractMediaReferenceRepo | None",
    media_urls: Iterable[str | None],
) -> int:
    """Delete each file in ``media_urls`` that no DB row still references.

    Call it *after* the owning row is gone (or its media columns cleared),
    so the only references left are other rows'. Without a reference repo
    nothing is deleted — a file that might still be in use is kept, and
    the orphan sweep reclaims it once nothing points at it. Returns the
    number of files removed; never raises.
    """
    if media_dir is None or refs is None:
        return 0
    names: list[str] = []
    for url in media_urls:
        name = media_basename(url)
        if name is not None and name not in names:
            names.append(name)
    if not names:
        return 0
    try:
        if len(names) > BATCH_REFERENCE_THRESHOLD:
            # A retention sweep or a space purge: one full read beats a
            # per-file scan.
            live = await refs.referenced_basenames()
            unused = [n for n in names if n not in live]
        else:
            unused = [n for n in names if not await refs.is_referenced(n)]
    except Exception:
        # Cannot tell what is still in use — keep everything; the orphan
        # sweep reclaims it later.
        log.warning(
            "media cleanup: reference check failed; keeping files", exc_info=True
        )
        return 0
    removed = 0
    for name in unused:
        if await unlink_media(media_dir, name):
            removed += 1
    return removed
