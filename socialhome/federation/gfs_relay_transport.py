"""Deliver §24.11 envelopes to a link-joined household via the GFS relay.

A household that joined a space from an invite link (§D2b,
:mod:`socialhome.federation.invite_bootstrap`) is seated on both sides as
a ``remote_instances`` row with
:data:`~socialhome.domain.federation.InstanceSource.SPACE_SESSION`,
matching directional session keys and — **by design** — an empty
``remote_inbox_url``: the two households never learn each other's
address, the connection server (GFS) shields them. There is nothing to
dial, so RTC signalling and the HTTPS inbox both have nowhere to go.

This module is the third transport tier: a
:class:`~socialhome.federation.strategies.TransportStrategy` that carries
an ordinary §24.11 envelope over the same opaque
``POST {gfs}/gfs/envelope`` relay the invite bootstrap used.

## Why the envelope is sealed again

The envelope handed to a transport is already AES-256-GCM-encrypted
under the pair's session key and Ed25519-signed — but its *routing*
fields are plaintext by construction: ``from_instance``, ``to_instance``,
``event_type``, ``space_id``, ``msg_id``, ``timestamp``. Handing that to
the relay would tell the GFS who talks to whom, about which space, how
often — exactly what §D2b was built to withhold. So the whole envelope
JSON is sealed a second time to the peer's static key-wrap public key
(:func:`~socialhome.federation.keywrap_seal.seal_to_keywrap`, the same
primitive the bootstrap redeem used, ``kem_suite`` tag included) and the
relay is handed the identity-free ``{to_instance, sealed}`` body.

What the GFS can infer is therefore ``(to_instance, time, size)`` per
envelope, and nothing else. That concession — including the fact that a
space fan-out to N link-joined members shows the relay N recipients at
once — is written up in ``docs/principles.md``.

## Wire shape

Outer (what the relay sees, built by
:class:`~socialhome.services.gfs_envelope_sender.GfsEnvelopeSender`)::

    {"to_instance": "<32 hex>",
     "sealed": {"kem_suite": "x25519", "eph_pk": …, "ciphertext": …}}

Inner (the sealed plaintext)::

    {"kind": "space_relay_envelope", "envelope": {…the §24.11 envelope…}}

``kind`` is the marker that tells the receiver which family this blob
belongs to. The relay leg carries two unrelated things through one
socket — bootstrap redeem bodies and full federation envelopes — and
guessing from field shape ("does it have ``msg_id``?") is exactly the
kind of sniffing that rots. The receiver
(:meth:`~socialhome.federation.invite_token_redeem
.SpaceInviteTokenRedeemCoordinator.handle_relayed_envelope`) dispatches
on this marker and hands the inner envelope to the **unmodified §24.11
pipeline**: instance lookup by ``from_instance`` → timestamp → signature
under the pair key → replay → decrypt → idempotency → ban → dispatch.
Nothing about riding the relay skips a step.

## What does not fit

The relay body is capped at :data:`RELAY_MAX_BODY_BYTES` (the GFS's
``ENVELOPE_MAX_BODY_BYTES``). Media chunks are 512 KiB — 1 MiB before
base64, so a chunk envelope is several times the cap and is refused
here, loudly, rather than shipped to a 413. See the media note in
``docs/protocol/invites.md``.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from ..domain.federation import RemoteInstance
from .invite_bootstrap import RelayEnvelopeSender
from .keywrap_seal import seal_to_keywrap

log = logging.getLogger(__name__)


#: ``kind`` discriminator on the sealed plaintext. Distinct from every
#: ``KIND_REDEEM*`` value in :mod:`socialhome.federation.invite_bootstrap`
#: so one relay socket can carry both families without either side
#: sniffing at field shapes.
RELAY_KIND_ENVELOPE: str = "space_relay_envelope"

#: Hard cap on the body handed to ``POST /gfs/envelope``. Mirrors
#: :data:`socialhome.global_server.envelope_relay.ENVELOPE_MAX_BODY_BYTES`
#: — duplicated rather than imported because the household half must not
#: depend on the server package, and pinned equal by
#: ``tests/federation/test_gfs_relay_transport.py``.
RELAY_MAX_BODY_BYTES: int = 320 * 1024

#: Cheap pre-seal bound on the envelope JSON, so an oversize frame costs a
#: length compare instead of an AES-GCM pass over a megabyte. Base64
#: inflates the ciphertext by 4/3 and the outer JSON adds ~150 bytes of
#: framing, so 3/4 of the body cap with 8 KiB of headroom is conservative
#: — anything that passes here is re-checked exactly against
#: :data:`RELAY_MAX_BODY_BYTES` once sealed.
RELAY_MAX_ENVELOPE_BYTES: int = (RELAY_MAX_BODY_BYTES * 3) // 4 - 8 * 1024


def seal_relay_envelope(
    *,
    envelope_dict: dict,
    peer_keywrap_pub: bytes,
) -> dict[str, str]:
    """Seal one §24.11 envelope to a peer's static key-wrap public key.

    Returns the ``{kem_suite, eph_pk, ciphertext}`` dict — the outer
    ``to_instance`` wrapper is built by the relay sender, which rebuilds
    it from the recipient id rather than forwarding a caller's dict (so
    no caller can grow a third, identifying field).
    """
    plaintext = orjson.dumps(
        {"kind": RELAY_KIND_ENVELOPE, "envelope": envelope_dict},
    )
    return seal_to_keywrap(
        recipient_keywrap_pub=peer_keywrap_pub,
        plaintext=plaintext,
    )


def is_relay_envelope_body(body: Any) -> bool:
    """True when an unsealed relay plaintext is a §24.11 envelope frame."""
    return isinstance(body, dict) and body.get("kind") == RELAY_KIND_ENVELOPE


class GfsRelayTransport:
    """:class:`TransportStrategy` over the connection server's envelope relay.

    Selected by :class:`~socialhome.federation.transport.FederationTransport`
    for peers seated from an invite link (``source = space_session``);
    every other peer keeps the RTC-first / HTTPS-fallback path untouched.

    Never raises on a transport-level failure — like every transport it
    answers ``(False, None)`` so the caller records the failure and
    queues for retry. The one thing it refuses outright is an envelope
    that cannot fit the relay (media), which is logged at WARNING naming
    the peer and the size so a silent drop is impossible.
    """

    __slots__ = ("_sender",)

    def __init__(self, *, relay_sender: RelayEnvelopeSender) -> None:
        self._sender = relay_sender

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> tuple[bool, int | None]:
        """Seal ``envelope_dict`` to *instance* and hand it to the relay.

        ``instance.remote_keywrap_pk`` is the peer's static X25519
        key-wrap key, verified bound to its identity key at seat time
        (:func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`);
        ``instance.relay_via`` names the connection server that
        introduced the pair. A row missing either cannot be reached and
        fails closed — never a fall-through to an HTTPS POST at the
        empty inbox URL.
        """
        keywrap_pk = instance.remote_keywrap_pk or ""
        if not keywrap_pk:
            log.warning(
                "gfs relay: no key-wrap key stored for %s — cannot seal "
                "an envelope to a household seated from an invite link",
                instance.id,
            )
            return False, None
        try:
            peer_keywrap_pub = bytes.fromhex(keywrap_pk)
        except ValueError:
            log.warning(
                "gfs relay: malformed key-wrap key stored for %s",
                instance.id,
            )
            return False, None

        raw_len = len(orjson.dumps(envelope_dict))
        if raw_len > RELAY_MAX_ENVELOPE_BYTES:
            # Refused HERE, not by the relay: shipping it would burn a
            # seal + a request to earn a 413, and the failure would read
            # as "the connection server is unhappy" rather than "this
            # frame is structurally too big for this transport".
            log.warning(
                "gfs relay: refusing a %d-byte %r envelope for %s — the "
                "relay body cap is %d bytes (media does not flow to "
                "households seated from an invite link yet)",
                raw_len,
                envelope_dict.get("event_type"),
                instance.id,
                RELAY_MAX_BODY_BYTES,
            )
            return False, None

        try:
            sealed = seal_relay_envelope(
                envelope_dict=envelope_dict,
                peer_keywrap_pub=peer_keywrap_pub,
            )
        except Exception as exc:
            log.warning(
                "gfs relay: could not seal an envelope for %s: %s",
                instance.id,
                exc,
            )
            return False, None

        body_len = len(orjson.dumps({"to_instance": instance.id, "sealed": sealed}))
        if body_len > RELAY_MAX_BODY_BYTES:  # pragma: no cover — guarded above
            log.warning(
                "gfs relay: sealed body for %s is %d bytes, over the %d cap",
                instance.id,
                body_len,
                RELAY_MAX_BODY_BYTES,
            )
            return False, None

        try:
            ok = await self._sender.send_sealed_envelope(
                to_instance_id=instance.id,
                envelope={"to_instance": instance.id, "sealed": sealed},
                # The connection server that introduced this pair — the
                # only relay known to reach the peer. Empty means "any
                # relay this household can use", which is correct only
                # for a single-server household; a stored value keeps a
                # multi-server household answering where the peer listens.
                gfs_url=instance.relay_via or "",
            )
        except Exception as exc:
            # A *configuration* refusal (EnvelopeRelayUnavailable) is a
            # transport failure here, not a user-facing error: the send
            # is queued and the operator sees the reason in the log. A
            # transport must never raise — the contract in
            # ``strategies.TransportStrategy``.
            log.info(
                "gfs relay: no connection server could carry an envelope for %s: %s",
                instance.id,
                exc,
            )
            return False, None
        return bool(ok), None


__all__ = [
    "RELAY_KIND_ENVELOPE",
    "RELAY_MAX_BODY_BYTES",
    "RELAY_MAX_ENVELOPE_BYTES",
    "GfsRelayTransport",
    "is_relay_envelope_body",
    "seal_relay_envelope",
]
