"""Pairing coordinator — §11 QR-code pairing handshake.

Extracted from :class:`FederationService` so the pairing flow can be
exercised independently of outbound delivery + inbound validation.

The flow is three steps:

1. :meth:`initiate` — local admin generates a QR payload (token,
   identity_pk, dh_pk, inbox_url, expires_at) for the peer to scan.
2. :meth:`accept` — peer scans the QR, derives a shared X25519 secret,
   stores a provisional ``RemoteInstance`` in ``PENDING_RECEIVED`` and
   returns a 6-digit SAS code that the two admins compare out-of-band.
3. :meth:`confirm` — local admin enters the SAS code; the
   ``RemoteInstance`` is upgraded to ``CONFIRMED``.

The coordinator is stateless apart from its dependencies (federation
repo + key manager + own identity public key).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    derive_instance_id,
    generate_x25519_keypair,
    random_token,
    verify_ed25519,
    x25519_exchange,
)
from ..domain.events import (
    PairingAcceptReceived,
    PairingConfirmed,
)
from ..utils.datetime import parse_iso8601_strict
from .crypto_suite import DEFAULT_SUITE, negotiate
from .peer_pairing_client import _canonical_body_bytes
from .peer_url import validate_peer_url
from ..domain.federation import (
    InstanceSource,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo

if TYPE_CHECKING:
    from .peer_pairing_client import PeerPairingClient


log = logging.getLogger(__name__)

#: Pairing QR token lifetime (seconds).
PAIRING_TTL_SECONDS = 300

#: Length of the SAS verification code (digits).
SAS_DIGITS = 6


def _require_fields(data: dict, *fields: str) -> None:
    """Raise ``ValueError`` if any of ``fields`` are missing from ``data``."""
    missing = [f for f in fields if f not in data]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")


class PairingCoordinator:
    """§11 QR-code pairing handshake."""

    __slots__ = (
        "_repo",
        "_key_manager",
        "_own_identity_pk",
        "_own_pq_pk",
        "_own_sig_suite",
        "_bus",
        "_peer_pairing_client",
    )

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        key_manager: KeyManager,
        own_identity_pk: bytes,
        own_pq_pk: bytes | None = None,
        own_sig_suite: str = DEFAULT_SUITE,
        bus: EventBus | None = None,
    ) -> None:
        self._repo = federation_repo
        self._key_manager = key_manager
        self._own_identity_pk = own_identity_pk
        self._own_pq_pk = own_pq_pk
        self._own_sig_suite = own_sig_suite
        self._bus = bus
        #: Lazily attached by :class:`FederationService` after construction
        #: (circular-dep-free). ``None`` in unit tests that exercise
        #: ``initiate()`` / ``accept()`` without the outbound wire.
        self._peer_pairing_client: "PeerPairingClient | None" = None

    def attach_peer_pairing_client(
        self,
        client: "PeerPairingClient",
    ) -> None:
        """Wire the outbound client. Called from the service after both
        objects are constructed.
        """
        self._peer_pairing_client = client

    async def initiate(self, inbox_base_url: str) -> dict:
        """Generate a QR payload for the §11 pairing handshake.

        ``inbox_base_url`` is the scheme+host+path prefix peers will
        POST to (e.g. ``https://my-instance.example/federation/inbox``).
        We append a freshly-generated secret id to produce the full
        per-peer URL baked into the QR; that same id lands on
        :attr:`PairingSession.own_local_inbox_id` and later becomes
        :attr:`RemoteInstance.local_inbox_id` when the pair confirms,
        which is how the inbound pipeline resolves the sender.
        """
        token = random_token(24)
        dh_kp = generate_x25519_keypair()
        own_local_inbox_id = secrets.token_urlsafe(24)
        inbox_url = f"{inbox_base_url.rstrip('/')}/{own_local_inbox_id}"
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()

        session = PairingSession(
            token=token,
            own_identity_pk=self._own_identity_pk.hex(),
            own_dh_pk=dh_kp.public_key.hex(),
            own_dh_sk=dh_kp.private_key.hex(),
            inbox_url=inbox_url,
            own_local_inbox_id=own_local_inbox_id,
            issued_at=now.isoformat(),
            expires_at=expires_at,
            status=PairingStatus.PENDING_SENT,
        )
        await self._repo.create_pairing(session)

        # Carry our derived ``instance_id`` alongside the public key so
        # the receiver can assert :func:`derive_instance_id(identity_pk)`
        # matches the claimed id — closes the §4.1.2 TOFU gap where a
        # tampered QR could swap the instance_id while keeping a valid
        # keypair. Without this check the receiver would accept any
        # ``display_name`` the attacker baked in.
        own_instance_id = derive_instance_id(self._own_identity_pk)
        local = await self._repo.get_local_identity()
        own_display_name = (local or {}).get("display_name") or ""
        payload: dict = {
            "token": token,
            "instance_id": own_instance_id,
            "identity_pk": self._own_identity_pk.hex(),
            "dh_pk": dh_kp.public_key.hex(),
            "inbox_url": inbox_url,
            "expires_at": expires_at,
            "sig_suite": self._own_sig_suite,
            "display_name": own_display_name,
        }
        if self._own_pq_pk is not None:
            payload["pq_algorithm"] = "mldsa65"
            payload["pq_identity_pk"] = self._own_pq_pk.hex()
        # Carry the initiator's home coords in the QR so the scanner
        # seats our row with coords immediately on ``accept`` — without
        # this, only the reverse direction (scanner → initiator, via
        # the peer-accept body) ever picked up coords during the
        # handshake, and the scanner's view of the initiator stayed
        # NULL until a later ``LOCAL_HOME_LOCATION_CHANGED`` envelope.
        # The QR is a one-shot, in-person exchange — exposing the
        # 4dp-truncated household coords (already a paired-peer
        # surface) doesn't widen the information set.
        if (
            local
            and local.get("home_lat") is not None
            and local.get("home_lon") is not None
        ):
            payload["home_lat"] = float(local["home_lat"])
            payload["home_lon"] = float(local["home_lon"])
        return payload

    def _derive_directional_keys(
        self,
        *,
        own_dh_sk: bytes,
        peer_dh_pk: bytes,
        is_initiator: bool,
    ) -> tuple[str, str]:
        """X25519 ECDH → HKDF → KEK-encrypted directional AES-256-GCM keys.

        Both sides of the handshake produce the same shared secret via
        ECDH, but the "self"/"remote" labels each side uses to store
        the per-direction keys are mirror images of one another. Anchor
        the HKDF info strings to the *role* (initiator vs acceptor)
        rather than to ``self``/``remote`` so the initiator's
        ``key_self_to_remote`` (the key it encrypts with on outbound)
        is the same value as the acceptor's ``key_remote_to_self`` (the
        key it decrypts inbound traffic with) — and vice versa.
        """
        shared_secret = x25519_exchange(own_dh_sk, peer_dh_pk)

        def _derive(info: bytes) -> bytes:
            hkdf = HKDF(
                algorithm=_hashes.SHA256(),
                length=32,
                salt=None,
                info=info,
            )
            return hkdf.derive(shared_secret)

        key_i_to_a = _derive(b"socialhome/session/initiator-to-acceptor")
        key_a_to_i = _derive(b"socialhome/session/acceptor-to-initiator")
        if is_initiator:
            key_self_to_remote = key_i_to_a
            key_remote_to_self = key_a_to_i
        else:
            key_self_to_remote = key_a_to_i
            key_remote_to_self = key_i_to_a
        return (
            self._key_manager.encrypt(key_self_to_remote),
            self._key_manager.encrypt(key_remote_to_self),
        )

    async def accept(
        self,
        qr_payload: dict,
        own_inbox_base_url: str | None = None,
    ) -> dict:
        """Process an incoming QR scan.

        ``own_inbox_base_url`` is this instance's externally-reachable
        federation inbox base (e.g. ``https://b.example/federation/inbox``)
        — required so we can tell A where to route subsequent envelopes
        back to us. Without it the peer-accept body cannot carry our
        own URL and A's :attr:`RemoteInstance.remote_inbox_url` ends
        up pointing back at A itself.
        """
        _require_fields(
            qr_payload,
            "token",
            "identity_pk",
            "dh_pk",
            "inbox_url",
        )

        token: str = qr_payload["token"]
        peer_identity_pk_hex: str = qr_payload["identity_pk"]
        peer_dh_pk_hex: str = qr_payload["dh_pk"]
        # The QR's inbox URL is where the signed peer-accept goes next and
        # where every later envelope is delivered — refuse a bad one before
        # any pairing state exists (raises InvalidPeerUrlError → 422).
        peer_inbox_url: str = validate_peer_url(
            qr_payload["inbox_url"],
            field="inbox_url",
        )

        # Generate our ephemeral DH keypair.
        own_dh_kp = generate_x25519_keypair()

        key_self_enc, key_remote_enc = self._derive_directional_keys(
            own_dh_sk=own_dh_kp.private_key,
            peer_dh_pk=bytes.fromhex(peer_dh_pk_hex),
            is_initiator=False,
        )

        # Derive peer instance_id from their identity public key.
        peer_identity_pk_bytes = bytes.fromhex(peer_identity_pk_hex)
        peer_instance_id = derive_instance_id(peer_identity_pk_bytes)

        # §4.1.2: if the QR payload claims an instance_id, it must match
        # the one derived from the supplied public key. A mismatch means
        # the QR was tampered with (attacker substituted a keypair while
        # keeping the victim's display_name / metadata). Older QR payloads
        # without ``instance_id`` are still accepted (TOFU baseline).
        claimed_instance_id = qr_payload.get("instance_id")
        if claimed_instance_id and claimed_instance_id != peer_instance_id:
            raise ValueError(
                "pairing QR instance_id does not match identity_pk — "
                "refuse to complete handshake",
            )

        # Generate an inbox id for the peer to POST to.
        own_local_inbox_id = secrets.token_urlsafe(24)

        # 6-digit SAS verification code.
        verification_code = str(secrets.randbelow(10**SAS_DIGITS)).zfill(SAS_DIGITS)

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()

        # Compose our own externally-reachable inbox URL so A learns
        # where to route back to. Falls back to the QR's inbox URL
        # (the legacy behaviour) only when the caller didn't supply
        # a base — that path is broken end-to-end and exists only so
        # in-process unit tests that bypass the route layer keep
        # passing.
        if own_inbox_base_url:
            own_inbox_url = f"{own_inbox_base_url.rstrip('/')}/{own_local_inbox_id}"
        else:
            own_inbox_url = qr_payload.get("inbox_url", "")

        # Store the in-progress pairing session for confirm_pairing.
        session = PairingSession(
            token=token,
            own_identity_pk=self._own_identity_pk.hex(),
            own_dh_pk=own_dh_kp.public_key.hex(),
            own_dh_sk=own_dh_kp.private_key.hex(),
            peer_identity_pk=peer_identity_pk_hex,
            peer_dh_pk=peer_dh_pk_hex,
            peer_inbox_url=peer_inbox_url,
            inbox_url=own_inbox_url,
            own_local_inbox_id=own_local_inbox_id,
            verification_code=verification_code,
            issued_at=now.isoformat(),
            expires_at=expires_at,
            status=PairingStatus.PENDING_RECEIVED,
        )
        await self._repo.create_pairing(session)

        # Negotiate the suite: both sides must announce the same hybrid
        # suite for the pair to run hybrid. Classical is the floor.
        peer_suite = str(qr_payload.get("sig_suite") or DEFAULT_SUITE)
        peer_pq_pk = qr_payload.get("pq_identity_pk")
        peer_pq_alg = qr_payload.get("pq_algorithm")
        negotiated = negotiate(self._own_sig_suite, peer_suite)

        # Persist a provisional RemoteInstance. Mirror the QR's
        # optional ``home_lat`` / ``home_lon`` onto the row at 4dp
        # precision (same rule the schema enforces). The peer-confirm
        # rebuild downstream preserves these, so a confirmed pair lands
        # with coords already populated — no wait for the next
        # ``LOCAL_HOME_LOCATION_CHANGED`` envelope.
        remote_inst = RemoteInstance(
            id=peer_instance_id,
            display_name=qr_payload.get("display_name", peer_instance_id[:8]),
            remote_identity_pk=peer_identity_pk_hex,
            key_self_to_remote=key_self_enc,
            key_remote_to_self=key_remote_enc,
            remote_inbox_url=peer_inbox_url,
            local_inbox_id=own_local_inbox_id,
            status=PairingStatus.PENDING_RECEIVED,
            source=InstanceSource.MANUAL,
            remote_pq_algorithm=str(peer_pq_alg) if peer_pq_alg else None,
            remote_pq_identity_pk=str(peer_pq_pk) if peer_pq_pk else None,
            sig_suite=negotiated,
            paired_at=now.isoformat(),
            home_lat=(
                round(float(qr_payload["home_lat"]), 4)
                if qr_payload.get("home_lat") is not None
                else None
            ),
            home_lon=(
                round(float(qr_payload["home_lon"]), 4)
                if qr_payload.get("home_lon") is not None
                else None
            ),
        )
        await self._repo.save_instance(remote_inst)

        # Tell A we accepted. A has a PairingSession but no RemoteInstance
        # yet — delivering our identity + dh keys in this plaintext
        # Ed25519-signed body is what lets A materialise its side of the
        # pair. Best-effort: on failure we still return the SAS locally
        # so the admin sees a surfaced retry hint in the UI.
        if self._peer_pairing_client is not None:
            local = await self._repo.get_local_identity()
            own_display_name = (local or {}).get("display_name") or ""
            peer_accept_body: dict = {
                "token": token,
                "verification_code": verification_code,
                "identity_pk": self._own_identity_pk.hex(),
                "instance_id": derive_instance_id(self._own_identity_pk),
                "dh_pk": own_dh_kp.public_key.hex(),
                "inbox_url": own_inbox_url,
                "display_name": own_display_name,
                "sig_suite": self._own_sig_suite,
            }
            if (
                local
                and local.get("home_lat") is not None
                and local.get("home_lon") is not None
            ):
                peer_accept_body["home_lat"] = float(local["home_lat"])
                peer_accept_body["home_lon"] = float(local["home_lon"])
            if self._own_pq_pk is not None:
                peer_accept_body["pq_algorithm"] = "mldsa65"
                peer_accept_body["pq_identity_pk"] = self._own_pq_pk.hex()
            # The QR's ``inbox_url`` is A's federation inbox. The
            # peer-accept rides it as a ``PAIRING_PEER_ACCEPT``
            # federation event so the HA integration's existing inbox
            # proxy is the only path we depend on.
            result = await self._peer_pairing_client.send_peer_accept(
                peer_inbox_url=peer_inbox_url,
                body=peer_accept_body,
            )
            if not result.ok:
                log.warning(
                    "peer-accept delivery failed (status=%s, err=%s) — admin can retry",
                    result.status_code,
                    result.error,
                )

        return {
            "verification_code": verification_code,
            "token": token,
            "local_inbox_id": own_local_inbox_id,
            "own_dh_pk": own_dh_kp.public_key.hex(),
        }

    async def handle_peer_accept(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """A-side receiver for :meth:`accept`'s outbound ``peer-accept``.

        Verifies the Ed25519 signature against the payload's
        ``identity_pk`` (TOFU — the QR SAS round-trip protects us
        against MITM), materialises the :class:`RemoteInstance` for
        the peer, updates the :class:`PairingSession` with peer data,
        and publishes :class:`PairingAcceptReceived` so the admin UI
        can auto-fill the SAS digits.

        ``expected_local_inbox_id`` is the inbox id extracted from the
        URL path the federation inbox view received. When non-None we
        assert it matches the session's ``own_local_inbox_id`` — the
        URL was the per-peer secret we baked into the QR, so a
        mismatch means the body and the URL were stitched together
        from different pairing sessions. Defense-in-depth on top of
        the token-based lookup (the token alone is unguessable, but
        binding the URL to the session prevents a future client bug
        from POSTing to the wrong inbox and the server happily
        accepting it).

        Returns a small status dict on success. Raises ``ValueError``
        on missing fields / bad signature / unknown token / expired
        session / URL-vs-session mismatch.
        """
        _require_fields(
            body,
            "token",
            "verification_code",
            "identity_pk",
            "dh_pk",
            "inbox_url",
            "signature",
        )
        token = str(body["token"])
        peer_identity_pk_hex = str(body["identity_pk"])
        peer_dh_pk_hex = str(body["dh_pk"])
        # B's inbox URL is where our peer-confirm (and every later envelope)
        # goes — validate before it is stored on the RemoteInstance.
        peer_inbox_url = validate_peer_url(body["inbox_url"], field="inbox_url")
        verification_code = str(body["verification_code"])

        # Look up our PairingSession by token — the invitee's original
        # QR payload carried this token, so a hit means the sender saw
        # the QR. No session → rogue sender.
        session = await self._repo.get_pairing(token)
        if session is None:
            raise ValueError(f"No pending pairing for token={token!r}")
        if (
            expected_local_inbox_id is not None
            and session.own_local_inbox_id != expected_local_inbox_id
        ):
            raise ValueError(
                "peer-accept inbox_id does not match pairing session",
            )
        if session.status is not PairingStatus.PENDING_SENT:
            # Replay-safe idempotency: if the RemoteInstance already
            # exists for this session's peer, accept silently without
            # rebuilding (lets a retried peer-accept land cleanly).
            peer_instance_id = derive_instance_id(
                bytes.fromhex(peer_identity_pk_hex),
            )
            existing = await self._repo.get_instance(peer_instance_id)
            if existing is not None:
                return {
                    "ok": True,
                    "instance_id": peer_instance_id,
                    "replay": True,
                }
            raise ValueError(
                f"Pairing session status is {session.status.value!r}, "
                "cannot accept peer-accept",
            )
        if session.expires_at:
            expires = parse_iso8601_strict(session.expires_at)
            if datetime.now(timezone.utc) > expires:
                raise ValueError("Pairing session has expired")

        # Verify the Ed25519 signature — TOFU, using the identity_pk
        # that arrived in the body. The SAS round-trip (admin-verified
        # out-of-band) is what finally closes the MITM gap.
        signature_hex = str(body["signature"])
        try:
            signature_bytes = bytes.fromhex(signature_hex)
            peer_identity_pk_bytes = bytes.fromhex(peer_identity_pk_hex)
        except ValueError as exc:
            raise ValueError(f"Malformed signature / identity_pk hex: {exc}") from exc
        canonical = _canonical_body_bytes(body)
        if not verify_ed25519(
            peer_identity_pk_bytes,
            canonical,
            signature_bytes,
        ):
            raise ValueError("peer-accept signature verification failed")

        # §4.1.2 TOFU safety: the body's claimed instance_id (if any)
        # must agree with derive_instance_id(identity_pk).
        peer_instance_id = derive_instance_id(peer_identity_pk_bytes)
        claimed_instance_id = body.get("instance_id")
        if claimed_instance_id and claimed_instance_id != peer_instance_id:
            raise ValueError(
                "peer-accept instance_id does not match identity_pk",
            )

        # Derive the directional keys — our side of ECDH. ``own_dh_sk``
        # was stashed in PENDING_SENT's PairingSession on ``initiate``.
        try:
            peer_dh_pk_bytes = bytes.fromhex(peer_dh_pk_hex)
            own_dh_sk = bytes.fromhex(session.own_dh_sk)
        except ValueError as exc:
            raise ValueError(f"Malformed dh hex: {exc}") from exc
        key_self_enc, key_remote_enc = self._derive_directional_keys(
            own_dh_sk=own_dh_sk,
            peer_dh_pk=peer_dh_pk_bytes,
            is_initiator=True,
        )

        # Negotiate the suite — classical is the floor; hybrid requires
        # both sides to announce it.
        peer_suite = str(body.get("sig_suite") or DEFAULT_SUITE)
        peer_pq_pk = body.get("pq_identity_pk")
        peer_pq_alg = body.get("pq_algorithm")
        negotiated = negotiate(self._own_sig_suite, peer_suite)

        now = datetime.now(timezone.utc)
        remote_inst = RemoteInstance(
            id=peer_instance_id,
            display_name=str(body.get("display_name") or peer_instance_id[:8]),
            remote_identity_pk=peer_identity_pk_hex,
            key_self_to_remote=key_self_enc,
            key_remote_to_self=key_remote_enc,
            remote_inbox_url=peer_inbox_url,
            local_inbox_id=session.own_local_inbox_id,
            status=PairingStatus.PENDING_RECEIVED,
            source=InstanceSource.MANUAL,
            remote_pq_algorithm=str(peer_pq_alg) if peer_pq_alg else None,
            remote_pq_identity_pk=str(peer_pq_pk) if peer_pq_pk else None,
            sig_suite=negotiated,
            paired_at=now.isoformat(),
            home_lat=(
                round(float(body["home_lat"]), 4)
                if body.get("home_lat") is not None
                else None
            ),
            home_lon=(
                round(float(body["home_lon"]), 4)
                if body.get("home_lon") is not None
                else None
            ),
        )
        await self._repo.save_instance(remote_inst)

        # Update the PairingSession with peer data + SAS so ``confirm``
        # (called later when the admin enters the SAS) can proceed.
        updated = PairingSession(
            token=session.token,
            own_identity_pk=session.own_identity_pk,
            own_dh_pk=session.own_dh_pk,
            own_dh_sk=session.own_dh_sk,
            inbox_url=session.inbox_url,
            own_local_inbox_id=session.own_local_inbox_id,
            peer_identity_pk=peer_identity_pk_hex,
            peer_dh_pk=peer_dh_pk_hex,
            peer_inbox_url=peer_inbox_url,
            intro_note=session.intro_note,
            relay_via=session.relay_via,
            verification_code=verification_code,
            issued_at=session.issued_at,
            expires_at=session.expires_at,
            status=PairingStatus.PENDING_RECEIVED,
        )
        await self._repo.update_pairing(updated)

        # Publish so the realtime service can forward the SAS to the
        # admin UI via the ``pairing.accept_received`` WS frame.
        if self._bus is not None:
            await self._bus.publish(
                PairingAcceptReceived(
                    from_instance=peer_instance_id,
                    token=token,
                    verification_code=verification_code,
                ),
            )

        log.info("peer-accept materialised: instance_id=%s", peer_instance_id)
        return {"ok": True, "instance_id": peer_instance_id, "replay": False}

    async def handle_peer_confirm(
        self,
        body: dict,
        *,
        expected_local_inbox_id: str | None = None,
    ) -> dict:
        """B-side receiver for :meth:`confirm`'s outbound ``peer-confirm``.

        Verifies the signature using the stored
        ``RemoteInstance.remote_identity_pk`` (not TOFU — we already
        have the peer's identity_pk from the QR scan), flips our
        local ``RemoteInstance`` status to ``CONFIRMED``, and
        publishes :class:`PairingConfirmed`.

        ``expected_local_inbox_id`` is the URL-path inbox id; see
        :meth:`handle_peer_accept` for the rationale.
        """
        _require_fields(body, "token", "instance_id", "signature")
        token = str(body["token"])
        claimed_instance_id = str(body["instance_id"])

        session = await self._repo.get_pairing(token)
        if session is None:
            raise ValueError(f"No pending pairing for token={token!r}")
        if (
            expected_local_inbox_id is not None
            and session.own_local_inbox_id != expected_local_inbox_id
        ):
            raise ValueError(
                "peer-confirm inbox_id does not match pairing session",
            )

        instance = await self._repo.get_instance(claimed_instance_id)
        if instance is None:
            raise ValueError(
                f"RemoteInstance not found for instance_id={claimed_instance_id!r}",
            )

        # Derived consistency: claimed_instance_id must agree with the
        # peer's identity_pk we already have on file.
        expected_iid = derive_instance_id(
            bytes.fromhex(instance.remote_identity_pk),
        )
        if expected_iid != claimed_instance_id:
            raise ValueError(
                "peer-confirm instance_id does not match stored identity_pk",
            )

        try:
            signature_bytes = bytes.fromhex(str(body["signature"]))
        except ValueError as exc:
            raise ValueError(f"Malformed signature hex: {exc}") from exc
        canonical = _canonical_body_bytes(body)
        if not verify_ed25519(
            bytes.fromhex(instance.remote_identity_pk),
            canonical,
            signature_bytes,
        ):
            raise ValueError("peer-confirm signature verification failed")

        if instance.status is PairingStatus.CONFIRMED:
            # Idempotent: already confirmed. Don't re-emit the event,
            # don't re-write the row. Safe replay.
            return {"ok": True, "instance_id": claimed_instance_id, "replay": True}

        confirmed = RemoteInstance(
            id=instance.id,
            display_name=instance.display_name,
            remote_identity_pk=instance.remote_identity_pk,
            key_self_to_remote=instance.key_self_to_remote,
            key_remote_to_self=instance.key_remote_to_self,
            remote_inbox_url=instance.remote_inbox_url,
            local_inbox_id=instance.local_inbox_id,
            status=PairingStatus.CONFIRMED,
            source=instance.source,
            intro_relay_enabled=instance.intro_relay_enabled,
            proto_version=instance.proto_version,
            remote_pq_algorithm=instance.remote_pq_algorithm,
            remote_pq_identity_pk=instance.remote_pq_identity_pk,
            sig_suite=instance.sig_suite,
            relay_via=instance.relay_via,
            home_lat=instance.home_lat,
            home_lon=instance.home_lon,
            paired_at=instance.paired_at,
            created_at=instance.created_at,
            last_reachable_at=instance.last_reachable_at,
            unreachable_since=instance.unreachable_since,
        )
        await self._repo.save_instance(confirmed)
        await self._repo.delete_pairing(token)

        if self._bus is not None:
            await self._bus.publish(PairingConfirmed(instance_id=instance.id))

        log.info("peer-confirm: instance_id=%s → CONFIRMED", instance.id)
        return {"ok": True, "instance_id": instance.id, "replay": False}

    async def confirm(
        self,
        token: str,
        verification_code: str,
    ) -> RemoteInstance:
        """Admin confirms the 6-digit SAS code → finalize the ``RemoteInstance``.

        Raises ``ValueError`` if the token or code is invalid, or the
        pairing session has expired.
        """
        session = await self._repo.get_pairing(token)
        if session is None:
            raise ValueError(f"No pending pairing for token={token!r}")

        if session.verification_code != verification_code:
            raise ValueError("Verification code mismatch")

        if session.expires_at:
            expires = parse_iso8601_strict(session.expires_at)
            if datetime.now(timezone.utc) > expires:
                raise ValueError("Pairing session has expired")

        if session.peer_identity_pk is None:
            raise ValueError("Pairing session missing peer identity key")

        peer_instance_id = derive_instance_id(
            bytes.fromhex(session.peer_identity_pk),
        )

        instance = await self._repo.get_instance(peer_instance_id)
        if instance is None:
            raise ValueError(
                f"RemoteInstance not found for peer_instance_id={peer_instance_id!r}"
            )

        # Replace with a confirmed instance (frozen dataclass — rebuild).
        # ``home_lat`` / ``home_lon`` mirror :meth:`peer_confirm` —
        # without them the rebuild silently drops the coords carried
        # via the peer-accept body and the row goes NULL until the next
        # ``LOCAL_HOME_LOCATION_CHANGED`` envelope refills it.
        confirmed = RemoteInstance(
            id=instance.id,
            display_name=instance.display_name,
            remote_identity_pk=instance.remote_identity_pk,
            key_self_to_remote=instance.key_self_to_remote,
            key_remote_to_self=instance.key_remote_to_self,
            remote_inbox_url=instance.remote_inbox_url,
            local_inbox_id=instance.local_inbox_id,
            status=PairingStatus.CONFIRMED,
            source=instance.source,
            remote_pq_algorithm=instance.remote_pq_algorithm,
            remote_pq_identity_pk=instance.remote_pq_identity_pk,
            sig_suite=instance.sig_suite,
            home_lat=instance.home_lat,
            home_lon=instance.home_lon,
            paired_at=instance.paired_at,
            created_at=instance.created_at,
            last_reachable_at=instance.last_reachable_at,
            unreachable_since=instance.unreachable_since,
        )
        await self._repo.save_instance(confirmed)
        await self._repo.delete_pairing(token)

        # Tell B we confirmed. Signature is verifiable by B using the
        # identity_pk B already stored during its ``accept()``.
        if self._peer_pairing_client is not None:
            peer_confirm_body: dict = {
                "token": token,
                "instance_id": derive_instance_id(self._own_identity_pk),
            }
            result = await self._peer_pairing_client.send_peer_confirm(
                peer_inbox_url=instance.remote_inbox_url,
                body=peer_confirm_body,
            )
            if not result.ok:
                log.warning(
                    "peer-confirm delivery failed (status=%s, err=%s) — "
                    "B will retry next peer-accept replay",
                    result.status_code,
                    result.error,
                )

        # Fire the local-side ``PairingConfirmed`` so downstream
        # subscribers (capabilities announcement, peer-directory mirror,
        # …) see the new confirmed peer on both sides of the handshake.
        # Without this the initiator side would silently miss the event
        # — the responder publishes it in ``peer_confirm`` already, so
        # the receiver-side path is unchanged.
        if self._bus is not None:
            await self._bus.publish(
                PairingConfirmed(instance_id=peer_instance_id),
            )

        log.info("Pairing confirmed: instance_id=%s", peer_instance_id)
        return confirmed
