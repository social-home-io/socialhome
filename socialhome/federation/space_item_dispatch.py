"""One receiver for v_97 space items (``docs/protocol/space-items.md``).

A space item reaches a household from a GFS space's channel (live push or
a ring fetch) or, in a mesh space, inside a ``SPACE_ITEM`` federation event.
Both end in :meth:`SpacePublicInbound.handle_channel_item`, which decrypts
the item at its epoch and hands an **event-shaped** (v2) item here.

:class:`SpaceItemDispatch` checks what only an item needs — the type is a
person action of :data:`~socialhome.domain.space_item.ITEM_SPECS`, the
author signature (``space-item-author:v2:``) over the origin household's
identity key, the signed ``space_id``, a stamp at most 300 s ahead, the
writer cert at a fresh epoch with the scope the spec names — then builds the
:class:`~socialhome.domain.federation.FederationEvent` the item replaces,
``from_instance`` = the proven origin, and hands it to
:meth:`FederationService.dispatch_space_item`: the same post-decrypt gates
and the same registry handler as a federation envelope of that type. The
roster authorizes the user a body names (owner Q1 A); the cert binds the
household and the epoch.

An item reaches us more than once (several GFSs, a live push and a ring
fetch, a mention note): the ``item_id`` is remembered, bounded, so a copy
costs a signature check and no handler run.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Protocol

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.space_item import item_spec, item_stamp
from ..domain.writer_cert import WriterCert
from ..services.space_item_author import (
    UnsupportedItemAuthorSigSuite,
    verify_signed_event_item,
)
from ..services.space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..services.space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: How many item ids are remembered, and for how long (seconds). An item
#: is re-offered at most for the ring's life (24 h) — a copy older than the
#: window re-runs its handler, which is idempotent per ``ITEM_SPECS``.
SEEN_ITEMS_MAX: int = 8192
SEEN_ITEMS_TTL_S: float = 24 * 3600.0

#: The ``msg_id`` prefix of an event built from an item (never on a wire).
ITEM_MSG_ID_PREFIX: str = "item:"


class _ItemSink(Protocol):
    async def dispatch_space_item(self, event: FederationEvent) -> bool: ...


def parse_event_item(plaintext: bytes) -> tuple[str, dict] | None:
    """``(item_type, inner)`` when decrypted item bytes are a v_97
    event-shaped item (its type in ``ITEM_SPECS``), else ``None`` — a v1
    item, an authority item or garbage go their own way."""
    try:
        body = orjson.loads(plaintext)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    item_type = body.get("item_type")
    inner = body.get("inner")
    if item_spec(item_type) is None or not isinstance(inner, dict):
        return None
    return str(item_type), inner


class SpaceItemDispatch:
    """Verify an event-shaped space item and apply it as its event."""

    __slots__ = ("_federation", "_own_instance_id", "_seen", "_writer_certs")

    def __init__(
        self,
        *,
        federation: _ItemSink,
        writer_certs: SpaceWriterCertService,
        own_instance_id: str,
    ) -> None:
        self._federation = federation
        self._writer_certs = writer_certs
        self._own_instance_id = own_instance_id
        self._seen: OrderedDict[str, float] = OrderedDict()

    # ── Seen ids ─────────────────────────────────────────────────────────

    def _was_seen(self, item_id: str, now: float) -> bool:
        at = self._seen.get(item_id)
        return at is not None and now - at < SEEN_ITEMS_TTL_S

    def _remember(self, item_id: str, now: float) -> None:
        self._seen[item_id] = now
        self._seen.move_to_end(item_id)
        while len(self._seen) > SEEN_ITEMS_MAX:
            self._seen.popitem(last=False)

    # ── Apply ────────────────────────────────────────────────────────────

    async def apply(
        self, space: Space, *, epoch: int, item_type: str, inner: dict
    ) -> bool:
        """Verify an event-shaped person item of ``space`` sealed at
        ``epoch`` and dispatch it. ``True`` when its handler ran; ``False``
        when it was refused (logged), a copy, our own echo, or held."""
        spec = item_spec(item_type)
        if spec is None or spec.authority:
            log.warning("space_item: %r is not a person item type — dropped", item_type)
            return False
        if inner.get("item_type") != item_type:
            log.warning("space_item: inner names another type — dropped")
            return False
        origin = str(inner.get("origin_instance_id") or "")
        if origin and origin == self._own_instance_id:
            return False  # our own echo off the channel
        try:
            signed = verify_signed_event_item(inner)
        except UnsupportedItemAuthorSigSuite as exc:
            log.warning("space_item: unknown author_sig_suite %s — dropped", exc)
            return False
        if not signed:
            log.warning(
                "space_item: author signature refused for %s in space %s",
                item_type,
                space.id,
            )
            return False
        item_id = str(inner["item_id"])
        now = time.monotonic()
        if self._was_seen(item_id, now):
            return False
        if inner.get("space_id") != space.id:
            log.warning("space_item: %s names another space — dropped", item_id)
            return False
        body = inner["body"]
        body_space = body.get("space_id")
        if body_space is not None and body_space != space.id:
            log.warning("space_item: %s body names another space — dropped", item_id)
            return False
        ts = str(inner["ts"])
        if item_stamp(ts) is None:
            log.warning("space_item: %s has a bad or future stamp — dropped", item_id)
            return False
        raw_cert = inner.get(WRITER_CERT_FIELD)
        try:
            WriterCert.from_wire(raw_cert)
        except ValueError:
            log.warning("space_item: %s carries no valid writer cert", item_id)
            return False
        if not await self._writer_certs.check_item(
            space,
            raw_cert,
            epoch=epoch,
            author_pk=bytes.fromhex(str(inner["author_pk"])),
            required_scope=spec.scope,
            item_type=item_type,
        ):
            log.warning(
                "space_item: writer cert refused for %s (%s) from %s in space %s",
                item_id,
                item_type,
                origin,
                space.id,
            )
            return False
        event = FederationEvent(
            msg_id=ITEM_MSG_ID_PREFIX + item_id,
            event_type=FederationEventType(item_type),
            from_instance=origin,
            to_instance=self._own_instance_id,
            timestamp=ts,
            payload=body,
            space_id=space.id,
            epoch=epoch,
        )
        dispatched = await self._federation.dispatch_space_item(event)
        # Remembered whatever the gates said: the same item again gets the
        # same verdict (a held one is replayed by the seat buffer itself).
        self._remember(item_id, now)
        return dispatched
