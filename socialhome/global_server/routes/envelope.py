"""``POST /gfs/envelope`` — the opaque household-to-household relay (§D2b).

The design rationale, the constants and the deliver-or-queue logic live in
:mod:`socialhome.global_server.envelope_relay`; this module is the thin
aiohttp surface over it.
"""

from __future__ import annotations

import json
import logging

from aiohttp import web

from .. import app_keys as K
from ..envelope_relay import ENVELOPE_MAX_BODY_BYTES, InvalidEnvelope, validate_envelope
from .base import GfsBaseView

log = logging.getLogger(__name__)

#: Read granularity for the bounded body read. Large enough that a legitimate
#: envelope is a handful of chunks, small enough that an oversized body is
#: refused within one chunk of the cap. Mirrors ``routes/relay.py``.
_BODY_CHUNK_BYTES = 64 * 1024

#: The ONE response body every well-formed request gets back. Built once so no
#: future edit can make it depend on what happened to the envelope.
_ACCEPTED_BODY = {"status": "accepted"}


class EnvelopeRelayView(GfsBaseView):
    """``POST /gfs/envelope`` — relay a sealed blob to ``to_instance``.

    Body: ``{"to_instance": "<instance id>", "sealed": {"kem_suite": …,
    "eph_pk": …, "ciphertext": …}}`` — the exact outer shape
    ``federation/invite_bootstrap.py`` produces. The ``sealed`` dict is
    opaque: this server checks its key shape and never looks inside.

    **Unauthenticated on purpose.** The sender is deliberately anonymous —
    the whole point of the relay is that neither household learns the
    other's address and this server learns neither's relationship to the
    other. There is no identity on the wire to authenticate, so the per-IP
    limiter (``build_envelope_rate_limit``) is the accountability handle, as
    it is for the equally anonymous ``/gfs/publish``.

    **Uniform response.** Every well-formed request gets ``202
    {"status": "accepted"}`` — byte-identical whether the recipient was
    online, offline, or is not a client of this server at all. Any variation
    would turn the endpoint into a presence/existence oracle for anyone
    willing to walk instance ids. Malformed → 400, over the cap → 413,
    rate-limited → 429 (from the middleware).
    """

    async def post(self) -> web.Response:
        relay = self.svc(K.gfs_envelope_relay_key)
        raw = await self._bounded_body()
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=f"Invalid JSON body: {exc}") from exc
        try:
            to_instance, sealed = validate_envelope(parsed)
        except InvalidEnvelope as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        # ``accept`` returns nothing: online / offline / unknown all end here
        # with the same response, and there is no outcome to branch on.
        await relay.accept(to_instance, sealed)
        return web.json_response(_ACCEPTED_BODY, status=202)

    async def _bounded_body(self) -> bytes:
        """Read the body under :data:`ENVELOPE_MAX_BODY_BYTES`.

        Bounded BEFORE it is buffered or parsed: a declared ``Content-Length``
        over the cap is refused outright, and the read itself is capped so a
        chunked body (which declares no length at all) cannot exceed it
        either.
        """
        declared = self.request.content_length
        if declared is not None and declared > ENVELOPE_MAX_BODY_BYTES:
            raise web.HTTPRequestEntityTooLarge(
                max_size=ENVELOPE_MAX_BODY_BYTES,
                actual_size=declared,
            )
        raw = bytearray()
        async for chunk in self.request.content.iter_chunked(_BODY_CHUNK_BYTES):
            raw += chunk
            if len(raw) > ENVELOPE_MAX_BODY_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=ENVELOPE_MAX_BODY_BYTES,
                    actual_size=len(raw),
                )
        return bytes(raw)
