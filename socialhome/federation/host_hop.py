"""The space host as the one ``SPACE_ROUTED`` hop toward a carded member (v_89).

A member household that holds a seat for another member of a space but
cannot reach it directly — it has no connection to the GFS the other
reaches the space through, or the other has no mailbox — sends through the
space's host instead: a three-hop route ``[us, host, them]`` of that space
(owner decision 2026-11-08: "we need the relay only members").

"The host" is the one notion of
:mod:`socialhome.federation.host_equivalence` (owner decision 2026-11-13):
the owner household, or — for a household a delegated admin invited — that
admin while the trust holds. Each side checks it on its own rows: the hop
relays only where it acts as host itself, and an end takes (or sends) the
hop only through a household acting as host for IT.

* **No discovery.** The route is fixed and the inner payload is sealed to
  the target's static key-wrap key from its verified member card — the
  same key its seat was made from — so there is no probe flood, and a
  relay seat stays out of route discovery entirely.
* **The host relays, never reads.** The inner payload is sealed end to end
  (:mod:`socialhome.federation.routed_crypto`); the host sees the route and
  the routing ``space_id``.
* **One shape only.** :meth:`HostHop.allows` is what ``SpaceRoutedHandler``
  asks before it relays or unwraps a ``SPACE_ROUTED`` that a
  ``space_session`` seat sent us, or that it would forward to one: exactly
  three hops, a household acting as the routing space's host in the middle,
  both ends holding a card for that space on the side that checks. Anything
  else from that peer class is dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ..crypto import b64url_encode
from ..domain.federation import (
    DELIVERY_ERROR_QUEUED,
    SPACE_SESSION_ALLOWED_EVENT_TYPES,
    DeliveryResult,
    FederationEventType,
)
from ..domain.member_card import MemberCard

if TYPE_CHECKING:
    from .routed_envelope import SpaceRoutedHandler

log = logging.getLogger(__name__)

#: ``DeliveryResult.error`` when no host hop can carry a send.
HOST_HOP_UNAVAILABLE: str = "host_hop_unavailable"

CardLookup = Callable[[str, str], Awaitable[MemberCard | None]]
#: ``(space_id, target)`` → the households a hop toward *target* may go
#: through, best first: only ones *target* takes as its host (never us).
HostsLookup = Callable[[str, str], Awaitable[list[str]]]
#: ``space_id`` → the households acting as its host for US.
ActingLookup = Callable[[str], Awaitable[list[str]]]
#: ``space_id`` → whether WE act as its host toward other members.
ServesLookup = Callable[[str], Awaitable[bool]]
#: Asks a space's host for its member directory again (``space_id``).
AskHost = Callable[[str], Awaitable[None]]


class HostHop:
    """See the module docstring."""

    __slots__ = (
        "_acting",
        "_ask_host",
        "_card_of",
        "_hosts_of",
        "_keywrap_private",
        "_keywrap_public_b64",
        "_own_instance_id",
        "_routed",
        "_serves",
    )

    def __init__(
        self,
        *,
        own_instance_id: str,
        card_of: CardLookup,
        hosts_of: HostsLookup,
        acting_hosts: ActingLookup,
        serves: ServesLookup,
        keywrap_private_key: bytes,
        keywrap_public_key: bytes,
        ask_host: AskHost | None = None,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._card_of = card_of
        self._hosts_of = hosts_of
        self._acting = acting_hosts
        self._serves = serves
        self._ask_host = ask_host
        self._keywrap_private = keywrap_private_key
        self._keywrap_public_b64 = b64url_encode(keywrap_public_key)
        self._routed: SpaceRoutedHandler | None = None

    def attach_routed(self, routed: SpaceRoutedHandler) -> None:
        self._routed = routed

    def static_target_priv(self, target_pub_b64: str) -> str | None:
        """Our key-wrap private key (base64url) when a routed envelope was
        sealed to our key-wrap public key — the host hop's target key —
        else ``None``."""
        if target_pub_b64 and target_pub_b64 == self._keywrap_public_b64:
            return b64url_encode(self._keywrap_private)
        return None

    async def send(
        self,
        *,
        to_instance_id: str,
        event_type: FederationEventType,
        payload: dict,
        space_id: str,
    ) -> DeliveryResult:
        """Ship one event of *space_id* to *to_instance_id* through a
        household acting as the space's host for BOTH ends that holds both
        ends' cards — ``hosts_of(space, target)``, tried in order: the owner
        (every member takes it), then our inviting admin when it seated the
        target too. ``ok`` only when a hop took the envelope; a hop that is
        unreachable now leaves it in our outbox and the next hop is tried
        (the target takes a second copy as the same event); when none took
        it the result is the queued error, and with no hop at all
        :data:`HOST_HOP_UNAVAILABLE` — never ``ok`` for a delivery that
        cannot happen. Never raises."""
        failed = DeliveryResult(
            instance_id=to_instance_id, ok=False, error=HOST_HOP_UNAVAILABLE
        )
        if self._routed is None or not space_id:
            return failed
        card = None
        queued = False
        for host in await self._hosts_of(space_id, to_instance_id):
            if host in (self._own_instance_id, to_instance_id):
                continue
            card = card or await self._card_of(space_id, to_instance_id)
            if card is None:
                return failed
            leg: list[DeliveryResult] = []
            try:
                await self._routed.send_routed(
                    path=[self._own_instance_id, host, to_instance_id],
                    target_eph_pk_b64=b64url_encode(bytes.fromhex(card.keywrap_pk)),
                    inner_event_type=event_type,
                    inner_payload=payload,
                    target_identity_pk=card.identity_pk,
                    space_id=space_id,
                    first_leg=leg,
                )
            except Exception:
                log.warning("host hop: routed send failed", exc_info=True)
                continue
            if not leg or leg[0].ok:
                return DeliveryResult(
                    instance_id=to_instance_id, ok=True, via="host_hop"
                )
            # That hop did not take it now (it is in our outbox for it at
            # best): the next one that holds both ends' cards is tried.
            queued = queued or leg[0].error == DELIVERY_ERROR_QUEUED
            log.info("host hop: %s did not take it now (%s)", host, leg[0].error)
        if queued:
            return DeliveryResult(
                instance_id=to_instance_id, ok=False, error=DELIVERY_ERROR_QUEUED
            )
        return failed

    async def allows(
        self,
        *,
        space_id: str | None,
        path: list[str],
        inner_type: FederationEventType,
    ) -> bool:
        """Whether a ``SPACE_ROUTED`` with *path*, routing *space_id* and
        inner *inner_type* is the host-hop shape we take from (or forward
        to) a ``space_session`` seat: three hops, a household acting as
        the space's host in the middle, an inner event a seat may send
        directly too (the §D2b vocabulary — the hop never widens it), and —
        on the hop — both ends' cards held for that space; on an end, the
        other end's. The hop relays only where WE act as host (the owner,
        or a delegated admin); an end takes it only through a household
        acting as host for it (never through any other member)."""
        if not space_id or len(path) != 3 or len(set(path)) != 3:
            return False
        if (
            inner_type not in SPACE_SESSION_ALLOWED_EVENT_TYPES
            or inner_type is FederationEventType.SPACE_ROUTED
        ):
            log.info("host hop: an inner %r is not seat vocabulary", inner_type.value)
            return False
        me = self._own_instance_id
        hop = path[1]
        ends: tuple[str, ...]
        if me == hop:
            if not await self._serves(space_id):
                return False
            ends = (path[0], path[2])
        elif hop not in await self._acting(space_id):
            return False
        elif me == path[2]:
            ends = (path[0],)
        elif me == path[0]:
            ends = (path[2],)
        else:
            return False
        for end in ends:
            if await self._card_of(space_id, end) is None:
                log.info("host hop: no card for an end of the route — dropping")
                if me == path[2] and self._ask_host is not None:
                    # The host holds both ends' cards, so it has the one we
                    # lack: our directory is incomplete (a first snapshot we
                    # never got). Ask for it now, not at the next sync.
                    await self._ask_host(space_id)
                return False
        return True


__all__ = ["HOST_HOP_UNAVAILABLE", "HostHop"]
