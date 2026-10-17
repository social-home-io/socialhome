"""Shared visibility-lookup mixin for outbound federation services.

The per-pair user-visibility toggle in ConnectionDetail
(``PATCH /api/pairing/connections/{instance_id}/visible-users``) writes
into ``peer_user_visibility``. Outbound federation services that fan
user-scoped events to peers (presence, DMs, highlights, moments, …)
all need the same read: "which of our local users are hidden from this
specific peer?"

This module exposes that read as a small mixin so every service shares
one implementation. Subclasses:

* Accept ``visibility_repo`` as an optional constructor kwarg.
* Assign it to ``self._visibility_repo`` in ``__init__`` (the slot
  lives on the mixin so the service's own ``__slots__`` doesn't need
  to repeat it).
* Call ``await self.hidden_for_peer(peer_id)`` before each per-peer
  send and skip the send when the sender's ``user_id`` is in the
  returned set.

Shape is a ``frozenset[str]`` per peer so a service that fans one event
to many users (e.g. ``USERS_SYNC``, ``DM_HISTORY_CHUNK``) does one DB
read and filters with set membership. The companion shape — one read
per (peer, user) pair — would be N lookups for the same batch; we
prefer the bulk read.

Fail-soft: a missing repo (``None``) or a repo error returns the empty
set, i.e. default-visible. The contract matches the existing
``ProfileFederationOutbound._on_updated`` semantics — visibility lookup
must never block federation outbound on a transient infra failure.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..repositories.peer_user_visibility_repo import (
        AbstractPeerUserVisibilityRepo,
    )

log = logging.getLogger(__name__)


class VisibilityMixin:
    """Mixin: outbound federation service that filters per-pair on
    peer-user visibility.

    Subclasses inherit the ``_visibility_repo`` slot from this mixin
    and write to it in their own ``__init__``. They never need to add
    ``"_visibility_repo"`` to their own ``__slots__``.
    """

    __slots__ = ("_visibility_repo",)

    _visibility_repo: AbstractPeerUserVisibilityRepo | None

    async def hidden_for_peer(self, peer_id: str) -> frozenset[str]:
        """Return the set of local user_ids hidden from ``peer_id``.

        Returns the empty set when ``self._visibility_repo`` is
        ``None`` (back-compat for test wiring that doesn't inject the
        repo) or when the repo raises (transient infra failure →
        default-visible, not fail-closed).
        """
        if self._visibility_repo is None:
            return frozenset()
        try:
            return await self._visibility_repo.hidden_user_ids_for_peer(peer_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug(
                "VisibilityMixin.hidden_for_peer: lookup failed for %s: %s",
                peer_id,
                exc,
                exc_info=True,
            )
            return frozenset()
