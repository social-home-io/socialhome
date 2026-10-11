"""Space writes held until the seat they need reaches the roster mirror.

The same bounded buffer also holds a direct message whose sender this
household has no user row for yet (keyed under the ``"dm"`` scope, which
never collides with a space id); it is handed back when that user syncs.

The §24.11 authorship rule (``federation/space_authorship.py``) and the
follower write gate both judge a space write against this household's
roster mirror, ``space_remote_members``. The mirror converges by
authority-signed gossip, and gossip can lose the race: a household that
has just joined sends its first post before the host's
``SPACE_MEMBER_JOINED`` reached us, or a household adds a member and posts
as them before we heard of the new seat. Refusing those writes answers the
sender ``status: ok``, so nothing ever resends them.

:class:`PendingSeatBuffer` holds such a write — only one that names a
user, or comes from a household, we hold **no row for at all** (a user
seated on another household, or a removed one, is a refusal, not a race) —
and hands it back when a seat for that user or that household lands. The
caller replays it through the same post-decrypt gates and handlers, so the
decision is taken again with the seat in place; a write that still does
not qualify is refused then, or held once more until it expires.

In memory and bounded on purpose: a household with no seat may fill it,
so it is capped in entries, entries per sending household, entries per
key and bytes, and every entry expires — the shared bookkeeping of
:class:`~socialhome.federation.bounded_hold.BoundedHold`. A restart loses it
— the same outcome as before it existed, not a new failure mode. Media bytes are never held (they are large and name
nobody).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import orjson

from ..domain.federation import FederationEvent, FederationEventType
from .bounded_hold import BoundedHold, HoldKey

log = logging.getLogger(__name__)

#: Defaults — generous for the join race, small against a flood.
DEFAULT_TTL_SECONDS = 900.0
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_PER_KEY = 32
DEFAULT_MAX_PER_SENDER = 64
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

_NEVER_HELD: frozenset[FederationEventType] = frozenset(
    {FederationEventType.SPACE_MEDIA_BLOB}
)


class PendingSeatBuffer:
    """Bounded, expiring hold for writes waiting on a roster seat."""

    __slots__ = ("_hold",)

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_per_key: int = DEFAULT_MAX_PER_KEY,
        max_per_sender: int = DEFAULT_MAX_PER_SENDER,
        max_bytes: int = DEFAULT_MAX_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._hold: BoundedHold[FederationEvent] = BoundedHold(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            max_per_key=max_per_key,
            max_per_sender=max_per_sender,
            max_bytes=max_bytes,
            clock=clock,
        )

    def __len__(self) -> int:
        return len(self._hold)

    def hold(
        self,
        event: FederationEvent,
        *,
        space_id: str,
        user_id: str = "",
        instance_id: str = "",
    ) -> bool:
        """Hold ``event`` until a seat for ``user_id`` (or, without one, for
        household ``instance_id``) lands in ``space_id``. ``False`` when it
        cannot be held — no key, a type never held, or a bound is hit."""
        if event.event_type in _NEVER_HELD or not space_id:
            return False
        key: HoldKey
        if user_id:
            key = (space_id, "user", user_id)
        elif instance_id:
            key = (space_id, "instance", instance_id)
        else:
            return False
        # The held event is alive while it waits, so its id is unique among
        # the entries: a re-hold of the same object takes no second slot.
        if self._hold.hold(
            key,
            event,
            token=id(event),
            sender=event.from_instance,
            size=len(orjson.dumps(event.payload or {})),
        ):
            return True
        log.warning(
            "%s from %s: seat-wait buffer is full — not holding the write",
            event.event_type,
            event.from_instance,
        )
        return False

    def release(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> list[FederationEvent]:
        """A seat ``(space_id, instance_id, user_id)`` just went live: hand
        back every write held for that user or that household, oldest first."""
        return self._hold.release(
            (space_id, "user", user_id),
            (space_id, "instance", instance_id),
        )
