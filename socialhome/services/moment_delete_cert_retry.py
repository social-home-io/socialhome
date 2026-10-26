"""Bounded retry of a public moment's delete certificate post (v_66).

A public moment's delete goes to every GFS that proved ``moment_delete_log``
as its signed certificate (``POST /gfs/moments/delete_cert``) — what a
follower household offline when the delete went out is handed when it
reconnects. Posted once, a certificate whose post failed (the GFS
restarting, a network blip, a 5xx, the per-household limiter, or an old node
of a cluster mid-upgrade answering 404 / 422 though the server advertised
the log) was lost: offline followers kept the moment for up to 30 days
(adversarial review F3). Now a failed post is retried with backoff for up to
:data:`DELETE_CERT_RETRY_WINDOW_S` after the delete; the GFS takes a
certificate up to 25 h old (``DELETE_CERT_MAX_AGE_S``) and a replay of one
it already keeps changes nothing.

**What is queued** is the signed post body — the certificate (author, moment
id, origin, delete time and the origin signature; no content) inside the
household's wire signature, the very bytes the first attempt sent. Nothing
is re-signed, so a retry can't carry anything the first post didn't.

**Why in memory, not the outbox or a table.** The outbox
(``federation_outbox``) is keyed by a paired household and its rows are
re-sealed per peer on redelivery — a GFS is no paired household, so it
would need a new row kind (a migration) and a redelivery branch that never
seals. The moment's own tombstone can't stand in either: it keeps no
``is_public`` (wiped to 0), and re-posting a certificate for a moment no GFS
carried would name a private moment to a GFS. A new table is a one-way
ratchet for a window of one day. What a restart within that day loses is
the certificate of a delete whose first post failed AND whose GFS stayed
unreachable until the restart — the live sealed delete, the 24 h GFS queue
and the mesh's 30-day outbox rows still carry it; the same trade
:mod:`.gfs_publish_retry` makes. Bounded by
:data:`DELETE_CERT_RETRY_MAX_PENDING` (and per GFS).

Lifecycle follows the scheduler pattern (``_stop: asyncio.Event``, see
``infrastructure/replay_cache_scheduler.py``); a second event wakes the loop
when work is queued.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .gfs_publish_retry import PublishOutcome, parse_retry_after_s

log = logging.getLogger(__name__)

#: Wait before retry *n + 1* of one certificate; past the last step it stays
#: at the last (hourly) until :data:`DELETE_CERT_RETRY_WINDOW_S` runs out —
#: at most ~27 tries per certificate per GFS. A longer ``Retry-After`` wins.
DELETE_CERT_RETRY_BACKOFF_S: tuple[float, ...] = (60.0, 300.0, 900.0, 3600.0)

#: How long after the delete a certificate is still retried. The GFS takes
#: one up to 25 h old (``routes.moments_public.DELETE_CERT_MAX_AGE_S``),
#: so the last try still lands with an hour of clock slack.
DELETE_CERT_RETRY_WINDOW_S: float = 24 * 3600.0

#: Ceiling on queued certificates across every GFS (~1 KB each). Past it a
#: failed post is lost as before (logged once until the queue drains).
DELETE_CERT_RETRY_MAX_PENDING: int = 1000

#: …and for one GFS, so a single dead server can't crowd out the rest.
DELETE_CERT_RETRY_MAX_PENDING_PER_GFS: int = 500


def classify_cert_status(status: int, retry_after: str | None) -> PublishOutcome:
    """Map a ``POST /gfs/moments/delete_cert`` status to an outcome.

    2xx delivered. 408 / 429 / 5xx transient (429 with its ``Retry-After``),
    and so are 404 and 422: the household posts only to a server that
    advertised the log, so a 404 (no such route) or 422 is an older node of
    a cluster mid-upgrade — another node, or the same one upgraded, takes
    it. Anything else (409 too old, 403 not ours, a 3xx never followed) is
    the same answer on every try."""
    if 200 <= status < 300:
        return PublishOutcome.delivered()
    if status in (404, 408, 422, 429) or status >= 500:
        return PublishOutcome.transient(parse_retry_after_s(retry_after))
    return PublishOutcome.permanent()


@dataclass(slots=True)
class _Pending:
    """One certificate post waiting for a retry. Process-local."""

    body: dict
    #: Wall-clock second past which it is no longer tried.
    give_up_at: float
    #: ``monotonic`` second it is next tried.
    due_at: float
    tries: int = 0


#: ``send(gfs_id, body)`` → outcome. Supplied by the owner
#: (:class:`~.moment_public_outbound.MomentPublicOutbound`), which re-checks
#: the connection before every try.
CertSender = Callable[[str, dict], Awaitable[PublishOutcome]]


class DeleteCertRetryQueue:
    """Failed delete certificate posts, retried with backoff for a day."""

    __slots__ = (
        "_full_logged",
        "_monotonic",
        "_pending",
        "_send",
        "_stop",
        "_task",
        "_wake",
        "_wall",
        "_what",
    )

    def __init__(
        self,
        send: CertSender,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        what: str = "delete certificate",
    ) -> None:
        self._send = send
        #: What is retried, for the logs — the same queue retries a sealed
        #: reaction's GFS post (v_68, :mod:`.moment_reaction_gfs`).
        self._what = what
        self._monotonic = monotonic
        self._wall = wall
        #: (gfs_id, moment_id) → pending post. A re-queue of the same
        #: certificate replaces it.
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._full_logged = False

    def pending_count(self) -> int:
        return len(self._pending)

    def enqueue(
        self,
        gfs_id: str,
        moment_id: str,
        body: dict,
        *,
        deleted_at: float,
        retry_after_s: float | None = None,
    ) -> bool:
        """Queue the post *body* of moment *moment_id*'s certificate to
        *gfs_id*, deleted at *deleted_at* (Unix seconds). ``False`` when it
        is refused — stopped, past its window, or the queue is full."""
        if self._stop.is_set():
            return False
        give_up_at = deleted_at + DELETE_CERT_RETRY_WINDOW_S
        if self._wall() >= give_up_at:
            return False
        key = (gfs_id, moment_id)
        if key not in self._pending:
            on_gfs = sum(1 for g, _m in self._pending if g == gfs_id)
            if (
                len(self._pending) >= DELETE_CERT_RETRY_MAX_PENDING
                or on_gfs >= DELETE_CERT_RETRY_MAX_PENDING_PER_GFS
            ):
                if not self._full_logged:
                    self._full_logged = True
                    log.warning(
                        "%s retry queue is full — a failed post to %s is lost",
                        self._what,
                        gfs_id,
                    )
                return False
        self._pending[key] = _Pending(
            body=body,
            give_up_at=give_up_at,
            due_at=self._due(0, retry_after_s),
        )
        self._wake.set()
        return True

    def discard(self, gfs_id: str, key: str) -> None:
        """Forget the pending post *key* to *gfs_id* (a newer one went out)."""
        self._pending.pop((gfs_id, key), None)

    async def start(self) -> None:
        """Start the retry loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._loop(), name=f"{self._what.replace(' ', '-')}-retry"
        )

    async def stop(self) -> None:
        """Stop the loop, wait for it, and drop what is still pending (in
        memory by design — see the module docstring)."""
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None
        if self._pending:
            log.warning(
                "%s retry: dropping %d pending post(s) on shutdown",
                self._what,
                len(self._pending),
            )
        self._pending.clear()
        self._full_logged = False

    def _due(self, tries: int, retry_after_s: float | None) -> float:
        step = DELETE_CERT_RETRY_BACKOFF_S[
            min(tries, len(DELETE_CERT_RETRY_BACKOFF_S) - 1)
        ]
        return self._monotonic() + max(step, retry_after_s or 0.0)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                await self.run_due()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("%s retry pass failed: %s", self._what, exc, exc_info=True)
            if self._stop.is_set():
                return
            timeout = None
            if self._pending:
                next_due = min(p.due_at for p in self._pending.values())
                timeout = max(0.0, next_due - self._monotonic())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                continue

    async def run_due(self) -> None:
        """Try every pending post that is due; drop those past their window
        or refused for good."""
        for key in list(self._pending):
            if self._stop.is_set():
                return
            item = self._pending.get(key)
            if item is None or item.due_at > self._monotonic():
                continue
            if self._wall() >= item.give_up_at:
                del self._pending[key]
                log.warning(
                    "%s retry: giving up on %s at %s after %d tries",
                    self._what,
                    key[1],
                    key[0],
                    item.tries,
                )
                continue
            try:
                outcome = await self._send(key[0], item.body)
            except Exception:
                log.warning(
                    "%s retry to %s raised; treating it as transient",
                    self._what,
                    key[0],
                    exc_info=True,
                )
                outcome = PublishOutcome.transient()
            if self._pending.get(key) is not item:
                continue  # replaced meanwhile — the new one keeps its slot
            if outcome.kind != "transient":
                del self._pending[key]
                if outcome.kind != "delivered":
                    log.warning(
                        "%s retry: %s refused %s for good",
                        self._what,
                        key[0],
                        key[1],
                    )
                continue
            item.tries += 1
            item.due_at = self._due(item.tries, outcome.retry_after_s)
        if not self._pending:
            self._full_logged = False
