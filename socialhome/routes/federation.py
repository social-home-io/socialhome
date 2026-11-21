"""Federation inbound envelope routes — /federation/inbox/{inbox_id} (section 24.11).

This is the single inbound entry point for federation. The full
validation pipeline (JSON parse -> timestamp skew -> instance lookup ->
signature verify -> replay cache -> decrypt -> dispatch) lives in
:class:`FederationService.handle_inbound_envelope` — this route is a
thin shim that forwards the raw body and converts the service's
canonical ``ValueError`` rejections into HTTP 400 / 403 / 410.

Binary media (proto v_81) rides the same URL: a body that starts with
``MEDIA_BODY_MAGIC`` is one media chunk frame
(:meth:`FederationService.handle_inbound_media_https` — the same §24.11
chain), anything else is a JSON envelope. The body decides, never
``Content-Type`` or a header: Home Assistant's inbox relay forwards every
body as ``application/octet-stream`` and drops custom headers.

§11 peer-pairing bootstrap (``PAIRING_PEER_ACCEPT`` /
``PAIRING_PEER_CONFIRM``) rides the same URL. The view peeks the
body's ``event_type`` before invoking the pipeline because pairing
bodies are plaintext + Ed25519 + TOFU on identity_pk — the §24.11
pipeline assumes a confirmed ``RemoteInstance`` exists and would
reject them at the instance-lookup step. Pairing events short-circuit
to :meth:`FederationService.handle_peer_accept` /
:meth:`handle_peer_confirm`.

Authentication: the path is in ``_DEFAULT_PUBLIC_PATHS`` because the
envelope is itself authenticated (Ed25519 over the canonical bytes).
The auth middleware bypass is the contract — no ``Authorization``
header is expected.

Rate limiting, in two layers:

* a per-address **backstop** before anything past the body's first bytes
  is read (the global rate-limit middleware buckets by user_id, empty
  here, so a flood would otherwise sail through). Behind Home Assistant's
  inbox relay — and behind any reverse proxy — every peer household
  arrives from the same address, so it is sized for many households at
  once, not for one;
* the **per-household budget** — the pipeline's step 4a
  (:func:`~socialhome.federation.inbound_validator.make_check_instance_rate`),
  counted once the signature proved who sent the envelope. Its refusal is
  a ``429`` with ``Retry-After`` like the backstop's.
"""

from __future__ import annotations

import asyncio
import logging

import orjson
from aiohttp import web

from .. import app_keys as K
from ..domain.errors import PayloadTooLargeError
from ..domain.federation import FederationEventType
from ..federation.inbound_validator import (
    InboundRateLimitedError,
    ReplayDetectedError,
    UnpairedPeerRefusedError,
)
from ..federation.inbox_reply import INBOX_CODE_REPLAY, INBOX_REPLAY_STATUS
from ..federation.media_framing import (
    HTTPS_MEDIA_MAX_BODY_BYTES,
    MEDIA_BODY_MAGIC,
    is_media_body,
)
from ..hardening import DEFAULT_JSON_MAX_BYTES, read_body_capped
from ..rate_limiter import client_bucket
from .base import BaseView

log = logging.getLogger(__name__)

#: §Audit #4 — the per-address backstop of the unauthenticated inbox,
#: checked before the body is read. Per address rather than one bucket for
#: the whole household: where every peer shares an address (Home
#: Assistant's inbox relay, a reverse proxy) it IS one bucket anyway, and on
#: a directly reachable household one stranger's flood then can't 429 every
#: other peer. Ten households at their full per-household budget
#: (:data:`~socialhome.federation.inbound_validator.INSTANCE_INBOUND_LIMIT`,
#: 600 / min — the limit that decides who waits) fit through one address at
#: once; a host with ~200 paired households after a restart (capability
#: updates, sync requests, outbox backlogs) hit the old 1000 / min in waves.
#: A flood from one address is still 429'd here, before any JSON parse or
#: signature check.
INBOX_RATE_LIMIT = 6000
INBOX_RATE_WINDOW_S = 60

#: Largest JSON envelope the inbox takes (§25.7).
INBOX_MAX_BODY_BYTES = DEFAULT_JSON_MAX_BYTES

#: Own per-IP backstop for binary media bodies on the inbox (proto v_81),
#: so media never eats into the JSON inbox's. The same shared-address
#: problem as above, so six households at their full per-household media
#: budget (:data:`~socialhome.federation.inbound_validator
#: .INSTANCE_MEDIA_INBOUND_LIMIT`, 600 chunks / min) fit through one
#: address: 3600 bodies of ≤ 528 KiB a minute. A 429 sends the sender's
#: chunk down the JSON path, which the outbox redelivers.
MEDIA_INBOX_RATE_LIMIT = 3600
MEDIA_INBOX_RATE_WINDOW_S = 60

#: Generic strings returned to the wire on rejection. Never echo the
#: raw exception (#7) — that text leaks ban-list / replay-cache state to
#: an attacker probing inbox IDs.
_GENERIC_ERROR_BY_STATUS: dict[int, str] = {
    400: "bad_envelope",
    403: "forbidden",
    404: "unknown_inbox",
    410: "gone",
    413: "envelope_too_large",
    429: "rate_limited",
}


# Substrings within a ValueError message that map to specific status codes.
# Order matters — first match wins.
_STATUS_CODE_RULES: tuple[tuple[str, int], ...] = (
    ("Invalid JSON", 400),
    ("Missing required fields", 400),
    ("Unparseable timestamp", 400),
    ("Unknown event_type", 400),
    ("No instance found", 404),
    ("Timestamp skew too large", 410),  # gone — too old
    ("Replay detected", 410),  # gone — already saw this msg_id
    ("Invalid envelope signature", 403),
    ("Failed to decrypt", 400),
    ("Decrypted payload", 400),
    ("Malformed encrypted payload", 400),
)


# §11 bootstrap event-type → pairing-service method. The receiver
# dispatches these ahead of the §24.11 pipeline because the pair
# doesn't exist on this side yet — the message itself materialises it.
_PAIRING_BOOTSTRAP_DISPATCH: dict[str, str] = {
    FederationEventType.PAIRING_PEER_ACCEPT.value: "handle_peer_accept",
    FederationEventType.PAIRING_PEER_CONFIRM.value: "handle_peer_confirm",
}


# Substrings within a pairing-service ValueError message → HTTP status.
# Mirrors the legacy ``routes/pairing_peer.py`` table so the wire
# behaviour for §11 errors is unchanged after the transport swap.
_PAIRING_STATUS_RULES: tuple[tuple[str, int], ...] = (
    ("Missing required fields", 400),
    ("Malformed signature", 400),
    ("Malformed dh", 400),
    ("Malformed identity", 400),
    ("No pending pairing", 404),
    ("RemoteInstance not found", 404),
    ("signature verification failed", 403),
    ("does not match identity_pk", 403),
    ("does not match stored identity_pk", 403),
    ("has expired", 410),
    ("cannot accept peer-accept", 409),
)


def _classify(msg: str) -> int:
    for needle, status in _STATUS_CODE_RULES:
        if needle in msg:
            return status
    return 400


def _classify_pairing(msg: str) -> int:
    for needle, status in _PAIRING_STATUS_RULES:
        if needle in msg:
            return status
    return 400


def _throttled(
    request: web.Request, prefix: str, *, limit: int, window_s: int
) -> web.Response | None:
    """The per-IP bucket of an unauthenticated federation route: a 429 with
    ``Retry-After`` when *request*'s address is over *limit* per
    *window_s*, else ``None``. The global rate-limit middleware buckets by
    user_id (empty here), so these routes would otherwise be uncapped."""
    limiter = request.app.get(K.rate_limiter_key)
    if limiter is None:
        return None
    # Fall back to "unknown" so a missing peername still hits *some*
    # shared bucket.
    bucket = f"{prefix}:{client_bucket(request.remote)}"
    if limiter.is_allowed(bucket, limit=limit, window_s=window_s):
        return None
    return _rate_limited(limiter.retry_after_s(bucket, window_s=window_s))


def _rate_limited(retry_after_s: int) -> web.Response:
    """A ``429`` whose ``Retry-After`` (delta-seconds) tells the sender's
    outbox when the window frees up, so it waits instead of hammering — a
    429 is back-pressure the sender retries, never a drop."""
    return web.json_response(
        {"error": _GENERIC_ERROR_BY_STATUS[429]},
        status=429,
        headers={"Retry-After": str(retry_after_s or 1)},
    )


async def _read_body_head(request: web.Request) -> bytes:
    """The first ``len(MEDIA_BODY_MAGIC)`` bytes of the body (fewer for a
    shorter body) — enough to tell a binary media body from JSON. A
    declared ``Content-Length`` over the JSON cap is refused first."""
    declared = request.content_length
    if declared is not None and declared > INBOX_MAX_BODY_BYTES:
        raise PayloadTooLargeError(INBOX_MAX_BODY_BYTES)
    try:
        return await request.content.readexactly(len(MEDIA_BODY_MAGIC))
    except asyncio.IncompleteReadError as exc:
        return exc.partial


def _bad_body(status: int) -> web.Response:
    return web.json_response({"error": _GENERIC_ERROR_BY_STATUS[status]}, status=status)


class FederationInboxView(BaseView):
    """POST /federation/inbox/{inbox_id} — federation envelope arrives here.

    Two body shapes, told apart by the body alone — never by
    ``Content-Type`` or a header, which Home Assistant's inbox relay
    (``/api/socialhome/inbox/{inbox_id}``) rewrites to
    ``application/octet-stream`` / drops:

    * a JSON envelope (at most 1 MiB) —
      :meth:`FederationService.handle_inbound_envelope`;
    * a binary media body (proto v_81) — the ``MEDIA_BODY_MAGIC``, a
      suite byte, one ``fed-media-v1`` frame (at most one 512 KiB chunk +
      overhead, :data:`~socialhome.federation.media_framing.HTTPS_MEDIA_MAX_BODY_BYTES`)
      — :meth:`FederationService.handle_inbound_media_https`, the same
      §24.11 chain. Its own per-IP bucket, so media never eats the
      envelope budget.

    Returns ``{"status":"ok"}`` on successful dispatch (200), or
    an error code on validation failure. All errors are silent
    on the client side beyond the status — we never echo back the
    envelope or details that would help an attacker probe.
    """

    async def post(self) -> web.Response:
        inbox_id = self.match("inbox_id")

        try:
            head = await _read_body_head(self.request)
        except PayloadTooLargeError:
            return _bad_body(413)
        except Exception as exc:
            log.debug("federation inbox: body read error: %s", exc, exc_info=True)
            return _bad_body(400)
        media = is_media_body(head)

        # §Audit #4: per-IP throttle, mirroring ``/api/auth/token``.
        throttled = (
            _throttled(
                self.request,
                "federation-media",
                limit=MEDIA_INBOX_RATE_LIMIT,
                window_s=MEDIA_INBOX_RATE_WINDOW_S,
            )
            if media
            else _throttled(
                self.request,
                "federation-inbox",
                limit=INBOX_RATE_LIMIT,
                window_s=INBOX_RATE_WINDOW_S,
            )
        )
        if throttled is not None:
            return throttled

        try:
            raw_body = await read_body_capped(
                self.request,
                HTTPS_MEDIA_MAX_BODY_BYTES if media else INBOX_MAX_BODY_BYTES,
                prefix=head,
            )
        except PayloadTooLargeError:
            return _bad_body(413)
        except Exception as exc:
            log.debug("federation inbox: body read error: %s", exc, exc_info=True)
            return _bad_body(400)

        federation_service = self.request.app.get(K.federation_service_key)
        if federation_service is None:
            log.warning(
                "federation inbox: service not yet wired (inbox_id=%s)",
                inbox_id,
            )
            return web.json_response(
                {"error": "service_unavailable"},
                status=503,
            )

        # §11 bootstrap fast-path: peek ``event_type`` before the §24.11
        # pipeline. Pairing bodies carry their own Ed25519 signature
        # and the receiver doesn't yet have a ``RemoteInstance`` row
        # for the sender, so the pipeline would reject them. (A binary
        # media body is never a pairing event.)
        if not media:
            pairing_response = await self._maybe_dispatch_pairing(
                federation_service,
                raw_body,
                inbox_id,
            )
            if pairing_response is not None:
                return pairing_response

        try:
            if media:
                result = await federation_service.handle_inbound_media_https(
                    inbox_id,
                    raw_body,
                )
            else:
                result = await federation_service.handle_inbound_envelope(
                    inbox_id,
                    raw_body,
                )
        except InboundRateLimitedError as exc:
            # Step 4a: a verified household over its budget. Back-pressure,
            # not a refusal — nothing about the envelope was kept (not even
            # its msg_id), so the redelivery after Retry-After is taken.
            log.info(
                "federation inbox: household over its budget inbox_id=%s "
                "retry_after=%ds",
                inbox_id,
                exc.retry_after_s,
            )
            return _rate_limited(exc.retry_after_s)
        except ValueError as exc:
            status = _classify(str(exc))
            # §Audit #7: never echo ``str(exc)`` to the wire — that
            # leaks ban-list, replay-cache, and inbox-existence state
            # to an attacker probing IDs. Log full detail server-side
            # so operators can still triage.
            if isinstance(exc, ReplayDetectedError):
                # An envelope we already hold, from the household whose
                # key signed it (the replay step runs after the signature
                # step): its retry after a POST that timed out while we
                # were still working on it, or the DataChannel → HTTPS
                # failover's second copy. Refused — nothing is applied
                # twice — but not a problem, so INFO for every event type
                # (a content event used to be a WARNING per retry), and
                # the answer carries the one fixed code that lets the
                # sender count it as delivered instead of dropping it as
                # "permanently rejected" (``federation/inbox_reply.py``).
                log.info(
                    "federation inbox: duplicate inbox_id=%s status=%d reason=%s",
                    inbox_id,
                    INBOX_REPLAY_STATUS,
                    exc,
                )
                return web.json_response(
                    {
                        "error": _GENERIC_ERROR_BY_STATUS[INBOX_REPLAY_STATUS],
                        "code": INBOX_CODE_REPLAY,
                    },
                    status=INBOX_REPLAY_STATUS,
                )
            if isinstance(exc, UnpairedPeerRefusedError):
                # A household we unpaired while it was offline, still
                # talking as if paired. The pipeline step logged it once
                # (INFO when it pulled our UNPAIR forward, DEBUG after) —
                # a WARNING per envelope here was the noise it avoids.
                log.debug(
                    "federation inbox: refused unpaired peer inbox_id=%s status=%d",
                    inbox_id,
                    status,
                )
            else:
                log.warning(
                    "federation inbox: rejected inbox_id=%s status=%d reason=%s",
                    inbox_id,
                    status,
                    exc,
                )
            return web.json_response(
                {"error": _GENERIC_ERROR_BY_STATUS.get(status, "rejected")},
                status=status,
            )
        except Exception:
            log.exception(
                "federation inbox: unexpected error (inbox_id=%s)",
                inbox_id,
            )
            return web.json_response(
                {"error": "internal"},
                status=500,
            )

        return web.json_response(result)

    async def _maybe_dispatch_pairing(
        self,
        federation_service: object,
        raw_body: bytes,
        inbox_id: str,
    ) -> web.Response | None:
        """Dispatch §11 bootstrap events ahead of the §24.11 pipeline.

        Returns the response to send back, or ``None`` if the body is
        not a pairing event and the caller should fall through to the
        regular pipeline. A malformed JSON body always falls through —
        the pipeline produces a stable 400 with the same wire shape.
        """
        try:
            body = orjson.loads(raw_body)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        event_type = body.get("event_type")
        if not isinstance(event_type, str):
            return None
        handler_name = _PAIRING_BOOTSTRAP_DISPATCH.get(event_type)
        if handler_name is None:
            return None

        handler = getattr(federation_service, handler_name)
        try:
            # Bind URL inbox_id to the body so the pairing coordinator
            # can refuse a body whose ``own_local_inbox_id`` doesn't
            # match the per-peer secret path we baked into the QR.
            # Defense-in-depth on top of the unguessable token lookup.
            result = await handler(body, expected_local_inbox_id=inbox_id)
        except ValueError as exc:
            msg = str(exc)
            status = _classify_pairing(msg)
            log.warning(
                "federation inbox: pairing %s rejected inbox_id=%s status=%d reason=%s",
                event_type,
                inbox_id,
                status,
                exc,
            )
            return web.json_response(
                {"error": _GENERIC_ERROR_BY_STATUS.get(status, "rejected")},
                status=status,
            )
        except Exception:
            log.exception(
                "federation inbox: pairing %s unexpected error (inbox_id=%s)",
                event_type,
                inbox_id,
            )
            return web.json_response(
                {"error": "internal"},
                status=500,
            )
        return web.json_response(result, status=200)
