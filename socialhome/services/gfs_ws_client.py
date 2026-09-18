"""SH-side persistent WebSocket client for one paired GFS (spec §24.12).

The GFS is publicly reachable, so an SH instance opens a long-lived
``wss://`` connection to it for receiving relay events the GFS pushes.
SH→GFS calls (publish, subscribe, report, appeal) keep using the
existing REST endpoints in :class:`GfsConnectionService` — request /
response semantics fit those calls and a WS RPC envelope would only add
``request_id`` bookkeeping.

Lifecycle:

* :meth:`start` spawns the connect-and-listen loop in a background task.
* :meth:`stop` signals the loop to exit and awaits it (≤5 s).
* The loop reconnects with exponential backoff
  ``[1, 2, 4, 8, 30]`` seconds (clamped to the last value).
* On each connect: sends the signed hello frame
  ``{type:"hello", instance_id, ts, sig}``; thereafter dispatches every
  inbound ``{type:"relay", ...}`` frame to the injected ``on_relay``
  callable.

Heartbeat is WebSocket-protocol-level (aiohttp ``heartbeat=30``); the SH
never sends application frames after hello.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp
import orjson

from ..crypto import b64url_encode, sign_ed25519

log = logging.getLogger(__name__)


RECONNECT_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 30.0)

# WebSocket close codes the GFS uses to reject a hello (see
# ``global_server/routes/ws.py``): auth failure / hello timeout / protocol
# violation. A close with one of these codes means re-pairing or a config
# fix is required — the loop must back off, not reset its retry counter.
_AUTH_CLOSE_CODES: frozenset[int] = frozenset({4401, 4408, 4400})


def _sanitize_close_reason(reason: str | None) -> str | None:
    """Sanitize a GFS-controlled WebSocket close reason before use.

    The reason flows into logs (``last_auth_error``) and is later rendered in
    the SPA, so a malicious/compromised GFS could embed newlines or control
    characters (log-injection — forged log lines) or an overlong string. Strip
    non-printable chars (incl. newlines) and cap the length.
    """
    if not reason:
        return None
    cleaned = "".join(ch for ch in reason if ch.isprintable())
    cleaned = cleaned.strip()
    return cleaned[:80] or None


def _to_ws_url(http_url: str) -> str:
    """Convert ``http(s)://host`` to the matching ``ws(s)://host/gfs/ws``."""
    base = http_url.rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/gfs/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/gfs/ws"
    return base + "/gfs/ws"


class GfsWebSocketClient:
    """One persistent SH→GFS WebSocket connection.

    ``on_relay`` is invoked once per inbound ``relay`` frame; it must not
    raise — exceptions are caught and logged so a single bad frame cannot
    tear the loop down.
    """

    __slots__ = (
        "_gfs_url",
        "_instance_id",
        "_signing_key",
        "_session_factory",
        "_on_relay",
        "_on_highlight_signal",
        "_on_moment_signal",
        "_on_moment_public",
        "_on_follow_changed",
        "_on_new_subscriber",
        "_on_envelope",
        "_on_connected",
        "_reconnect_delays",
        "_stop",
        "_task",
        "_connected_event",
        "last_auth_error",
    )

    def __init__(
        self,
        *,
        gfs_url: str,
        instance_id: str,
        signing_key: bytes,
        session_factory: Callable[[], aiohttp.ClientSession],
        on_relay: Callable[[dict], Awaitable[None]],
        on_highlight_signal: Callable[[dict], Awaitable[None]] | None = None,
        on_moment_signal: Callable[[dict], Awaitable[None]] | None = None,
        on_moment_public: Callable[[dict], Awaitable[None]] | None = None,
        on_follow_changed: Callable[[dict], Awaitable[None]] | None = None,
        on_new_subscriber: Callable[[dict], Awaitable[None]] | None = None,
        on_envelope: Callable[[dict], Awaitable[None]] | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS,
    ) -> None:
        self._gfs_url = gfs_url
        self._instance_id = instance_id
        self._signing_key = signing_key
        self._session_factory = session_factory
        self._on_relay = on_relay
        self._on_highlight_signal = on_highlight_signal
        self._on_moment_signal = on_moment_signal
        self._on_moment_public = on_moment_public
        self._on_follow_changed = on_follow_changed
        self._on_new_subscriber = on_new_subscriber
        self._on_envelope = on_envelope
        self._on_connected = on_connected
        self._reconnect_delays = reconnect_delays
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connected_event = asyncio.Event()
        # Last GFS-supplied reason for an auth-related close (e.g.
        # "unknown-instance", "bad-signature"). ``None`` while the link is
        # healthy; set on a 4401/4408/4400 close so the supervisor/UI can
        # surface "re-pair may be required" instead of a silent reconnect
        # hammer. Cleared once a session is confirmed live.
        self.last_auth_error: str | None = None

    def attach_highlight_signal_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound wiring for the §highlights_public answerer.

        The :class:`HighlightSignalingHandler` is constructed after the WS
        client (it depends on the federation signing key + an
        ``aiohttp`` session that come online during ``app._on_startup``);
        this lets startup attach the handler without re-creating the
        client.
        """
        self._on_highlight_signal = handler

    def attach_moment_signal_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound wiring for the §Momentum-public answerer.

        Mirrors :meth:`attach_highlight_signal_handler`: the
        :class:`MomentPublicSignalingHandler` is constructed after the WS
        client and attached during startup. Receives ``moment_signal``
        frames (``offer`` / ``ice`` / ``relay_offer``) the GFS pushes when
        a guest reads the author's public-moment index.
        """
        self._on_moment_signal = handler

    def attach_moment_public_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound wiring for the §Momentum-public inbound handler.

        Receives ``incoming_public_moment`` and
        ``incoming_public_moment_delete`` frames pushed by the GFS
        broker. The handler verifies the envelope's signature and
        persists the moment locally with ``received_via='gfs'``.
        """
        self._on_moment_public = handler

    def attach_follow_changed_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Receive ``follow_changed`` frames from the GFS — the
        author's UI uses these to keep follower counts live."""
        self._on_follow_changed = handler

    def attach_new_subscriber_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound wiring for the Phase-5b subscriber key-handoff producer.

        The GFS pushes a ``new_subscriber`` frame when a household subscribes
        to a space this household owns; a seed-holder answers by sealing the
        per-space content key to the new subscriber. The
        :class:`SpaceSubscriberKeyOutbound` service is constructed after the
        WS client (it depends on the space crypto, wired during startup), so
        this lets startup attach the handler without re-creating the client.
        """
        self._on_new_subscriber = handler

    def attach_envelope_handler(
        self,
        handler: Callable[[dict], Awaitable[None]],
    ) -> None:
        """Late-bound wiring for the §D2b invite-bootstrap inbound leg.

        The GFS pushes an ``envelope`` frame when another household sealed a
        blob addressed to this one — an invite redeem from a stranger, or the
        issuer's sealed reply. The frame carries only ``{type, sealed}``: the
        relay knows the recipient and nothing else, and everything the
        handler needs (who sent it, which space, which token) is inside the
        ciphertext. The :class:`~socialhome.federation.invite_token_redeem
        .SpaceInviteTokenRedeemCoordinator` is wired after the WS client, so
        this lets startup attach it without re-creating the client.
        """
        self._on_envelope = handler

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background connect-and-listen loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._connected_event.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name=f"gfs-ws-client[{self._instance_id}->{self._gfs_url}]",
        )

    async def stop(self) -> None:
        """Stop the loop and wait for it to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    @property
    def connected(self) -> bool:
        """``True`` while a WebSocket is currently open."""
        return self._connected_event.is_set()

    def is_alive(self) -> bool:
        """``True`` while the connect-and-listen loop task is running.

        Distinct from :attr:`connected` — ``connected`` means a WebSocket is
        currently open, whereas ``is_alive`` means the background loop task
        itself is still running (it may be between reconnect attempts with no
        socket open). The supervisor uses this to detect a loop that died
        (uncaught error / cancellation) and restart the client.
        """
        return self._task is not None and not self._task.done()

    # ─── Internals ────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        attempt = 0
        ws_url = _to_ws_url(self._gfs_url)
        while not self._stop.is_set():
            try:
                await self._run_once(ws_url)
                # Clean disconnect from the server side counts as a retry —
                # we want to come back up.
                attempt = 0
            except _GfsWsAuthFailure as exc:
                # Persist the reason for the supervisor/UI, then fall through
                # WITHOUT resetting ``attempt`` so the backoff keeps widening
                # — an always-rejecting GFS must not be hammered.
                self.last_auth_error = exc.reason or f"code={exc.code}"
                log.warning(
                    "gfs.ws.client: auth rejected by %s — %s (re-pair may be "
                    "required); backing off",
                    self._gfs_url,
                    self.last_auth_error,
                )
            except Exception as exc:
                log.info(
                    "gfs.ws.client: connection to %s failed: %s",
                    self._gfs_url,
                    exc,
                )

            if self._stop.is_set():
                return
            delay = self._reconnect_delays[
                min(attempt, len(self._reconnect_delays) - 1)
            ]
            attempt += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # _stop fired — exit loop
            except asyncio.TimeoutError:
                continue

    async def _run_once(self, ws_url: str) -> None:
        """One connect-attempt cycle. Returns on clean disconnect; raises on error."""
        session = self._session_factory()
        async with session.ws_connect(
            ws_url,
            heartbeat=30.0,
            max_msg_size=4 * 1024 * 1024,
        ) as ws:
            await ws.send_json(self._build_hello())
            self._connected_event.set()
            if self._on_connected is not None:
                try:
                    await self._on_connected()
                except Exception as exc:  # defensive — never tear the loop down
                    log.warning(
                        "gfs.ws.client: on_connected handler raised for %s: %s",
                        self._gfs_url,
                        exc,
                    )
            # Explicit ``receive()`` loop (not ``async for``): a
            # server-initiated close ends ``async for`` WITHOUT yielding a
            # CLOSE message, so the auth-close code was never observed and the
            # loop reconnected at the floor delay forever. ``receive()`` yields
            # the CLOSE/CLOSING/CLOSED frame whose ``.extra`` carries the GFS's
            # reason string and whose ``ws.close_code`` is the auth code.
            try:
                auth_window_open = self.last_auth_error is not None
                while not self._stop.is_set():
                    if auth_window_open:
                        # The GFS rejects a bad hello within milliseconds. If
                        # nothing (frame OR close) arrives within the grace
                        # window, the hello was accepted — clear the stale
                        # reason. This avoids depending on the GFS proactively
                        # sending a frame (it may stay quiet on a healthy link).
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                        except asyncio.TimeoutError:
                            self.last_auth_error = None
                            auth_window_open = False
                            continue
                    else:
                        msg = await ws.receive()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        # A real frame proves the GFS accepted the hello — the
                        # auth window is closed, so clear any stale reason.
                        self.last_auth_error = None
                        auth_window_open = False
                        await self._on_text(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                    ):
                        code = ws.close_code
                        if code in _AUTH_CLOSE_CODES:
                            extra = getattr(msg, "extra", None)
                            reason = _sanitize_close_reason(
                                extra if isinstance(extra, str) else None
                            )
                            raise _GfsWsAuthFailure(code, reason)
                        # Clean / normal close → let the caller reset backoff
                        # and reconnect promptly.
                        self.last_auth_error = None
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning(
                            "gfs.ws.client: socket error on %s: %s",
                            self._gfs_url,
                            ws.exception(),
                        )
                        break
            finally:
                self._connected_event.clear()

    async def _on_text(self, raw: str) -> None:
        try:
            frame = orjson.loads(raw)
        except orjson.JSONDecodeError:
            log.warning(
                "gfs.ws.client: ignoring malformed JSON frame from %s",
                self._gfs_url,
            )
            return
        if not isinstance(frame, dict):
            return
        frame_type = frame.get("type")
        if frame_type == "highlight_signal":
            if self._on_highlight_signal is None:
                log.debug(
                    "gfs.ws.client: dropping highlight_signal — no handler attached on %s",
                    self._gfs_url,
                )
                return
            try:
                await self._on_highlight_signal(frame)
            except Exception as exc:  # defensive
                log.warning(
                    "gfs.ws.client: on_highlight_signal handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type == "moment_signal":
            if self._on_moment_signal is None:
                log.debug(
                    "gfs.ws.client: dropping moment_signal — no handler attached on %s",
                    self._gfs_url,
                )
                return
            try:
                await self._on_moment_signal(frame)
            except Exception as exc:  # defensive
                log.warning(
                    "gfs.ws.client: on_moment_signal handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type in ("incoming_public_moment", "incoming_public_moment_delete"):
            if self._on_moment_public is None:
                log.debug(
                    "gfs.ws.client: dropping %s — no handler attached on %s",
                    frame_type,
                    self._gfs_url,
                )
                return
            try:
                await self._on_moment_public(frame)
            except Exception as exc:  # defensive
                log.warning(
                    "gfs.ws.client: on_moment_public handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type == "follow_changed":
            if self._on_follow_changed is None:
                return
            try:
                await self._on_follow_changed(frame)
            except Exception as exc:  # defensive
                log.warning(
                    "gfs.ws.client: on_follow_changed handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type == "new_subscriber":
            if self._on_new_subscriber is None:
                log.debug(
                    "gfs.ws.client: dropping new_subscriber — no handler "
                    "attached on %s",
                    self._gfs_url,
                )
                return
            try:
                await self._on_new_subscriber(frame)
            except Exception as exc:  # defensive
                log.warning(
                    "gfs.ws.client: on_new_subscriber handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type == "envelope":
            if self._on_envelope is None:
                # WARNING, not DEBUG: the relay DELETED its queue row to
                # hand us this frame, so dropping it here loses the
                # envelope for good — an invite redeem that never
                # completes, or a space event that silently never
                # arrives. A missing handler is a wiring bug on this
                # household, and it has to be visible in an ordinary log.
                log.warning(
                    "gfs.ws.client: dropping a relayed envelope from %s — no "
                    "handler is attached, so this frame is lost (the "
                    "connection server has already dequeued it)",
                    self._gfs_url,
                )
                return
            try:
                await self._on_envelope(frame)
            except Exception as exc:  # defensive
                # The message names the rejection ("replay detected",
                # "signature verification failed"), never the blob.
                log.warning(
                    "gfs.ws.client: on_envelope handler raised for %s: %s",
                    self._gfs_url,
                    exc,
                )
            return
        if frame_type == "server_info_updated":
            # The GFS renamed itself. Re-run the same refresh the reconnect
            # path uses (re-fetch GET /gfs/info → update display_name) so we
            # pull the authoritative name rather than trusting the frame.
            if self._on_connected is not None:
                await self._on_connected()
            return
        if frame_type != "relay":
            log.debug(
                "gfs.ws.client: ignoring non-relay frame type=%r",
                frame_type,
            )
            return
        try:
            await self._on_relay(frame)
        except Exception as exc:  # defensive
            log.warning(
                "gfs.ws.client: on_relay handler raised for %s: %s",
                self._gfs_url,
                exc,
            )

    def _build_hello(self) -> dict:
        ts = int(time.time())
        message = f"{self._instance_id}|{ts}".encode("utf-8")
        sig = sign_ed25519(self._signing_key, message)
        return {
            "type": "hello",
            "instance_id": self._instance_id,
            "ts": ts,
            "sig": b64url_encode(sig),
        }


class _GfsWsAuthFailure(Exception):
    """Raised when the GFS closes the connection with an auth-related code.

    Carries the close ``code`` (one of :data:`_AUTH_CLOSE_CODES`) and the
    GFS-supplied ``reason`` string (e.g. ``"unknown-instance"``,
    ``"bad-signature"``) so the loop can surface it on ``last_auth_error``.
    """

    def __init__(self, code: int | None, reason: str | None) -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"server-closed code={code} reason={reason!r}")
