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
accepted for this phase. Authors' own edits and deletes travel over the
member relay (``space_item``); see below for removals.

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

Readability gate: a PUBLIC/GLOBAL space whose ``features.allow_subscribers``
is OFF is published to the GFS *directory* (that is how people discover it and
get invited) but is **not publicly readable** — no post of it is ever relayed,
on either the local-author or the remote-author path. This is NOT ``join_mode``:
how a person becomes a posting member is a separate dial, and an ``invite_only``
space with followers on is a legitimate broadcast space. The flag is the OWNER's
alone (``SpaceService.update_config`` gates it with ``_require_owner``, and the
forwarded-admin path pins it) — exposing a space's content to strangers is not a
delegated-admin decision. Members are unaffected either way: they receive content
through ``broadcast_to_space_members``, not the GFS.

Writer certificates (v_49): the relayed inner carries the author
household's ``writer_cert`` — the space authority key's per-epoch statement
that this household may write (:mod:`socialhome.writer_cert`). On the
local-author path a seed holder attaches its own (self-issued) cert for the
epoch it encrypts under. On the remote-author path a cert in the
``public_relay`` is checked first (signature against the pinned space key,
names the author's ``author_pk``, ``write`` scope, this space): a cert that
fails is a v_49 author's item that must not travel — it is dropped with a
WARNING. A valid cert is then RE-STAMPED for the epoch this relay encrypts
under, but only while the author household still holds a seat in our roster
that permits the item (never a ``comment`` cert onto a post) — a household
removed since its cert was issued is not relayed. An inner with NO cert is
relayed as before, on this seed holder's authority signature alone, only
from an origin below v_49 whose pinned identity key is the inner's
``author_pk`` (a v_49 author always attaches its cert, so its absence is a
stripped cert). That no-cert branch is the
migration tripwire — once every member ships v_49 it can become a refusal.

Authority-only items (:mod:`socialhome.services.space_public_authority`):

* **Removals.** Every space post or comment delete this seed holder applies —
  a moderator's, an admin's or the author's own, local or federated (the host
  path already authorized it) — is relayed as an
  :class:`~socialhome.domain.space_item.AuthorityRemoval` naming the item
  and its author: never who removed it or why. Followers soft-delete, or keep
  a tombstone under an id owner-bound to that author in this space.
  Calendar-derived posts are skipped (never relayed).
* **Approved posts.** A remote member's post released from the moderation
  queue is applied here as the submitter's; the queue kept the submitter's
  author-signed copy (signed at submission), and
  :meth:`SpacePublicOutbound._relay_approved` relays it — when it is exactly
  the published post — marked ``approved_post``, with the author household's
  writer cert re-stamped for the relay epoch at ``comment`` scope at least.
  The approver is never named; without a signed copy nothing is relayed. A
  local author's approved post goes out author-signed like any local post.

Both ride ``space_post_public`` like a post — the connection server cannot
tell them apart — and every plaintext of this relay is padded to
:data:`~socialhome.domain.space_item.ITEM_SIZE_BUCKETS`, inside the AEAD and
outside every author signature. A member's ``public_relay`` that names an
authority kind is refused.

This service is the encryption boundary: the cleartext post never leaves
in a GFS-bound envelope (CLAUDE.md Encryption-First Rule). If the space
has no content key, :meth:`SpaceContentEncryption.encrypt` raises
``RuntimeError`` rather than degrading to plaintext.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    sign_authority_event,
    strip_authority_sig_fields,
)
from ..domain.events import CommentDeleted, PostDeleted, SpacePostCreated
from ..domain.space import PUBLIC_SPACE_TIERS
from ..domain.space_item import (
    AUTHORITY_KIND_FIELD,
    REMOVAL_TARGET_COMMENT,
    REMOVAL_TARGET_POST,
    AuthorityRemoval,
    pad_json_object,
)
from ..domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    scope_permits,
)
from ..federation.space_scope import archive_refusal
from ..infrastructure.event_bus import EventBus
from .space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)
from .space_public_authority import approved_relay_for
from .space_writer_cert_service import WRITER_CERT_FIELD, SpaceWriterCertService

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.space_post_repo import AbstractSpacePostRepo
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
        "_writer_certs",
        "_posts",
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
        #: v_49 writer certs — ``None`` keeps the pre-v_49 relay unchanged.
        self._writer_certs: SpaceWriterCertService | None = None
        #: The space post store — only read to skip the removal of a
        #: calendar-derived post (never relayed, so nothing to remove).
        self._posts: "AbstractSpacePostRepo | None" = None

    def attach_posts(self, posts: "AbstractSpacePostRepo") -> None:
        """Wire the space post store (see :attr:`_posts`)."""
        self._posts = posts

    def attach_writer_certs(self, writer_certs: SpaceWriterCertService) -> None:
        """Wire the v_49 writer-cert service (see the module docstring)."""
        self._writer_certs = writer_certs

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
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)

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
        # Readability is an explicit admin opt-in, independent of join_mode: a
        # public/global space with ``allow_subscribers`` OFF is LISTED in the
        # GFS directory (discovery + invites) but is NOT publicly readable —
        # only members get content. Stop the relay at the source, so nothing
        # of its content stream ever reaches the GFS.
        if not space.features.allow_subscribers:
            log.debug(
                "space_public.outbound: space %s does not allow subscribers — "
                "not publicly readable, skipping relay",
                event.space_id,
            )
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
            # ``author_sig``. A remote member's own post reaches us with its
            # pre-signed ``public_relay`` (the inbound branch above); the one
            # we apply locally is a post released from the moderation queue,
            # which we relay on the authority's word. Anything else is skipped
            # rather than relayed unattributable.
            if event.approved_by:
                await self._relay_approved(event, space, seed)
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
        # we never relay plaintext (Encryption-First Rule). v_49: our own
        # writer cert for exactly the epoch we encrypt under rides inside.
        try:
            epoch, ct = await self._encrypt_with_cert(
                event.space_id, inner, self._own_instance_id
            )
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — "
                "cannot relay post %s",
                event.space_id,
                post.id,
            )
            return
        await self._publish(event.space_id, seed, epoch, ct, what=f"post {post.id}")

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
        # Same gate on the owner-offline path: a seed-holder must not launder
        # another member's post into the (nonexistent) public stream of a
        # space that admits no subscribers.
        if not space.features.allow_subscribers:
            log.debug(
                "space_public.outbound: space %s does not allow subscribers — "
                "not publicly readable, skipping remote-authored relay",
                event.space_id,
            )
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
        # An authority notice carries NO author signature; a member's signed
        # inner that names an authority kind is a forgery attempt — followers
        # would read it as a post anyway, but it never travels.
        if AUTHORITY_KIND_FIELD in relay:
            log.warning(
                "space_public.outbound: public_relay names an authority kind "
                "for space=%s — not relaying",
                event.space_id,
            )
            return
        restamp_for: str | None = None
        if relay.get(WRITER_CERT_FIELD) is not None:
            # v_49 author: its cert must hold before anything travels.
            if not self._remote_cert_ok(space, relay):
                log.warning(
                    "space_public.outbound: writer cert in public_relay failed "
                    "verification for space=%s — not relaying",
                    event.space_id,
                )
                return
            restamp_for = event.origin_instance_id
        elif self._writer_certs is not None and not await self._legacy_hint_ok(
            event.origin_instance_id, relay
        ):
            return
        try:
            if restamp_for is not None:
                epoch, ct = await self._encrypt_with_cert(
                    event.space_id,
                    relay,
                    restamp_for,
                    expect_pk=str(relay.get("author_pk") or ""),
                    required=True,
                )
            else:
                epoch, ct = await self._crypto.encrypt(
                    event.space_id, pad_json_object(relay)
                )
        except _NoWriterCert:
            log.warning(
                "space_public.outbound: author household %s holds no writer "
                "seat that permits a post — writer cert not re-stamped, not relaying "
                "(space=%s)",
                restamp_for,
                event.space_id,
            )
            return
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — "
                "cannot relay remote-authored post",
                event.space_id,
            )
            return
        await self._publish(event.space_id, seed, epoch, ct, what="remote-author post")

    # ── Authority-only items: approved posts and removals ────────────────

    async def _relay_approved(
        self, event: SpacePostCreated, space: "Space", seed: bytes
    ) -> None:
        """Relay a remote member's post just released from the moderation
        queue, under its AUTHOR's signature: the submitter's household signed
        the post inner when it submitted the item, and the queue kept that
        copy (``event.public_relay``). It must be exactly the post being
        published (:func:`approved_relay_for`); it goes out marked
        ``approved_post`` with the author household's writer cert re-stamped
        for the relay epoch at ``comment`` scope at least — a plain member of
        a ``MODERATED`` space holds only that. The approver is never named.
        No signed copy (an older submitter) → not relayed: a seed holder
        never vouches for authorship on its own."""
        post = event.post
        inner = approved_relay_for(
            event.public_relay, post=post, space_id=event.space_id
        )
        if inner is None:
            log.info(
                "space_public.outbound: approved post %s has no valid author-"
                "signed copy (older submitter?) — not relayed to followers",
                post.id,
            )
            return
        origin = str(inner.get("origin_instance_id") or "")
        try:
            epoch, ct = await self._encrypt_with_cert(
                event.space_id,
                inner,
                origin,
                expect_pk=str(inner.get("author_pk") or ""),
                required=True,
                required_scope=WRITER_SCOPE_COMMENT,
            )
        except _NoWriterCert:
            log.warning(
                "space_public.outbound: author household %s holds no writer "
                "seat — approved post %s not relayed (space=%s)",
                origin,
                post.id,
                event.space_id,
            )
            return
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — cannot "
                "relay approved post %s",
                event.space_id,
                post.id,
            )
            return
        await self._publish(
            event.space_id, seed, epoch, ct, what=f"approved post {post.id}"
        )

    async def _on_post_deleted(self, event: PostDeleted) -> None:
        if not event.space_id:
            return
        author = event.author_user_id
        if self._posts is not None:
            got = await self._posts.get(event.post_id)
            if got is None or got[0] != event.space_id:
                return  # nothing here that followers could hold
            if got[1].linked_event_id is not None:
                return  # a calendar-derived post was never relayed
            author = author or got[1].author
        await self._relay_removal(
            event.space_id, REMOVAL_TARGET_POST, event.post_id, event.post_id, author
        )

    async def _on_comment_deleted(self, event: CommentDeleted) -> None:
        if not event.space_id:
            return
        author = event.author_user_id
        if self._posts is not None:
            comment = await self._posts.get_comment(event.comment_id)
            if comment is None:
                return
            author = author or comment.author
        await self._relay_removal(
            event.space_id,
            REMOVAL_TARGET_COMMENT,
            event.comment_id,
            event.post_id,
            author,
        )

    async def _relay_removal(
        self, space_id: str, target: str, item_id: str, post_id: str, author: str
    ) -> None:
        """Relay a post or comment removal applied here — a moderator's, an
        admin's or the author's own, local or federated (the host path
        already authorized it) — to the space's GFS followers, on this seed
        holder's authority. The notice names the item and its author (for
        the follower's tombstone rule) — never who removed it or why. Inside
        the ciphertext and padded, so the connection server cannot tell it
        from a short post. A follower that already applied it is unaffected
        by a duplicate."""
        space = await self._spaces.get(space_id)
        if space is None or space.space_type not in PUBLIC_SPACE_TIERS:
            return
        if not space.features.allow_subscribers:
            return
        if archive_refusal(space, "") is not None:
            return  # read-only: followers drop it anyway
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return  # only a seed holder speaks for the space
        try:
            removal = AuthorityRemoval(
                space_id=space_id,
                target=target,
                item_id=item_id,
                post_id=post_id,
                author_user_id=author,
            )
        except ValueError:
            log.warning(
                "space_public.outbound: unusable %s id for a removal in space %s "
                "— not relayed",
                target,
                space_id,
            )
            return
        try:
            epoch, ct = await self._crypto.encrypt(
                space_id, pad_json_object(removal.to_inner())
            )
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — cannot "
                "relay a %s removal",
                space_id,
                target,
            )
            return
        await self._publish(space_id, seed, epoch, ct, what=f"{target} removal")

    async def _publish(
        self, space_id: str, seed: bytes, epoch: int, ct: str, *, what: str
    ) -> None:
        """Authority-sign ``{space_id, epoch, encrypted_payload}`` under
        ``space_post_public`` and hand it to every GFS the space is on."""
        envelope: dict = {
            "space_id": space_id,
            "epoch": epoch,
            "encrypted_payload": ct,
        }
        envelope.update(
            sign_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                space_id=space_id,
                payload=strip_authority_sig_fields(envelope),
                space_seed=seed,
            )
        )
        try:
            await self._gfs.publish_space_event(
                space_id=space_id,
                event_type=AUTHORITY_EVENT_SPACE_POST_PUBLIC,
                payload=envelope,
            )
        except Exception:
            log.exception(
                "space_public.outbound: %s relay failed for space=%s", what, space_id
            )

    # ── v_49 writer certs ─────────────────────────────────────────────────

    @staticmethod
    def _remote_cert_ok(space, relay: dict) -> bool:
        """The cert a remote author attached: authority-signed for this
        space, naming the inner's ``author_pk``, at ``write`` scope. Checked
        at the epoch it names — the relay re-stamps it for the current one."""
        raw = relay.get(WRITER_CERT_FIELD)
        try:
            author_pk = bytes.fromhex(str(relay.get("author_pk") or ""))
            epoch = int(raw["epoch"]) if isinstance(raw, dict) else -1
        except ValueError, TypeError, KeyError:
            return False
        return SpaceWriterCertService.check_item_cert(
            space,
            raw,
            epoch=epoch,
            author_pk=author_pk,
            required_scope=WRITER_SCOPE_WRITE,
        )

    async def _legacy_hint_ok(self, origin: str | None, relay: dict) -> bool:
        """A hint with NO cert: only from a pre-v_49 origin (a v_49 one
        always attaches it — its absence is a stripped cert), and only when
        its ``author_pk`` is the origin household's key — the one we pin for
        it, or for a mesh-only household (no row) the key its instance id
        derives from, as the v_31 routed-origin check verified it."""
        assert self._writer_certs is not None
        if not origin:
            return False
        if await self._writer_certs.peer_is_cert_aware(origin):
            log.warning(
                "space_public.outbound: v_49 household %s sent a public_relay "
                "without a writer cert — not relaying",
                origin,
            )
            return False
        try:
            claimed = bytes.fromhex(str(relay.get("author_pk") or ""))
        except ValueError:
            claimed = None
        verified = await self._writer_certs.verified_instance_pk(
            origin, claimed=claimed
        )
        if verified is None or verified != claimed:
            log.warning(
                "space_public.outbound: public_relay author_pk is not %s's "
                "identity key — not relaying",
                origin,
            )
            return False
        return True

    async def _encrypt_with_cert(
        self,
        space_id: str,
        inner: dict,
        instance_id: str,
        *,
        expect_pk: str = "",
        required: bool = False,
        required_scope: str = WRITER_SCOPE_WRITE,
    ) -> tuple[int, str]:
        """Encrypt ``inner`` with ``instance_id``'s writer cert for the epoch
        it is encrypted under (re-read once if a rotation lands in between).
        Without a cert service, or with no cert for that household that
        permits ``required_scope``, the inner goes as is — unless ``required``
        (a re-stamp), which raises :class:`_NoWriterCert`. ``expect_pk`` pins the cert to the author key
        the inner names. Raises ``RuntimeError`` when there is no content key.
        """
        if self._writer_certs is None:
            if required:
                raise _NoWriterCert(instance_id)
            return await self._crypto.encrypt(space_id, pad_json_object(inner))
        for _attempt in range(2):
            current = await self._crypto.get_current_epoch(space_id)
            if current is None:
                raise RuntimeError(f"no content key for space {space_id}")
            if instance_id == self._own_instance_id:
                cert = await self._writer_certs.own_cert(space_id, current)
            else:
                hint: bytes | None
                try:
                    hint = bytes.fromhex(expect_pk) if expect_pk else None
                except ValueError:
                    hint = None
                cert = await self._writer_certs.issue_for_instance(
                    space_id, instance_id, epoch=current, instance_pk_hint=hint
                )
            if cert is not None and expect_pk:
                named = SpaceWriterCertService.cert_instance_pk(cert.to_wire())
                if named is None or named.hex() != expect_pk:
                    cert = None
            # Never put a cert on an item it does not authorize (a comment
            # cert on a post).
            if cert is not None and not scope_permits(cert.scope, required_scope):
                cert = None
            if cert is None and required:
                raise _NoWriterCert(instance_id)
            body = dict(inner)
            body.pop(WRITER_CERT_FIELD, None)
            if cert is not None:
                body[WRITER_CERT_FIELD] = cert.to_wire()
            epoch, ct = await self._crypto.encrypt(space_id, pad_json_object(body))
            if cert is None or epoch == cert.epoch:
                return epoch, ct
        log.warning(
            "space_public.outbound: content epoch kept moving for space %s — "
            "relaying without a writer cert",
            space_id,
        )
        if required:
            raise _NoWriterCert(instance_id)
        body = {k: v for k, v in inner.items() if k != WRITER_CERT_FIELD}
        return await self._crypto.encrypt(space_id, pad_json_object(body))


class _NoWriterCert(Exception):
    """A required writer-cert re-stamp was impossible (the household holds
    no writer seat, or its key does not match the inner's author)."""
