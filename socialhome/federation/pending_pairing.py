"""Where a pending pairing lives — the one rule for QR and trust relay.

A pairing handshake (§11: a pairing code, or an introduction through a
household both sides know) holds a *pending* half on each side until the
people have confirmed: the keys derived for it, the peer's inbox URL, our
inbox id for it. That half normally IS the ``remote_instances`` row
(``pending_sent`` / ``pending_received``).

It cannot be, when the row for that household is a **space seat**
(:func:`~socialhome.domain.federation.is_space_seat`): two households that
share a space hold a seat for each other, and the space's events are sealed
under the seat's keys. A seat is NOT a pairing — sharing a space never
pairs anybody — but it must keep working while the people decide:

* writing the pending half over the row (what a pairing code did) stops
  the space between the two households until the handshake confirms, and
  loses the seat for good when it is cancelled;
* refusing the handshake because the row is confirmed (what the trust relay
  did) makes pairing impossible for households that share a space.

So the rule, for every handshake: **over a seat, the pending half is kept
beside it** — here, in memory — **and the seat row is not touched.** The
confirmed pairing replaces the seat in one transaction when the handshake
completes (:meth:`PendingPairings.promote` →
``AbstractFederationRepo.upgrade_seat_instance``); a cancel or an expiry
just forgets the half, and the seat is still there.

In memory, bounded twice: each half for the lifetime of its handshake
(``ttl_s`` — the pairing code's or the introduction's), and
:data:`STAGED_MAX` halves in all. A restart forgets them, exactly as it
forgets a trust-relay request — the people start again; no migration, and
no key of an unconfirmed handshake at rest.

What the inbound pipeline reads of a staged half
(:meth:`PendingPairings.keyed_for_inbox`, gated by
``make_check_staged_half``):

* an **approval** (our admin approved a household's introduction) takes
  the requester's first envelope under the new keys, which confirms it;
* a **pairing code**'s half, which nobody verified yet, takes exactly one
  thing: the peer's ``PAIRING_ABORT`` — so a cancel on the other side is
  heard here too, and our half goes with it. Nothing else.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.federation import (
    PairingStatus,
    RemoteInstance,
    is_space_seat,
)
from ..repositories.federation_repo import AbstractFederationRepo

log = logging.getLogger(__name__)

#: Most pending halves kept beside seats at once. A household runs a
#: handful of handshakes at a time; the cap only stops unbounded growth.
STAGED_MAX: int = 64

_PENDING: frozenset[PairingStatus] = frozenset(
    {PairingStatus.PENDING_SENT, PairingStatus.PENDING_RECEIVED}
)


class TooManyPendingPairingsError(ValueError):
    """:data:`STAGED_MAX` pairings with households we share a space with
    are already waiting. Cancel one, or wait for one to expire."""


@dataclass(slots=True, frozen=True)
class _Staged:
    instance: RemoteInstance
    expires_at: float
    #: Our GFS connection to seed as a relay route once the pair confirms
    #: (a pairing code with a GFS reach); a seat row holds no routes.
    gfs_route: str | None


class PendingPairings:
    """Reads and writes the pending half of a pairing handshake: in the
    ``remote_instances`` row, or beside a space seat (module docstring)."""

    __slots__ = ("_clock", "_repo", "_staged")

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = federation_repo
        self._clock = clock
        self._staged: dict[str, _Staged] = {}

    def _live(self, instance_id: str) -> _Staged | None:
        entry = self._staged.get(instance_id)
        if entry is not None and entry.expires_at <= self._clock():
            del self._staged[instance_id]
            return None
        return entry

    def _prune(self) -> None:
        now = self._clock()
        for iid in [i for i, e in self._staged.items() if e.expires_at <= now]:
            del self._staged[iid]

    async def save(
        self,
        pending: RemoteInstance,
        *,
        ttl_s: float,
        gfs_route: str | None = None,
    ) -> bool:
        """Store the pending half *pending*. Returns ``True`` when it was
        kept beside a seat — then nothing was written, and the caller must
        not touch the row either (its opt-ins and routes are the seat's;
        the half carries the handshake's own, applied on
        :meth:`promote`). ``False``: written to the row, as ever.

        *ttl_s* is how long the handshake can still complete; *gfs_route*
        the relay route to seed when it does.
        """
        if pending.status not in _PENDING:
            raise ValueError("only a pending pairing is saved here")
        if not is_space_seat(await self._repo.get_instance(pending.id)):
            self._staged.pop(pending.id, None)
            await self._repo.save_instance(pending)
            return False
        self._prune()
        if pending.id not in self._staged and len(self._staged) >= STAGED_MAX:
            raise TooManyPendingPairingsError(
                "too many pairings are waiting — cancel one or try again later"
            )
        self._staged[pending.id] = _Staged(
            instance=pending,
            expires_at=self._clock() + ttl_s,
            gfs_route=gfs_route,
        )
        return True

    def staged(self, instance_id: str) -> RemoteInstance | None:
        """The pending half kept beside *instance_id*'s seat, if any."""
        entry = self._live(instance_id)
        return entry.instance if entry is not None else None

    def list_staged(self) -> list[RemoteInstance]:
        """Every pending half kept beside a seat (the connections list
        shows them as pending)."""
        self._prune()
        return [entry.instance for entry in self._staged.values()]

    async def get(self, instance_id: str) -> RemoteInstance | None:
        """A handshake's view of *instance_id*: the pending half kept
        beside its seat, else its row — but never a seat, which is no
        pairing and nothing a handshake may read keys from or take for
        "already paired"."""
        entry = self._live(instance_id)
        if entry is not None:
            return entry.instance
        row = await self._repo.get_instance(instance_id)
        return None if is_space_seat(row) else row

    def keyed_for_inbox(self, inbox_id: str) -> RemoteInstance | None:
        """The staged half whose inbox id is *inbox_id*, when it holds the
        peer's identity key and session keys — what the inbound pipeline
        resolves an envelope on that inbox to. Never our own keyless
        request. What such an envelope may then BE is decided by the
        pipeline's staged-half gate, by the half's kind."""
        if not inbox_id:
            return None
        self._prune()
        for entry in self._staged.values():
            inst = entry.instance
            if (
                inst.local_inbox_id == inbox_id
                and inst.remote_identity_pk
                and inst.key_remote_to_self
            ):
                return inst
        return None

    async def promote(
        self, confirmed: RemoteInstance, *, token: str | None = None
    ) -> bool:
        """Confirm the pairing: write *confirmed* over its pending half —
        the row (``promote_pending_instance``), or the seat the half was
        kept beside (``upgrade_seat_instance``) — consuming the
        ``pending_pairings`` session *token* in the same transaction.
        Returns whether it happened; a refusal changes nothing.

        A pairing without the peer's identity key or session keys is never
        confirmed, and a staged half confirms only as itself: *confirmed*
        must carry its inbox id (the handshake it belongs to). Callers
        check the half's KIND before they build *confirmed* — a pairing
        code confirms a ``code`` half, an ack a ``request``, the first
        envelope an ``approval``.
        """
        if not (
            confirmed.remote_identity_pk
            and confirmed.key_self_to_remote
            and confirmed.key_remote_to_self
        ):
            return False
        entry = self._live(confirmed.id)
        if entry is None:
            return await self._repo.promote_pending_instance(confirmed, token=token)
        if confirmed.local_inbox_id != entry.instance.local_inbox_id:
            return False
        # The half goes only once the database has answered: an error in
        # the write leaves it in place for the retry (the peer's next
        # envelope, the admin's next click).
        ok = await self._repo.upgrade_seat_instance(confirmed, token=token)
        self._staged.pop(confirmed.id, None)
        if not ok:
            return False
        if entry.gfs_route is not None:
            try:
                await self._repo.upsert_gfs_route(
                    confirmed.id,
                    entry.gfs_route,
                    now=datetime.now(UTC).isoformat(),
                )
            except Exception:
                # The pair IS confirmed; route discovery finds the shared
                # GFS again. Never fail the confirmation over it.
                log.warning(
                    "pairing: could not seed the relay route for %s",
                    confirmed.id,
                    exc_info=True,
                )
        return True

    async def cancel(self, instance_id: str) -> tuple[RemoteInstance, list[str]] | None:
        """Cancel the pending half kept beside *instance_id*'s seat: forget
        it and drop the ``pending_pairings`` sessions behind it. Returns
        the half and the consumed tokens, or ``None`` when nothing is
        staged (the caller then cancels the row). The seat stays."""
        entry = self._live(instance_id)
        if entry is None:
            return None
        inst = entry.instance
        # Sessions first: if that write fails the half is still here and
        # the cancel can be tried again.
        tokens = await self._repo.cancel_pairing_sessions(
            local_inbox_id=inst.local_inbox_id,
            peer_identity_pk=inst.remote_identity_pk,
        )
        self._staged.pop(instance_id, None)
        return inst, tokens


__all__ = [
    "STAGED_MAX",
    "PendingPairings",
    "TooManyPendingPairingsError",
]
