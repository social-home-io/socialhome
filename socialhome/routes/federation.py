"""Federation inbound envelope routes — /federation/inbox/{inbox_id} (section 24.11).

This is the single inbound entry point for federation. The full
validation pipeline (JSON parse -> timestamp skew -> instance lookup ->
signature verify -> replay cache -> decrypt -> dispatch) lives in
:class:`FederationService.handle_inbound_envelope` — this route is a
thin shim that forwards the raw body and converts the service's
canonical ``ValueError`` rejections into HTTP 400 / 403 / 410.

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

Rate limiting: the global rate-limit middleware buckets by user_id
which is empty for unauthenticated calls — that means a flood from one
remote IP would otherwise sail through. The view applies an explicit
per-IP bucket on top, mirroring ``/api/auth/token``.
"""

from __future__ import annotations

import logging

import orjson
from aiohttp import web

from .. import app_keys as K
from ..rate_limiter import client_bucket
from ..domain.federation import FederationEventType
from .base import BaseView

log = logging.getLogger(__name__)

#: §Audit #4 — IP-bucket throttle for unauthenticated federation inbox.
#: Tuned generous-but-finite: a real peer with a backlog of 1000 events
#: still drains in under 17 minutes; a flood from one IP gets 429'd.
INBOX_RATE_LIMIT = 1000
INBOX_RATE_WINDOW_S = 60

#: Event types whose replay-cache hit is expected noise rather than
#: a signal. The federation transport's DataChannel → HTTPS-inbox
#: failover path is intentionally "duplicate-and-dedup": if a send
#: hits a post-queue ``ConnectionClosedError``, the wrapper falls back
#: to HTTPS with the same ``msg_id``, the receiver dedups via the
#: replay cache, and the second copy lands as a 410. That's working
#: as designed — we prefer noise to silent loss. These ephemeral /
#: idempotent events show up in that fallback path constantly; the
#: noisy WARNING log is reserved for content events (DM_MESSAGE,
#: POST_CREATED, MOMENT_CREATED, …) where a true replay reading
#: matters more for ops triage.
_EXPECTED_REPLAY_EVENTS: frozenset[str] = frozenset(
    {
        FederationEventType.INSTANCE_CAPABILITIES_UPDATED.value,
        FederationEventType.PRESENCE_UPDATED.value,
        FederationEventType.USER_ONLINE.value,
        FederationEventType.USER_IDLE.value,
        FederationEventType.USER_OFFLINE.value,
        FederationEventType.USER_UPDATED.value,
        FederationEventType.USERS_SYNC.value,
        FederationEventType.LOCAL_HOME_LOCATION_CHANGED.value,
        FederationEventType.FEDERATION_RTC_OFFER.value,
        FederationEventType.FEDERATION_RTC_ANSWER.value,
        FederationEventType.FEDERATION_RTC_ICE.value,
    }
)


def _is_expected_replay(reason: str, raw_body: bytes) -> bool:
    """Return True if a 410 ``Replay detected`` rejection is benign log
    noise rather than an operational concern.

    The rejection happens after the envelope has been canonical-bytes
    parsed but before signature verification — so we re-peek the body
    here just to read ``event_type``. The peek failing (malformed body)
    counts as "not expected" so the WARNING fires; the validator's
    earlier parse step would have surfaced a 400 in that case anyway.
    """
    if not reason.startswith("Replay detected"):
        return False
    try:
        envelope = orjson.loads(raw_body)
    except Exception:
        return False
    event_type = envelope.get("event_type")
    if not isinstance(event_type, str):
        return False
    return event_type in _EXPECTED_REPLAY_EVENTS


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


class FederationInboxView(BaseView):
    """POST /federation/inbox/{inbox_id} — federation envelope arrives here.

    Returns ``{"status":"ok"}`` on successful dispatch (200), or
    an error code on validation failure. All errors are silent
    on the client side beyond the status — we never echo back the
    envelope or details that would help an attacker probe.
    """

    async def post(self) -> web.Response:
        inbox_id = self.match("inbox_id")

        # §Audit #4: per-IP throttle. The global rate-limit middleware
        # buckets by user_id (empty here), so an unauthenticated path
        # would otherwise be uncapped. Mirror the ``/api/auth/token``
        # pattern — bucket on remote IP, fall back to "unknown" so that
        # a missing peername still hits *some* shared bucket.
        limiter = self.request.app.get(K.rate_limiter_key)
        if limiter is not None:
            client_ip = client_bucket(self.request.remote)
            bucket = f"federation-inbox:{client_ip}"
            if not limiter.is_allowed(
                bucket,
                limit=INBOX_RATE_LIMIT,
                window_s=INBOX_RATE_WINDOW_S,
            ):
                # ``Retry-After`` (delta-seconds) tells the sender's outbox
                # when the window frees up, so it waits instead of hammering
                # — a 429 is back-pressure the sender retries, never a drop.
                return web.json_response(
                    {"error": _GENERIC_ERROR_BY_STATUS[429]},
                    status=429,
                    headers={
                        "Retry-After": str(
                            limiter.retry_after_s(bucket, window_s=INBOX_RATE_WINDOW_S)
                            or 1
                        )
                    },
                )

        try:
            raw_body = await self.request.read()
        except Exception as exc:
            log.debug("federation inbox: body read error: %s", exc)
            return web.json_response(
                {"error": _GENERIC_ERROR_BY_STATUS[400]},
                status=400,
            )

        if len(raw_body) > 1 * 1024 * 1024:  # 1 MiB cap
            return web.json_response(
                {"error": _GENERIC_ERROR_BY_STATUS[413]},
                status=413,
            )

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
        # for the sender, so the pipeline would reject them.
        pairing_response = await self._maybe_dispatch_pairing(
            federation_service,
            raw_body,
            inbox_id,
        )
        if pairing_response is not None:
            return pairing_response

        try:
            result = await federation_service.handle_inbound_envelope(
                inbox_id,
                raw_body,
            )
        except ValueError as exc:
            status = _classify(str(exc))
            # §Audit #7: never echo ``str(exc)`` to the wire — that
            # leaks ban-list, replay-cache, and inbox-existence state
            # to an attacker probing IDs. Log full detail server-side
            # so operators can still triage. Demote the ``Replay
            # detected`` line to INFO for ephemeral / idempotent
            # events (capabilities, presence, RTC signaling, …) since
            # those are the transport's DataChannel→HTTPS failover
            # noise (see ``_EXPECTED_REPLAY_EVENTS``); content-event
            # replays still WARNING so a real ops signal stays loud.
            if _is_expected_replay(str(exc), raw_body):
                log.info(
                    "federation inbox: dedup inbox_id=%s status=%d reason=%s",
                    inbox_id,
                    status,
                    exc,
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
        except Exception:
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
