"""Hard-delete a space and its on-disk media.

Shared by both ends of a space removal:

* the owner host (``SpaceService.dissolve_space``), and
* every member household (the inbound ``SPACE_DISSOLVED`` handler).

The DB graph is dropped by a single ``DELETE FROM spaces`` — every
space-scoped child table is ``REFERENCES spaces(id) ON DELETE CASCADE``
and the connection runs ``PRAGMA foreign_keys=ON`` (see
``repositories/space_repo.py::SqliteSpaceRepo.purge``). Media *files*
have no FK, so they are collected first and unlinked after the rows are
gone.

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
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from ..repositories.gallery_repo import AbstractGalleryRepo
    from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)


async def collect_space_media_urls(
    *,
    post_repo: "AbstractSpacePostRepo",
    gallery_repo: "AbstractGalleryRepo | None",
    bazaar_repo: "AbstractBazaarRepo | None" = None,
    space_id: str,
) -> list[str]:
    """Deduped media references (post + comment + gallery + bazaar) for a space.

    Returns the raw stored values (``api/media/<file>`` URLs and bare
    gallery basenames); :func:`unlink_media` resolves either shape to a
    basename, so mirrored-blob overlap between posts and the gallery
    system album is harmless.

    Best-effort completeness: anything a producer adds that isn't
    enumerated here is still reaped by the periodic media-orphan sweep
    once the rows are gone — collection just frees the disk *promptly*.
    """
    urls: list[str] = list(await post_repo.list_space_media_urls(space_id))
    if gallery_repo is not None:
        urls.extend(await gallery_repo.list_space_item_filenames(space_id))
    if bazaar_repo is not None:
        urls.extend(await bazaar_repo.list_space_media_urls(space_id))
    seen: set[str] = set()
    deduped: list[str] = []
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


async def purge_space_and_media(
    *,
    space_repo: "AbstractSpaceRepo",
    post_repo: "AbstractSpacePostRepo",
    gallery_repo: "AbstractGalleryRepo | None",
    bazaar_repo: "AbstractBazaarRepo | None" = None,
    media_dir: pathlib.Path | None,
    media_refs: "AbstractMediaReferenceRepo | None" = None,
    space_id: str,
) -> int:
    """Hard-delete ``space_id``: collect media → drop rows → unlink files.

    Returns the number of media files actually removed. Collection runs
    before the cascade delete (the rows pointing at the files are gone
    afterwards); each unlink is best-effort, so a shared/missing file
    never blocks the purge.
    """
    media = await collect_space_media_urls(
        post_repo=post_repo,
        gallery_repo=gallery_repo,
        bazaar_repo=bazaar_repo,
        space_id=space_id,
    )
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
