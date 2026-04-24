"""Strategy protocols for federation transport + envelope encryption.

These ``typing.Protocol`` classes formalise patterns that already exist
implicitly inside :mod:`socialhome.federation.transport` and
:mod:`socialhome.federation.encoder`. By naming them we get:

* explicit type-check seams for tests (`runtime_checkable`),
* a single place to document the contract a new transport (Tor, Nostr…)
  or encryption scheme (post-quantum, hybrid PQ+ECC) must satisfy,
* the architectural property that :class:`FederationService` and
  :class:`FederationTransport` no longer depend on concrete classes —
  they depend on the protocol.

Concrete implementations:

* :class:`socialhome.federation.transport.HttpsInboxTransport` →
  :class:`TransportStrategy`
* :class:`socialhome.federation.transport._RtcPeer` (channel-level) →
  :class:`TransportStrategy` via :class:`FederationTransport` facade.
* :class:`socialhome.federation.encoder.FederationEncoder` →
  :class:`EncryptionStrategy`

The protocols carry no runtime behaviour. Importing this module is
side-effect free; it does not pull in aiohttp or aiolibdatachannel.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.federation import RemoteInstance


# ─── Transport ───────────────────────────────────────────────────────────


@runtime_checkable
class TransportStrategy(Protocol):
    """Send a fully-built envelope to a paired peer.

    The envelope is already AES-256-GCM-encrypted and Ed25519-signed.
    The transport is just a delivery channel — it must not inspect or
    rewrite the body. Returns ``(ok, status_code)`` where:

    * ``ok=True`` iff the peer accepted the envelope (HTTP 2xx for
      inboxes, channel write success for WebRTC).
    * ``status_code`` is the HTTP status when meaningful, or ``None``
      for non-HTTP transports.

    A transport must never raise on transport-level failure — return
    ``(False, None)`` instead so the caller can record a failure and
    enqueue for retry.
    """

    async def send(
        self,
        *,
        instance: RemoteInstance,
        envelope_dict: dict,
    ) -> tuple[bool, int | None]: ...


# ─── Encryption ──────────────────────────────────────────────────────────


@runtime_checkable
class EncryptionStrategy(Protocol):
    """Encrypt + sign federation envelopes.

    The default implementation
    (:class:`socialhome.federation.encoder.FederationEncoder`) uses
    AES-256-GCM for the payload and one or more signature algorithms
    (Ed25519 classical, optionally ML-DSA-65 hybrid) for the envelope.
    A test double can swap in a deterministic strategy to make crypto
    test fixtures readable.

    Suite-aware surface: :meth:`sign_envelope_all` +
    :meth:`verify_signatures_all` are what the federation service calls
    for actual envelope emission / validation. :meth:`sign_envelope` /
    :meth:`verify_signature` are single-algorithm conveniences kept for
    internal callers (SDP signing, compatibility tests).
    """

    def encrypt_payload(
        self,
        payload_json: str,
        session_key: bytes,
    ) -> str: ...

    def decrypt_payload(
        self,
        encrypted: str,
        session_key: bytes,
    ) -> str: ...

    def sign_envelope(self, envelope_bytes: bytes) -> str: ...

    def sign_envelope_all(
        self,
        envelope_bytes: bytes,
        *,
        suite: str | None = None,
    ) -> dict[str, str]: ...

    def verify_signature(
        self,
        envelope_bytes: bytes,
        signature: str,
        public_key: bytes,
    ) -> bool: ...

    def verify_signatures_all(
        self,
        envelope_bytes: bytes,
        *,
        suite: str,
        signatures: dict[str, str],
        ed_public_key: bytes,
        pq_public_key: bytes | None,
    ) -> bool: ...
