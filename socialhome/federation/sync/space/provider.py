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

import logging
from typing import Any, TYPE_CHECKING

from .exporter import ChunkBuilder, RESOURCE_ORDER, serialise_chunk

if TYPE_CHECKING:
    from ...sync_manager import SyncSessionRecord
    from .exporter import ResourceExporter

log = logging.getLogger(__name__)


class SpaceSyncService:
    """Streams encrypted space content over a negotiated DataChannel."""

    __slots__ = ("_builder", "_exporters", "_sig_suite")

    def __init__(
        self,
        *,
        builder: ChunkBuilder,
        exporters: dict[str, "ResourceExporter"],
        sig_suite: str = "ed25519",
    ) -> None:
        self._builder = builder
        self._exporters = exporters
        self._sig_suite = sig_suite

    async def stream_initial(self, session: "SyncSessionRecord") -> None:
        """Send every resource for ``session.space_id`` over the channel
        in :data:`RESOURCE_ORDER`, then a ``__complete__`` sentinel.

        Safe to call from ``asyncio.create_task`` — exceptions are
        logged, not re-raised. Callers rely on the session record's
        task slot to cancel this if the channel dies.
        """
        sync_id = session.sync_id
        space_id = session.space_id
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
                    await _send(session, envelope)
            sentinel = await self._builder.build_sentinel(
                space_id=space_id,
                sync_id=sync_id,
                sig_suite=self._sig_suite,
            )
            await _send(session, sentinel)
        except Exception:  # pragma: no cover
            log.exception(
                "stream_initial failed for sync_id=%s space=%s",
                sync_id,
                space_id,
            )

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
                await _send(session, envelope)
        except Exception:  # pragma: no cover
            log.exception(
                "stream_request_more failed for sync_id=%s resource=%s",
                session.sync_id,
                resource,
            )


async def _send(session, envelope: dict[str, Any]) -> None:
    """Serialise and dispatch one envelope over the DataChannel."""
    rtc = getattr(session, "rtc", None)
    if rtc is None:
        raise RuntimeError(
            f"SyncSessionRecord {session.sync_id} has no rtc handle",
        )
    await rtc.send_chunk(serialise_chunk(envelope))
