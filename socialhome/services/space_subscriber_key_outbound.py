"""Seed-holder producer for the Phase-5b subscriber content-key handoff.

When a household subscribes to a PUBLIC/GLOBAL space at the GFS and a
seed-holder (the owner or a delegated admin) is online, the GFS notifies
the owner with a ``new_subscriber`` frame carrying the subscriber's
published identity + key-wrap material. This service answers it: it seals
the per-space content key to the subscriber's static X25519 key-wrap pubkey
and relays the sealed envelope back through the content-blind GFS as a
space-authority-signed ``space_subscriber_key_handoff`` event.

The point is to let a GFS subscriber DECRYPT the Phase-5a public-content
relay (which it receives but currently drops, having no key) — delivered
GFS-blind so the GFS (and any non-target subscriber the GFS fans it out to)
never sees the content key.

Pipeline (fail-closed at every step):

1. **Gate on seed + tier + readability.** Act only if this household holds the
   space seed (``get_space_seed`` non-None → owner or delegated admin), the
   space is PUBLIC/GLOBAL, and its ``features.allow_subscribers`` is ON. A
   non-seed-holder can't authority-sign; a private/household space is never
   GFS-discoverable, so its key must never leave via the GFS; and a
   public/global space with followers OFF is *listed* for discovery but is
   **not publicly readable** — only members get content, so its key is never
   sealed to a mere subscriber. The readability flag is NOT ``join_mode`` (an
   ``invite_only`` space with followers on is a broadcast space), and it is
   the OWNER's alone — a seed-holding delegated admin can sign a config edit
   but cannot flip this one.
2. **Verify the key-wrap binding (anti-substitution).** The key-wrap pubkey
   is learned *from the GFS*; a malicious GFS could substitute one it
   controls and read the sealed key. ``verify_keywrap_binding`` binds the
   key-wrap key to the subscriber identity end-to-end — if it fails, DROP and
   never seal. A subscriber that shipped no key-wrap key (older HFS) is
   skipped (it can't be sealed-to yet — graceful, no relay).
3. **Export the current content key** and build the same
   ``{space_content_key: {key_suite, epoch, key_base64, rotated_by}}`` meta
   that :func:`apply_space_content_key_from_metadata` consumes (mirrors the
   §D1b / rekey distribution shape).
4. **Seal** the meta to the verified key-wrap pubkey
   (:func:`seal_to_keywrap`) → ``{kem_suite, eph_pk, ciphertext}``.
5. **Wrap + authority-sign** ``{space_id, sealed}`` with the space seed under
   ``space_subscriber_key_handoff`` and relay via the existing GFS publish
   path. The GFS authorizes it against the TOFU-pinned space pubkey (same
   authority-relay path as ``space_post_public``). The envelope is
   **identity-free**: it does NOT name the target household, so neither the
   GFS nor the other subscribers it is fanned out to learn who is being
   onboarded — the **seal is the gate**, and a household that can't
   ``open_keywrap`` it drops it quietly. (Receivers still accept the legacy
   ``target_instance_id``-bearing shape from an older seed-holder.)

Phase 5b-c adds the owner-offline RECONCILE (:meth:`reconcile`): on each GFS-WS
(re)connect, a seed-holder pulls the GFS subscriber list for every local
PUBLIC/GLOBAL space it holds the seed of + has published to that GFS, and runs
the SAME verified-seal+relay for each subscriber. This catches handoffs missed
while no seed-holder was online (e.g. the owner was offline at subscribe time
and only a delegated admin is up now) and is idempotent — the subscriber's
key import is per-epoch idempotent, so re-sealing is harmless.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..domain.space import PUBLIC_SPACE_TIERS
from ..federation.keywrap_seal import seal_to_keywrap, verify_keywrap_binding
from ..services.space_crypto_service import KEY_SUITE_AESGCM_256

if TYPE_CHECKING:
    from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


class SpaceSubscriberKeyOutbound:
    """``new_subscriber`` GFS frame → sealed content-key handoff producer."""

    __slots__ = (
        "_spaces",
        "_crypto",
        "_gfs",
        "_own_instance_id",
        "_gfs_conn_repo",
        "_http_session",
    )

    def __init__(
        self,
        *,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        gfs_service: "GfsConnectionService",
    ) -> None:
        self._spaces = space_repo
        self._crypto = space_crypto
        self._gfs = gfs_service
        self._own_instance_id: str = ""
        # Reconcile-only deps, wired lazily (the GFS-connection repo + the
        # shared HTTP session aren't available at the same step as the rest).
        # Until attached, :meth:`reconcile` no-ops.
        self._gfs_conn_repo: "AbstractGfsConnectionRepo | None" = None
        self._http_session: aiohttp.ClientSession | None = None

    def attach_identity(self, *, own_instance_id: str) -> None:
        self._own_instance_id = own_instance_id

    def attach_reconcile_context(
        self,
        *,
        gfs_conn_repo: "AbstractGfsConnectionRepo",
        http_session: aiohttp.ClientSession,
    ) -> None:
        """Wire the Phase-5b-c reconcile dependencies (the GFS-connection repo
        for enumerating published spaces + the shared HTTP session for pulling
        the subscriber list). Without this, :meth:`reconcile` no-ops."""
        self._gfs_conn_repo = gfs_conn_repo
        self._http_session = http_session

    async def handle(self, frame: dict[str, Any]) -> None:
        """Handle one GFS ``new_subscriber`` frame. Never raises."""
        if frame.get("type") != "new_subscriber":
            return
        space_id = str(frame.get("space_id") or "")
        sub = frame.get("subscriber")
        if not space_id or not isinstance(sub, dict):
            return

        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        # A public/global space with ``allow_subscribers`` OFF is LISTED in
        # the GFS directory (that's how people discover it and get invited)
        # but is NOT publicly readable: only members get content. Never seal
        # the content key to a mere subscriber — no key ⇒ the relayed
        # ciphertext (which is itself suppressed, see space_public_outbound)
        # is unopenable.
        if not space.features.allow_subscribers:
            log.debug(
                "space_subscriber_key.outbound: space %s does not allow "
                "subscribers — not publicly readable, skipping handoff",
                space_id,
            )
            return
        # Only a seed-holder (owner or delegated admin) can authority-sign.
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return
        if not self._own_instance_id:
            log.warning(
                "space_subscriber_key.outbound: identity not attached — "
                "dropping handoff for space %s",
                space_id,
            )
            return

        await self._seal_and_relay(
            space_id=space_id,
            seed=seed,
            target_instance_id=str(sub.get("instance_id") or ""),
            identity_pub_hex=str(sub.get("identity_public_key") or ""),
            keywrap_pub_hex=str(sub.get("keywrap_public_key") or ""),
            keywrap_sig=str(sub.get("keywrap_sig") or ""),
        )

    async def _seal_and_relay(
        self,
        *,
        space_id: str,
        seed: bytes,
        target_instance_id: str,
        identity_pub_hex: str,
        keywrap_pub_hex: str,
        keywrap_sig: str,
    ) -> None:
        """Verify the subscriber's key-wrap binding, seal the current content
        key to it, authority-sign, and relay one ``space_subscriber_key_handoff``.

        Shared by the notify-driven :meth:`handle` and the Phase-5b-c
        :meth:`reconcile` so both run the identical anti-substitution gate +
        seal + sign + relay. Caller guarantees the space is a seed-held
        PUBLIC/GLOBAL space and ``self._own_instance_id`` is set. Never raises —
        a per-subscriber failure is logged and swallowed."""
        if not target_instance_id or not identity_pub_hex:
            return
        # A subscriber that published no key-wrap key (older HFS) can't be
        # sealed-to — skip gracefully (no relay).
        if not keywrap_pub_hex or not keywrap_sig:
            log.info(
                "space_subscriber_key.outbound: subscriber %s shipped no "
                "key-wrap key — skipping handoff for space %s",
                target_instance_id,
                space_id,
            )
            return
        try:
            identity_pub = bytes.fromhex(identity_pub_hex)
            keywrap_pub = bytes.fromhex(keywrap_pub_hex)
        except ValueError:
            log.warning(
                "space_subscriber_key.outbound: malformed subscriber keys for "
                "%s on space %s — dropped",
                target_instance_id,
                space_id,
            )
            return
        # ANTI-SUBSTITUTION GATE: verify the GFS-served key-wrap pubkey is
        # genuinely bound to the subscriber identity (derive_instance_id +
        # self-sig + 32-byte) BEFORE sealing. A swapped key (e.g. a malicious
        # GFS slipping in a key it controls) fails here → DROP, never seal.
        if not verify_keywrap_binding(
            instance_id=target_instance_id,
            identity_pub=identity_pub,
            keywrap_pub=keywrap_pub,
            keywrap_sig=keywrap_sig,
        ):
            log.warning(
                "space_subscriber_key.outbound: key-wrap binding FAILED for "
                "subscriber %s on space %s — refusing to seal (possible GFS "
                "substitution)",
                target_instance_id,
                space_id,
            )
            return

        exported = await self._crypto.export_current_key(space_id)
        if exported is None:
            log.warning(
                "space_subscriber_key.outbound: no content key for space %s — "
                "cannot hand off to %s",
                space_id,
                target_instance_id,
            )
            return
        epoch, raw_key = exported
        # Mirror the meta apply_space_content_key_from_metadata consumes (and
        # the §D1b / rekey distribution shape).
        meta = {
            "space_content_key": {
                "epoch": epoch,
                "key_suite": KEY_SUITE_AESGCM_256,
                "key_base64": base64.b64encode(raw_key).decode("ascii"),
                "rotated_by": self._own_instance_id,
            }
        }
        sealed = seal_to_keywrap(
            recipient_keywrap_pub=keywrap_pub,
            plaintext=json.dumps(meta).encode("utf-8"),
        )
        # IDENTITY-FREE WIRE SHAPE: the envelope names nobody. The target is
        # used locally (to pick + verify the key-wrap key we seal to, and for
        # our own logging) but never travels: the SEAL is the gate, so only
        # the household holding the matching key-wrap private key can open it.
        # Shipping ``target_instance_id`` would tell the GFS — and every other
        # subscriber the GFS fans this out to — which household is being
        # onboarded to which space.
        envelope: dict = {
            "space_id": space_id,
            "sealed": sealed,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
            space_id=space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            await self._gfs.publish_space_event(
                space_id=space_id,
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
                payload=envelope,
                from_instance=self._own_instance_id,
            )
        except Exception:
            log.exception(
                "space_subscriber_key.outbound: relay failed for space=%s target=%s",
                space_id,
                target_instance_id,
            )

    async def reconcile(self, gfs_id: str) -> None:
        """Owner-offline RECONCILE (Phase-5b-c). Never raises.

        Triggered on each GFS-WS (re)connect. For every PUBLIC/GLOBAL space
        this household (a) holds the seed for and (b) has published to *gfs_id*,
        pull the GFS subscriber list under a space-authority-signed query and
        re-seal the content key to each subscriber via :meth:`_seal_and_relay`.
        This delivers the key to subscribers whose owner was offline at
        subscribe time (any delegated admin can do it) and catches notifies
        missed while no seed-holder was online. Idempotent — the subscriber's
        import is per-epoch idempotent, so re-sealing is harmless.

        Fail-soft at every level: a missing reconcile context, an unknown /
        inactive GFS, a per-space transport error, or a per-subscriber seal
        failure is logged and skipped — never aborts the whole pass.
        """
        if (
            self._gfs_conn_repo is None
            or self._http_session is None
            or not self._own_instance_id
        ):
            return
        # Fail-soft on the lookups too, so the "never raises" contract holds on
        # the unwrapped on_connected invocation paths (a repo hiccup here must
        # not propagate into the reconnect loop).
        try:
            conn = await self._gfs_conn_repo.get(gfs_id)
            if conn is None or conn.status != "active":
                return
            publications = await self._gfs_conn_repo.list_publications(gfs_id)
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: lookup failed for gfs %s", gfs_id
            )
            return
        for pub in publications:
            try:
                await self._reconcile_space(conn.inbox_url, pub.space_id)
            except Exception:
                log.exception(
                    "space_subscriber_key.reconcile: space %s on gfs %s failed",
                    pub.space_id,
                    gfs_id,
                )

    async def reconcile_space(self, gfs_id: str, space_id: str) -> None:
        """Reconcile ONE space on ONE GFS. Never raises.

        Narrow entry point for the forward-secrecy rekey path: after a member
        removal rotates the space content key, the new epoch must reach the
        GFS subscribers too (they are not member households, so the
        ``space_instances`` fan-out skips them). Applies exactly the guards
        :meth:`reconcile` applies — reconcile context wired, GFS known and
        active, space actually published to it — and then defers to the shared
        :meth:`_reconcile_space`, which enforces the PUBLIC/GLOBAL tier +
        seed-holder gates.
        """
        if (
            self._gfs_conn_repo is None
            or self._http_session is None
            or not self._own_instance_id
        ):
            return
        try:
            conn = await self._gfs_conn_repo.get(gfs_id)
            if conn is None or conn.status != "active":
                return
            publications = await self._gfs_conn_repo.list_publications(gfs_id)
            if not any(pub.space_id == space_id for pub in publications):
                return
            await self._reconcile_space(conn.inbox_url, space_id)
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: space %s on gfs %s failed",
                space_id,
                gfs_id,
            )

    async def reconcile_space_everywhere(self, space_id: str) -> None:
        """Re-seal one space's current content key to its subscribers on every
        GFS it is published to. Never raises.

        Called after a content-key rotation so a subscriber isn't dark until
        the next GFS reconnect.
        """
        if self._gfs_conn_repo is None:
            return
        try:
            publications = await self._gfs_conn_repo.list_publications_for_space(
                space_id
            )
        except Exception:
            log.exception(
                "space_subscriber_key.reconcile: publication lookup failed for "
                "space %s",
                space_id,
            )
            return
        for pub in publications:
            await self.reconcile_space(pub.gfs_connection_id, space_id)

    async def _reconcile_space(self, gfs_base_url: str, space_id: str) -> None:
        """Pull + re-seal for one published space. Skips a space this household
        doesn't hold the seed for, or a non-PUBLIC/GLOBAL space (its key must
        never leave via the GFS). Only reached after :meth:`reconcile` has
        confirmed the HTTP session is wired."""
        if self._http_session is None:
            return
        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        # Subscribers off ⇒ listed for discovery but not publicly readable.
        # Gated here (before the subscriber-list round-trip) so we never even
        # ask the GFS who subscribed to a space whose content nobody may read.
        if not space.features.allow_subscribers:
            log.debug(
                "space_subscriber_key.reconcile: space %s does not allow "
                "subscribers — not publicly readable, skipping",
                space_id,
            )
            return
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return

        ts = datetime.now(timezone.utc).isoformat()
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
            space_id=space_id,
            payload={"space_id": space_id, "ts": ts},
            space_seed=seed,
        )
        params = {
            "ts": ts,
            "authority_sig": sig["authority_sig"],
            "authority_sig_suite": sig["authority_sig_suite"],
        }
        url = f"{gfs_base_url}/gfs/spaces/{space_id}/subscribers"
        async with self._http_session.get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                log.warning(
                    "space_subscriber_key.reconcile: GFS returned HTTP %d for "
                    "space %s subscriber list",
                    resp.status,
                    space_id,
                )
                return
            data = await resp.json()
        subscribers = data.get("subscribers") if isinstance(data, dict) else None
        if not isinstance(subscribers, list):
            return
        for sub in subscribers:
            if not isinstance(sub, dict):
                continue
            await self._seal_and_relay(
                space_id=space_id,
                seed=seed,
                target_instance_id=str(sub.get("instance_id") or ""),
                identity_pub_hex=str(sub.get("identity_public_key") or ""),
                keywrap_pub_hex=str(sub.get("keywrap_public_key") or ""),
                keywrap_sig=str(sub.get("keywrap_sig") or ""),
            )
