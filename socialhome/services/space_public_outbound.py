"""Author-side producer for the public space-content relay (Phase 5a2).

Bus subscriber on :class:`SpacePostCreated`. When a PUBLIC/GLOBAL space
post is created locally on a household that *holds the space's signing
seed* (the owner, or a delegated admin per the owner-offline epic), this
service relays the post to the space's GFS subscribers via the
content-blind GFS relay:

1. Build the inner content payload (post id, author identity, content,
   media refs, created_at) — including ``author_pk`` so the subscriber
   can self-certify the author, and ``post_id`` for dedupe.
2. Encrypt it under the per-space AES-256 content key (the *existing*
   epoch key — no new key) so the GFS and any relay see only ciphertext.
3. Wrap as ``{space_id, epoch, encrypted_payload}`` and authority-sign
   with the space seed (:func:`sign_authority_event`) under
   ``space_post_public`` so the GFS can authorize the relay against the
   TOFU-pinned space public key without ever learning the content.
4. POST to every GFS the space is published to via
   :meth:`GfsConnectionService.publish_space_event`.

Mirrors :class:`socialhome.services.moment_public_outbound.MomentPublicOutbound`.

Each relayed post carries TWO signatures: the outer **space-authority**
signature (this seed-holder, verified against the pinned space key — proves
the relay is authorised) AND an inner **per-author** signature
(``author_sig``, the author's household identity seed signing the inner
content via :func:`author_signing_bytes`, verified against ``author_pk`` —
proves the named author wrote it). Without the per-author signature,
``author_pk`` / ``author_user_id`` are both public, so a seed-holder could
attribute a post to any member; the signature closes that hole.

Scope (Phase 5a2): only a *seed-holding* household relays, and only its OWN
household's locally-authored posts (the producer's ``author`` must be a local
user — enforced below — so this household always holds the author's identity
seed and can produce ``author_sig``). A plain member's public post reaches
subscribers only when a seed-holder (owner or delegated admin) relays —
accepted for this phase. Deletes/edits are a follow-up (the GFS relay is
created-only today).

Remote-author relay (Phase 5a relay): a *remote* member's public post — where
this seed-holder does NOT hold the author's identity seed — is relayed when it
arrives carrying the author household's pre-signed inner as
``SpacePostCreated.public_relay`` (built by
:func:`build_signed_author_inner` on the author's household and propagated
through the mesh). :meth:`_relay_remote_authored` re-verifies the author's
self-cert + signature (:func:`verify_signed_author_inner`) — never relaying
forgeable / unverifiable content — re-encrypts the inner under the existing
per-space content key, and authority-signs the GFS envelope with the space
seed. An inbound event with no ``public_relay`` is the pure loop guard and is
never re-fanned.

This service is the encryption boundary: the cleartext post never leaves
in a GFS-bound envelope (CLAUDE.md Encryption-First Rule). If the space
has no content key, :meth:`SpaceContentEncryption.encrypt` raises
``RuntimeError`` rather than degrading to plaintext.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..domain.events import SpacePostCreated
from ..domain.space import PUBLIC_SPACE_TIERS
from ..infrastructure.event_bus import EventBus
from .space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)

if TYPE_CHECKING:
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_connection_service import GfsConnectionService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


class SpacePublicOutbound:
    """Bus-event → GFS-relay producer for public/global space posts."""

    __slots__ = (
        "_bus",
        "_spaces",
        "_crypto",
        "_users",
        "_gfs",
        "_own_instance_id",
        "_own_instance_pk",
        "_own_identity_seed",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        user_repo: "AbstractUserRepo",
        gfs_service: "GfsConnectionService",
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        self._users = user_repo
        self._gfs = gfs_service
        self._own_instance_id: str = ""
        #: This household's 32-byte Ed25519 identity public key. Shipped as
        #: ``author_pk`` inside the encrypted payload so the subscriber can
        #: self-certify ``derive_user_id(author_pk, username) ==
        #: author_user_id`` (mirrors the moments self-cert).
        self._own_instance_pk: bytes = b""
        #: This household's 32-byte Ed25519 identity *seed*. Signs the inner
        #: ``author_sig`` over the content (verified against ``author_pk`` by
        #: the subscriber). The producer only relays its own household's
        #: locally-authored posts, so this seed can always author-sign them.
        self._own_identity_seed: bytes = b""

    def attach_identity(
        self,
        *,
        own_instance_id: str,
        own_instance_public_key: bytes,
        own_identity_seed: bytes,
    ) -> None:
        self._own_instance_id = own_instance_id
        self._own_instance_pk = own_instance_public_key
        self._own_identity_seed = own_identity_seed

    def wire(self) -> None:
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        # Inbound (another member's post). Relay it to the GFS subscribers
        # only when it carries a verified, author-signed public_relay hint and
        # we hold the space seed (remote-author relay, owner-offline). Otherwise
        # this is the loop guard — never re-fan an inbound post.
        if event.origin_instance_id is not None:
            await self._relay_remote_authored(event)
            return
        if not event.space_id:
            return
        post = event.post
        # Calendar-derived posts are minted locally on every household by
        # the CalendarFeedBridge from the federated calendar event — a
        # relay would double up. Mirrors SpacePostOutbound.
        if post.linked_event_id is not None:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        # Only a seed-holder (owner or delegated admin) relays. A NULL seed
        # means this household can't authority-sign — skip silently.
        seed = await self._spaces.get_space_seed(event.space_id)
        if seed is None:
            return
        if (
            not self._own_instance_id
            or not self._own_instance_pk
            or not self._own_identity_seed
        ):
            log.warning(
                "space_public.outbound: identity not attached — "
                "dropping relay for space %s",
                event.space_id,
            )
            return
        author = await self._users.get_by_user_id(post.author)
        if author is None:
            # Author isn't a LOCAL user (system/bot/remote member). We don't
            # hold their identity seed, so we cannot produce the per-author
            # ``author_sig`` — skip rather than relay an unattributable post.
            # Relaying remote-authored posts is a follow-up (see module
            # docstring): it needs the author_sig propagated through the mesh.
            return

        # Per-author signature: the author's household identity seed signs the
        # canonical, domain-separated bytes over the attributable inner fields
        # (excluding ``author_sig`` itself). Verified by the subscriber against
        # ``author_pk`` so a relaying seed-holder can't forge authorship.
        # Centralised in build_signed_author_inner so the member-broadcast
        # relay-hint produces a byte-identical signed inner.
        inner = build_signed_author_inner(
            post=post,
            space_id=event.space_id,
            author_username=author.username,
            author_pk=self._own_instance_pk,
            author_identity_seed=self._own_identity_seed,
            origin_instance_id=self._own_instance_id,
            # Derivation input for the subscriber's self-cert: the immutable
            # uuid anchor (new users) so the check survives a username change.
            # Passed raw — the builder normalises a legacy username-anchored
            # row (``anchor == username``, the 0041 backfill) to "absent" so
            # its signed bytes stay v_25-compatible.
            author_identity_anchor=author.identity_anchor,
        )
        # Encrypt under the existing per-space epoch key. Raises if no key —
        # we never relay plaintext (Encryption-First Rule).
        try:
            epoch, ct = await self._crypto.encrypt(
                event.space_id, json.dumps(inner).encode("utf-8")
            )
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — "
                "cannot relay post %s",
                event.space_id,
                post.id,
            )
            return
        envelope: dict = {
            "space_id": event.space_id,
            "epoch": epoch,
            "encrypted_payload": ct,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=event.space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            await self._gfs.publish_space_event(
                space_id=event.space_id,
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                payload=envelope,
                from_instance=self._own_instance_id,
            )
        except Exception:
            log.exception(
                "space_public.outbound: relay failed for space=%s post=%s",
                event.space_id,
                post.id,
            )

    async def _relay_remote_authored(self, event: SpacePostCreated) -> None:
        """Relay another member household's public/global post to the GFS
        subscribers — owner-offline path.

        A remote member's public post can't reach subscribers on its own
        (only a seed-holder can authority-sign the GFS relay). When such a post
        reaches THIS household and we hold the space seed, we re-wrap the
        author's pre-signed inner (carried as ``event.public_relay``) under our
        authority signature. We verify the original author's self-cert +
        signature first — never relay forgeable / unverifiable content — and
        re-encrypt under the existing per-space content key (Encryption-First).
        """
        relay = event.public_relay
        if not isinstance(relay, dict):
            return
        if not event.space_id:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        seed = await self._spaces.get_space_seed(event.space_id)
        if seed is None:
            return  # not a seed-holder — can't authority-sign the relay
        if not self._own_instance_id:
            log.warning(
                "space_public.outbound: identity not attached — cannot relay "
                "remote-authored post for space %s",
                event.space_id,
            )
            return
        # Verify the ORIGINAL author's signature + self-cert before relaying —
        # never relay unverifiable / forgeable content (the subscriber
        # re-verifies, but we fail-closed here too and don't waste a GFS
        # round-trip).
        if not verify_signed_author_inner(relay):
            log.warning(
                "space_public.outbound: public_relay failed verification for "
                "space=%s — not relaying",
                event.space_id,
            )
            return
        # Cross-space injection guard: the inner's signed ``space_id`` MUST
        # equal the space we're relaying under. A validly-signed inner from
        # space X must never be relayed/persisted under space Y.
        if str(relay.get("space_id") or "") != event.space_id:
            log.warning(
                "space_public.outbound: public_relay space_id mismatch "
                "(inner=%s envelope=%s) — not relaying",
                relay.get("space_id"),
                event.space_id,
            )
            return
        try:
            epoch, ct = await self._crypto.encrypt(
                event.space_id, json.dumps(relay).encode("utf-8")
            )
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — "
                "cannot relay remote-authored post",
                event.space_id,
            )
            return
        envelope: dict = {
            "space_id": event.space_id,
            "epoch": epoch,
            "encrypted_payload": ct,
        }
        sig = sign_authority_event(
            event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
            space_id=event.space_id,
            payload=strip_authority_sig_fields(envelope),
            space_seed=seed,
        )
        envelope.update(sig)
        try:
            await self._gfs.publish_space_event(
                space_id=event.space_id,
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                payload=envelope,
                from_instance=self._own_instance_id,
            )
        except Exception:
            log.exception(
                "space_public.outbound: remote-author relay failed for space=%s",
                event.space_id,
            )
