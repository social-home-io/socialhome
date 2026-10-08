"""Deliver §24.11 envelopes through the GFS envelope relay.

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

The same tier is the last-resort fallback for a **paired** household
(``source = manual``) that this household opted into the relay with
(``RemoteInstance.gfs_relay``), when neither the DataChannel nor the
HTTPS inbox reaches it. Such a peer may share several connection servers
with us; the facade passes one confirmed route per attempt as
``gfs_url`` (round-robin, :class:`~socialhome.federation.transport
.FederationTransport`).

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

What the GFS can infer is therefore ``(to_instance, time, size bucket)``
per envelope, and nothing else. That concession — including the fact that
a space fan-out to N link-joined members shows the relay N recipients at
once — is written up in ``docs/principles.md``.

## Size padding

The sealed plaintext is padded to exactly one of
:data:`RELAY_SIZE_BUCKETS` before sealing, so the ciphertext length — the
one thing about the payload the GFS still sees — names a bucket, not a
size: a reaction and a short post look alike, and so do a plain write and
a moderation submission carrying the author's signed inner. The pad is a
:data:`~socialhome.domain.space_item.PAD_FIELD` key on the *wrapper*, a
sibling of ``envelope``:

* it sits inside the seal, so it is authenticated with everything else;
* the receiver only ever reads ``kind`` and ``envelope`` and hands the
  pipeline ``orjson.dumps(body["envelope"])`` — so the envelope bytes the
  §24.11 signature step sees are identical with or without the pad, and a
  receiver from before padding (which ignores unknown wrapper keys) opens
  a padded blob unchanged. No seal-format change, no new ``kem_suite``, no
  protocol-version gate.

An envelope that does not fit the top bucket is refused with
:data:`RELAY_STATUS_TOO_LARGE`: the top bucket is the largest sealed
plaintext a receiver opens at all (its ``MAX_SEALED_BLOB_BYTES`` check on
the ciphertext string), so there is no unpadded "too big to hide" class.

## Wire shape

Outer (what the relay sees, built by
:class:`~socialhome.services.gfs_envelope_sender.GfsEnvelopeSender`)::

    {"to_instance": "<32-char base32 instance id>",
     "sealed": {"kem_suite": "x25519", "eph_pk": …, "ciphertext": …}}

Inner (the sealed plaintext)::

    {"kind": "space_relay_envelope", "envelope": {…the §24.11 envelope…},
     "_pad": "000…"}      # to exactly one RELAY_SIZE_BUCKETS size

``kind`` is the marker that tells the receiver which family this blob
belongs to. The relay leg carries two unrelated things through one
socket — bootstrap redeem bodies and full federation envelopes — and
guessing from field shape ("does it have ``msg_id``?") is exactly the
kind of sniffing that rots. The receiver
(:meth:`~socialhome.services.gfs_relay_inbound.GfsRelayInbound
.handle_frame`) dispatches on this marker and hands the inner envelope to
the **unmodified §24.11 pipeline**: instance lookup by ``from_instance`` →
peer-class and relay-opt-in gates → timestamp → signature under the pair
key → replay → decrypt → idempotency → ban → dispatch. Nothing about
riding the relay skips a step.

## What does not fit

The relay body is capped at :data:`RELAY_MAX_BODY_BYTES` (the GFS's
``ENVELOPE_MAX_BODY_BYTES``), and the receiver opens a sealed plaintext of
at most the top of :data:`RELAY_SIZE_BUCKETS` (~191 KiB). Media chunks
are 512 KiB — 1 MiB before base64, so a chunk envelope is several times
the cap and is refused here, loudly, rather than shipped to a 413. See
the media note in ``docs/protocol/invites.md``.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson

from ..domain.federation import RemoteInstance
from ..domain.space_item import ITEM_SIZE_BUCKETS, pad_json_object
from .invite_bootstrap import EnvelopeRelayThrottled, RelayEnvelopeSender
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

#: Sealed-plaintext sizes a relayed envelope is padded up to, so the GFS
#: learns the bucket and not the size. The item ladder
#: (:data:`~socialhome.domain.space_item.ITEM_SIZE_BUCKETS`, 1 / 4 / 16 /
#: 64 / 128 KiB) plus one rung at 191 KiB: the largest round size whose
#: seal every receiver opens — ``unseal_envelope_body`` refuses a
#: ciphertext string over ``MAX_SEALED_BLOB_BYTES`` (256 KiB of base64,
#: i.e. ~192 KiB of plaintext) before the AEAD. Pinned by
#: ``tests/federation/test_gfs_relay_transport.py``.
#:
#: Queue cost on the GFS: padding at most quadruples a small envelope
#: (a ~1.1 KiB frame ships at 4 KiB), so a recipient's full
#: ``ENVELOPE_QUEUE_MAX_PER_RECIPIENT`` (2000 rows) of 4 KiB-bucket
#: envelopes is ~11 MiB — well under the 64 MiB
#: ``ENVELOPE_QUEUE_MAX_BYTES_PER_RECIPIENT``; the byte cap still binds only
#: for top-bucket frames, exactly as it did for large frames before.
RELAY_SIZE_BUCKETS: tuple[int, ...] = (*ITEM_SIZE_BUCKETS, 191 * 1024)

#: Cheap pre-seal bound on the envelope JSON, so an oversize frame costs a
#: length compare instead of a JSON pad + AES-GCM pass over a megabyte. A
#: frame over the top bucket can never be padded into one; anything that
#: passes is re-checked exactly once padded (:func:`build_relay_plaintext`).
RELAY_MAX_ENVELOPE_BYTES: int = RELAY_SIZE_BUCKETS[-1]

#: Status this transport reports for a frame it refuses BEFORE the wire,
#: because it cannot fit :data:`RELAY_MAX_BODY_BYTES`. It is the status the
#: relay itself would have answered, so one number means one thing all the
#: way up the stack — and the facade above turns it into
#: :data:`~socialhome.domain.federation.DELIVERY_ERROR_RELAY_TOO_LARGE`, a
#: PERMANENT outcome, rather than a retry that can only fail again.
RELAY_STATUS_TOO_LARGE: int = 413

#: Status the relay answers when the caller is over its per-minute window.
#: Reported upward so the facade can mark the send *waitable* rather than
#: failed — see
#: :data:`~socialhome.domain.federation.DELIVERY_ERROR_RELAY_THROTTLED`.
RELAY_STATUS_THROTTLED: int = 429


def build_relay_plaintext(envelope_dict: dict) -> bytes:
    """The sealed plaintext for one envelope: the ``kind`` wrapper, padded
    to exactly one of :data:`RELAY_SIZE_BUCKETS` (see "Size padding"
    above). Larger than the top bucket comes back unpadded and over it —
    callers refuse that (:func:`seal_relay_envelope` raises,
    :meth:`GfsRelayTransport.send` answers ``413``)."""
    return pad_json_object(
        {"kind": RELAY_KIND_ENVELOPE, "envelope": envelope_dict},
        buckets=RELAY_SIZE_BUCKETS,
    )


def seal_relay_envelope(
    *,
    envelope_dict: dict,
    peer_keywrap_pub: bytes,
) -> dict[str, str]:
    """Pad and seal one §24.11 envelope to a peer's static key-wrap key.

    Returns the ``{kem_suite, eph_pk, ciphertext}`` dict — the outer
    ``to_instance`` wrapper is built by the relay sender, which rebuilds
    it from the recipient id rather than forwarding a caller's dict (so
    no caller can grow a third, identifying field).

    Raises :class:`ValueError` when the envelope does not fit the top size
    bucket — a receiver could not open the seal.
    """
    plaintext = build_relay_plaintext(envelope_dict)
    if len(plaintext) > RELAY_SIZE_BUCKETS[-1]:
        raise ValueError(
            f"relay envelope is {len(plaintext)} bytes once wrapped, over "
            f"the top size bucket ({RELAY_SIZE_BUCKETS[-1]})",
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
    for peers seated from an invite link (``source = space_session``) —
    their only transport — and, as the last-resort tier after RTC and the
    HTTPS inbox, for paired peers that opted into the relay
    (``RemoteInstance.gfs_relay``), once per confirmed route.

    Never raises on a transport-level failure — like every transport it
    answers ``(False, status)`` so the caller records the failure and
    queues for retry. Two failures carry a *status* rather than ``None``
    because the caller must tell them apart from "this peer is not
    reachable":

    * :data:`RELAY_STATUS_TOO_LARGE` — an envelope that cannot fit the
      relay (media). Refused outright, logged at WARNING naming the peer
      and the size so a silent drop is impossible, and **permanent**: the
      same frame fails the same arithmetic on every retry.
    * :data:`RELAY_STATUS_THROTTLED` — the relay's per-minute window is
      full. **Waitable**: the caller sleeps it out and retries.
    """

    __slots__ = ("_sender",)

    def __init__(self, *, relay_sender: RelayEnvelopeSender) -> None:
        self._sender = relay_sender

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
        gfs_url: str | None = None,
    ) -> tuple[bool, int | None]:
        """Seal ``envelope_dict`` to *instance* and hand it to the relay.

        ``instance.remote_keywrap_pk`` is the peer's static X25519
        key-wrap key, verified bound to its identity key at seat time
        (:func:`~socialhome.federation.keywrap_seal.verify_keywrap_binding`).
        The connection server is ``gfs_url`` when given — a paired peer's
        confirmed route, one of THIS household's connection servers —
        otherwise ``instance.relay_via``, the server that introduced a
        link-joined pair. A row with no usable key-wrap key cannot be
        reached and fails closed — never a fall-through to an HTTPS POST
        at an inbox URL. The server URL only picks which of our
        connections carries the blob; it never enters the blob.
        """
        keywrap_pk = instance.remote_keywrap_pk or ""
        if not keywrap_pk:
            log.warning(
                "gfs relay: no key-wrap key stored for %s — cannot seal "
                "an envelope for the connection-server relay",
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
                "relay body cap is %d bytes (media does not flow over the "
                "connection-server relay)",
                raw_len,
                envelope_dict.get("event_type"),
                instance.id,
                RELAY_MAX_BODY_BYTES,
            )
            return False, RELAY_STATUS_TOO_LARGE

        plaintext = build_relay_plaintext(envelope_dict)
        if len(plaintext) > RELAY_SIZE_BUCKETS[-1]:
            # Within the cheap bound but not once wrapped: a seal no
            # receiver would open. Same permanent status, same reason.
            log.warning(
                "gfs relay: refusing a %d-byte %r envelope for %s — over "
                "the relay body cap once wrapped (largest size bucket %d)",
                raw_len,
                envelope_dict.get("event_type"),
                instance.id,
                RELAY_SIZE_BUCKETS[-1],
            )
            return False, RELAY_STATUS_TOO_LARGE

        try:
            sealed = seal_to_keywrap(
                recipient_keywrap_pub=peer_keywrap_pub,
                plaintext=plaintext,
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
            return False, RELAY_STATUS_TOO_LARGE

        try:
            ok = await self._sender.send_sealed_envelope(
                to_instance_id=instance.id,
                envelope={"to_instance": instance.id, "sealed": sealed},
                # An explicit route (a paired peer's confirmed server) or
                # the connection server that introduced a link-joined
                # pair — the only relay known to reach the peer. Empty
                # means "any relay this household can use", which is
                # correct only for a single-server household; a stored
                # value keeps a multi-server household answering where
                # the peer listens.
                gfs_url=gfs_url if gfs_url is not None else (instance.relay_via or ""),
            )
        except EnvelopeRelayThrottled:
            # Back-pressure, not a failure: the relay is up and the blob
            # is fine, we are simply over the window. Reported as its own
            # status so the facade can mark the send waitable — a caller
            # that read this as "peer unreachable" would abandon a
            # space-sync catch-up over a few seconds of throttling.
            log.info(
                "gfs relay: throttled while sending to %s — the caller "
                "will wait the window out and retry",
                instance.id,
            )
            return False, RELAY_STATUS_THROTTLED
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
    "RELAY_SIZE_BUCKETS",
    "RELAY_STATUS_THROTTLED",
    "RELAY_STATUS_TOO_LARGE",
    "GfsRelayTransport",
    "build_relay_plaintext",
    "is_relay_envelope_body",
    "seal_relay_envelope",
]
