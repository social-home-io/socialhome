"""Ed25519 request authentication for signed household → GFS requests.

Every signed POST a household sends the GFS outside ``/gfs/ws`` (the
public-highlight answer / ICE / relay upload, highlight publications, the
public-moment endpoints) carries an Ed25519 signature over the canonical
body minus the ``signature`` field, same scheme as ``/gfs/report``.
:func:`_rtc_authenticate` checks it; :func:`authenticate_relay_stream` is
the header-based variant for the raw relay upload.

The household-to-household signalling rendezvous that used to live here
(``/gfs/rtc/offer`` / ``answer`` / ``ice`` / ``ping`` / ``session``) was
removed: no Social Home release ever called it — paired households signal
WebRTC over their own federation events (``FEDERATION_RTC_*``).
"""

from __future__ import annotations

import logging
import time

from aiohttp import web

from .. import app_keys as K
from ..admin_service import verify_report_signature
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: Accepted clock skew on the relay-stream auth timestamp. Matches the
#: §24.11 inbound-envelope window so a captured ``X-SH-Signature`` header
#: can't be replayed once the relay id is reused / forgotten.
RELAY_AUTH_MAX_SKEW_SECONDS: int = 300


async def _rtc_authenticate(
    view: GfsBaseView,
    *,
    keep_signature: bool = False,
    max_bytes: int | None = None,
) -> tuple[dict, str] | web.Response:
    """Shared signature check for signed household → GFS POST bodies.

    Returns the parsed + verified payload dict + sender instance_id on
    success, or a ready-to-return error Response. The returned body has
    no ``signature`` unless ``keep_signature`` — a body the GFS forwards
    verbatim (a public moment envelope) keeps it, because the receiver
    verifies that same signature itself. With *max_bytes* the body is
    read bounded (413 past it) before anything is parsed.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    body = (
        await view.bounded_json(max_bytes)
        if max_bytes is not None
        else await view.body_or_400()
    )
    instance_id = str(body.get("instance_id") or "")
    if not instance_id:
        return web.json_response(
            {"error": "missing_fields", "required": ["instance_id"]},
            status=422,
        )
    sender = await fed_repo.get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    signature = body.pop("signature", "")
    if not verify_report_signature(body, signature, sender.public_key):
        return web.json_response({"error": "invalid_signature"}, status=401)
    if keep_signature:
        body["signature"] = signature
    return body, instance_id


async def authenticate_relay_stream(view: GfsBaseView) -> str | web.Response:
    """Header-based Ed25519 check for the author's relay-stream upload.

    The relay ``POST .../relay-stream/{relay_id}`` body is the raw framed
    byte stream, so the signature can't ride inside it. Instead the author
    signs the canonical ``{"instance_id", "relay_id", "ts"}`` dict (same
    scheme as :func:`_rtc_authenticate`) and ships ``instance_id`` /
    ``ts`` / signature in ``X-SH-Instance`` / ``X-SH-Timestamp`` /
    ``X-SH-Signature`` headers. The ``ts`` must be within
    :data:`RELAY_AUTH_MAX_SKEW_SECONDS` of now, so a captured header can't
    be replayed later. Returns the verified sender instance_id or a
    ready-to-return error Response.
    """
    fed_repo = view.svc(K.gfs_fed_repo_key)
    instance_id = view.request.headers.get("X-SH-Instance", "")
    signature = view.request.headers.get("X-SH-Signature", "")
    ts_raw = view.request.headers.get("X-SH-Timestamp", "")
    relay_id = view.match("relay_id")
    if not instance_id or not signature or not ts_raw:
        return web.json_response({"error": "missing_auth_headers"}, status=422)
    try:
        ts = int(ts_raw)
    except ValueError:
        return web.json_response({"error": "invalid_timestamp"}, status=422)
    if abs(int(time.time()) - ts) > RELAY_AUTH_MAX_SKEW_SECONDS:
        return web.json_response({"error": "stale_timestamp"}, status=401)
    sender = await fed_repo.get_instance(instance_id)
    if sender is None or sender.status != "active":
        return web.json_response({"error": "forbidden"}, status=403)
    body = {"instance_id": instance_id, "relay_id": relay_id, "ts": ts}
    if not verify_report_signature(body, signature, sender.public_key):
        return web.json_response({"error": "invalid_signature"}, status=401)
    return instance_id
