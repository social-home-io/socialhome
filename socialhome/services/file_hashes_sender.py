"""Shared sender behaviour for the ``files`` hash map (v_85).

Every service that sends a space item with files (posts and comments,
calendar events, gallery items, bazaar listings, pages, moderation
submissions) adds the SHA-256 of each file it stores to the item's
payload, inside the encryption, so a receiver can store the file
content-addressed and check the bytes (:mod:`socialhome.domain.file_hashes`).
``FederationService.broadcast_to_space_members`` leaves the map out for a
household below v_85.

Behaviour-only mixin (``__slots__ = ()``): the consumer declares the
``_file_hasher`` slot and sets it to ``None`` in its ``__init__``.
"""

from __future__ import annotations

from collections.abc import Iterable

from .media_file_hasher import MediaFileHasher


class FileHashesMixin:
    """Attach the ``files`` map to an outbound space item."""

    __slots__ = ()

    _file_hasher: MediaFileHasher | None

    def attach_file_hasher(self, hasher: MediaFileHasher) -> None:
        """Wire the household's file hasher (v_85)."""
        # The slot is the consumer's (behaviour-only mixin).
        self._file_hasher = hasher  # type: ignore[misc]

    async def _with_file_hashes(
        self, payload: dict, urls: Iterable[str | None]
    ) -> dict:
        """*payload* plus the ``files`` map of *urls* — a new dict — or
        *payload* unchanged (nothing hashed, not wired, an error: fail-soft,
        the item then takes the legacy names on receivers)."""
        return await MediaFileHasher.attach(self._file_hasher, payload, urls)
