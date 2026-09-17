"""GFS federation service — instance registration, event relay, subscriptions.

Business logic only — all SQL lives in :mod:`.repositories`. Crypto
helpers are reused from :mod:`socialhome.crypto` (no duplication).

Fan-out delivery is **WebSocket-primary, HTTPS-fallback** (spec §24.12):
if a paired SH instance has an open ``/gfs/ws`` WebSocket, the event is
pushed over that connection; otherwise it falls back to an HTTPS POST
to the subscriber's inbox URL.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
    AUTHORITY_RELAY_EVENT_TYPES,
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..crypto import b64url_decode, verify_ed25519
from ..domain.space import normalize_category
from .domain import (
    ClientInstance,
    GfsSubscriber,
    GfsSubscriberWithKeys,
    GlobalSpace,
)
from .repositories import AbstractGfsFederationRepo

if TYPE_CHECKING:
    from .ws_registry import GfsWebSocketRegistry

log = logging.getLogger(__name__)

#: Storage cap for a published space's ``about_markdown``. Generous for a
#: real "about" blurb; bounds DB growth + the public-page render cost from
#: an oversized publish. The public renderer applies its own (smaller) cap.
MAX_ABOUT_MARKDOWN_CHARS: int = 8000

#: Max display_name length accepted by ``update_instance`` (chars). Mirrors
#: the household-name bound on the HFS side.
MAX_DISPLAY_NAME_CHARS: int = 80

#: Freshness window for the signed instance-update timestamp (seconds). A
#: ``ts`` further than this from now is treated as a replay and rejected —
#: same ±300 s tolerance the §24.11 inbound pipeline uses.
INSTANCE_UPDATE_TS_SKEW_SECONDS: int = 300


class GfsFederationService:
    """Lightweight federation relay for the GFS process.

    Responsible for:
    * Registering/updating client household instances.
    * Verifying Ed25519 signatures on inbound publish requests.
    * Fanning out events to all subscribers (WS push, HTTPS fallback).
    * Managing space subscription lists.
    * Listing all known global spaces.
    """

    __slots__ = ("_repo", "_ws_registry")

    def __init__(
        self,
        repo: AbstractGfsFederationRepo,
        ws_registry: "GfsWebSocketRegistry | None" = None,
    ) -> None:
        self._repo = repo
        self._ws_registry = ws_registry

    async def register_instance(
        self,
        instance_id: str,
        public_key: str,
        inbox_url: str,
        *,
        display_name: str = "",
        auto_accept: bool = False,
        keywrap_public_key: str = "",
        kem_suite: str = "",
        keywrap_sig: str = "",
    ) -> None:
        """Register or update a client household instance.

        ``keywrap_public_key`` + ``kem_suite`` carry the household's published
        X25519 key-wrap pubkey (Phase 5b foundation) — empty for an older HFS
        that ships none, in which case that household simply can't be
        sealed-to yet. ``keywrap_sig`` is the household's self-signature over
        that key-wrap pubkey so a remote sealer can bind it to the household
        identity end-to-end (``verify_keywrap_binding``) and never trust this
        GFS-served value; empty for an older HFS (unsealable, graceful).
        """
        await self._repo.upsert_instance(
            ClientInstance(
                instance_id=instance_id,
                display_name=display_name,
                public_key=public_key,
                inbox_url=inbox_url,
                status="active" if auto_accept else "pending",
                auto_accept=auto_accept,
                keywrap_public_key=keywrap_public_key,
                kem_suite=kem_suite,
                keywrap_sig=keywrap_sig,
            )
        )
        log.debug("GFS: registered instance %s inbox=%s", instance_id, inbox_url)

    async def publish_event(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        from_instance: str,
        signature: str = "",
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> list[str]:
        """Relay an event to all subscribers of *space_id*.

        Validates the Ed25519 *signature* using the public key registered
        for *from_instance*. Returns the list of instance_ids successfully
        notified.

        Raises :class:`PermissionError` when *from_instance* is unknown,
        the signature is missing / malformed / invalid, the space was never
        published, or *from_instance* is not the space's owning instance.
        The signature is MANDATORY: an empty signature is rejected (a
        registered peer must not be able to publish another household's
        space content unsigned).

        Only the space's **owning instance** may relay events for it. The
        GFS has no space-membership roster (it knows registered instances,
        space ``owning_instance``, and subscribers — not who joined), so the
        owning instance is the only relationship it can authoritatively
        check. A non-owner peer that catches a space_id (they travel in
        discovery links) must not be able to inject events into another
        household's space fan-out, even with a signature valid for its own
        key. A multi-publisher model would be a deliberate feature (the GFS
        learning membership), not an implicit allow-any-registered-instance.
        """
        inst = await self._repo.get_instance(from_instance)
        if inst is None:
            raise PermissionError(f"Unknown instance: {from_instance}")

        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "space_id": space_id,
                "event_type": event_type,
                "payload": payload,
                "from_instance": from_instance,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            # A malformed stored pubkey is unverifiable — fail closed as a
            # 403 rather than escaping the handler as a 500.
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        # The space must already be published (no auto-mint of an ownership
        # row from an event — mirrors subscribe).
        existing = await self._repo.get_space(space_id)
        if existing is None:
            raise PermissionError("space not published")

        # Authorize the relay. The household transport signature above proved
        # WHO sent the bytes (anti-spoof at the GFS edge, #597/#598); this
        # step decides whether that sender may relay content for *this* space.
        #
        # Two acceptable proofs (Phase 5a):
        #   1. Legacy/owner: from_instance IS the space's owning instance.
        #   2. Space-authority: the payload carries a space-authority
        #      signature over its own bytes, verifiable against the TOFU-pinned
        #      space public key. Any seed-holder (owner OR delegated admin) can
        #      produce one, so the space keeps working while the owner is
        #      offline. The GFS stays BLIND to the content — it only verifies
        #      the signature over the opaque payload bytes, never decrypts.
        #
        # Fail-closed: a present-but-invalid authority sig is a hard reject
        # (no fall-through to the owner check); an unknown suite is a reject; a
        # non-owner against a space with no pinned pubkey is a reject (the GFS
        # cannot verify authority, so only the owner may relay until a pubkey
        # is pinned on a later publish).
        is_owner = existing.owning_instance == from_instance
        if not is_owner:
            self._authorize_authority_relay(space_id, event_type, payload, existing)

        subscribers = await self._repo.list_subscribers(
            space_id,
            exclude=from_instance,
        )

        event_body = {
            "space_id": space_id,
            "event_type": event_type,
            "payload": payload,
            "from_instance": from_instance,
        }

        return await self._fan_out(subscribers, event_body, session)

    def _authorize_authority_relay(
        self,
        space_id: str,
        event_type: str,
        payload: object,
        existing: GlobalSpace,
    ) -> None:
        """Authorize a NON-owner relay via the space-authority signature.

        Raises :class:`PermissionError` unless the ``payload`` carries a valid
        space-authority signature verifiable against the space's TOFU-pinned
        public key. Fail-closed in every other case:

        * wire ``event_type`` isn't one the authority sig authorizes (the
          ``AUTHORITY_RELAY_EVENT_TYPES`` set — ``space_post_public`` or
          ``space_subscriber_key_handoff``) → reject;
        * payload isn't a signed dict / no ``authority_sig`` → reject;
        * no pinned pubkey on the space → reject (GFS can't verify authority);
        * unknown authority suite → reject (no default fallback);
        * signature present but doesn't verify → reject.

        The GFS never decrypts ``payload`` — it only verifies the Ed25519
        authority signature over the payload bytes (with the two signature
        fields stripped, mirroring the signer).

        Replay/dedupe contract: the authority signature binds the space id +
        payload but NO timestamp / nonce / epoch, and the GFS keeps no replay
        cache, so this relay is idempotent / at-least-once — a captured
        authority-signed payload can be re-POSTed and re-fanned-out (a property
        the owner relay already had under #598, widened here). We deliberately
        do NOT add GFS-side replay machinery: the GFS is content-blind and
        can't see a post id inside the (encrypted) payload. The content-layer
        backstop is SUBSCRIBER-side dedupe by the post id carried inside the
        payload — enforced by the HFS ``space_public_inbound`` consumer (the
        same way moments dedupe by moment_id). See ``docs/protocol/discovery.md``.
        """
        if not isinstance(payload, dict) or "authority_sig" not in payload:
            raise PermissionError("not the owner of this space")
        # The authority signature authorizes only the event types in
        # ``AUTHORITY_RELAY_EVENT_TYPES`` (``space_post_public`` and the
        # Phase-5b ``space_subscriber_key_handoff``). Bind the caller-supplied
        # WIRE event_type to one of those AND verify the signature under that
        # SAME type below — a non-owner holding one valid payload can't relay
        # it under an arbitrary type (e.g. ``space_admin_action``), and a
        # payload signed for one allowed type can't be replayed under the
        # other (the signing bytes bind the event type). The owner path is
        # exempt (handled by the caller) and keeps relaying any type.
        if event_type not in AUTHORITY_RELAY_EVENT_TYPES:
            raise PermissionError(
                "authority relay only permits the "
                f"{sorted(AUTHORITY_RELAY_EVENT_TYPES)!r} event types",
            )
        if not existing.identity_public_key:
            # No TOFU-pinned key → the GFS cannot verify a space-authority
            # signature, so only the owner may relay (rejected above).
            raise PermissionError(
                "no pinned authority key for this space",
            )
        authority_sig = payload.get("authority_sig") or ""
        authority_sig_suite = payload.get("authority_sig_suite") or ""
        try:
            ok = verify_authority_event(
                event_type=event_type,
                space_id=space_id,
                payload=strip_authority_sig_fields(payload),
                authority_sig=str(authority_sig),
                authority_sig_suite=str(authority_sig_suite),
                space_public_key=bytes.fromhex(existing.identity_public_key),
            )
        except UnsupportedAuthoritySuite as exc:
            raise PermissionError(
                f"unknown authority signature suite: {exc}",
            ) from exc
        except ValueError as exc:
            # Malformed pinned pubkey hex — treat as unverifiable, fail-closed.
            raise PermissionError("invalid authority key") from exc
        if not ok:
            raise PermissionError("invalid authority signature")

    async def _verify_signed_request(
        self,
        instance_id: str,
        payload: dict[str, object],
        *,
        signature: str,
    ) -> ClientInstance:
        """Verify a self-signed, replay-guarded request from *instance_id*.

        Shared by every ``{instance_id, ..., ts}``-shaped GFS request
        (``update_instance``, ``subscribe``, ``unsubscribe``). The Ed25519
        *signature* is checked over the canonical JSON of *payload* against
        the instance's REGISTERED public key — because ``instance_id`` is
        part of the signed payload, a caller can only act as **itself**.
        The freshness check reads ``payload["ts"]`` — the very timestamp
        the signature covers, so a caller can never have a signature
        verified over one timestamp and freshness-checked against another.
        The signed ``ts`` must be a fresh, tz-aware ISO 8601 timestamp
        within ±300 s of now; unparseable / naive timestamps are rejected
        (a missing offset is treated as untrusted).

        Fails closed with :class:`PermissionError` on an unknown instance,
        a missing / malformed / non-verifying signature, or a stale ``ts``.
        Returns the looked-up :class:`ClientInstance` so callers can reuse
        it without a second read.
        """
        inst = await self._repo.get_instance(instance_id)
        if inst is None:
            raise PermissionError(f"Unknown instance: {instance_id}")

        # Signature is REQUIRED here (unlike publish_space's optional-sig
        # branch): an empty or invalid signature is a hard PermissionError.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        try:
            # A malformed stored pubkey is just as unverifiable as a
            # malformed signature — fail closed with the same
            # PermissionError instead of escaping the handler as a 500.
            raw_key = bytes.fromhex(inst.public_key)
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")

        ts = str(payload["ts"])
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")
        return inst

    async def subscribe(
        self,
        instance_id: str,
        space_id: str,
        ts: str,
        signature: str,
    ) -> None:
        """Add *instance_id* as a subscriber of *space_id*.

        Authenticated the same way as :meth:`update_instance`: the request
        is signed by *instance_id* over the canonical ``{action:
        "subscribe", instance_id, space_id, ts}`` JSON and verified against
        the instance's registered public key. The ``action`` discriminator
        is part of the signed bytes (domain separation), so a captured
        subscribe signature can never be replayed as an unsubscribe. Because the signature binds to the *instance_id* in the
        body, a caller can only subscribe **itself** — it can't sign as
        another household. The signed ``ts`` is replay-guarded (±300 s).

        Rejects (``PermissionError``) an unknown instance, a missing /
        malformed / invalid signature, a stale timestamp, and a space the
        GFS has never seen published (no auto-creation of a pending row from
        an unauthenticated demand signal — a subscription must target a real
        published space).
        """
        inst = await self._verify_signed_request(
            instance_id,
            {
                "action": "subscribe",
                "instance_id": instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )

        # A subscription must target a space the GFS already knows about.
        # Auto-minting a row from an (un)authenticated subscribe let any
        # caller seed arbitrary space ids — reject unknown ids instead.
        existing = await self._repo.get_space(space_id)
        if existing is None:
            raise PermissionError("space not published")

        await self._repo.add_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s subscribed to space %s", instance_id, space_id)

        # Phase 5b-b — best-effort notify the space OWNER so a seed-holder can
        # seal the per-space content key to this new subscriber (the GFS stays
        # blind: it only forwards the subscriber's already-published identity +
        # key-wrap material, never the key). Only the owner is notified here
        # because the GFS authoritatively knows ``owning_instance``; a
        # delegated admin catches up via the 5b-c reconcile (pulling the
        # subscriber list). If the owner is offline (no WS), the frame is
        # dropped — that, too, is the reconcile's job, NOT a subscribe failure.
        await self._notify_owner_new_subscriber(
            existing.owning_instance, space_id, inst
        )

    async def _notify_owner_new_subscriber(
        self,
        owning_instance: str,
        space_id: str,
        subscriber: ClientInstance,
    ) -> None:
        """Push a ``new_subscriber`` frame to the space owner's WS (best-effort).

        Carries the subscriber's registered Ed25519 identity ``public_key`` +
        its published key-wrap pubkey/suite/self-signature so the owner can
        verify the key-wrap binding end-to-end (``verify_keywrap_binding``,
        anti-GFS-substitution) before sealing. Never raises — a missing socket
        or a send error is logged and swallowed (the 5b-c reconcile backstops
        an offline owner)."""
        if self._ws_registry is None:
            return
        try:
            await self._ws_registry.send(
                owning_instance,
                {
                    "type": "new_subscriber",
                    "space_id": space_id,
                    "subscriber": {
                        "instance_id": subscriber.instance_id,
                        "identity_public_key": subscriber.public_key,
                        "keywrap_public_key": subscriber.keywrap_public_key,
                        "kem_suite": subscriber.kem_suite,
                        "keywrap_sig": subscriber.keywrap_sig,
                    },
                },
            )
        except Exception as exc:  # defensive — never fail a subscribe on notify
            log.warning(
                "GFS: new_subscriber notify to owner %s for space %s failed: %s",
                owning_instance,
                space_id,
                exc,
            )

    async def list_subscribers_with_keys(
        self,
        space_id: str,
        *,
        ts: str,
        authority_sig: str,
        authority_sig_suite: str,
    ) -> list[GfsSubscriberWithKeys]:
        """Release the subscriber list to a verified SEED-HOLDER (Phase-5b-c).

        Authorization mirrors the space-authority relay path: the caller proves
        it holds the space seed (owner OR delegated admin) by signing
        ``{space_id, ts}`` under :data:`AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY`
        with the seed; the GFS verifies it against the TOFU-pinned space public
        key it already stores (``global_spaces.identity_public_key``) — no new
        roster or key. Fail-closed (:class:`PermissionError`) on every failure:

        * unknown space, or a space with no pinned authority pubkey (the GFS
          can't verify a seed-holder, so it releases nothing);
        * stale ``ts`` (±300 s replay guard, same window as ``subscribe``);
        * missing / malformed / unknown-suite / non-verifying signature.

        The signing bytes bind the event type AND the space id, so a query
        signed for another space (or under the relay event type) can't be
        replayed here. Returns the subscriber rows (instance ids + their
        already-registered identity / key-wrap pubkeys) on success.
        """
        existing = await self._repo.get_space(space_id)
        if existing is None or not existing.identity_public_key:
            # No row, or no pinned key → nothing verifiable → release nothing.
            raise PermissionError("no pinned authority key for this space")

        # Replay guard FIRST (cheap, and independent of the signature): a
        # fresh, tz-aware ISO 8601 ``ts`` within ±300 s of now. Naive /
        # unparseable timestamps are rejected (a missing offset is untrusted).
        try:
            parsed = datetime.fromisoformat(ts)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Stale timestamp") from exc
        if parsed.tzinfo is None:
            raise PermissionError("Stale timestamp")
        now = datetime.now(timezone.utc)
        if abs((now - parsed).total_seconds()) > INSTANCE_UPDATE_TS_SKEW_SECONDS:
            raise PermissionError("Stale timestamp")

        if not authority_sig:
            raise PermissionError("invalid authority signature")
        try:
            ok = verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_SUBSCRIBERS_QUERY,
                space_id=space_id,
                payload={"space_id": space_id, "ts": ts},
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(existing.identity_public_key),
            )
        except UnsupportedAuthoritySuite as exc:
            raise PermissionError(
                f"unknown authority signature suite: {exc}",
            ) from exc
        except ValueError as exc:
            raise PermissionError("invalid authority key") from exc
        if not ok:
            raise PermissionError("invalid authority signature")

        return await self._repo.list_subscribers_with_keys(space_id)

    async def unsubscribe(
        self,
        instance_id: str,
        space_id: str,
        ts: str,
        signature: str,
    ) -> None:
        """Remove *instance_id* from the subscribers of *space_id*.

        Authenticated exactly like :meth:`subscribe`: an Ed25519 signature
        over the canonical ``{action: "unsubscribe", instance_id, space_id,
        ts}`` JSON — the ``action`` is inside the signed bytes, so an
        unsubscribe signature can't be replayed as a subscribe — verified
        against the instance's registered public key and replay-guarded
        (±300 s). The signature binds the request to *instance_id*, so a
        caller can only unsubscribe **itself** — without this, any caller
        could evict any household from any space's relay fan-out.

        Fails closed (``PermissionError``) on an unknown instance, a
        missing / malformed / invalid signature, or a stale / naive
        timestamp. Unlike :meth:`subscribe` it deliberately does NOT
        require the space to still exist: unsubscribing from an
        already-removed space stays idempotent.
        """
        await self._verify_signed_request(
            instance_id,
            {
                "action": "unsubscribe",
                "instance_id": instance_id,
                "space_id": space_id,
                "ts": ts,
            },
            signature=signature,
        )
        await self._repo.remove_subscriber(
            space_id=space_id,
            instance_id=instance_id,
        )
        log.debug("GFS: %s unsubscribed from space %s", instance_id, space_id)

    async def list_spaces(
        self,
        *,
        status: str | None = None,
    ) -> list[GlobalSpace]:
        """Return global/public spaces known to this GFS node.

        The public ``GET /gfs/spaces`` endpoint passes ``status='active'``
        to hide pending + banned rows. Internal callers (admin, tests)
        can pass ``status=None`` to see everything.
        """
        return await self._repo.list_spaces(status=status)

    async def get_space(self, space_id: str) -> GlobalSpace | None:
        """Single-space lookup — used by ``GET /gfs/spaces/{id}`` so SH
        clients fetching the metadata for a discovery-link can mirror
        the space row locally before subscribing."""
        return await self._repo.get_space(space_id)

    async def hide_space(self, space_id: str) -> None:
        """Mark a space as ``banned`` so it drops off ``GET /gfs/spaces``.

        Used by ``DELETE /gfs/spaces/{id}/unpublish`` when the owning
        HFS retracts the listing. We keep the row so the GFS admin's
        audit trail survives; a later ``publish_space`` call from the
        owner will flip the status back.
        """
        existing = await self._repo.get_space(space_id)
        if existing is None:
            return
        await self._repo.upsert_space(
            GlobalSpace(
                space_id=existing.space_id,
                owning_instance=existing.owning_instance,
                name=existing.name,
                description=existing.description,
                about_markdown=existing.about_markdown,
                cover_url=existing.cover_url,
                min_age=existing.min_age,
                category=existing.category,
                accent_color=existing.accent_color,
                status="banned",
                subscriber_count=existing.subscriber_count,
                posts_per_week=existing.posts_per_week,
                published_at=existing.published_at,
                identity_public_key=existing.identity_public_key,
            )
        )

    async def publish_space(
        self,
        *,
        space_id: str,
        owning_instance: str,
        name: str,
        description: str | None = None,
        about_markdown: str | None = None,
        cover_url: str | None = None,
        icon_url: str | None = None,
        min_age: int = 0,
        category: str = "general",
        accent_color: str = "#D2542A",
        primary_color: str = "#D2542A",
        identity_public_key: str = "",
        signature: str = "",
    ) -> GlobalSpace:
        """Register / refresh a space row from the owning instance.

        Drives the ``POST /gfs/spaces/{id}/publish`` route. The publish
        body is signed by the owning HFS so a malicious peer can't
        flip another household's space metadata; signature is verified
        against the registered ``ClientInstance.public_key``.
        Auto-accepted clients land as ``status='active'`` (visible on
        ``GET /gfs/spaces``); pending clients stay pending until the
        GFS admin flips them.
        """
        inst = await self._repo.get_instance(owning_instance)
        if inst is None:
            raise PermissionError(
                f"Unknown owning_instance: {owning_instance}",
            )
        # Signature is MANDATORY (same trust model as update_instance): an
        # empty / malformed / invalid signature is a hard PermissionError, so
        # a registered peer can't overwrite another household's space listing.
        if not signature:
            raise PermissionError("Invalid Ed25519 signature")
        canonical = json.dumps(
            {
                "space_id": space_id,
                "owning_instance": owning_instance,
                "name": name,
                "description": description or "",
                "about_markdown": about_markdown or "",
                "cover_url": cover_url or "",
                "icon_url": icon_url or "",
                "min_age": min_age,
                "category": category,
                "accent_color": accent_color,
                "primary_color": primary_color,
                "identity_public_key": identity_public_key or "",
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        raw_key = bytes.fromhex(inst.public_key)
        try:
            raw_sig = b64url_decode(signature)
        except (ValueError, TypeError) as exc:
            raise PermissionError("Invalid Ed25519 signature") from exc
        if not verify_ed25519(raw_key, canonical, raw_sig):
            raise PermissionError("Invalid Ed25519 signature")
        # Bound the stored ``about_markdown`` (verified above against the
        # full value, so the signature still holds). The public page caps
        # rendering too; capping at storage avoids DB bloat from a paired
        # instance publishing an oversized blob. Truncate rather than
        # reject so a slightly-long About still publishes.
        if about_markdown and len(about_markdown) > MAX_ABOUT_MARKDOWN_CHARS:
            about_markdown = about_markdown[:MAX_ABOUT_MARKDOWN_CHARS]
        existing = await self._repo.get_space(space_id)
        # Owner is immutable after first publish. space_id is a public,
        # owner-chosen UUID that travels in discovery links, so a registered
        # peer that learns it could otherwise re-publish the row with
        # owning_instance=itself — validly signed by its OWN key — and seize
        # the listing. Reject any publish whose owner differs from the stored
        # one (first-publisher wins; only that instance can refresh it).
        if existing is not None and existing.owning_instance != owning_instance:
            raise PermissionError("space already owned by another instance")
        # TOFU-pin the space's authority public key. The FIRST publish that
        # carries one pins it; thereafter it is IMMUTABLE — a later publish
        # offering a DIFFERENT pubkey keeps the pinned one (and logs a warning
        # so a swap attempt is diagnosable). This is what lets the GFS trust an
        # authority-signed relay from a non-owner seed-holder: the verify key
        # is established once by the owner and can't be silently rotated by a
        # later (possibly compromised-transport) publish. A first publish with
        # an empty pubkey leaves it NULL — that space can't use authority-signed
        # relay until a pubkey is pinned. The owner is already immutable
        # (checked above), so only the legit owner could ever pin/refresh.
        pinned_pubkey = identity_public_key or ""
        if existing is not None and existing.identity_public_key:
            if pinned_pubkey and pinned_pubkey != existing.identity_public_key:
                log.warning(
                    "GFS: ignoring attempt to change pinned authority pubkey "
                    "for space %s (pinned=%s…, offered=%s…)",
                    space_id,
                    existing.identity_public_key[:8],
                    pinned_pubkey[:8],
                )
            pinned_pubkey = existing.identity_public_key
        # Preserve subscriber_count / posts_per_week / published_at from
        # the existing row — those are GFS-side bookkeeping, not the
        # owner's to declare. Only the owner's name / description /
        # cover travel with the publish.
        next_status = "active" if inst.auto_accept else "pending"
        if existing is not None and existing.status == "banned":
            next_status = "banned"
        space = GlobalSpace(
            space_id=space_id,
            owning_instance=owning_instance,
            name=name,
            description=description,
            about_markdown=about_markdown,
            cover_url=cover_url,
            icon_url=icon_url,
            min_age=min_age,
            category=normalize_category(category),
            accent_color=accent_color,
            primary_color=primary_color,
            status=next_status,
            subscriber_count=existing.subscriber_count if existing else 0,
            posts_per_week=existing.posts_per_week if existing else 0.0,
            published_at=existing.published_at if existing else "",
            identity_public_key=pinned_pubkey,
        )
        await self._repo.upsert_space(space)
        log.info(
            "GFS: published space %s (owner=%s, status=%s)",
            space_id,
            owning_instance,
            next_status,
        )
        return space

    async def update_instance(
        self,
        instance_id: str,
        display_name: str,
        ts: str,
        signature: str,
    ) -> None:
        """Update a registered instance's display_name. Signed by the
        instance and verified against its registered public key (same
        trust model as publish_space — a peer can't rename another
        household). Rejects unknown instances, bad signatures, and stale
        timestamps (replay guard)."""
        await self._verify_signed_request(
            instance_id,
            {
                "instance_id": instance_id,
                "display_name": display_name,
                "ts": ts,
            },
            signature=signature,
        )

        cleaned = display_name.strip()
        if not cleaned or len(cleaned) > MAX_DISPLAY_NAME_CHARS:
            raise ValueError("display_name must be 1-80 chars")

        await self._repo.set_instance_display_name(instance_id, cleaned)
        log.info("GFS: instance %s renamed to %r", instance_id, cleaned)

    # ── Fan-out ──────────────────────────────────────────────────────────

    async def _fan_out(
        self,
        subscribers: list[GfsSubscriber],
        event_body: dict,
        session: aiohttp.ClientSession | None,
    ) -> list[str]:
        """Deliver *event_body* to each subscriber.

        Tries the SH↔GFS WebSocket first (push frame ``{type:"relay", ...}``).
        If no socket is registered for the subscriber or the send fails,
        falls back to an HTTPS POST to the subscriber's inbox URL.
        """
        own_session = session is None
        active: aiohttp.ClientSession = (
            session if session is not None else aiohttp.ClientSession()
        )
        push_frame = {"type": "relay", **event_body}
        try:
            delivered: list[str] = []
            for sub in subscribers:
                # WebSocket push first.
                if self._ws_registry is not None and await self._ws_registry.send(
                    sub.instance_id,
                    push_frame,
                ):
                    delivered.append(sub.instance_id)
                    continue

                # HTTPS-inbox fallback.
                try:
                    async with active.post(
                        sub.inbox_url,
                        json=event_body,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status < 400:
                            delivered.append(sub.instance_id)
                        else:
                            log.warning(
                                "GFS fan-out: %s returned HTTP %s",
                                sub.inbox_url,
                                resp.status,
                            )
                except Exception as exc:
                    log.warning(
                        "GFS fan-out: failed to deliver to %s: %s",
                        sub.inbox_url,
                        exc,
                    )
            return delivered
        finally:
            if own_session:
                await active.close()
