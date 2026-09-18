"""Subscriber consumer for the Phase-5b subscriber content-key handoff.

Handles the ``{type:"relay", event_type:"space_subscriber_key_handoff",
payload:<envelope>}`` frame the GFS pushes after a seed-holder seals the
per-space content key to this subscriber (see
:class:`socialhome.services.space_subscriber_key_outbound.SpaceSubscriberKeyOutbound`).
After importing the key, this household can decrypt the Phase-5a public
relay it previously received-but-dropped (no backfill is built here — a
later relay/backfill decodes; that is a follow-up).

Pipeline — fail-closed, the relay and the GFS are NEVER trusted:

1. **Target gate.** The GFS fans the handoff out to EVERY subscriber of the
   space. The shape current senders emit names NOBODY — the GFS never learns
   which household a key is for — and the **seal itself is the gate**:
   step 3's unseal failure means "not for us" and is dropped quietly at DEBUG
   (in a space with N subscribers every household receives the other N-1
   handoffs; a WARNING per drop would be pure noise). The LEGACY shape from an
   older seed-holder still names a ``target_instance_id``; it is still
   accepted, and then drops unless the target is us (a non-target also can't
   ``open_keywrap`` it, but the explicit gate avoids needless work + makes the
   intent auditable).
2. **Local space + authority verify.** Load the locally-mirrored space; drop
   if we don't mirror it or it carries no pinned pubkey. Re-verify the
   space-authority Ed25519 signature against ``spaces.identity_public_key``
   (the GFS already verified it, but the receiver re-checks first-hand) — a
   failed/forged signature → drop (WARNING).
3. **Unseal.** ``open_keywrap`` the sealed payload with our key-wrap private
   key. A payload sealed to a different key-wrap key (we can't open) raises
   ``InvalidTag``; a malformed wire shape / unknown KEM suite raises
   ``ValueError`` — both are dropped gracefully.
4. **Import.** Parse the content-key meta and feed it to
   :func:`apply_space_content_key_from_metadata`, which re-wraps under the
   local KEK via ``import_key`` (idempotent per epoch — a double delivery
   imports once).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..federation.keywrap_seal import UnsupportedKemSuite, open_keywrap
from .space_service import apply_space_content_key_from_metadata

if TYPE_CHECKING:
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


class SpaceSubscriberKeyInbound:
    """GFS-relay → local content-key import consumer for a subscriber."""

    __slots__ = ("_spaces", "_crypto", "_own_instance_id", "_keywrap_private_key")

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._own_instance_id: str = ""
        self._keywrap_private_key: bytes = b""

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        keywrap_private_key: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._keywrap_private_key = keywrap_private_key

    async def handle(self, frame: dict[str, Any]) -> None:
        """Dispatch one GFS relay frame. Non-handoff frames are ignored so
        this can sit on the generic relay channel. Never raises.

        The frame carries no household identity — an outer ``from_instance``
        from an older GFS is neither read nor logged.
        """
        if frame.get("event_type") != AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF:
            return
        envelope = frame.get("payload")
        if not isinstance(envelope, dict):
            return
        if not self._own_instance_id or not self._keywrap_private_key:
            log.warning(
                "space_subscriber_key.inbound: identity not attached — dropping"
            )
            return

        space_id = str(envelope.get("space_id") or "")
        target = str(envelope.get("target_instance_id") or "")
        if not space_id:
            return
        # Target gate: the GFS fans the handoff to every subscriber. The
        # current wire shape is untargeted (no ``target_instance_id`` —
        # nothing on the wire says which household the key is for) and falls
        # through to the unseal, which is then the gate. A LEGACY payload from
        # an older seed-holder still names its target; honour that gate.
        if target and target != self._own_instance_id:
            log.debug(
                "space_subscriber_key.inbound: handoff for %s not us (%s) — dropped",
                target,
                self._own_instance_id,
            )
            return

        space = await self._spaces.get(space_id)
        if space is None or not space.identity_public_key:
            log.warning(
                "space_subscriber_key.inbound: no local space / pubkey for %s "
                "— dropped",
                space_id,
            )
            return
        if not self._verify_authority(space_id, envelope, space.identity_public_key):
            log.warning(
                "space_subscriber_key.inbound: authority signature failed for space %s",
                space_id,
            )
            return

        sealed = envelope.get("sealed")
        if not isinstance(sealed, dict):
            log.warning(
                "space_subscriber_key.inbound: malformed sealed payload for space %s",
                space_id,
            )
            return
        try:
            plaintext = open_keywrap(
                sealed=sealed,
                recipient_keywrap_priv=self._keywrap_private_key,
            )
        except (InvalidTag, UnsupportedKemSuite, ValueError) as exc:
            # Sealed to a key-wrap key we don't hold (InvalidTag), an unknown
            # KEM suite (no fallback), or a malformed wire shape — drop.
            # Untargeted: the failure IS the "not for us" signal (including
            # for a handoff we sealed ourselves), and every subscriber sees
            # every other subscriber's handoff — so DEBUG, not INFO/WARNING.
            log.log(
                logging.DEBUG if not target else logging.INFO,
                "space_subscriber_key.inbound: cannot open sealed key for space %s: %s",
                space_id,
                exc,
            )
            return
        try:
            meta = json.loads(plaintext)
        except json.JSONDecodeError:
            log.warning(
                "space_subscriber_key.inbound: undecodable meta for space %s",
                space_id,
            )
            return
        if not isinstance(meta, dict):
            return
        # import_key (via apply_*) is idempotent per epoch — a double delivery
        # imports once. Guard the import so an unknown/unsupported key_suite (or
        # any malformed key-metadata) drops gracefully rather than escaping —
        # the handler's "never raises" contract holds even for a (authority-
        # verified-but) odd payload.
        try:
            await apply_space_content_key_from_metadata(
                space_id,
                meta=meta,
                space_crypto_service=self._crypto,
            )
        except (ValueError, KeyError) as exc:
            log.warning(
                "space_subscriber_key.inbound: key import failed for space %s: %s",
                space_id,
                exc,
            )
            return
        log.info(
            "space_subscriber_key.inbound: imported content key for space %s",
            space_id,
        )

    def _verify_authority(
        self, space_id: str, envelope: dict, space_public_key_hex: str
    ) -> bool:
        try:
            return verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
                space_id=space_id,
                payload=strip_authority_sig_fields(envelope),
                authority_sig=str(envelope.get("authority_sig") or ""),
                authority_sig_suite=str(envelope.get("authority_sig_suite") or ""),
                space_public_key=bytes.fromhex(space_public_key_hex),
            )
        except UnsupportedAuthoritySuite, ValueError:
            # Unknown suite (no default fallback) or malformed pinned pubkey
            # → unverifiable, fail-closed.
            return False
