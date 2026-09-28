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
key and bytes, and every entry expires. The per-household cap is checked
before the shared one, so no single household can take every slot. A restart loses it — the same outcome as before it existed, not a
new failure mode. Media bytes are never held (they are large and name
nobody).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import orjson

from ..domain.federation import FederationEvent, FederationEventType

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


@dataclass(slots=True, frozen=True)
class _Held:
    """Buffer-local record (never leaves this module)."""

    event: FederationEvent
    size: int
    expires_at: float


class PendingSeatBuffer:
    """Bounded, expiring hold for writes waiting on a roster seat."""

    __slots__ = (
        "_by_key",
        "_ttl",
        "_max_entries",
        "_max_per_key",
        "_max_per_sender",
        "_max_bytes",
        "_bytes",
        "_clock",
    )

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
        self._by_key: dict[tuple[str, str, str], list[_Held]] = {}
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._max_per_key = max_per_key
        self._max_per_sender = max_per_sender
        self._max_bytes = max_bytes
        self._bytes = 0
        self._clock = clock

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_key.values())

    def _held_from(self, sender: str) -> int:
        return sum(
            1
            for bucket in self._by_key.values()
            for h in bucket
            if h.event.from_instance == sender
        )

    def _purge(self) -> None:
        now = self._clock()
        for key in list(self._by_key):
            live = [h for h in self._by_key[key] if h.expires_at > now]
            self._bytes -= sum(h.size for h in self._by_key[key] if h.expires_at <= now)
            if live:
                self._by_key[key] = live
            else:
                del self._by_key[key]

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
        if user_id:
            key = (space_id, "user", user_id)
        elif instance_id:
            key = (space_id, "instance", instance_id)
        else:
            return False
        self._purge()
        bucket = self._by_key.get(key, [])
        if any(h.event is event for h in bucket):
            return True
        size = len(orjson.dumps(event.payload or {}))
        if (
            self._held_from(event.from_instance) >= self._max_per_sender
            or len(self) >= self._max_entries
            or len(bucket) >= self._max_per_key
            or self._bytes + size > self._max_bytes
        ):
            log.warning(
                "%s from %s: seat-wait buffer is full — not holding the write",
                event.event_type,
                event.from_instance,
            )
            return False
        bucket.append(_Held(event, size, self._clock() + self._ttl))
        self._by_key[key] = bucket
        self._bytes += size
        return True

    def release(
        self,
        *,
        space_id: str,
        instance_id: str,
        user_id: str,
    ) -> list[FederationEvent]:
        """A seat ``(space_id, instance_id, user_id)`` just went live: hand
        back every write held for that user or that household, oldest first."""
        self._purge()
        out: list[_Held] = []
        for key in (
            (space_id, "user", user_id),
            (space_id, "instance", instance_id),
        ):
            out.extend(self._by_key.pop(key, []))
        self._bytes -= sum(h.size for h in out)
        out.sort(key=lambda h: h.expires_at)
        return [h.event for h in out]
