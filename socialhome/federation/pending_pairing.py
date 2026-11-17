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
confirmed pairing replaces the seat in one transaction
(:meth:`PendingPairings.promote` →
``AbstractFederationRepo.upgrade_seat_instance``); a cancel or an expiry
just forgets the half, and the seat is still there.

**A seat is replaced only on proof that the other household confirmed.**
Replacing it cuts the seat's keys, so a household that replaced its seat
while the other still holds one has lost the space between the two. Each
side therefore waits for the message that proves the other side is in:

* trust relay — the requester on the approver's signed ack; the approver
  on the requester's first envelope under the approved keys;
* pairing code — the scanner on the code owner's signed peer-confirm; the
  code owner, whose admin entered the code, on the scanner's first
  envelope under the code's keys (:meth:`PendingPairings.await_peer`).

A half that waits for that first envelope is an **approval**
(:func:`~socialhome.domain.federation.pending_kind`), and it is the only
kind of staged half the inbound pipeline ever opens
(:meth:`keyed_for_inbox` / :meth:`keyed_for`, gated by
``make_check_staged_half``). A pairing code nobody verified opens nothing.

In memory, and bounded: a pairing code's half for the code's lifetime, a
request for the introduction's; an approval until the envelope it waits
for or an explicit cancel (as the row of an approval is); and
:data:`STAGED_MAX` halves in all. A restart forgets them, exactly as it
forgets a trust-relay request — the people start again; no migration, and
no key of an unconfirmed handshake at rest.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ..domain.federation import (
    PENDING_KIND_APPROVAL,
    PENDING_KIND_CODE,
    PairingStatus,
    RemoteInstance,
    is_space_seat,
    pending_kind,
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
    #: The pairing code's token, when the half came from one: what the
    #: peer's ``PAIRING_ABORT`` names, so it cancels THIS handshake only.
    token: str | None


class PendingPairings:
    """Reads and writes the pending half of a pairing handshake: in the
    ``remote_instances`` row, or beside a space seat (module docstring)."""

    __slots__ = ("_clock", "_locks", "_repo", "_staged")

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repo = federation_repo
        self._clock = clock
        self._staged: dict[str, _Staged] = {}
        #: One writer per household: ``[lock, users]``. The row path
        #: settles "confirm or cancel, whichever commits first" in one
        #: transaction; a half kept here spans a memory entry AND a
        #: database write, and an approval or a request has no session
        #: token to consume — so :meth:`save`, :meth:`promote`,
        #: :meth:`await_peer` and :meth:`cancel` are serialised per
        #: household, and the loser finds the half gone. An entry lives
        #: exactly as long as somebody holds or waits for it.
        self._locks: dict[str, list] = {}

    @asynccontextmanager
    async def _locked(self, instance_id: str) -> AsyncIterator[None]:
        slot = self._locks.setdefault(instance_id, [asyncio.Lock(), 0])
        slot[1] += 1
        try:
            async with slot[0]:
                yield
        finally:
            slot[1] -= 1
            if slot[1] == 0 and self._locks.get(instance_id) is slot:
                del self._locks[instance_id]

    def _live(self, instance_id: str) -> _Staged | None:
        entry = self._staged.get(instance_id)
        if entry is not None and entry.expires_at <= self._clock():
            self._staged.pop(instance_id, None)
            return None
        return entry

    def _prune(self) -> None:
        now = self._clock()
        for iid in [i for i, e in self._staged.items() if e.expires_at <= now]:
            self._staged.pop(iid, None)

    def _expiry(self, ttl_s: float | None) -> float:
        return math.inf if ttl_s is None else self._clock() + ttl_s

    async def save(
        self,
        pending: RemoteInstance,
        *,
        ttl_s: float | None,
        gfs_route: str | None = None,
        token: str | None = None,
    ) -> bool:
        """Store the pending half *pending*. Returns ``True`` when it was
        kept beside a seat — then nothing was written, and the caller must
        not touch the row either (its opt-ins and routes are the seat's;
        the half carries the handshake's own, applied on
        :meth:`promote`). ``False``: written to the row, as ever.

        Whether the row is a seat is decided inside the write itself
        (``save_pending_instance``): a seat made a moment ago is never
        overwritten by a pending half.

        *ttl_s* is how long the handshake can still complete (``None``:
        until it confirms or is cancelled); *gfs_route* the relay route to
        seed when it does; *token* the pairing code's.
        """
        if pending.status not in _PENDING:
            raise ValueError("only a pending pairing is saved here")
        async with self._locked(pending.id):
            if await self._repo.save_pending_instance(pending):
                self._staged.pop(pending.id, None)
                return False
            self._prune()
            if pending.id not in self._staged and len(self._staged) >= STAGED_MAX:
                raise TooManyPendingPairingsError(
                    "too many pairings are waiting — cancel one or try again later"
                )
            self._staged[pending.id] = _Staged(
                instance=pending,
                expires_at=self._expiry(ttl_s),
                gfs_route=gfs_route,
                token=token,
            )
            return True

    def staged(self, instance_id: str) -> RemoteInstance | None:
        """The pending half kept beside *instance_id*'s seat, if any."""
        entry = self._live(instance_id)
        return entry.instance if entry is not None else None

    def staged_route(self, instance_id: str) -> str | None:
        """The relay route the staged half of *instance_id* will seed (the
        GFS connection its pairing code named), if any — what a pairing
        body for that handshake travels through meanwhile."""
        entry = self._live(instance_id)
        return entry.gfs_route if entry is not None else None

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

    def keyed_for(self, instance_id: str) -> RemoteInstance | None:
        """The staged **approval** of *instance_id* — the one kind of half
        an inbound envelope may be read under (module docstring) — for the
        pipelines that know the sender by its id (relay, mailbox,
        DataChannel)."""
        entry = self._live(instance_id)
        if entry is None or pending_kind(entry.instance) != PENDING_KIND_APPROVAL:
            return None
        return entry.instance

    def keyed_for_inbox(self, inbox_id: str) -> RemoteInstance | None:
        """The staged **approval** whose inbox id is *inbox_id* — what the
        HTTPS inbox resolves an envelope on that inbox to. Never a pairing
        code nobody verified, never our own keyless request."""
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

    def has_code_half(self, instance_id: str) -> bool:
        """Whether a pairing code's half (verified by our side or not) is
        kept beside *instance_id*'s seat — the one case in which that
        household's seat may send us a ``PAIRING_ABORT``."""
        entry = self._live(instance_id)
        return entry is not None and entry.token is not None

    async def await_peer(
        self,
        instance_id: str,
        *,
        inbox_id: str,
        token: str,
        ttl_s: float | None,
    ) -> RemoteInstance | None:
        """Our admin verified the pairing code *token*: the staged half of
        *instance_id* now waits for the scanner's first envelope under the
        code's keys (it becomes an approval), and the seat stays until
        then. Returns the half, or ``None`` when the code half of that
        handshake — the one whose inbox the code's session minted,
        *inbox_id* — is not staged (cancelled, expired, replaced)."""
        async with self._locked(instance_id):
            entry = self._live(instance_id)
            if (
                entry is None
                or entry.instance.local_inbox_id != inbox_id
                or entry.token not in (None, token)
                or pending_kind(entry.instance) != PENDING_KIND_CODE
            ):
                return None
            waiting = replace(entry.instance, status=PairingStatus.PENDING_SENT)
            self._staged[instance_id] = replace(
                entry,
                instance=waiting,
                expires_at=self._expiry(ttl_s),
                # A half that was a row until a seat took the row carries
                # no token yet: it is this code's from here on.
                token=token,
            )
            return waiting

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
        async with self._locked(confirmed.id):
            return await self._promote_locked(confirmed, token)

    async def _promote_locked(
        self, confirmed: RemoteInstance, token: str | None
    ) -> bool:
        entry = self._live(confirmed.id)
        if entry is None:
            return await self._repo.promote_pending_instance(confirmed, token=token)
        if confirmed.local_inbox_id != entry.instance.local_inbox_id:
            return False
        # The half goes only once the database has answered: an error in
        # the write leaves it in place for the retry (the peer's next
        # envelope, the admin's next click). ``pop``, not ``del``: the
        # half may have expired while the write ran — the write decides.
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

    async def cancel(
        self,
        instance_id: str,
        *,
        token: str | None = None,
        inbox_id: str | None = None,
    ) -> tuple[RemoteInstance, list[str]] | None:
        """Cancel the pending half kept beside *instance_id*'s seat: forget
        it and drop the ``pending_pairings`` sessions behind it. Returns
        the half and the consumed tokens, or ``None`` when nothing is
        staged (the caller then cancels the row). The seat stays.

        With *token* (the peer's ``PAIRING_ABORT`` names one) only the
        half of THAT pairing code is cancelled, with *inbox_id* only the
        half an envelope was read under — never another handshake with
        the same household."""
        async with self._locked(instance_id):
            entry = self._live(instance_id)
            if (
                entry is None
                or (token is not None and entry.token != token)
                or (inbox_id is not None and entry.instance.local_inbox_id != inbox_id)
            ):
                # Nothing staged, another handshake's — or a confirm took
                # the half first.
                return None
            inst = entry.instance
            # Sessions first: if that write fails the half is still here
            # and the cancel can be tried again.
            tokens = await self._repo.cancel_pairing_sessions(
                local_inbox_id=inst.local_inbox_id,
                peer_identity_pk=inst.remote_identity_pk,
            )
            self._staged.pop(instance_id, None)
            if entry.token is not None and entry.token not in tokens:
                # The code was already entered here (its session is used
                # up): the handshake is still named by its token.
                tokens = [entry.token, *tokens]
            return inst, tokens


__all__ = [
    "STAGED_MAX",
    "PendingPairings",
    "TooManyPendingPairingsError",
]
