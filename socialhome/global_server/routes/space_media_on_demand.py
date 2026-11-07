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
  (``{"reason": "not_held" | "bad_hint" | "busy"}``): the GFS asks the next
  holder at once (``busy``: the space's members, for a listed space).
* ``POST /gfs/media/member-pull`` — the member fallback: a writer-key holder
  of a listed space asks for a want (body :class:`~socialhome.domain
  .gfs_space_media_on_demand.MemberPullRequest`). ``200 {"wants": [want]}``
  (at most one, claimed for this puller), ``204`` nothing within the hold,
  ``403`` one uniform refusal, ``404`` the fallback is off, ``422`` a
  malformed body, ``429`` the address pulled too often, ``503`` no slot
  (``Retry-After``). A member then serves / declines the want's ticket on
  the two routes above WITHOUT ``X-SH-Instance``: signed by the writer group
  key in ``X-SH-Writer-Epoch`` / ``X-SH-Timestamp`` / ``X-SH-Writer-Sig`` /
  ``X-SH-Writer-Sig-Suite`` + the want's claim secret in
  ``X-SH-Member-Claim`` over :func:`~socialhome.domain
  .gfs_space_media_on_demand.member_serve_signing_bytes`; refused with one
  uniform ``403`` unless the claim secret is the want's live claim's. Its re-upload is
  kept only under an anonymous upload authorization (space authority or
  writer key), never an identified writer cert.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiohttp import web

from ...domain.gfs_space_media import (
    AUTH_AUTHORITY,
    AUTH_WRITER_KEY,
    SPACE_MEDIA_AUTH_HEADER,
    InvalidSpaceMediaAuth,
    SpaceMediaUploadAuth,
)
from ...domain.gfs_space_media_on_demand import (
    DECLINE_BAD_HINT,
    DECLINE_BUSY,
    DECLINE_NOT_HELD,
    MAX_MEMBER_PULL_BODY_BYTES,
    MAX_WANTED_BODY_BYTES,
    MEMBER_CLAIM_HEADER,
    MEMBER_PULL_ROUTE,
    MEMBER_WRITER_EPOCH_HEADER,
    MEMBER_WRITER_SIG_HEADER,
    MEMBER_WRITER_SIG_SUITE_HEADER,
    MODE_CACHE,
    SERVE_INSTANCE_HEADER,
    SERVE_OP_DECLINE,
    SERVE_OP_SERVE,
    SERVE_SIG_HEADER,
    SERVE_TS_HEADER,
    InvalidOnDemandWire,
    MemberPullRequest,
    MemberWant,
    WantedRequest,
    serve_signing_body,
)
from ...moment_public_media import MAX_BLOB_BYTES
from .. import app_keys as K
from ..admin_service import verify_report_signature
from ..blob_store import MediaRefused, check_size
from ..space_media_on_demand import (
    BUSY,
    LIMITED,
    PULL_FORBIDDEN,
    PULL_FULL,
    SERVE_BODY_DEADLINE_S,
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
SPACE_MEDIA_MEMBER_PULL_ROUTE: str = MEMBER_PULL_ROUTE

#: The re-upload authorizations a member's serve may be stored under: the
#: anonymous ones only (a writer cert names the household's key).
_MEMBER_STORE_KINDS: frozenset[str] = frozenset({AUTH_AUTHORITY, AUTH_WRITER_KEY})

#: How far a holder's ``ts`` may be from this server's clock.
SERVE_MAX_SKEW_S: int = 300

#: A cached blob is written to a reader in pieces this size.
_CACHED_CHUNK_BYTES: int = 64 * 1024

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
                return await self._send_cached(svc, outcome.data)
            if isinstance(outcome, MediaSession):
                return await self._pipe(svc, outcome, prefix_key)
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

    async def _send_cached(self, svc, data: bytes) -> web.StreamResponse:
        """A cached blob, written under a deadline (a reader that stops
        reading can't hold it)."""
        resp = web.StreamResponse(status=200, headers=_BLOB_HEADERS)
        resp.content_length = len(data)
        await resp.prepare(self.request)
        view = memoryview(data)
        try:
            async with asyncio.timeout(svc.cached_deadline(len(data))):
                for start in range(0, len(data), _CACHED_CHUNK_BYTES):
                    await resp.write(bytes(view[start : start + _CACHED_CHUNK_BYTES]))
                    # Let other requests run between pieces of up to 8 MiB.
                    await asyncio.sleep(0)
        except TimeoutError:
            if self.request.transport is not None:
                self.request.transport.close()
            return resp
        await resp.write_eof()
        return resp

    async def _pipe(
        self, svc, session: MediaSession, prefix: str
    ) -> web.StreamResponse:
        """Wait (bounded) for the holder to start, then pipe the blob."""
        if not svc.pin(session, prefix):
            # This reader prefix already holds its share of sessions open.
            busy = _status(503, "busy")
            busy.headers["Retry-After"] = "5"
            return busy
        session.readers += 1
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
            svc.unpin(session, prefix)


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
        svc = self.svc(K.gfs_space_media_on_demand_key)
        session = await _serving_session(self, svc, ticket, SERVE_OP_SERVE)
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
            # A race past the check made before asking: end the session at
            # once (never left hanging), without marking the blob missing.
            svc.quota_refused(session)
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
            if session.member and auth.kind not in _MEMBER_STORE_KINDS:
                # A member serves anonymously: never under an identified
                # authorization (streamed only).
                raise PermissionError("identified authorization on a member serve")
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
        svc = self.svc(K.gfs_space_media_on_demand_key)
        session = await _serving_session(self, svc, ticket, SERVE_OP_DECLINE)
        if isinstance(session, web.Response):
            return session
        body = await self.bounded_json(256) if self.request.can_read_body else {}
        raw = body.get("reason")
        reason = raw if raw in (DECLINE_BAD_HINT, DECLINE_BUSY) else DECLINE_NOT_HELD
        if session.member:
            claim = self.request.headers.get(MEMBER_CLAIM_HEADER, "")
            if not await svc.member_decline(session, reason, claim):
                # Its claim ran out and another member holds the want now.
                return web.json_response({"error": "forbidden"}, status=403)
        else:
            svc.decline(session, reason)
        return web.json_response({"declined": True})


class GfsMediaMemberPullView(GfsBaseView):
    """``POST /gfs/media/member-pull`` — a writer-key holder of a listed
    space asks for a want (module docstring)."""

    async def post(self) -> web.Response:
        svc = self.svc(K.gfs_space_media_on_demand_key)
        if not svc.member_fallback:
            return _status(404)
        body = await self.bounded_json(MAX_MEMBER_PULL_BODY_BYTES)
        try:
            req = MemberPullRequest.from_wire(body)
        except InvalidOnDemandWire as exc:
            log.debug("space media member pull refused: %s", exc)
            return _status(422, "bad_request")
        store = self.svc(K.gfs_space_media_store_key)
        outcome = await svc.member_pull(
            req, prefix_key=store.prefix_key(self.client_ip())
        )
        if isinstance(outcome, MemberWant):
            return web.json_response({"wants": [outcome.to_wire()]}, headers=_NO_STORE)
        if outcome == PULL_FORBIDDEN:
            return _status(403, "forbidden")
        if outcome == PULL_FULL:
            resp = _status(503, "busy")
            resp.headers["Retry-After"] = "600"
            return resp
        return _status(204)


async def _serving_session(
    view: GfsBaseView, svc, ticket: str, op: str
) -> MediaSession | web.Response:
    """The session a serve / decline of *ticket* may act on: the owner's
    (household-signed, ``X-SH-Instance``) or a member's (writer-key-signed,
    naming no household) — or the refusal."""
    headers = view.request.headers
    if SERVE_INSTANCE_HEADER in headers:
        who = await _holder(view, ticket, op)
        if isinstance(who, web.Response):
            return who
        return _asked(svc, ticket, who)
    epoch = headers.get(MEMBER_WRITER_EPOCH_HEADER, "")
    ts = headers.get(SERVE_TS_HEADER, "")
    sig = headers.get(MEMBER_WRITER_SIG_HEADER, "")
    suite = headers.get(MEMBER_WRITER_SIG_SUITE_HEADER, "")
    claim = headers.get(MEMBER_CLAIM_HEADER, "")
    if not epoch or not ts or not sig or not suite or not claim:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    session = svc.session(ticket)
    if session is None:
        return web.json_response({"error": "unknown_ticket"}, status=404)
    if not await svc.authorize_member(
        session, op=op, epoch=epoch, ts=ts, sig=sig, suite=suite, claim=claim
    ):
        return web.json_response({"error": "forbidden"}, status=403)
    return session


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
