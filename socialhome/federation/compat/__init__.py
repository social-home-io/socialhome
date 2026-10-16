"""Per-version compatibility transforms for outbound federation payloads.

This subtree exists so the **main service code stays clean of
``if peer_supports(...) else legacy_shape``** branches. Every new
``proto_version`` bump that needs a real outbound rewrite (i.e. it
isn't fail-soft on the receiver side) lands as a new file here.

Adding a new compat shim:

1. Bump :data:`socialhome.domain.federation_capabilities.OURS` and add
   a named ``MIN_FOR_*`` constant on
   :class:`socialhome.domain.federation_capabilities.FederationCapability`.
2. Append a row to the version-history table in
   :doc:`docs/protocol/capabilities <docs/protocol/capabilities.md>`
   that names the §319-paragraph-5 policy (``skip`` / ``fallback`` /
   ``force-upgrade``).
3. Add a ``<feature>_v<N>.py`` file in this package that exports a
   :class:`OutboundTransform` (or matches the ``Transform`` Protocol
   below) and register it via :func:`register`.
4. Call :func:`transform_for_peer` from the outbound layer just before
   sending the event to a specific peer.

Dropping support for an old version is a single-file delete here —
the registry stops finding the shim, the helper becomes a no-op for
that event type, and the main service code never needs touching.

Issue #319 paragraph 5 ("Mixed-space degraded-shape policy per
feature") asks for the discipline this module enforces by
construction: each shim's docstring **must** name the picked policy
(``skip`` / ``fallback`` / ``force-upgrade``) and the user-facing
consequence.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ...domain.federation import FederationEventType

log = logging.getLogger(__name__)


class Transform(Protocol):
    """One peer-version-aware transform of a federation payload.

    A registered transform is called with the local protocol_version
    we want to send AT (always ``OURS``) and the receiving peer's
    advertised ``proto_version``. It returns either:

    * the same ``payload`` dict (peer can parse our wire shape — no
      rewrite needed),
    * a new dict (degraded fallback shape), or
    * ``None`` to drop the send entirely (force-upgrade policy).
    """

    def __call__(
        self,
        *,
        payload: dict,
        peer_version: int,
    ) -> dict | None: ...


_REGISTRY: dict[FederationEventType, list[Transform]] = {}


def register(event_type: FederationEventType, transform: Transform) -> None:
    """Register a per-event-type compat transform.

    Multiple transforms can stack for the same event type — they are
    applied in registration order, each receiving the output of the
    previous. A transform that returns ``None`` short-circuits the
    chain and the outbound is dropped.
    """
    _REGISTRY.setdefault(event_type, []).append(transform)


def transform_for_peer(
    *,
    event_type: FederationEventType,
    payload: dict,
    peer_version: int,
) -> dict | None:
    """Run every registered transform for ``event_type`` against ``payload``.

    Called by the outbound layer just before handing the payload to
    the transport. ``payload`` is the canonical (newest) wire shape
    that ``OURS`` produces; the transforms below may strip or rewrite
    fields based on the peer's advertised ``proto_version``.

    Returns the (possibly rewritten) payload, or ``None`` if a
    transform decided to drop the send (force-upgrade policy).
    Callers MUST treat ``None`` as "do not dispatch this event to
    this peer".
    """
    transforms = _REGISTRY.get(event_type)
    if not transforms:
        return payload
    out: dict | None = payload
    for t in transforms:
        if out is None:
            return None
        out = t(payload=out, peer_version=peer_version)
    return out


def _auto_register() -> None:
    """Wire up every shim in this package at import time.

    Importing the modules below executes their module-level
    :func:`register` calls. The list stays explicit (rather than a
    glob over the directory) so a future contributor adding a new
    shim has one place to touch and the registration order is
    deterministic.
    """
    from . import dm_media_v3  # noqa: F401


# Eagerly register on package import so a caller that imports
# ``socialhome.federation.compat`` and calls ``transform_for_peer``
# without touching the sub-modules still gets the right behaviour.
_auto_register()


# Re-exported so callers don't need to know the registry layout.
__all__: list[str] = [
    "Transform",
    "register",
    "transform_for_peer",
]
