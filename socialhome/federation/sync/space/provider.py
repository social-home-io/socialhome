"""Provider-side space sync (§25.6).

:class:`SpaceSyncService.stream_initial` walks :data:`RESOURCE_ORDER`,
paginates each resource via its exporter, encrypts + signs chunks,
and writes them to the DataChannel. Emits a final
``__complete__`` sentinel when done.

:class:`SpaceSyncService.stream_request_more` streams the slice asked
for by a peer's ``SPACE_SYNC_REQUEST_MORE`` event (after S-12 clamping).

Callers fire-and-forget via ``asyncio.create_task``; the session
record tracks the task so :class:`SyncSessionManager.close_session`
can cancel mid-stream if the peer gives up.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Any, TYPE_CHECKING

from ....domain.federation import (
    DELIVERY_ERROR_RELAY_THROTTLED,
    DELIVERY_ERROR_ROUTE_COOLDOWN,
    DeliveryResult,
    FederationEventType,
)
from .exporter import ChunkBuilder, RESOURCE_ORDER, serialise_chunk

if TYPE_CHECKING:
    from ...sync_manager import SyncSessionRecord
    from .exporter import ResourceExporter

log = logging.getLogger(__name__)


#: Consecutive failed chunk ships after which ``stream_initial`` gives up.
#: Each attempt on the mesh path costs a BFS plus a 3-hop signed round, so
#: continuing to push into a broken path is pure cost — the requester's
#: re-BEGIN is what recovers the stream (#648).
MAX_CONSECUTIVE_CHUNK_FAILURES: int = 3

#: Upper bound on a single wait for a route-discovery negative cooldown.
#: ``RouteDiscoveryService.ROUTE_NEGATIVE_COOLDOWN_S`` is 30 s today; the cap
#: is deliberately a local constant rather than an import of that value, so a
#: wrong/huge ``retry_after_s`` (a future cooldown change, a mocked sender)
#: can never park a provider task for an unbounded time.
MAX_ROUTE_COOLDOWN_WAIT_S: float = 35.0

#: How many cooldown windows ONE stream will wait out before a cooldown
#: failure starts counting against :data:`MAX_CONSECUTIVE_CHUNK_FAILURES`
#: like any other failure. Two waits ≈ a minute of patience for a route that
#: is genuinely coming back (one lost discovery window, one retry); a
#: household that is actually unreachable still terminates the stream.
MAX_ROUTE_COOLDOWN_WAITS: int = 2

#: Upper bound on a single wait for a connection-server relay 429.
#: :data:`~socialhome.federation.invite_bootstrap.RELAY_THROTTLE_COOLDOWN_S`
#: is what the sender actually reports; like the route-cooldown cap above
#: this is a deliberately local ceiling so a wrong ``retry_after_s`` cannot
#: park a provider task.
MAX_RELAY_THROTTLE_WAIT_S: float = 10.0

#: How many relay windows ONE stream will wait out. Much larger than
#: :data:`MAX_ROUTE_COOLDOWN_WAITS` because the two conditions are not
#: comparable: a route cooldown is an anomaly (discovery found nothing),
#: while relay back-pressure is the *expected* steady state of a big
#: catch-up — a household seated from an invite link has no other
#: transport, so a few hundred chunks meeting a per-minute window is
#: normal operation, not a broken path. ~20 waits ≈ 100 s of patience,
#: still bounded so a relay that never lets us through terminates.
MAX_RELAY_THROTTLE_WAITS: int = 20


def _wait_budget(error: str) -> tuple[int, float] | None:
    """``(max waits per stream, ceiling on one wait)`` for a waitable failure.

    Two delivery failures are *windows* rather than broken paths: mesh
    route-discovery's negative cooldown and the connection-server relay's
    per-minute limit. Both mean "we never got a fair attempt"; everything
    else means "we tried and it did not work" and counts a strike.

    Resolved per call rather than baked into a module-level table so the
    caps stay patchable in tests — and so the two budgets can never be
    conflated into one number, which is the bug that let a stream arrive
    at the relay window with its route-cooldown patience already spent.
    """
    if error == DELIVERY_ERROR_ROUTE_COOLDOWN:
        return MAX_ROUTE_COOLDOWN_WAITS, MAX_ROUTE_COOLDOWN_WAIT_S
    if error == DELIVERY_ERROR_RELAY_THROTTLED:
        return MAX_RELAY_THROTTLE_WAITS, MAX_RELAY_THROTTLE_WAIT_S
    return None


class SpaceSyncService:
    """Streams encrypted space content over a negotiated DataChannel."""

    __slots__ = (
        "_builder",
        "_exporters",
        "_sig_suite",
        "_media_sync",
        "_space_post_repo",
        "_gallery_repo",
        "_bazaar_repo",
        "_federation",
    )

    def __init__(
        self,
        *,
        builder: ChunkBuilder,
        exporters: dict[str, "ResourceExporter"],
        sig_suite: str = "ed25519",
        media_sync=None,
        space_post_repo=None,
        gallery_repo=None,
        bazaar_repo=None,
    ) -> None:
        self._builder = builder
        self._exporters = exporters
        self._sig_suite = sig_suite
        #: Optional — when wired, the provider enqueues bytes for
        #: every post / gallery / bazaar media URL after the metadata
        #: chunks stream, so a catch-up sync ALSO ships the historical
        #: images (not just the rows). Without it the receiver gets
        #: post + gallery metadata but renders broken thumbnails.
        self._media_sync = media_sync
        self._space_post_repo = space_post_repo
        self._gallery_repo = gallery_repo
        self._bazaar_repo = bazaar_repo
        #: Set by :meth:`attach_federation` after the federation
        #: service is constructed (resolves the chicken/egg between
        #: ``FederationService`` and ``SpaceSyncService``). Required
        #: for HTTPS-mode chunk delivery (``session.transport_mode ==
        #: "https"``); ``_send`` falls back to RTC when ``None``.
        self._federation = None

    def attach_federation(self, federation_service) -> None:
        """Wire the federation service so HTTPS-mode sessions can
        stream chunks via ``SPACE_SYNC_CHUNK`` events."""
        self._federation = federation_service

    async def stream_initial(self, session: "SyncSessionRecord") -> None:
        """Send every resource for ``session.space_id`` over the channel
        in :data:`RESOURCE_ORDER`, then a ``__complete__`` sentinel.

        Safe to call from ``asyncio.create_task`` — exceptions are
        logged, not re-raised. Callers rely on the session record's
        task slot to cancel this if the channel dies.
        """
        sync_id = session.sync_id
        space_id = session.space_id
        consecutive_failures = 0
        # Per-stream patience, one count per waitable reason — see
        # :meth:`_send_chunk`.
        waits: dict[str, int] = {}
        try:
            for resource in RESOURCE_ORDER:
                exporter = self._exporters.get(resource)
                if exporter is None:
                    log.debug("no exporter for resource %s — skipping", resource)
                    continue
                async for envelope in self._builder.build_chunks(
                    exporter=exporter,
                    space_id=space_id,
                    sync_id=sync_id,
                    sig_suite=self._sig_suite,
                ):
                    sent = await self._send_chunk(session, envelope, waits)
                    if sent:
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    if consecutive_failures >= MAX_CONSECUTIVE_CHUNK_FAILURES:
                        log.warning(
                            "sync %s: abandoning stream for space %s after "
                            "%d consecutive chunk failures — the requester "
                            "must re-BEGIN to get this metadata",
                            sync_id,
                            space_id,
                            consecutive_failures,
                        )
                        # Drop the session, or it holds one of the
                        # requester's three active-session slots until the
                        # 30-minute stale TTL — starving the re-BEGIN that
                        # is the documented recovery. Recovery is usually
                        # seconds away: a ROUTE_FOUND that missed the
                        # discovery window warms the cache right after this.
                        self._close_session(sync_id)
                        return
            sentinel = await self._builder.build_sentinel(
                space_id=space_id,
                sync_id=sync_id,
                sig_suite=self._sig_suite,
            )
            await self._send(session, sentinel)
            # Catch-up media: enumerate every post + gallery item in
            # the space, collect their media URLs, and enqueue
            # ``space_media_outbox`` rows so the requesting peer
            # receives the bytes via the same scheduler that real-time
            # uploads use. The receiver lands them on its media path
            # under the SAME filename the metadata referenced, so the
            # rendered ``<img src>`` resolves the moment the chunks
            # finish landing. The rows are bounded — same blob to
            # same peer dedups at the ON CONFLICT primary key.
            await self._enqueue_catchup_media(
                space_id,
                session.requester_instance_id,
            )
        except Exception:  # pragma: no cover
            log.exception(
                "stream_initial failed for sync_id=%s space=%s",
                sync_id,
                space_id,
            )
            # Same reasoning as the abandon path above — a session whose
            # stream died is garbage, and holding it blocks the retry.
            self._close_session(sync_id)

    async def _send_chunk(
        self,
        session,
        envelope: dict[str, Any],
        waits: dict[str, int],
    ) -> bool:
        """Ship one chunk, waiting out delivery windows that are not failures.

        ``waits`` counts how many windows of each kind this STREAM has
        already sat out — the caller owns it for the whole stream, so the
        patience budget is per-stream rather than per-chunk, and each
        reason keeps its own count (see :func:`_wait_budget`).

        Two ``ok=False`` results mean "we never got a fair attempt":

        * :data:`DELIVERY_ERROR_ROUTE_COOLDOWN` — ``RouteDiscoveryService``
          arms a 30 s negative cooldown after a flood that found nothing,
          and while it is live ``discover_route`` returns ``None``
          *immediately, without probing*. The chunk loop has no delay
          between chunks, so a single missed discovery window used to fail
          :data:`MAX_CONSECUTIVE_CHUNK_FAILURES` chunks within
          milliseconds and abandon the entire stream — converting a
          two-second race into a permanent loss, exactly when the cache
          was about to warm from a ROUTE_FOUND that missed the window.
        * :data:`DELIVERY_ERROR_RELAY_THROTTLED` — the connection server
          is over its per-minute window. A household seated from an
          invite link has NO other transport, and a first catch-up is
          hundreds of chunks, so meeting that window is ordinary
          operation; three of them in a row used to abandon the backfill
          and leave the household in a space it could not see.

        Both are waited out and the SAME chunk is retried, bounded so a
        genuinely unreachable household (or a relay that never lets us
        through) still terminates the stream. Every other failure counts
        a strike, unchanged.
        """
        while True:
            result = await self._send(session, envelope)
            if result.ok:
                return True
            reason = result.error or ""
            budget = _wait_budget(reason)
            if budget is None:
                # We probed (or the ship itself broke), so it counts.
                return False
            max_waits, max_wait_s = budget
            done = waits.get(reason, 0)
            if done >= max_waits:
                log.warning(
                    "sync %s: delivery to %s still blocked (%s) after %d "
                    "waits — counting it as a chunk failure",
                    session.sync_id,
                    session.requester_instance_id,
                    reason,
                    done,
                )
                return False
            waits[reason] = done + 1
            delay = min(max(result.retry_after_s or 0.0, 0.0), max_wait_s)
            log.info(
                "sync %s: delivery to %s is inside a %s window — waiting "
                "%.1fs and retrying the same chunk (wait %d/%d)",
                session.sync_id,
                session.requester_instance_id,
                reason,
                delay,
                done + 1,
                max_waits,
            )
            await asyncio.sleep(delay)

    def _close_session(self, sync_id: str) -> None:
        """Best-effort teardown of a session whose stream is over."""
        fed = self._federation
        if fed is None:
            return
        try:
            fed.close_sync_session(sync_id)
        except Exception:  # pragma: no cover — teardown must never raise
            log.exception("sync %s: closing the abandoned session failed", sync_id)

    async def _enqueue_catchup_media(
        self,
        space_id: str,
        target_instance_id: str,
    ) -> None:
        """Enqueue media bytes for every post + gallery item in ``space_id``.

        Repo reads catch :class:`sqlite3.Error` only — a renamed /
        missing repo method raises ``AttributeError`` (a logic bug),
        which propagates up to ``stream_initial``'s outer handler so
        it surfaces in logs as a single visible failure rather than
        being silently swallowed per-call. The original wide
        ``except Exception`` here masked exactly this kind of bug
        (see #443 — ``list_items_for_space`` never existed; tests
        mocked whatever was called so the gap never showed up). Same
        dedup semantics as the realtime enqueue: the ``(blob_id,
        target_instance_id)`` primary key drops duplicates.
        """
        if self._media_sync is None or target_instance_id == "":
            return
        # Posts
        if self._space_post_repo is not None:
            try:
                posts = await self._space_post_repo.list_for_sync(
                    space_id,
                    limit=1000,
                )
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list posts failed for space=%s",
                    space_id,
                )
                posts = []
            for post in posts:
                urls = self._post_media_urls(post)
                if not urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=post.id,
                        target_instance_ids=[target_instance_id],
                        media_urls=urls,
                    )
                except sqlite3.Error:
                    # One outbox-insert hitting a transient SQLite
                    # error (lock, disk full) shouldn't kill the whole
                    # loop — the next sync will re-enqueue.
                    log.exception(
                        "sync-catchup-media: enqueue failed for post=%s",
                        post.id,
                    )
        # Gallery items — enumerate every album in the space, then every
        # item per album. The repo API is per-album (no list_items_for_space
        # shortcut); we walk both levels so a space with multiple albums
        # catches up cleanly.
        if self._gallery_repo is not None:
            items: list = []
            try:
                albums = await self._gallery_repo.list_albums(
                    space_id,
                    limit=200,
                )
                for album in albums:
                    page = await self._gallery_repo.list_items(
                        album.id,
                        limit=500,
                    )
                    items.extend(page)
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list gallery items failed for space=%s",
                    space_id,
                )
                items = []
            for item in items:
                gallery_urls: list[str] = []
                if getattr(item, "thumbnail_url", None):
                    gallery_urls.append(item.thumbnail_url)
                if getattr(item, "url", None) and item.url != item.thumbnail_url:
                    gallery_urls.append(item.url)
                if not gallery_urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=item.id,
                        target_instance_ids=[target_instance_id],
                        media_urls=gallery_urls,
                    )
                except sqlite3.Error:
                    log.exception(
                        "sync-catchup-media: enqueue failed for gallery item=%s",
                        item.id,
                    )
        # Bazaar listings — each listing's photos live on
        # ``BazaarListing.image_urls`` (NOT on the wrapper Post). Without
        # this walk a remote member sees the wrapper ``PostType.BAZAAR``
        # post via ``SPACE_POST_CREATED`` catch-up but the listing
        # row stays empty and the photos render broken. Same dedup +
        # correlation_id semantics as posts; ``listing.post_id`` is
        # used as the correlation so the realtime + catch-up enqueues
        # collide cleanly at the outbox PK.
        if self._bazaar_repo is not None:
            try:
                listings = await self._bazaar_repo.list_in_space(
                    space_id,
                    limit=500,
                )
            except sqlite3.Error:
                log.exception(
                    "sync-catchup-media: list bazaar listings failed for space=%s",
                    space_id,
                )
                listings = []
            for listing in listings:
                if not listing.image_urls:
                    continue
                try:
                    await self._media_sync.enqueue_for_blob(
                        space_id=space_id,
                        correlation_id=listing.post_id,
                        target_instance_ids=[target_instance_id],
                        media_urls=list(listing.image_urls),
                    )
                except sqlite3.Error:
                    log.exception(
                        "sync-catchup-media: enqueue failed for bazaar listing=%s",
                        listing.post_id,
                    )

    @staticmethod
    def _post_media_urls(post) -> list[str]:
        urls: list[str] = []
        if getattr(post, "media_url", None):
            urls.append(post.media_url)
        urls.extend(getattr(post, "image_urls", None) or ())
        fm = getattr(post, "file_meta", None)
        if fm is not None and getattr(fm, "url", None):
            urls.append(fm.url)
        return urls

    async def stream_request_more(
        self,
        session: "SyncSessionRecord",
        cleaned: dict[str, Any],
    ) -> None:
        """Stream the specific resource slice the peer asked for.

        ``cleaned`` is the output of ``sync_manager.clamp_request_more``:
        already validated to be one of :data:`ALLOWED_RESOURCES` within
        sane bounds.
        """
        resource = str(cleaned.get("resource") or "")
        exporter = self._exporters.get(resource)
        if exporter is None:
            log.debug(
                "REQUEST_MORE for %s has no exporter — skipping",
                resource,
            )
            return
        try:
            async for envelope in self._builder.build_chunks(
                exporter=exporter,
                space_id=session.space_id,
                sync_id=session.sync_id,
                sig_suite=self._sig_suite,
            ):
                await self._send(session, envelope)
        except Exception:  # pragma: no cover
            log.exception(
                "stream_request_more failed for sync_id=%s resource=%s",
                session.sync_id,
                resource,
            )

    async def _send(self, session, envelope: dict[str, Any]) -> DeliveryResult:
        """Serialise and dispatch one envelope to the requester.

        Returns the :class:`DeliveryResult` of the ship (always ``ok=True``
        on the RTC path, which raises rather than failing softly). A failed
        result means the requester will be missing this chunk; callers count
        consecutive failures and abandon the stream rather than spending a
        full BFS-plus-3-hop round per remaining chunk on a path that is not
        working (#648). The result's ``error`` matters as well as its ``ok``:
        :data:`DELIVERY_ERROR_ROUTE_COOLDOWN` is a waitable window rather
        than a broken path — see :meth:`_send_chunk`.

        Picks the transport based on ``session.transport_mode``:

        * ``"rtc"`` — write the chunk to the open DataChannel.
        * ``"https"`` — wrap the serialised chunk in a signed
          ``SPACE_SYNC_CHUNK`` federation event and let the inbox
          path carry it. This is what Part C (Pascal's
          "fallback to https and federation/inbox for sync") wires up:
          when the WebRTC handshake never finished — carrier-grade
          NAT, missing STUN reachability — the requester re-issued
          the BEGIN with ``prefer_direct=False`` and the provider
          marked the session ``"https"`` accordingly. ``_send``
          honours that without the rest of the streaming logic
          having to know which path it took.
        """
        mode = getattr(session, "transport_mode", "rtc")
        if mode == "rtc":
            rtc_session = getattr(session, "rtc", None)
            if rtc_session is None:
                raise RuntimeError(
                    f"SyncSessionRecord {session.sync_id} has no rtc handle",
                )
            await rtc_session.send_chunk(serialise_chunk(envelope))
            return DeliveryResult(
                instance_id=session.requester_instance_id,
                ok=True,
            )
        if mode == "https":
            if self._federation is None:
                raise RuntimeError(
                    "HTTPS-mode SpaceSyncService requires attach_federation",
                )
            # A member that joined over a MESH route isn't a confirmed
            # direct peer, so a bare ``send_event`` can't reach it — the
            # chunks must ride ``SPACE_ROUTED``. ``send_with_mesh_fallback``
            # picks the direct path internally for a confirmed peer and
            # discovers a routed path otherwise.
            #
            # ``.decode()`` matters: the routed path seals the inner
            # payload for the target household and that sealing
            # JSON-encodes it, so raw ``bytes`` can't be shipped over the
            # mesh at all ("Object of type bytes is not JSON
            # serializable"). The direct path tolerates bytes, which is
            # why passing them through only ever broke the relayed
            # topology — silently, since the failure surfaces as a
            # warning inside the send. JSON is UTF-8 by definition so the
            # decode is lossless, and ``parse_chunk`` takes ``bytes | str``.
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=session.requester_instance_id,
                event_type=FederationEventType.SPACE_SYNC_CHUNK,
                payload={
                    "sync_id": session.sync_id,
                    "chunk": serialise_chunk(envelope).decode(),
                },
                space_id=session.space_id,
            )
            # Don't swallow a broken stream. Every chunk failing is what
            # left a mesh member with a space, a content key and media
            # bytes but no post rows, and nothing said so.
            ok = result is None or bool(getattr(result, "ok", True))
            # ``DeliveryResult`` carries ``error``; the original ``reason``
            # spelling made this diagnostic always print ``None`` — for
            # exactly the failure it exists to report.
            error = None if ok else getattr(result, "error", None)
            if not ok:
                log.warning(
                    "sync %s: chunk ship to %s failed (%s) — the requester "
                    "will be missing metadata",
                    session.sync_id,
                    session.requester_instance_id,
                    error,
                )
            return DeliveryResult(
                instance_id=session.requester_instance_id,
                ok=ok,
                error=error,
                retry_after_s=getattr(result, "retry_after_s", None),
            )
        raise ValueError(
            f"Unknown transport_mode {mode!r} on session {session.sync_id}",
        )
