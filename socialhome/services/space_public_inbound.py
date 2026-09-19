"""Recipient-side consumer for relayed public space posts (Phase 5a2).

Handles ``{type:"relay", event_type:"space_post_public", payload:<envelope>}``
frames the GFS pushes over the SH↔GFS WebSocket. Mirrors
:class:`socialhome.services.moment_public_inbound.MomentPublicInbound`.

The pipeline is defence-in-depth — the GFS already verified the authority
signature before fanning out, but the relay (and the GFS) are never
trusted by the receiver:

1. **Authority verify** — re-verify the space-authority Ed25519 signature
   against the locally-mirrored ``spaces.identity_public_key``. A space we
   don't mirror, a space with no pinned pubkey, or a failed/forged
   signature → drop (WARNING).
2. **Decrypt** — decrypt the envelope's ``encrypted_payload`` under the
   per-space content key for the stated epoch. If we don't hold that
   epoch's key (subscribers receive it in Phase 5b) → drop gracefully.
3. **Author self-cert** — verify ``derive_user_id(author_pk, username) ==
   author_user_id`` so a relay can't forge authorship.
4. **Self-echo guard** — the GFS can no longer identify the publisher, so it
   no longer excludes it from its own fan-out: a household that publishes to
   a space it also subscribes to receives its own post back. An inner
   ``origin_instance_id`` equal to our own instance id is dropped at DEBUG
   before any write (the dedupe below would usually catch it, but only while
   the local row still exists — an echo arriving after a local delete would
   otherwise resurrect the post from our own copy).
5. **Dedupe** — the GFS relay is at-least-once and keeps no replay cache
   (it's content-blind, it can't see the post id). Drop if a post with
   this id already exists locally (idempotent — the content-layer replay
   backstop the GFS relay design relies on).
6. **Persist + publish** — save to ``space_posts`` (the same store the
   §24.11 inbound path uses) and publish :class:`SpacePostCreated` with
   ``origin_instance_id`` set (so the local realtime/search surfaces light
   up AND the federation outbound bridge's loop-guard skips re-fanning).

Attribution comes from the **encrypted, authority-signed inner only**. The
fan-out frame carries no household identity (the GFS never learns which
household relayed); a frame from an older GFS may still carry an outer
``from_instance``, and that value is ignored — never read, never logged.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..domain.events import SpacePostCreated
from ..domain.post import LocationData, Post, PostType
from ..domain.presence import truncate_coord
from ..infrastructure.event_bus import EventBus
from ..utils.datetime import parse_iso8601_lenient
from .space_public_author import verify_signed_author_inner

if TYPE_CHECKING:
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


class SpacePublicInbound:
    """GFS-relay → local-persist consumer for public/global space posts."""

    __slots__ = ("_bus", "_spaces", "_crypto", "_posts", "_own_instance_id")

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        space_post_repo: "AbstractSpacePostRepo",
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        self._posts = space_post_repo
        self._own_instance_id: str = ""

    def attach_identity(self, *, own_instance_id: str) -> None:
        """Wire our own instance id — the self-echo guard's only input."""
        self._own_instance_id = own_instance_id

    async def handle(self, frame: dict[str, Any], *, gfs_id: str | None = None) -> None:
        """Dispatch one GFS relay frame. Non-``space_post_public`` frames
        are ignored so this can sit on the generic relay channel.

        The frame carries no household identity — an outer ``from_instance``
        from an older GFS is neither read nor logged.
        """
        if frame.get("event_type") != AUTHORITY_EVENT_SPACE_POST_PUBLIC:
            return
        envelope = frame.get("payload")
        if not isinstance(envelope, dict):
            return
        await self._on_relay(envelope)

    async def _on_relay(self, envelope: dict) -> None:
        space_id = str(envelope.get("space_id") or "")
        if not space_id:
            return
        space = await self._spaces.get(space_id)
        if space is None or not space.identity_public_key:
            log.warning(
                "space_public.inbound: no local space / pubkey for %s — dropped",
                space_id,
            )
            return
        if not self._verify_authority(space_id, envelope, space.identity_public_key):
            log.warning(
                "space_public.inbound: authority signature failed for space %s",
                space_id,
            )
            return
        epoch = envelope.get("epoch")
        ciphertext = envelope.get("encrypted_payload")
        if not isinstance(epoch, int) or not isinstance(ciphertext, str):
            log.warning(
                "space_public.inbound: malformed envelope for space %s", space_id
            )
            return
        try:
            pt = await self._crypto.decrypt(space_id, epoch, ciphertext)
        except (RuntimeError, ValueError, InvalidTag) as exc:
            # No epoch key held yet (Phase 5b ships subscriber keys), a
            # malformed ciphertext (ValueError), or a tampered-but-valid-b64
            # ciphertext whose AEAD tag fails (InvalidTag — NOT a ValueError,
            # so it would otherwise escape this guard). Drop gracefully, never
            # crash, per this module's defence-in-depth contract.
            log.info(
                "space_public.inbound: cannot decrypt space %s epoch %s: %s",
                space_id,
                epoch,
                exc,
            )
            return
        try:
            inner = json.loads(pt)
        except json.JSONDecodeError:
            log.warning(
                "space_public.inbound: undecodable inner payload for space %s",
                space_id,
            )
            return
        if not isinstance(inner, dict):
            return

        post_id = str(inner.get("post_id") or "")
        author_user_id = str(inner.get("author_user_id") or "")
        origin_instance_id = str(inner.get("origin_instance_id") or "")
        # Self-echo guard: the GFS can't identify the publisher any more, so it
        # fans every relay out to every subscriber — including us, when we both
        # publish to and subscribe to the space. Drop our own post before any
        # write: the dedupe below only covers it while the local row still
        # exists, so without this an echo arriving after a local delete would
        # resurrect the post from our own copy.
        if origin_instance_id and origin_instance_id == self._own_instance_id:
            log.debug("space_public.inbound: self-echo for post %s — dropped", post_id)
            return
        # Self-cert (author_pk ↔ author_user_id, both PUBLIC) + per-author
        # signature (the named author's HOUSEHOLD identity key must have signed
        # the inner content — only a household holding the author's identity
        # seed can produce it). The relaying seed-holder applies the IDENTICAL
        # check before relaying (space_public_author.verify_signed_author_inner),
        # so the two sites can't drift. Fail-closed on missing identity fields,
        # self-cert mismatch, or missing/malformed/invalid signature.
        if not verify_signed_author_inner(inner):
            log.warning(
                "space_public.inbound: author verification failed for space %s post %s",
                space_id,
                post_id,
            )
            return
        # Cross-space injection guard: the inner's signed ``space_id`` MUST
        # equal the outer envelope's space. A validly-signed inner authored for
        # space X must never be persisted under space Y (the relay decrypts +
        # authority-signs under Y but the author never posted in Y).
        if str(inner.get("space_id") or "") != space_id:
            log.warning(
                "space_public.inbound: inner space_id mismatch "
                "(inner=%s envelope=%s) for post %s — dropped",
                inner.get("space_id"),
                space_id,
                post_id,
            )
            return
        # Dedupe by post id — the GFS relay is at-least-once.
        if await self._posts.get(post_id) is not None:
            log.debug("space_public.inbound: duplicate post %s — dropped", post_id)
            return

        post = self._post_from_inner(post_id, author_user_id, inner)
        if await self._posts.save(space_id, post) is None:
            log.warning(
                "space_public.inbound: post %s already exists in another space "
                "— refusing the relayed write",
                post_id,
            )
            return
        await self._bus.publish(
            SpacePostCreated(
                post=post,
                space_id=space_id,
                origin_instance_id=origin_instance_id,
            )
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _verify_authority(
        self, space_id: str, envelope: dict, space_public_key_hex: str
    ) -> bool:
        try:
            return verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
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

    @staticmethod
    def _post_from_inner(post_id: str, author: str, inner: dict) -> Post:
        try:
            post_type = PostType(str(inner.get("type") or "text"))
        except ValueError:
            post_type = PostType.TEXT
        # GPS is re-truncated to 4 dp on receive (never trust the wire
        # precision) — mirrors federation_inbound_service._post_from_payload.
        location: LocationData | None = None
        raw_loc = inner.get("location")
        if isinstance(raw_loc, dict):
            try:
                lat_t = truncate_coord(float(raw_loc["lat"]))
                lon_t = truncate_coord(float(raw_loc["lon"]))
                if lat_t is None or lon_t is None:
                    raise ValueError("nan coordinate")
                location = LocationData(
                    lat=lat_t, lon=lon_t, label=raw_loc.get("label")
                )
            except KeyError, TypeError, ValueError:
                location = None
        image_urls = inner.get("image_urls")
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=inner.get("content"),
            media_url=inner.get("media_url"),
            image_urls=tuple(image_urls) if isinstance(image_urls, list) else (),
            location=location,
            created_at=parse_iso8601_lenient(inner.get("created_at")),
            hidden_from_feed=bool(inner.get("hidden_from_feed", False)),
        )
