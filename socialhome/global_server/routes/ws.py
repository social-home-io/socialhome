"""SH↔GFS WebSocket push route (``GET /gfs/ws``, spec §24.12).

A paired Social Home household opens one persistent WebSocket against
this endpoint so the GFS can push relay events to it. It is the ONLY
delivery path — the GFS holds no household address, so a frame for a
household whose socket is down waits for it to reconnect (queued where the
feature queues, dropped where it does not). The WS is **one-way (GFS →
SH)** for a household that did not opt into the channel ring: after the
signed hello frame it sends no application frames. A household whose hello
listed ``channel_ring`` (v_97) sends ``channel_watch`` / ``channel_fetch``
frames (:mod:`..channel_ring`); anything else is ignored. Heartbeat is
WebSocket-protocol-level via aiohttp's ``heartbeat=30.0``.

SH → GFS calls (pair, the space directory, channels, report, appeal) keep
using REST endpoints — request/response semantics
fit those calls and a WS RPC envelope would only add ``request_id``
bookkeeping for no gain.

Hello frame (must arrive within 5 s of upgrade)::

    {"type": "hello", "instance_id": "<id>", "ts": <unix>, "sig": "<b64url>",
     "accepts": ["ws_binary1"]}

``accepts`` (optional) is an unsigned, per-connection hint: ``ws_binary1``
asks for binary push frames on THIS socket
(:mod:`socialhome.domain.ws_binary_frame`), ``space_media_serve`` says the
household answers ``space_media_request`` (v_83), ``envelope_media`` that it
opens binary relay media frames (v_87). Other labels are ignored.

``sig`` is ``Ed25519(public_key, f"{instance_id}|{ts}")`` and ``ts`` must
be within ±300 s of the GFS clock — same convention as §24.11. On
verification failure the GFS closes with WebSocket close code ``4401``;
on hello timeout, ``4408``.

An unregistered (or inactive) ``instance_id`` and a bad signature for a
registered one close with the SAME reason, ``auth-failed``, after the SAME
work — the unknown branch verifies the signature against a fixed dummy key
(:data:`..federation.TIMING_UNIFORM_DUMMY_KEY_HEX`). Distinct reasons, or an
unknown id answered before any Ed25519 verify, would let anyone learn which
instance ids are registered here. (Households older than this release
reported ``unknown-instance`` / ``bad-signature``; their UI treats all three
as "re-pair needed".)

Push frames (a channel item, by :mod:`..fan_out_workers`)::

    {"type": "relay", "channel_id": ..., "event_type": "space_item",
     "epoch": ..., "payload": ...}

Sealed household-to-household envelopes (§D2b invite bootstrap) arrive
as::

    {"type": "envelope", "sealed": {kem_suite, eph_pk, ciphertext}}

Both frames are deliberately identity-free: the GFS never learns which
household published a channel item, nor who sealed an envelope, so it has
nothing to forward. Queued envelopes are drained in order right after
the hello verifies.

Fire-and-forget — the SH never acks at the application layer.
"""

from __future__ import annotations

import asyncio
import logging
import time

import orjson
from aiohttp import WSMsgType, web

from ... import crypto
from ...domain.gfs_hello_accepts import (
    ACCEPTS_CHANNEL_RING,
    ACCEPTS_ENVELOPE_MEDIA,
    ACCEPTS_SPACE_MEDIA_SERVE,
    ACCEPTS_WS_BINARY1,
    parse_accepts,
)
from .. import app_keys as K
from ..envelope_relay import RELAY_SEEN_MIN_SESSION_S, RELAY_SEEN_REFRESH_S
from ..federation import TIMING_UNIFORM_DUMMY_KEY_HEX

log = logging.getLogger(__name__)


HELLO_TIMEOUT_SECONDS = 5.0
TIMESTAMP_WINDOW_SECONDS = 300

#: Largest frame a household may send (a hello, a ``channel_watch`` naming
#: 256 channels, a ``channel_fetch``).
WS_MAX_INBOUND_BYTES = 64 * 1024

WS_CLOSE_AUTH_FAILED = 4401
WS_CLOSE_HELLO_TIMEOUT = 4408
WS_CLOSE_PROTOCOL_VIOLATION = 4400

#: The one close reason for "this hello does not authenticate": unknown
#: instance, inactive instance, undecodable or wrong signature alike.
WS_AUTH_FAILED_REASON = b"auth-failed"


async def _mark_seen_while_open(
    fed_repo, instance_id: str, ws: web.WebSocketResponse
) -> None:
    """Mark *instance_id* relay-seen once its socket has stayed open for
    :data:`RELAY_SEEN_MIN_SESSION_S`, then again every
    :data:`RELAY_SEEN_REFRESH_S` while it stays open.

    The refresh is what keeps a long-lived household in the offline-copy
    window (``relay_seen_at`` within the 24 h queue TTL): marked only once, a
    socket held for more than a day — on whichever cluster node — aged out
    while still connected, and got nothing queued once it dropped. A failed
    write is logged and retried at the next tick; it never ends the loop."""
    delay = RELAY_SEEN_MIN_SESSION_S
    while True:
        await asyncio.sleep(delay)
        delay = RELAY_SEEN_REFRESH_S
        if ws.closed:
            return
        try:
            await fed_repo.mark_relay_seen(instance_id, at=int(time.time()))
        except Exception as exc:
            log.warning(
                "gfs.ws: relay-seen mark failed for %s: %r",
                instance_id,
                exc,
                exc_info=True,
            )


class GfsWebSocketView(web.View):
    """``GET /gfs/ws`` — upgrade to a GFS→SH push WebSocket."""

    async def get(self) -> web.StreamResponse:
        # A household sends only its hello and (v_97) small ring frames.
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=WS_MAX_INBOUND_BYTES)
        await ws.prepare(self.request)

        hello = await self._authenticate(ws)
        if hello is None:
            return ws
        instance_id, accepts = hello
        ring = ACCEPTS_CHANNEL_RING in accepts

        registry = self.request.app[K.gfs_ws_registry_key]
        fed_repo = self.request.app[K.gfs_fed_repo_key]
        # ``ws_binary1`` (hello only): push frames carrying a ciphertext go to
        # THIS socket as binary frames, ciphertext raw instead of base64.
        await registry.register(
            instance_id,
            ws,
            binary=ACCEPTS_WS_BINARY1 in accepts,
            # v_83 — it answers ``space_media_request`` (re-upload / stream
            # a space blob a reader missed).
            media_serve=ACCEPTS_SPACE_MEDIA_SERVE in accepts,
            # v_87 — it opens binary relay media frames (``\x00SHR``).
            envelope_media=ACCEPTS_ENVELOPE_MEDIA in accepts,
            # v_97 — it reads its channels from the ring (watch / fetch).
            ring=ring,
        )
        await fed_repo.upsert_rtc_connection(instance_id, transport="websocket")
        # Remembered past this socket: an offline ring household gets no
        # offline copy of a channel item (it pulls from the ring).
        channel_ring = self.request.app[K.gfs_channel_ring_key]
        if not ring:
            # A v_96 socket replacing a ring socket: no heads memory left.
            channel_ring.forget(instance_id)
        await self.request.app[K.gfs_channel_ring_repo_key].set_channel_ring(
            instance_id, ring
        )

        # v_62 — sealed moment keys that went out while this household was
        # offline (the latest per followed author, only the undelivered).
        self.request.app[K.gfs_moment_public_registry_key].schedule_follower_connected(
            instance_id
        )
        # v_66 — signed delete certificates of followed authors written while
        # this household was away (the live delete and the 24 h queue missed
        # it). An older household ignores the frame.
        self.request.app[K.gfs_moment_delete_log_key].schedule_household_connected(
            instance_id
        )

        # §D2b — flush any sealed envelopes that arrived while this household
        # was offline, oldest first, deleting each row only after its frame
        # went out. Awaited (not backgrounded) so queue order is the delivery
        # order; bounded by ENVELOPE_QUEUE_MAX_PER_RECIPIENT, and the SH sends
        # no application frames of its own, so nothing is missed meanwhile.
        await self.request.app[K.gfs_envelope_relay_key].drain(instance_id)
        # v_49 — a session held long enough marks the household "seen": only
        # seen households get channel items queued while offline (a
        # bare hello must not earn disk). Marked from a timer WHILE the socket
        # is open, and refreshed hourly while it stays open, never from the
        # ``finally`` below, which aiohttp may cancel mid-way when the peer
        # drops.
        seen_timer = asyncio.create_task(
            _mark_seen_while_open(fed_repo, instance_id, ws),
            name=f"gfs-ws-seen-{instance_id}",
        )
        if ring:
            # Every seated channel's head, so the household can badge and
            # fetch what it missed while away.
            try:
                await channel_ring.send_heads(instance_id, initial=True)
            except Exception:
                log.warning("gfs.ws: initial channel heads failed", exc_info=True)

        try:
            async for msg in ws:
                # The SH does not send application frames; aiohttp handles
                # ping/pong control frames natively. Anything else is
                # logged and ignored — a polite client shouldn't send it.
                if msg.type == WSMsgType.TEXT and ring:
                    try:
                        frame = orjson.loads(msg.data)
                    except orjson.JSONDecodeError:
                        continue
                    try:
                        await channel_ring.on_frame(instance_id, frame)
                    except Exception:
                        # A ring frame never ends the household's socket.
                        log.warning("gfs.ws: channel ring frame failed", exc_info=True)
                elif msg.type == WSMsgType.TEXT:
                    log.debug(
                        "gfs.ws: ignored unexpected TEXT frame from %s (len=%d)",
                        instance_id,
                        len(msg.data),
                    )
                elif msg.type == WSMsgType.ERROR:
                    log.warning(
                        "gfs.ws.error: instance=%s exc=%s",
                        instance_id,
                        ws.exception(),
                    )
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "gfs.ws.loop_error: instance=%s exc=%s",
                instance_id,
                exc,
                exc_info=True,
            )
        finally:
            seen_timer.cancel()
            if registry.is_current(instance_id, ws):
                channel_ring.forget(instance_id)
            await registry.unregister(instance_id, ws)
            await fed_repo.upsert_rtc_connection(instance_id, transport="https")

        return ws

    # ─── Hello-frame authentication ──────────────────────────────────────

    async def _authenticate(
        self, ws: web.WebSocketResponse
    ) -> tuple[str, tuple[str, ...]] | None:
        """Receive + verify the hello frame; close with ``4401``/``4408`` on failure.

        Returns the verified ``instance_id`` and the hello's ``accepts``
        labels (#5b — an unsigned hint, :func:`parse_accepts`) on success,
        ``None`` after closing.
        """
        try:
            msg = await ws.receive(timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError:
            await ws.close(
                code=WS_CLOSE_HELLO_TIMEOUT,
                message=b"hello-timeout",
            )
            return None

        if msg.type is not WSMsgType.TEXT:
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-required",
            )
            return None

        try:
            payload = orjson.loads(msg.data)
        except orjson.JSONDecodeError:
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-not-json",
            )
            return None

        if not isinstance(payload, dict) or payload.get("type") != "hello":
            await ws.close(
                code=WS_CLOSE_PROTOCOL_VIOLATION,
                message=b"hello-required",
            )
            return None

        instance_id = str(payload.get("instance_id") or "")
        ts_raw = payload.get("ts")
        sig = str(payload.get("sig") or "")
        if not instance_id or ts_raw is None or not sig:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"missing-fields")
            return None

        try:
            ts = int(ts_raw)
        except TypeError, ValueError:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"bad-ts")
            return None

        now = int(time.time())
        if abs(now - ts) > TIMESTAMP_WINDOW_SECONDS:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=b"ts-skew")
            return None

        fed_repo = self.request.app[K.gfs_fed_repo_key]
        instance = await fed_repo.get_instance(instance_id)
        known = instance is not None and instance.status == "active"
        # An unknown id still runs the full verify, against a key nothing
        # signs with, so it answers no sooner than a wrong signature does.
        key_hex = (
            instance.public_key
            if known and instance is not None
            else TIMING_UNIFORM_DUMMY_KEY_HEX
        )
        try:
            raw_key = bytes.fromhex(key_hex)
            raw_sig = crypto.b64url_decode(sig)
        except ValueError, TypeError:
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=WS_AUTH_FAILED_REASON)
            return None

        message = f"{instance_id}|{ts}".encode()
        verified = crypto.verify_ed25519(raw_key, message, raw_sig)
        if not (known and verified):
            await ws.close(code=WS_CLOSE_AUTH_FAILED, message=WS_AUTH_FAILED_REASON)
            return None

        log.info("gfs.ws.hello accepted: instance=%s", instance_id)
        return instance_id, parse_accepts(payload.get("accepts"))
