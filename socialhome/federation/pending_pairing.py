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

Only one kind of staged half is ever read by the inbound pipeline:
an **approval** (our admin approved a household's introduction), which the
requester's first envelope under the new keys confirms
(:meth:`PendingPairings.approval_for_inbox`). A pairing code nobody
verified yet opens nothing.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..domain.federation import (
    PENDING_KIND_APPROVAL,
    PairingStatus,
    RemoteInstance,
    is_space_seat,
    pending_kind,
)
from ..repositories.federation_repo import AbstractFederationRepo

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

    def approval_for_inbox(self, inbox_id: str) -> RemoteInstance | None:
        """The staged **approval** whose inbox id is *inbox_id* — what the
        inbound pipeline resolves an envelope on that inbox to, so the
        requester's first envelope under the approved keys can confirm
        the pair. Never a pairing code's half (nobody verified it yet) and
        never our own keyless request."""
        if not inbox_id:
            return None
        self._prune()
        for entry in self._staged.values():
            inst = entry.instance
            if (
                inst.local_inbox_id == inbox_id
                and pending_kind(inst) == PENDING_KIND_APPROVAL
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

        A staged half confirms only as itself: *confirmed* must carry its
        inbox id (the handshake it belongs to).
        """
        entry = self._live(confirmed.id)
        if entry is None:
            return await self._repo.promote_pending_instance(confirmed, token=token)
        if confirmed.local_inbox_id != entry.instance.local_inbox_id:
            return False
        del self._staged[confirmed.id]
        if not await self._repo.upgrade_seat_instance(confirmed, token=token):
            return False
        if entry.gfs_route is not None:
            await self._repo.upsert_gfs_route(
                confirmed.id,
                entry.gfs_route,
                now=datetime.now(UTC).isoformat(),
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
        del self._staged[instance_id]
        inst = entry.instance
        tokens = await self._repo.cancel_pairing_sessions(
            local_inbox_id=inst.local_inbox_id,
            peer_identity_pk=inst.remote_identity_pk,
        )
        return inst, tokens

    def discard(self, instance_id: str) -> bool:
        """Forget the pending half kept beside *instance_id*'s seat (the
        peer aborted, or the pair confirmed another way). Returns whether
        there was one."""
        return self._staged.pop(instance_id, None) is not None


__all__ = [
    "STAGED_MAX",
    "PendingPairings",
    "TooManyPendingPairingsError",
]
