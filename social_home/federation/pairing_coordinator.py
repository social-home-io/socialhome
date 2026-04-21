"""Pairing coordinator — §11 QR-code pairing handshake.

Extracted from :class:`FederationService` so the pairing flow can be
exercised independently of outbound delivery + inbound validation.

The flow is three steps:

1. :meth:`initiate` — local admin generates a QR payload (token,
   identity_pk, dh_pk, webhook_url, expires_at) for the peer to scan.
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

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    derive_instance_id,
    generate_x25519_keypair,
    random_token,
    x25519_exchange,
)
from ..utils.datetime import parse_iso8601_strict
from .crypto_suite import DEFAULT_SUITE, negotiate
from ..domain.federation import (
    InstanceSource,
    PairingSession,
    PairingStatus,
    RemoteInstance,
)
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo


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
    )

    def __init__(
        self,
        federation_repo: AbstractFederationRepo,
        key_manager: KeyManager,
        own_identity_pk: bytes,
        own_pq_pk: bytes | None = None,
        own_sig_suite: str = DEFAULT_SUITE,
    ) -> None:
        self._repo = federation_repo
        self._key_manager = key_manager
        self._own_identity_pk = own_identity_pk
        self._own_pq_pk = own_pq_pk
        self._own_sig_suite = own_sig_suite

    async def initiate(self, webhook_url: str) -> dict:
        """Generate a QR payload for the §11 pairing handshake.

        The payload advertises this instance's supported ``sig_suite`` and
        — if configured for hybrid — its post-quantum public key. The
        peer picks the intersection via :func:`crypto_suite.negotiate`
        during :meth:`accept`.
        """
        token = random_token(24)
        dh_kp = generate_x25519_keypair()
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()

        session = PairingSession(
            token=token,
            own_identity_pk=self._own_identity_pk.hex(),
            own_dh_pk=dh_kp.public_key.hex(),
            own_dh_sk=dh_kp.private_key.hex(),
            webhook_url=webhook_url,
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
        payload: dict = {
            "token": token,
            "instance_id": own_instance_id,
            "identity_pk": self._own_identity_pk.hex(),
            "dh_pk": dh_kp.public_key.hex(),
            "webhook_url": webhook_url,
            "expires_at": expires_at,
            "sig_suite": self._own_sig_suite,
        }
        if self._own_pq_pk is not None:
            payload["pq_algorithm"] = "mldsa65"
            payload["pq_identity_pk"] = self._own_pq_pk.hex()
        return payload

    async def accept(self, qr_payload: dict) -> dict:
        """Process an incoming QR scan."""
        _require_fields(
            qr_payload,
            "token",
            "identity_pk",
            "dh_pk",
            "webhook_url",
        )

        token: str = qr_payload["token"]
        peer_identity_pk_hex: str = qr_payload["identity_pk"]
        peer_dh_pk_hex: str = qr_payload["dh_pk"]
        peer_webhook_url: str = qr_payload["webhook_url"]

        # Generate our ephemeral DH keypair.
        own_dh_kp = generate_x25519_keypair()

        # Derive session keys via ECDH.
        peer_dh_pk_bytes = bytes.fromhex(peer_dh_pk_hex)
        shared_secret = x25519_exchange(own_dh_kp.private_key, peer_dh_pk_bytes)

        # Two directional keys: self→remote and remote→self.
        def _derive_key(info: bytes) -> bytes:
            hkdf = HKDF(
                algorithm=_hashes.SHA256(),
                length=32,
                salt=None,
                info=info,
            )
            return hkdf.derive(shared_secret)

        key_self_to_remote = _derive_key(b"social-home/session/self-to-remote")
        key_remote_to_self = _derive_key(b"social-home/session/remote-to-self")

        # Encrypt both keys under the KEK.
        key_self_enc = self._key_manager.encrypt(key_self_to_remote)
        key_remote_enc = self._key_manager.encrypt(key_remote_to_self)

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

        # Generate a webhook ID for the peer to POST to.
        local_webhook_id = secrets.token_urlsafe(24)

        # 6-digit SAS verification code.
        verification_code = str(secrets.randbelow(10**SAS_DIGITS)).zfill(SAS_DIGITS)

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=PAIRING_TTL_SECONDS)).isoformat()

        # Store the in-progress pairing session for confirm_pairing.
        session = PairingSession(
            token=token,
            own_identity_pk=self._own_identity_pk.hex(),
            own_dh_pk=own_dh_kp.public_key.hex(),
            own_dh_sk=own_dh_kp.private_key.hex(),
            peer_identity_pk=peer_identity_pk_hex,
            peer_dh_pk=peer_dh_pk_hex,
            peer_webhook_url=peer_webhook_url,
            webhook_url=qr_payload.get("webhook_url", ""),
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

        # Persist a provisional RemoteInstance.
        remote_inst = RemoteInstance(
            id=peer_instance_id,
            display_name=qr_payload.get("display_name", peer_instance_id[:8]),
            remote_identity_pk=peer_identity_pk_hex,
            key_self_to_remote=key_self_enc,
            key_remote_to_self=key_remote_enc,
            remote_webhook_url=peer_webhook_url,
            local_webhook_id=local_webhook_id,
            status=PairingStatus.PENDING_RECEIVED,
            source=InstanceSource.MANUAL,
            remote_pq_algorithm=str(peer_pq_alg) if peer_pq_alg else None,
            remote_pq_identity_pk=str(peer_pq_pk) if peer_pq_pk else None,
            sig_suite=negotiated,
            paired_at=now.isoformat(),
        )
        await self._repo.save_instance(remote_inst)

        return {
            "verification_code": verification_code,
            "token": token,
            "local_webhook_id": local_webhook_id,
            "own_dh_pk": own_dh_kp.public_key.hex(),
        }

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
        confirmed = RemoteInstance(
            id=instance.id,
            display_name=instance.display_name,
            remote_identity_pk=instance.remote_identity_pk,
            key_self_to_remote=instance.key_self_to_remote,
            key_remote_to_self=instance.key_remote_to_self,
            remote_webhook_url=instance.remote_webhook_url,
            local_webhook_id=instance.local_webhook_id,
            status=PairingStatus.CONFIRMED,
            source=instance.source,
            remote_pq_algorithm=instance.remote_pq_algorithm,
            remote_pq_identity_pk=instance.remote_pq_identity_pk,
            sig_suite=instance.sig_suite,
            paired_at=instance.paired_at,
            created_at=instance.created_at,
            last_reachable_at=instance.last_reachable_at,
            unreachable_since=instance.unreachable_since,
        )
        await self._repo.save_instance(confirmed)
        await self._repo.delete_pairing(token)

        log.info("Pairing confirmed: instance_id=%s", peer_instance_id)
        return confirmed
