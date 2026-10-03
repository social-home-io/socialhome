"""Send a member household's page drafts to the space's host (v_48).

Under a v_48 host a local edit of a space page is an optimistic draft
(``pending_base_seq`` set) until the host sequences it
(:mod:`.page_conflict_service`). This forwarder sends each draft to the
host as a *proposal* — to the host alone, never broadcast — **stop and
wait**: at most one proposal per page is outstanding; the next goes out
when the host answers (:class:`PageProposalSettled`), so typing while the
host is slow never queues a stream of versions.

While the host is marked unreachable nothing is sent (no new outbox rows
pile up); drafts flush when it answers again (:class:`ConnectionReachable`),
at startup and on a 30-minute tick — the host recognises a resent
proposal by its hash and simply acknowledges it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ..domain.events import (
    ConnectionReachable,
    PageCreated,
    PageProposalSettled,
    PageUpdated,
)
from ..domain.federation import FederationEventType
from .page_conflict_service import PageMode, Proposal

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.page_repo import AbstractPageRepo
    from .page_conflict_service import PageConflictService

log = logging.getLogger(__name__)

#: How often every pending draft is offered to its host again.
FLUSH_INTERVAL_S = 30 * 60

#: ``DeliveryResult.error`` values of a send that was not queued for retry.
_NOT_QUEUED = frozenset({"not_confirmed", "no_route"})


class PageProposalForwarder:
    """Stop-and-wait delivery of page drafts to their host."""

    __slots__ = (
        "_pages",
        "_conflicts",
        "_federation",
        "_federation_repo",
        "_bus",
        "_outstanding",
        "_interval",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        page_repo: "AbstractPageRepo",
        conflicts: "PageConflictService",
        federation_service: "FederationService",
        federation_repo: "AbstractFederationRepo",
        bus: "EventBus",
        interval_seconds: float = FLUSH_INTERVAL_S,
    ) -> None:
        self._pages = page_repo
        self._conflicts = conflicts
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._bus = bus
        #: ``(space_id, page_id)`` → the proposal on the wire.
        self._outstanding: dict[tuple[str, str], Proposal] = {}
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def wire(self) -> None:
        self._bus.subscribe(PageUpdated, self._on_updated)
        self._bus.subscribe(PageCreated, self._on_created)
        self._bus.subscribe(PageProposalSettled, self._on_settled)
        self._bus.subscribe(ConnectionReachable, self._on_reachable)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Flush pending drafts, then tick. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.flush(resend=True)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("page proposals: flush failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                continue

    # ── Triggers ─────────────────────────────────────────────────────────

    async def _on_updated(self, event: PageUpdated) -> None:
        if event.proposal and event.space_id:
            await self.kick(event.space_id, event.page_id)

    async def _on_created(self, event: PageCreated) -> None:
        if event.proposal and event.space_id:
            await self.kick(event.space_id, event.page_id)

    async def _on_settled(self, event: PageProposalSettled) -> None:
        key = (event.space_id, event.page_id)
        sent = self._outstanding.get(key)
        if sent is not None and sent.hash == event.proposal_hash:
            del self._outstanding[key]
            if event.outcome == "applied" and event.seq:
                # We kept editing: the newer draft builds on what we sent.
                await self._conflicts.rebase_draft(
                    event.space_id, event.page_id, sent=sent, seq=event.seq
                )
        if event.outcome == "refused" and event.reason in ("rate_limited", "bad_base"):
            return  # retried on the next tick
        await self.kick(event.space_id, event.page_id)

    async def _on_reachable(self, event: ConnectionReachable) -> None:
        await self.flush(owner=event.instance_id, resend=True)

    # ── Sending ──────────────────────────────────────────────────────────

    async def flush(self, *, owner: str | None = None, resend: bool = False) -> int:
        """Offer every pending draft (of spaces hosted by ``owner``, or all)
        to its host. ``resend`` re-sends one already on the wire."""
        sent = 0
        for space_id, page_id in await self._pages.list_pending_drafts():
            if owner is not None:
                _mode, host = await self._conflicts.mode(space_id)
                if host != owner:
                    continue
            if resend:
                self._outstanding.pop((space_id, page_id), None)
            if await self.kick(space_id, page_id):
                sent += 1
        return sent

    async def kick(self, space_id: str, page_id: str) -> bool:
        """Send the page's draft unless one is already outstanding. ``True``
        when a proposal went out."""
        key = (space_id, page_id)
        proposal = await self._conflicts.proposal_for(space_id, page_id)
        if proposal is None:
            # Nothing pending (settled, refused or deleted): nothing waits.
            self._outstanding.pop(key, None)
            return False
        if key in self._outstanding:
            return False
        mode, host = await self._conflicts.mode(space_id)
        if mode is PageMode.HOST:
            # A draft is made only under another household's v_48 host, and
            # a space's host never changes (``owner_instance_id`` is fixed),
            # so this is a corrupt row — say so, never propose to ourselves.
            log.warning(
                "page %s in space %s: a pending draft on the space's host — "
                "not proposed",
                page_id,
                space_id,
            )
            return False
        if mode is not PageMode.MEMBER or not host:
            return False
        instance = await self._federation_repo.get_instance(host)
        if instance is not None and not instance.is_reachable():
            return False
        payload: dict[str, Any] = {
            "id": page_id,
            "page_id": page_id,
            "space_id": space_id,
            "title": proposal.title,
            "content": proposal.content,
            "cover_image_url": proposal.cover_image_url,
            "actor_user_id": proposal.actor_user_id,
            "base_seq": proposal.base_seq,
        }
        if proposal.base_hash is not None:
            payload["base_hash"] = proposal.base_hash
        if proposal.resolves:
            payload["resolves"] = list(proposal.resolves)
        creates = proposal.base_seq == 0
        if creates:
            payload["created_by"] = proposal.created_by
        self._outstanding[key] = proposal
        try:
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=host,
                event_type=(
                    FederationEventType.SPACE_PAGE_CREATED
                    if creates
                    else FederationEventType.SPACE_PAGE_UPDATED
                ),
                payload=payload,
                space_id=space_id,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("page proposal to %s failed: %s", host, exc)
            self._outstanding.pop(key, None)
            return False
        if not getattr(result, "ok", True):
            error = getattr(result, "error", None)
            log.debug(
                "page proposal for %s to %s not delivered: %s", page_id, host, error
            )
            if error in _NOT_QUEUED:
                # Neither delivered nor queued (no route): try again later.
                self._outstanding.pop(key, None)
                return False
            # A direct send that failed is in the outbox already: it stays
            # the outstanding proposal — no second row for this page.
        await self._conflicts.mark_sent(space_id, page_id, proposal.hash)
        return True

    def outstanding(self) -> dict[tuple[str, str], str]:
        """Hashes of the proposals on the wire (tests, diagnostics)."""
        return {k: p.hash for k, p in self._outstanding.items()}
