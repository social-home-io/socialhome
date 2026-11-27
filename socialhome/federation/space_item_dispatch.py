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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from ..domain.gfs_channel import (
    RING_CLASS_EPHEMERAL,
    RING_DURABLE_TTL_S,
    RING_EPHEMERAL_TTL_S,
)
from ..domain.space_item import (
    ItemSpec,
    item_replay_key,
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
#: An ``ephemeral`` item (a live location pin) is never older than its
#: ring life plus clock skew: nobody (a GFS holding it back) can bring a pin
#: back long after the person moved on or stopped sharing.
MAX_EPHEMERAL_ITEM_AGE_S: int = RING_EPHEMERAL_TTL_S + 300


def max_item_age_s(spec: ItemSpec, body: dict) -> int:
    """How old ``body``'s item may be: an ephemeral one only minutes, but a
    location "off" (which rides the ring as ``durable``) as long as any."""
    if spec.ring_class == RING_CLASS_EPHEMERAL and body.get("mode") != "off":
        return MAX_EPHEMERAL_ITEM_AGE_S
    return MAX_ITEM_AGE_S


#: The ``msg_id`` prefix of an event built from an item (never on a wire).
ITEM_MSG_ID_PREFIX: str = "item:"


class _ItemSink(Protocol):
    async def dispatch_space_item(self, event: FederationEvent) -> bool: ...

    async def note_replay_id(self, msg_id: str) -> bool: ...


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


class SpaceItemDispatch:
    """Verify an event-shaped space item and apply it as its event."""

    __slots__ = ("_federation", "_own_instance_id", "_writer_certs")

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

    # ── Apply ────────────────────────────────────────────────────────────

    async def apply(
        self,
        space: Space,
        *,
        epoch: int,
        item_type: str,
        inner: dict,
        sender: str | None = None,
    ) -> bool:
        """Verify an event-shaped person item of ``space`` sealed at
        ``epoch`` and dispatch it. ``True`` when its handler ran; ``False``
        when it was refused (logged), a copy, our own echo, or held."""
        spec = item_spec(item_type)
        if spec is None:
            log.warning("space_item: %r is not a person item type — dropped", item_type)
            return False
        if inner.get("item_type") != item_type:
            log.warning("space_item: inner names another type — dropped")
            return False
        origin = str(inner.get("origin_instance_id") or "")
        if origin and origin == self._own_instance_id:
            return False  # our own echo off the channel
        if sender is not None and sender != origin:
            # A ``SPACE_ITEM`` comes from its origin only: nobody forwards
            # another household's item (a narrowed one would otherwise
            # reach the households its author left out).
            log.warning(
                "space_item: %s sent by %s, not its origin %s — dropped",
                item_type,
                sender,
                origin,
            )
            return False
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
            seconds=max_item_age_s(spec, body)
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
            # M1 — a SPACE_ITEM envelope comes from its origin through the
            # durable per-household outbox, which may hold it past a rekey
            # (an offline member): any epoch whose key we hold, the cert
            # still bound to it, the roster still deciding. A channel item
            # names no household: a fresh epoch only.
            require_fresh=sender is None,
        ):
            log.warning(
                "space_item: writer cert refused for %s (%s) from %s in space %s",
                item_id,
                item_type,
                origin,
                space.id,
            )
            return False
        # Claimed only now — after the signature and the cert — so nobody
        # but the origin can spend an id. No budget is charged here: a
        # channel item already spent the channel's inbound budget (refused
        # there, it is not taken and comes again on the next fetch), and a
        # SPACE_ITEM envelope spent its sender's — which is the origin.
        # The same key its legacy twin claims in the 14-day window (M10).
        replay_key = item_replay_key(origin, item_id)
        if await self._federation.note_replay_id(replay_key):
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
