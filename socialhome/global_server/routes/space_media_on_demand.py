"""Space media on demand routes (v_83, :mod:`..space_media_on_demand`).

* ``POST /gfs/media/{blob_id}/wanted`` — a reader missed the blob. Anonymous
  (the reader's cookie-less session); body :class:`~socialhome.domain
  .gfs_space_media_on_demand.WantedRequest` — the scope it knows and a
  sealed locator — never in the URL. Every answer is ``no-store`` (neither a
  CDN nor a browser keeps it): ``200`` the blob (from the cache, or piped
  from a holder: ``Content-Length`` is the blob's, and the connection is cut
  short if the bytes do not end at their address), ``404`` a clean miss (no
  holder, nobody answered within the wait, a repeat right after such a
  miss, a scope not listed here), ``422`` a malformed body, ``429`` the
  reader's prefix asked too often, ``503`` busy.
* ``PUT /gfs/media/serve/{ticket}`` — the holder household the GFS asked
  sends the blob; signed by its household key in ``X-SH-Instance`` /
  ``X-SH-Timestamp`` / ``X-SH-Signature`` over
  :func:`~socialhome.domain.gfs_space_media_on_demand.serve_signing_body`.
  ``Content-Length`` must be a padded bucket. With the upload authorization
  header (``X-SH-Space-Media-Auth``, the same as ``PUT /gfs/media/{blob}``)
  and a :data:`MODE_CACHE` ticket the blob is also kept in the cache.
  ``200 {"stored": bool}``; ``404`` unknown ticket (or another node's — the
  ticket names its node, :mod:`..node_route`); ``403`` not the household
  asked; ``409`` already being served; ``422`` wrong size or address;
  ``429`` the scope's stream budget.
* ``POST /gfs/media/serve/{ticket}/decline`` — the holder can't serve it
  (no such file, its budget): the GFS asks the next holder at once.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from ...domain.gfs_space_media import (
    SPACE_MEDIA_AUTH_HEADER,
    InvalidSpaceMediaAuth,
    SpaceMediaUploadAuth,
)
from ...domain.gfs_space_media_on_demand import (
    MAX_WANTED_BODY_BYTES,
    MODE_CACHE,
    SERVE_INSTANCE_HEADER,
    SERVE_OP_DECLINE,
    SERVE_OP_SERVE,
    SERVE_SIG_HEADER,
    SERVE_TS_HEADER,
    InvalidOnDemandWire,
    WantedRequest,
    serve_signing_body,
)
from ...moment_public_media import MAX_BLOB_BYTES
from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..blob_store import MediaRefused, check_size
from ..space_media_on_demand import (
    BUSY,
    DECLINE_BAD_HINT,
    DECLINE_NOT_HELD,
    LIMITED,
    Cached,
    MediaSession,
    StreamFailed,
)
from .base import GfsBaseView
from .space_media import MIN_PROGRESS_BYTES, PROGRESS_WINDOW_S

log = logging.getLogger(__name__)

SPACE_MEDIA_WANTED_ROUTE: str = "/gfs/media/{blob_id:[0-9a-f]{64}}/wanted"
SPACE_MEDIA_SERVE_ROUTE: str = "/gfs/media/serve/{ticket}"
SPACE_MEDIA_DECLINE_ROUTE: str = "/gfs/media/serve/{ticket}/decline"

#: How far a holder's ``ts`` may be from this server's clock.
SERVE_MAX_SKEW_S: int = 300

#: The longest a holder may take to send a whole blob (its progress is
#: checked every window too).
SERVE_BODY_DEADLINE_S: float = 120.0

_NO_STORE = {
    "Cache-Control": "no-store",
    "Access-Control-Allow-Origin": "*",
}
_BLOB_HEADERS = {
    **_NO_STORE,
    "Content-Type": "application/octet-stream",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


def _status(status: int, error: str | None = None) -> web.Response:
    if error is None:
        return web.Response(status=status, headers=_NO_STORE)
    return web.json_response({"error": error}, status=status, headers=_NO_STORE)


class GfsMediaWantedView(GfsBaseView):
    """``POST /gfs/media/{blob_id}/wanted`` — a reader asks for a blob it
    missed (module docstring)."""

    async def post(self) -> web.StreamResponse:
        svc = self.svc(K.gfs_space_media_on_demand_key)
        store = self.svc(K.gfs_space_media_store_key)
        if not svc.enabled:
            return _status(404)
        body = await self.bounded_json(MAX_WANTED_BODY_BYTES)
        try:
            req = WantedRequest.from_wire(body)
        except InvalidOnDemandWire as exc:
            log.debug("space media wanted refused: %s", exc)
            return _status(422, "bad_request")
        client_key = store.client_key(self.client_bucket())
        prefix_key = store.prefix_key(self.client_ip())
        if not svc.wait_gate.enter(client_key, prefix_key, 0):
            resp = _status(503, "busy")
            resp.headers["Retry-After"] = "5"
            return resp
        try:
            outcome = await svc.want(self.match("blob_id"), req, prefix_key=prefix_key)
            if isinstance(outcome, Cached):
                return web.Response(body=outcome.data, headers=_BLOB_HEADERS)
            if isinstance(outcome, MediaSession):
                return await self._pipe(svc, outcome)
            if outcome == LIMITED:
                resp = _status(429, "rate_limited")
                resp.headers["Retry-After"] = "600"
                return resp
            if outcome == BUSY:
                resp = _status(503, "busy")
                resp.headers["Retry-After"] = "5"
                return resp
            return _status(404)
        finally:
            svc.wait_gate.leave(client_key, prefix_key, 0)

    async def _pipe(self, svc, session: MediaSession) -> web.StreamResponse:
        """Wait (bounded) for the holder to start, then pipe the blob."""
        session.readers += 1
        svc.pin(session)
        try:
            try:
                await asyncio.wait_for(
                    session.started.wait(), timeout=max(svc.remaining(session), 0.001)
                )
            except TimeoutError:
                return _status(404)
            if session.failed and not session.buf:
                return _status(404)
            resp = web.StreamResponse(status=200, headers=_BLOB_HEADERS)
            resp.content_length = session.size
            await resp.prepare(self.request)
            try:
                # A reader that stops reading can't hold the blob's memory.
                async with asyncio.timeout(svc.reader_deadline(session)):
                    async for chunk in session.chunks():
                        await resp.write(chunk)
            except StreamFailed, TimeoutError:
                # Never a whole blob that is not at its address (nor a stalled
                # reader kept): cut the connection short of Content-Length.
                if self.request.transport is not None:
                    self.request.transport.close()
                return resp
            await resp.write_eof()
            return resp
        finally:
            session.readers -= 1
            svc.unpin(session)


async def _holder(view: GfsBaseView, ticket: str, op: str) -> str | web.Response:
    """The household that signed this serve / decline, or the refusal."""
    instance_id = view.request.headers.get(SERVE_INSTANCE_HEADER, "")
    signature = view.request.headers.get(SERVE_SIG_HEADER, "")
    ts_raw = view.request.headers.get(SERVE_TS_HEADER, "")
    if not instance_id or not signature or not ts_raw:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    try:
        ts = int(ts_raw)
    except ValueError:
        return web.json_response({"error": "invalid_timestamp"}, status=422)
    if abs(int(time.time()) - ts) > SERVE_MAX_SKEW_S:
        return web.json_response({"error": "stale_timestamp"}, status=401)
    sender = await view.svc(K.gfs_fed_repo_key).get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    if not verify_report_signature(
        serve_signing_body(instance_id, ticket, ts, op), signature, sender.public_key
    ):
        return web.json_response({"error": "invalid_signature"}, status=401)
    return instance_id


def _asked(svc, ticket: str, instance_id: str) -> MediaSession | web.Response:
    session = svc.session(ticket)
    if session is None:
        return web.json_response({"error": "unknown_ticket"}, status=404)
    if not session.target or session.target != instance_id:
        return web.json_response({"error": "forbidden"}, status=403)
    return session


class GfsMediaServeView(GfsBaseView):
    """``PUT /gfs/media/serve/{ticket}`` — the asked holder sends the blob."""

    async def put(self) -> web.Response:
        ticket = self.match("ticket")
        who = await _holder(self, ticket, SERVE_OP_SERVE)
        if isinstance(who, web.Response):
            return who
        svc = self.svc(K.gfs_space_media_on_demand_key)
        session = _asked(svc, ticket, who)
        if isinstance(session, web.Response):
            return session
        if session.claimed or session.done.is_set():
            return web.json_response({"error": "already_streaming"}, status=409)
        size = self.request.content_length
        try:
            if size is None or size > MAX_BLOB_BYTES:
                raise MediaRefused(422, "bad_size")
            check_size(size)
        except MediaRefused:
            return web.json_response({"error": "bad_size"}, status=422)
        # Claimed before any await: a second PUT (a retry, the previous
        # target) can never feed the same buffer.
        session.claimed = True
        if not await svc.charge_stream(session, size):
            session.claimed = False
            svc.decline(session, DECLINE_NOT_HELD)
            return web.json_response({"error": "scope_stream_quota"}, status=429)
        session.size = size
        session.started.set()
        try:
            async with asyncio.timeout(SERVE_BODY_DEADLINE_S):
                await _feed_with_progress(self.request, session)
        except TimeoutError:
            session.finish(ok=False)
            return web.json_response({"error": "body_timeout"}, status=408)
        except BaseException:
            session.finish(ok=False)
            raise
        ok = await svc.verify(session)
        session.finish(ok=ok)
        if not ok:
            log.warning("space media on demand: a holder sent another blob")
            return web.json_response({"error": "blob_id_mismatch"}, status=422)
        stored = False
        if session.mode == MODE_CACHE:
            stored = await self._store(session)
        svc.note_served(session, stored=stored)
        return web.json_response({"stored": stored})

    async def _store(self, session: MediaSession) -> bool:
        """Keep the blob in the cache, under the holder's normal upload
        authorization (absent or refused: streamed only)."""
        raw = self.request.headers.get(SPACE_MEDIA_AUTH_HEADER, "")
        if not raw:
            return False
        try:
            auth = SpaceMediaUploadAuth.from_header(raw)
            household = await self.svc(K.gfs_space_media_auth_key).authorize_upload(
                auth,
                scope_kind=session.scope_kind,
                scope_id=session.scope_id,
                blob_id=session.blob_id,
            )
        except (InvalidSpaceMediaAuth, PermissionError) as exc:
            log.debug("space media on demand: not stored: %s", exc)
            return False
        store = self.svc(K.gfs_space_media_store_key)
        owner = (
            ("household", household)
            if session.scope_kind == "space"
            else ("client", store.prefix_key(self.client_ip()))
        )
        try:
            return await store.put(
                scope_kind=session.scope_kind,
                scope_id=session.scope_id,
                blob_id=session.blob_id,
                data=bytes(session.buf),
                expires_at=auth.claim.expires_at,
                client=self.client_ip(),
                owner=owner,
            )
        except MediaRefused as exc:
            log.info("space media on demand: not stored: %s", exc.reason)
            return False


class GfsMediaDeclineView(GfsBaseView):
    """``POST /gfs/media/serve/{ticket}/decline`` — the asked holder can't."""

    async def post(self) -> web.Response:
        ticket = self.match("ticket")
        who = await _holder(self, ticket, SERVE_OP_DECLINE)
        if isinstance(who, web.Response):
            return who
        svc = self.svc(K.gfs_space_media_on_demand_key)
        session = _asked(svc, ticket, who)
        if isinstance(session, web.Response):
            return session
        body = await self.bounded_json(256) if self.request.can_read_body else {}
        reason = body.get("reason")
        svc.decline(
            session,
            DECLINE_BAD_HINT if reason == DECLINE_BAD_HINT else DECLINE_NOT_HELD,
        )
        return web.json_response({"declined": True})


async def _feed_with_progress(request: web.Request, session: MediaSession) -> None:
    """Feed the holder's body into *session*, up to its size, requiring
    :data:`MIN_PROGRESS_BYTES` (or the rest) every :data:`PROGRESS_WINDOW_S`
    (:class:`TimeoutError` otherwise; 413 past the size)."""
    loop = asyncio.get_running_loop()
    window_end = loop.time() + PROGRESS_WINDOW_S
    window_start_len = 0
    while True:
        got = len(session.buf)
        need = min(MIN_PROGRESS_BYTES, session.size - window_start_len)
        try:
            chunk = await asyncio.wait_for(
                request.content.readany(), max(window_end - loop.time(), 0.001)
            )
        except TimeoutError:
            if got - window_start_len < need:
                raise
            window_end = loop.time() + PROGRESS_WINDOW_S
            window_start_len = got
            continue
        if not chunk:
            return
        if got + len(chunk) > session.size:
            raise web.HTTPRequestEntityTooLarge(
                max_size=session.size, actual_size=got + len(chunk)
            )
        session.feed(chunk)
        if loop.time() >= window_end:
            if len(session.buf) - window_start_len < need:
                raise TimeoutError
            window_end = loop.time() + PROGRESS_WINDOW_S
            window_start_len = len(session.buf)
