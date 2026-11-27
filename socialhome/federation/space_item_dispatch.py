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
fetch, a mention note) and may be replayed on purpose (anyone who saw its
ciphertext — the GFS too). Three things make a copy harmless: the signed
``epoch`` must be the epoch it was sealed at (nobody re-seals an old item
under a newer key); its stamp may be at most :data:`MAX_ITEM_AGE_S` old;
and ``(origin, item_id)`` is claimed in the DURABLE replay cache the §24.11
pipeline uses (``FederationService.note_replay_id``), whose window
(``REPLAY_CACHE_WINDOW``, 25 h) outlives every item still accepted — so a
copy is refused across restarts and never evicted by another household.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.gfs_channel import RING_DURABLE_TTL_S
from ..domain.space_item import (
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_SPACE_EVENT,
    item_spec,
    item_stamp,
)
from ..services.space_item_author import (
    UnsupportedItemAuthorSigSuite,
    verify_signed_event_item,
)
from ..services.space_writer_cert_service import WRITER_CERT_FIELD

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..services.space_writer_cert_service import SpaceWriterCertService

log = logging.getLogger(__name__)

#: The oldest stamp an item may carry (seconds): the ring's life. The
#: durable replay cache keeps a claimed id longer (asserted in the tests),
#: so every item still accepted is one whose copies are refused.
MAX_ITEM_AGE_S: int = RING_DURABLE_TTL_S

#: The replay-cache namespace of a claimed item.
ITEM_REPLAY_PREFIX: str = "space-item:"

#: The ``msg_id`` prefix of an event built from an item (never on a wire).
ITEM_MSG_ID_PREFIX: str = "item:"


class _ItemSink(Protocol):
    async def dispatch_space_item(self, event: FederationEvent) -> bool: ...

    async def note_replay_id(self, msg_id: str) -> bool: ...

    def item_budget_allows(self, origin: str) -> bool: ...


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


#: ``(space, origin) -> bool``: whether ``origin`` acts as the space's host
#: here (``federation.host_equivalence.acts_as_host``).
HostCheck = Callable[["Space", str], Awaitable[bool]]


class SpaceItemDispatch:
    """Verify an event-shaped space item and apply it as its event."""

    __slots__ = ("_acts_as_host", "_federation", "_own_instance_id", "_writer_certs")

    def __init__(
        self,
        *,
        federation: _ItemSink,
        writer_certs: SpaceWriterCertService,
        own_instance_id: str,
        acts_as_host: HostCheck | None = None,
    ) -> None:
        self._federation = federation
        self._writer_certs = writer_certs
        self._own_instance_id = own_instance_id
        self._acts_as_host = acts_as_host

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
        if inner.get("epoch") != epoch:
            log.warning(
                "space_item: %s was signed for another epoch — dropped", item_id
            )
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
        stamp = item_stamp(ts)
        if stamp is None or stamp < datetime.now(UTC) - timedelta(
            seconds=MAX_ITEM_AGE_S
        ):
            log.warning(
                "space_item: %s has a bad, future or too old stamp — dropped", item_id
            )
            return False
        raw_cert = inner.get(WRITER_CERT_FIELD)
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
        # The origin's inbound budget, before the claim: an item refused
        # here keeps no trace and is taken when it comes again (a ring
        # re-fetch, another GFS).
        if not self._federation.item_budget_allows(origin):
            log.warning(
                "space_item: %s from %s dropped — over its inbound budget",
                item_id,
                origin,
            )
            return False
        # Claimed only now — after the signature and the cert — so nobody
        # but the origin can spend an id, and atomically against the copy
        # arriving on another socket (the check-and-insert has no await
        # before the in-memory mark).
        if await self._federation.note_replay_id(
            f"{ITEM_REPLAY_PREFIX}{origin}:{item_id}"
        ):
            log.debug("space_item: %s from %s seen before — dropped", item_id, origin)
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
        # Claimed whatever the gates say: their verdict is stable, so the
        # same item again gets the same one (a held item is replayed by the
        # seat buffer itself).
        return await self._federation.dispatch_space_item(event)

    async def apply_authority(self, space: Space, *, epoch: int, inner: dict) -> bool:
        """A host statement from the channel (``authority_kind:
        space_event``) whose SPACE AUTHORITY signature the caller verified
        (``open_channel_authority``, for this space and ``epoch``). Here: the
        statement's own type is a host statement of ``ITEM_SPECS``; the
        ORIGIN household's v2 signature over it verifies (so the household
        that spoke is proven — a delegated admin never passes as the owner
        for an owner-only rule, which the handler judges on
        ``from_instance``); the origin acts as the space's host here; the
        signed epoch, space and stamp hold; the id is claimed once. Then
        the same gates and the event's own handler, from that origin."""
        if inner.get(AUTHORITY_KIND_FIELD) != AUTHORITY_KIND_SPACE_EVENT:
            return False
        item_type = inner.get("item_type")
        spec = item_spec(item_type)
        if spec is None or not spec.authority:
            log.warning("space_item: %r is no host statement — dropped", item_type)
            return False
        origin = str(inner.get("origin_instance_id") or "")
        if origin and origin == self._own_instance_id:
            return False
        try:
            signed = verify_signed_event_item(inner)
        except UnsupportedItemAuthorSigSuite as exc:
            log.warning("space_item: unknown author_sig_suite %s — dropped", exc)
            return False
        if not signed:
            log.warning("space_item: host statement not signed by its origin")
            return False
        item_id = str(inner["item_id"])
        body = inner["body"]
        body_space = body.get("space_id")
        stamp = item_stamp(str(inner["ts"]))
        if (
            inner.get("epoch") != epoch
            or inner.get("space_id") != space.id
            or (body_space is not None and body_space != space.id)
            or stamp is None
            or stamp < datetime.now(UTC) - timedelta(seconds=MAX_ITEM_AGE_S)
        ):
            log.warning("space_item: host statement %s out of bounds", item_id)
            return False
        if self._acts_as_host is None or not await self._acts_as_host(space, origin):
            log.warning(
                "space_item: host statement %s from %s, which does not act "
                "as the host of space %s here — dropped",
                item_id,
                origin,
                space.id,
            )
            return False
        if not self._federation.item_budget_allows(origin):
            return False
        if await self._federation.note_replay_id(
            f"{ITEM_REPLAY_PREFIX}{origin}:{item_id}"
        ):
            return False
        event = FederationEvent(
            msg_id=ITEM_MSG_ID_PREFIX + item_id,
            event_type=FederationEventType(str(item_type)),
            from_instance=origin,
            to_instance=self._own_instance_id,
            timestamp=str(inner["ts"]),
            payload=body,
            space_id=space.id,
            epoch=epoch,
        )
        return await self._federation.dispatch_space_item(event)
