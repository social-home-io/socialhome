"""Hard-delete a space and its on-disk media.

Shared by both ends of a space removal:

* the owner host (``SpaceService.dissolve_space``), and
* every member household (the inbound ``SPACE_DISSOLVED`` handler).

The DB graph is dropped by a single ``DELETE FROM spaces`` — every
space-scoped child table is ``REFERENCES spaces(id) ON DELETE CASCADE``
and the connection runs ``PRAGMA foreign_keys=ON`` (see
``repositories/space_repo.py::SqliteSpaceRepo.purge``). Media *files*
have no FK, so they are collected first (the media reference repo's
``space_files``: the space media-field registry's columns) and unlinked
after the rows are gone.

Ordering invariant: callers that also publish a UI/realtime event (which
resolves recipients from the now-doomed ``space_members`` rows) MUST
publish it **before** calling :func:`purge_space_and_media` — once the
cascade fires there are no members left to notify.
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

from ..media.cleanup import unlink_unreferenced

if TYPE_CHECKING:
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


async def purge_space_and_media(
    *,
    space_repo: AbstractSpaceRepo,
    media_dir: pathlib.Path | None,
    media_refs: AbstractMediaReferenceRepo | None = None,
    space_id: str,
) -> int:
    """Hard-delete ``space_id``: collect media → drop rows → unlink files.

    Returns the number of media files actually removed. Collection runs
    before the cascade delete (the rows pointing at the files are gone
    afterwards); each unlink is best-effort, so a shared/missing file
    never blocks the purge.
    """
    # Every file field of every space item (the registry's columns —
    # ``space_media_columns()``), only canonical non-reserved space files.
    # Without a reference repo nothing would be unlinked anyway.
    media = await media_refs.space_files(space_id) if media_refs is not None else []
    await space_repo.purge(space_id)
    # Rows in this space may name files other rows (another space, the
    # feed, a DM) still use; only files nothing references any more go.
    removed = await unlink_unreferenced(media_dir, media_refs, media)
    log.info(
        "purged space %s: dropped content graph, removed %d/%d media file(s)",
        space_id,
        removed,
        len(media),
    )
    return removed
