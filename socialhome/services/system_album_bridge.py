"""SystemAlbumBridge — mirror feed media into the per-scope "Posts" album.

The Gallery's *Posts* album is purely derived state: every photo or
video shared via a feed post (household or space) shows up there
automatically and disappears when its source post is deleted. This
bridge owns that derivation by subscribing to the relevant domain
events and delegating the actual writes to
:class:`socialhome.services.gallery_service.GalleryService`.

The split keeps responsibilities tidy: the gallery service owns the
domain rules (race-safe ensure, idempotent mirror, scope-agnostic
unmirror); the bridge owns *when* to call them.

Six subscriptions cover every code path that creates or removes feed
media:

* ``PostCreated`` — household feed post (image / video).
* ``PostEdited`` — household feed image edit (add or remove).
* ``SpacePostCreated`` — space feed post; carries ``space_id``.
* ``SpacePostSynced`` — a space post a §25.6 sync stored.
* ``PostDeleted`` — author-delete on either feed; ``space_service``
  publishes this on both author and moderation paths.
* ``SpacePostModerated`` — admin removal of a space post; the post
  payload gives us the id.

Wired by :func:`socialhome.app.create_app` next to the other
event-bus subscribers.
"""

from __future__ import annotations

import logging

from ..domain.events import (
    PostCreated,
    PostDeleted,
    PostEdited,
    SpacePostCreated,
    SpacePostModerated,
    SpacePostSynced,
)
from ..infrastructure.event_bus import EventBus
from .gallery_service import GalleryService

log = logging.getLogger(__name__)


class SystemAlbumBridge:
    """Subscribe to post lifecycle events and mirror media into the system album."""

    __slots__ = ("_gallery", "_bus")

    def __init__(self, gallery: GalleryService, bus: EventBus) -> None:
        self._gallery = gallery
        self._bus = bus

    def wire(self) -> None:
        """Subscribe handlers on the bus. Idempotent — safe to call twice."""
        self._bus.subscribe(PostCreated, self._on_post_created)
        self._bus.subscribe(PostEdited, self._on_post_edited)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(SpacePostModerated, self._on_space_post_moderated)
        self._bus.subscribe(SpacePostSynced, self._on_space_post_synced)

    # ─── Household feed ───────────────────────────────────────────────────

    async def _on_post_created(self, event: PostCreated) -> None:
        try:
            await self._gallery.mirror_post(event.post, space_id=None)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: mirror PostCreated failed for %s: %s",
                event.post.id,
                exc,
            )

    async def _on_post_edited(self, event: PostEdited) -> None:
        try:
            await self._gallery.mirror_post(event.post, space_id=None)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: mirror PostEdited failed for %s: %s",
                event.post.id,
                exc,
            )

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        # ``unmirror_post`` is scope-agnostic: it finds rows by
        # ``source_post_id`` regardless of which album they live in.
        # That lets a single PostDeleted handler clean up both
        # household and space mirrors without needing the scope.
        try:
            await self._gallery.unmirror_post(event.post_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: unmirror PostDeleted failed for %s: %s",
                event.post_id,
                exc,
            )

    # ─── Space feed ───────────────────────────────────────────────────────

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        try:
            await self._gallery.mirror_post(event.post, space_id=event.space_id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: mirror SpacePostCreated failed for %s: %s",
                event.post.id,
                exc,
            )

    async def _on_space_post_synced(self, event: SpacePostSynced) -> None:
        # A post a §25.6 sync stored: its media belongs in this household's
        # own system album too (the provider's mirror rows never stream).
        # ``mirror_post`` is idempotent, so a re-applied post churns nothing,
        # and quiet: a catch-up sends no live frame per photo. A deleted or
        # moderated post (a tombstone) mirrors nothing.
        try:
            if event.post.deleted or event.post.moderated:
                await self._gallery.unmirror_post(event.post.id)
            else:
                await self._gallery.mirror_post(
                    event.post, space_id=event.space_id, quiet=True
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: mirror SpacePostSynced failed for %s: %s",
                event.post.id,
                exc,
            )

    async def _on_space_post_moderated(self, event: SpacePostModerated) -> None:
        # Moderation = soft-delete with a moderator flag. ``space_service``
        # publishes both ``SpacePostModerated`` and ``PostDeleted`` on
        # the moderation path, so this handler is technically redundant
        # — but it's the explicit signal that the row was admin-killed,
        # and unmirror is idempotent (a second call finds zero rows
        # and short-circuits in the repo).
        try:
            await self._gallery.unmirror_post(event.post.id)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning(
                "system-album: unmirror SpacePostModerated failed for %s: %s",
                event.post.id,
                exc,
            )
