"""Transitive auto-pair coordinator (§11 extension — "simple pairing").

"If A is paired with B, and B is paired with C, A can pair with C via
B. The routing happens without any admin approval at B — B just
auto-forwards. C's admin still reviews and approves each incoming
request, but the approval is a one-click action (no QR, no SAS),
because B's vouch signature already attests that A's identity is the
one B trusts."

Wire protocol (3 parties: A=originator, B=vouching peer, C=target):

    A → B    PAIRING_INTRO_AUTO
               {target_id, a_dh_pk, a_inbox_url, ts, nonce, token}

    B → C    PAIRING_INTRO_AUTO
               {from_a_id, from_a_pk, from_a_inbox_url, from_a_dh_pk,
                via_b_id, vouch_sig, ts, nonce, token}
             where vouch_sig = sign(B_seed,
                 a_id || a_pk || a_inbox_url || a_dh_pk || c_id || ts || nonce)

    C: queues the request in :class:`AutoPairInbox` + notifies admin.
       Admin clicks approve → coordinator.finalize_pending(request_id)
       builds the ack and sends it back to A:

    C → A    PAIRING_INTRO_AUTO_ACK
               {a_id, c_id, c_pk, c_inbox_url, c_dh_pk,
                via_b_id, vouch_sig, ack_sig, ts, nonce, token}
             where ack_sig = sign(C_seed,
                 a_id || a_dh_pk || c_id || c_dh_pk || ts || nonce)

Both sides verify every signature against identity public keys they
already trust (via their own direct pairing with B). Session keys are
derived from ephemeral X25519 ECDH — B sees the public halves but
cannot derive the secret.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import hashes as _hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..crypto import (
    derive_instance_id,
    generate_x25519_keypair,
    random_token,
    sign_ed25519,
    verify_ed25519,
    x25519_exchange,
)
from ..domain.events import AutoPairRequestIncoming, PairingConfirmed
from ..domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from ..infrastructure.event_bus import EventBus
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo
from ..services.auto_pair_inbox import AutoPairInbox

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from .federation_service import FederationService

log = logging.getLogger(__name__)

#: Anti-replay window — an intro nonce is rejected if its timestamp
#: is older than this threshold. Generous enough to survive clock
#: skew + queue delay.
INTRO_TTL_SECONDS = 300


@dataclass(slots=True)
class _PendingAutoSession:
    """Originator-side (A) record we keep while waiting for C's ack."""

    token: str
    target_instance_id: str
    via_instance_id: str
    dh_sk: bytes
    dh_pk_hex: str
    ts: str
    nonce: str


def _vouch_blob(
    *,
    a_id: str,
    a_pk_hex: str,
    a_inbox_url: str,
    a_dh_pk_hex: str,
    c_id: str,
    ts: str,
    nonce: str,
) -> bytes:
    return "|".join(
        [
            "vouch/v1",
            a_id,
            a_pk_hex,
            a_inbox_url,
            a_dh_pk_hex,
            c_id,
            ts,
            nonce,
        ]
    ).encode("utf-8")


def _ack_blob(
    *,
    a_id: str,
    a_dh_pk_hex: str,
    c_id: str,
    c_dh_pk_hex: str,
    ts: str,
    nonce: str,
) -> bytes:
    return "|".join(
        [
            "ack/v1",
            a_id,
            a_dh_pk_hex,
            c_id,
            c_dh_pk_hex,
            ts,
            nonce,
        ]
    ).encode("utf-8")


def _derive_session_keys(
    own_sk: bytes,
    peer_pk_hex: str,
) -> tuple[bytes, bytes]:
    """ECDH → HKDF → two directional 32-byte keys."""
    shared = x25519_exchange(own_sk, bytes.fromhex(peer_pk_hex))

    def _derive(info: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=_hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        )
        return hkdf.derive(shared)

    return (
        _derive(b"socialhome/session/self-to-remote"),
        _derive(b"socialhome/session/remote-to-self"),
    )


class AutoPairCoordinator:
    """Coordinator for the three-party transitive auto-pair flow."""

    __slots__ = (
        "_repo",
        "_key_manager",
        "_bus",
        "_federation",
        "_own_identity_seed",
        "_own_identity_pk",
        "_pending",
        "_inbox",
    )

    def __init__(
        self,
        *,
        federation_repo: AbstractFederationRepo,
        key_manager: KeyManager,
        bus: EventBus,
        federation_service: "FederationService",
        own_identity_seed: bytes,
        own_identity_pk: bytes,
        inbox: AutoPairInbox,
    ) -> None:
        self._repo = federation_repo
        self._key_manager = key_manager
        self._bus = bus
        self._federation = federation_service
        self._own_identity_seed = own_identity_seed
        self._own_identity_pk = own_identity_pk
        self._pending: dict[str, _PendingAutoSession] = {}
        self._inbox = inbox

    # ── Originator (A) ─────────────────────────────────────────────────

    async def request_via(
        self,
        *,
        via_instance_id: str,
        target_instance_id: str,
        target_display_name: str,
    ) -> dict:
        via = await self._repo.get_instance(via_instance_id)
        if via is None or via.status is not PairingStatus.CONFIRMED:
            raise ValueError(
                "vouching peer must be a confirmed paired instance",
            )
        if target_instance_id == derive_instance_id(self._own_identity_pk):
            raise ValueError("cannot auto-pair with yourself")
        existing = await self._repo.get_instance(target_instance_id)
        if existing is not None and existing.status is PairingStatus.CONFIRMED:
            raise ValueError("already paired with this instance")

        dh_kp = generate_x25519_keypair()
        token = random_token(16)
        nonce = secrets.token_hex(16)
        ts = datetime.now(timezone.utc).isoformat()

        a_inbox_url = (
            self._federation.own_inbox_url
            if hasattr(self._federation, "own_inbox_url")
            else ""
        )
        self._pending[token] = _PendingAutoSession(
            token=token,
            target_instance_id=target_instance_id,
            via_instance_id=via_instance_id,
            dh_sk=dh_kp.private_key,
            dh_pk_hex=dh_kp.public_key.hex(),
            ts=ts,
            nonce=nonce,
        )

        provisional = RemoteInstance(
            id=target_instance_id,
            display_name=target_display_name or target_instance_id[:8],
            remote_identity_pk="",
            key_self_to_remote="",
            key_remote_to_self="",
            remote_inbox_url="",
            local_inbox_id=secrets.token_urlsafe(24),
            status=PairingStatus.PENDING_SENT,
            source=InstanceSource.MANUAL,
            relay_via=via_instance_id,
            paired_at=ts,
        )
        await self._repo.save_instance(provisional)

        payload = {
            "target_id": target_instance_id,
            "a_dh_pk": dh_kp.public_key.hex(),
            "a_inbox_url": a_inbox_url,
            "ts": ts,
            "nonce": nonce,
            "token": token,
        }
        await self._federation.send_event(
            to_instance_id=via_instance_id,
            event_type=FederationEventType.PAIRING_INTRO_AUTO,
            payload=payload,
        )
        return {"status": "sent", "token": token}

    # ── Vouching peer (B) ──────────────────────────────────────────────

    async def on_intro_from_peer(self, event: "FederationEvent") -> None:
        """B side: auto-forwards the signed envelope without admin approval."""
        p = event.payload
        target_id = str(p.get("target_id") or "")
        a_inbox_url = str(p.get("a_inbox_url") or "")
        a_dh_pk_hex = str(p.get("a_dh_pk") or "")
        ts = str(p.get("ts") or "")
        nonce = str(p.get("nonce") or "")
        token = str(p.get("token") or "")
        if not all([target_id, a_dh_pk_hex, ts, nonce, token]):
            log.debug("PAIRING_INTRO_AUTO missing fields")
            return
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - t).total_seconds() > INTRO_TTL_SECONDS:
                log.debug("PAIRING_INTRO_AUTO stale ts=%s", ts)
                return
        except ValueError:
            return
        a = await self._repo.get_instance(event.from_instance)
        if a is None or a.status is not PairingStatus.CONFIRMED:
            log.info(
                "auto-pair: refusing vouch for unknown sender=%s",
                event.from_instance,
            )
            return
        c = await self._repo.get_instance(target_id)
        if c is None or c.status is not PairingStatus.CONFIRMED:
            log.info(
                "auto-pair: cannot vouch — target %s is not a paired peer",
                target_id,
            )
            return

        vouch_sig = sign_ed25519(
            self._own_identity_seed,
            _vouch_blob(
                a_id=a.id,
                a_pk_hex=a.remote_identity_pk,
                a_inbox_url=a_inbox_url,
                a_dh_pk_hex=a_dh_pk_hex,
                c_id=c.id,
                ts=ts,
                nonce=nonce,
            ),
        )
        via_b_id = derive_instance_id(self._own_identity_pk)
        forward_payload = {
            "from_a_id": a.id,
            "from_a_pk": a.remote_identity_pk,
            "from_a_inbox_url": a_inbox_url,
            "from_a_dh_pk": a_dh_pk_hex,
            "from_a_display": a.display_name,
            "via_b_id": via_b_id,
            "via_b_display": "",  # C already knows B's display_name
            "vouch_sig": vouch_sig.hex(),
            "ts": ts,
            "nonce": nonce,
            "token": token,
        }
        await self._federation.send_event(
            to_instance_id=c.id,
            event_type=FederationEventType.PAIRING_INTRO_AUTO,
            payload=forward_payload,
        )

    # ── Target (C) ─────────────────────────────────────────────────────

    async def on_intro_at_target(self, event: "FederationEvent") -> None:
        """C side: verify B's vouch, then queue for admin approval."""
        p = event.payload
        # Dispatch: A→B event has ``target_id``; B→C event has ``via_b_id``.
        if "via_b_id" not in p:
            return await self.on_intro_from_peer(event)

        a_id = str(p.get("from_a_id") or "")
        a_pk_hex = str(p.get("from_a_pk") or "")
        a_inbox_url = str(p.get("from_a_inbox_url") or "")
        a_dh_pk_hex = str(p.get("from_a_dh_pk") or "")
        a_display = str(p.get("from_a_display") or "")
        via_b_id = str(p.get("via_b_id") or "")
        vouch_sig_hex = str(p.get("vouch_sig") or "")
        ts = str(p.get("ts") or "")
        nonce = str(p.get("nonce") or "")
        token = str(p.get("token") or "")

        if not all(
            [
                a_id,
                a_pk_hex,
                a_inbox_url,
                a_dh_pk_hex,
                via_b_id,
                vouch_sig_hex,
                ts,
                nonce,
                token,
            ]
        ):
            log.debug("PAIRING_INTRO_AUTO target: missing fields")
            return

        b = await self._repo.get_instance(via_b_id)
        if b is None or b.status is not PairingStatus.CONFIRMED:
            log.info(
                "auto-pair: refusing intro via unknown vouching peer=%s",
                via_b_id,
            )
            return

        # Verify B's vouch signature before we even bother the admin.
        c_id = derive_instance_id(self._own_identity_pk)
        try:
            ok = verify_ed25519(
                bytes.fromhex(b.remote_identity_pk),
                _vouch_blob(
                    a_id=a_id,
                    a_pk_hex=a_pk_hex,
                    a_inbox_url=a_inbox_url,
                    a_dh_pk_hex=a_dh_pk_hex,
                    c_id=c_id,
                    ts=ts,
                    nonce=nonce,
                ),
                bytes.fromhex(vouch_sig_hex),
            )
        except ValueError:
            ok = False
        if not ok:
            log.warning(
                "auto-pair: invalid vouch sig from via=%s",
                via_b_id,
            )
            return

        # Don't duplicate pending entries for the same sender.
        existing = await self._repo.get_instance(a_id)
        if existing is not None and existing.status is PairingStatus.CONFIRMED:
            log.info("auto-pair: already paired with %s", a_id)
            return

        req = self._inbox.enqueue(
            from_a_id=a_id,
            from_a_pk=a_pk_hex,
            from_a_inbox_url=a_inbox_url,
            from_a_dh_pk=a_dh_pk_hex,
            via_b_id=via_b_id,
            vouch_sig=vouch_sig_hex,
            ts=ts,
            nonce=nonce,
            token=token,
            from_a_display=a_display,
            via_b_display=b.display_name,
        )
        await self._bus.publish(
            AutoPairRequestIncoming(
                request_id=req.request_id,
                from_a_id=a_id,
                via_b_id=via_b_id,
                from_a_display=req.from_a_display,
                via_b_display=req.via_b_display,
            )
        )

    async def finalize_pending(self, request_id: str) -> RemoteInstance:
        """C's admin clicked approve — finish the pair instantly."""
        req = self._inbox.pop(request_id)
        if req is None:
            raise KeyError(f"no pending auto-pair request {request_id!r}")

        dh_kp = generate_x25519_keypair()
        k_self_to_remote, k_remote_to_self = _derive_session_keys(
            dh_kp.private_key,
            req.from_a_dh_pk,
        )
        now = datetime.now(timezone.utc).isoformat()
        existing = await self._repo.get_instance(req.from_a_id)
        inbox_id = existing.local_inbox_id if existing else secrets.token_urlsafe(24)
        confirmed = RemoteInstance(
            id=req.from_a_id,
            display_name=req.from_a_display or req.from_a_id[:8],
            remote_identity_pk=req.from_a_pk,
            key_self_to_remote=self._key_manager.encrypt(k_self_to_remote),
            key_remote_to_self=self._key_manager.encrypt(k_remote_to_self),
            remote_inbox_url=req.from_a_inbox_url,
            local_inbox_id=inbox_id,
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
            relay_via=req.via_b_id,
            paired_at=now,
        )
        await self._repo.save_instance(confirmed)
        await self._bus.publish(PairingConfirmed(instance_id=req.from_a_id))

        # Sign ack and send back to A.
        c_id = derive_instance_id(self._own_identity_pk)
        ack_sig = sign_ed25519(
            self._own_identity_seed,
            _ack_blob(
                a_id=req.from_a_id,
                a_dh_pk_hex=req.from_a_dh_pk,
                c_id=c_id,
                c_dh_pk_hex=dh_kp.public_key.hex(),
                ts=req.ts,
                nonce=req.nonce,
            ),
        )
        c_inbox_url = (
            self._federation.own_inbox_url
            if hasattr(self._federation, "own_inbox_url")
            else ""
        )
        ack_payload = {
            "a_id": req.from_a_id,
            "c_id": c_id,
            "c_pk": self._own_identity_pk.hex(),
            "c_inbox_url": c_inbox_url,
            "c_dh_pk": dh_kp.public_key.hex(),
            "via_b_id": req.via_b_id,
            "vouch_sig": req.vouch_sig,
            "ack_sig": ack_sig.hex(),
            "ts": req.ts,
            "nonce": req.nonce,
            "token": req.token,
        }
        await self._federation.send_event(
            to_instance_id=req.from_a_id,
            event_type=FederationEventType.PAIRING_INTRO_AUTO_ACK,
            payload=ack_payload,
        )
        return confirmed

    async def decline_pending(
        self,
        request_id: str,
        *,
        reason: str = "",
    ) -> None:
        req = self._inbox.pop(request_id)
        if req is None:
            raise KeyError(f"no pending auto-pair request {request_id!r}")
        # Best-effort notify A so the spinner can resolve.
        await self._federation.send_event(
            to_instance_id=req.from_a_id,
            event_type=FederationEventType.PAIRING_ABORT,
            payload={
                "token": req.token,
                "reason": reason or "declined_by_target",
            },
        )

    # ── Originator (A) — ack handler ───────────────────────────────────

    async def on_ack_at_originator(self, event: "FederationEvent") -> None:
        p = event.payload
        token = str(p.get("token") or "")
        session = self._pending.pop(token, None)
        if session is None:
            log.debug("PAIRING_INTRO_AUTO_ACK: unknown token=%s", token)
            return
        c_id = str(p.get("c_id") or "")
        c_pk_hex = str(p.get("c_pk") or "")
        c_inbox_url = str(p.get("c_inbox_url") or "")
        c_dh_pk_hex = str(p.get("c_dh_pk") or "")
        via_b_id = str(p.get("via_b_id") or "")
        vouch_sig_hex = str(p.get("vouch_sig") or "")
        ack_sig_hex = str(p.get("ack_sig") or "")
        ts = str(p.get("ts") or "")
        nonce = str(p.get("nonce") or "")

        if c_id != session.target_instance_id:
            log.warning(
                "auto-pair ack: target mismatch session=%s payload=%s",
                session.target_instance_id,
                c_id,
            )
            return
        if via_b_id != session.via_instance_id:
            log.warning("auto-pair ack: via mismatch")
            return
        a_id = derive_instance_id(self._own_identity_pk)
        a_inbox_url = (
            self._federation.own_inbox_url
            if hasattr(self._federation, "own_inbox_url")
            else ""
        )
        b = await self._repo.get_instance(via_b_id)
        if b is None:
            log.warning("auto-pair ack: via-peer %s unknown", via_b_id)
            return
        try:
            vouch_ok = verify_ed25519(
                bytes.fromhex(b.remote_identity_pk),
                _vouch_blob(
                    a_id=a_id,
                    a_pk_hex=self._own_identity_pk.hex(),
                    a_inbox_url=a_inbox_url,
                    a_dh_pk_hex=session.dh_pk_hex,
                    c_id=c_id,
                    ts=ts,
                    nonce=nonce,
                ),
                bytes.fromhex(vouch_sig_hex),
            )
        except ValueError:
            vouch_ok = False
        if not vouch_ok:
            log.warning("auto-pair ack: vouch sig invalid")
            return
        try:
            ack_ok = verify_ed25519(
                bytes.fromhex(c_pk_hex),
                _ack_blob(
                    a_id=a_id,
                    a_dh_pk_hex=session.dh_pk_hex,
                    c_id=c_id,
                    c_dh_pk_hex=c_dh_pk_hex,
                    ts=ts,
                    nonce=nonce,
                ),
                bytes.fromhex(ack_sig_hex),
            )
        except ValueError:
            ack_ok = False
        if not ack_ok:
            log.warning("auto-pair ack: ack sig invalid")
            return

        k_self_to_remote, k_remote_to_self = _derive_session_keys(
            session.dh_sk,
            c_dh_pk_hex,
        )
        existing = await self._repo.get_instance(c_id)
        display_name = existing.display_name if existing else c_id[:8]
        confirmed = RemoteInstance(
            id=c_id,
            display_name=display_name,
            remote_identity_pk=c_pk_hex,
            key_self_to_remote=self._key_manager.encrypt(k_self_to_remote),
            key_remote_to_self=self._key_manager.encrypt(k_remote_to_self),
            remote_inbox_url=c_inbox_url,
            local_inbox_id=(
                existing.local_inbox_id if existing else secrets.token_urlsafe(24)
            ),
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
            relay_via=via_b_id,
            paired_at=datetime.now(timezone.utc).isoformat(),
        )
        await self._repo.save_instance(confirmed)
        await self._bus.publish(PairingConfirmed(instance_id=c_id))
