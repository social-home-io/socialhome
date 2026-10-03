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
   A space that is **archived** (or dissolved) here is a read-only
   snapshot — the relayed post is dropped before any further work, the
   same rule every other inbound door enforces.
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
4b. **Writer cert (v_49)** — when the inner carries the author household's
   ``writer_cert``, it must verify against the pinned space key, name this
   space and the envelope's epoch, name the inner's ``author_pk`` and grant
   ``write``; otherwise the item is dropped (WARNING). No epoch freshness
   gate here: the frame's authority signature is the authorizer and the host
   re-stamped the cert at its own current epoch, so an old epoch is late
   delivery (freshness applies to member-authorized items, PR 2). An inner
   with NO cert
   is a pre-v_49 author and keeps today's behaviour (authorized by the
   relaying seed holder's authority signature) — the migration tripwire.
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
from ..domain.gfs_member_publish import (
    SPACE_ITEM_EVENT_TYPE,
    InvalidMemberPublish,
    SpaceItemFrame,
)
from ..domain.post import FEED_POST_MAX_IMAGES, LocationData, Post, PostType
from ..domain.presence import truncate_coord
from ..crypto import derive_instance_id
from ..domain.space import SpaceRole
from ..domain.writer_cert import WRITER_SCOPE_WRITE, WriterCert
from ..writer_cert import verify_writer_users
from ..federation.space_scope import archive_refusal
from ..infrastructure.event_bus import EventBus
from ..utils.datetime import parse_iso8601_lenient
from .gfs_member_publish_service import parse_item_plaintext
from .inbound_media_store import local_media_ref, local_media_refs
from .link_preview_service import wire_link_preview
from .space_public_author import (
    UnsupportedLinkPreviewSigSuite,
    verified_link_preview,
    verify_signed_author_inner,
)
from .space_writer_cert_service import WRITER_CERT_FIELD, SpaceWriterCertService

if TYPE_CHECKING:
    from ..federation.space_authorship import SpaceAuthorship
    from .space_authority_pin import AuthorityPinRefresher
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .space_crypto_service import SpaceContentEncryption
    from .space_mentions import SpaceMentionResolver

log = logging.getLogger(__name__)


class SpacePublicInbound:
    """GFS-relay → local-persist consumer for public/global space posts."""

    __slots__ = (
        "_bus",
        "_spaces",
        "_crypto",
        "_posts",
        "_own_instance_id",
        "_mentions",
        "_pin_refresher",
        "_writer_certs",
        "_authorship",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: "AbstractSpaceRepo",
        space_crypto: "SpaceContentEncryption",
        space_post_repo: "AbstractSpacePostRepo",
        mention_resolver: "SpaceMentionResolver | None" = None,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        self._posts = space_post_repo
        self._own_instance_id: str = ""
        #: Resolves @-mentions in the decrypted relayed post against this
        #: household's view of the space's members. ``None`` → no mentions.
        self._mentions = mention_resolver
        self._pin_refresher: "AuthorityPinRefresher | None" = None
        #: v_49 — the writer-cert service. Host-signed frames (all of them
        #: today) skip its epoch-freshness gate; member-authorized items
        #: (PR 2) will run it.
        self._writer_certs: SpaceWriterCertService | None = None
        #: v_49 — a member household's own view of the access levels and the
        #: roster, run on member-published items as defence in depth.
        self._authorship: "SpaceAuthorship | None" = None

    def attach_authorship(self, authorship: "SpaceAuthorship") -> None:
        """Wire the access-level / seat check for ``space_item``."""
        self._authorship = authorship

    def attach_writer_certs(self, writer_certs: SpaceWriterCertService) -> None:
        """Wire the v_49 writer-cert service."""
        self._writer_certs = writer_certs

    def attach_pin_refresher(self, refresher: "AuthorityPinRefresher") -> None:
        """Wire the lazy pin heal (v_44): on an authority failure the
        subscriber re-fetches the GFS listing's owner cert once."""
        self._pin_refresher = refresher

    def attach_identity(self, *, own_instance_id: str) -> None:
        """Wire our own instance id — the self-echo guard's only input."""
        self._own_instance_id = own_instance_id

    async def handle(self, frame: dict[str, Any], *, gfs_id: str | None = None) -> None:
        """Dispatch one GFS relay frame: ``space_post_public`` (host relay)
        or ``space_item`` (v_49 member publish). Other frames are ignored so
        this can sit on the generic relay channel.

        The frame carries no household identity — an outer ``from_instance``
        from an older GFS is neither read nor logged.
        """
        if frame.get("event_type") == SPACE_ITEM_EVENT_TYPE:
            await self._on_space_item(frame)
            return
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
        # Read-only snapshot here, like every other inbound door. The relay
        # frame names no household, so there is no host to exempt: ask as
        # an anonymous sender.
        reason = archive_refusal(space, "")
        if reason is not None:
            log.info(
                "space_public.inbound: space %s is %s here (read-only) "
                "— relayed post dropped",
                space_id,
                reason,
            )
            return
        pinned_pk_hex = space.identity_public_key
        if not self._verify_authority(space_id, envelope, space.identity_public_key):
            # v_44 — the owner may have rotated the space authority key
            # since we pinned it: heal from the GFS listing's owner cert
            # (rate-limited) and verify ONCE more against the new pin.
            healed = None
            if self._pin_refresher is not None and (
                await self._pin_refresher.refresh_authority_pin(space_id)
            ):
                healed = await self._spaces.get(space_id)
            if healed is None or not self._verify_authority(
                space_id, envelope, healed.identity_public_key
            ):
                log.warning(
                    "space_public.inbound: authority signature failed for space %s",
                    space_id,
                )
                return
            pinned_pk_hex = healed.identity_public_key
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
        if not _origin_is_author_household(inner, origin_instance_id):
            log.warning(
                "space_public.inbound: post %s names an origin that is not the "
                "author's household — dropped",
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
        # v_49 — a v_49 author's item carries its household's writer cert;
        # present means it MUST hold (an absent one is a pre-v_49 author).
        if inner.get(WRITER_CERT_FIELD) is not None and not await self._writer_cert_ok(
            space, inner, epoch=epoch, pinned_pk_hex=pinned_pk_hex
        ):
            log.warning(
                "space_public.inbound: writer cert failed for space %s post %s "
                "— dropped",
                space_id,
                post_id,
            )
            return
        await self._persist(
            space_id, post_id, author_user_id, origin_instance_id, inner
        )

    async def _persist(
        self,
        space_id: str,
        post_id: str,
        author_user_id: str,
        origin_instance_id: str,
        inner: dict,
    ) -> None:
        """Dedupe by post id, save, publish — shared by both relay paths."""
        # Dedupe by post id — the GFS relay is at-least-once, and a
        # member-published item usually also arrives over federation.
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
                mentions=(
                    await self._mentions.resolve(
                        space_id, post.content, author_id=post.author
                    )
                    if self._mentions is not None
                    else ()
                ),
                origin_instance_id=origin_instance_id,
            )
        )

    # ── Member-published items (v_49 trusted mode) ───────────────────────

    async def _on_space_item(self, frame: dict[str, Any]) -> None:
        """A ``space_item`` a member household published itself over the GFS.

        No authority signature — the authorizer is the author household's
        writer cert, checked HERE with epoch freshness and the scope the real
        item type needs (the GFS can see neither): decrypt; read the real
        type and the author-signed inner; drop our own echo; verify the
        author signature + self-cert + owner-bound id; require the inner
        cert (identical to the frame's), valid for this space, the frame's
        epoch, the inner's ``author_pk`` and ``write``, at an epoch still
        open here; then dedupe by post id against the host-relayed /
        federated copy."""
        try:
            item = SpaceItemFrame.from_wire(frame)
        except InvalidMemberPublish:
            log.warning("space_public.inbound: malformed space_item frame — dropped")
            return
        space = await self._spaces.get(item.space_id)
        if space is None or not space.identity_public_key:
            log.info(
                "space_public.inbound: space_item for unknown space %s — dropped",
                item.space_id,
            )
            return
        if archive_refusal(space, "") is not None:
            return
        if self._writer_certs is None:
            return
        try:
            pt = await self._crypto.decrypt(item.space_id, item.epoch, item.payload)
        except (RuntimeError, ValueError, InvalidTag) as exc:
            log.info(
                "space_public.inbound: cannot decrypt space_item for %s epoch %s: %s",
                item.space_id,
                item.epoch,
                exc,
            )
            return
        parsed = parse_item_plaintext(pt)
        if parsed is None:
            log.warning(
                "space_public.inbound: unsupported space_item for space %s — dropped",
                item.space_id,
            )
            return
        item_type, inner = parsed
        post_id = str(inner.get("post_id") or "")
        author_user_id = str(inner.get("author_user_id") or "")
        origin_instance_id = str(inner.get("origin_instance_id") or "")
        if origin_instance_id and origin_instance_id == self._own_instance_id:
            log.debug("space_public.inbound: self-echo for item %s — dropped", post_id)
            return
        if not verify_signed_author_inner(inner):
            log.warning(
                "space_public.inbound: author verification failed for space_item "
                "%s in space %s",
                post_id,
                item.space_id,
            )
            return
        if str(inner.get("space_id") or "") != item.space_id:
            log.warning(
                "space_public.inbound: space_item %s names another space — dropped",
                post_id,
            )
            return
        # The real type and its target are bound inside the author signature
        # (verified above): nobody holding the content key can re-wrap a
        # signed post as another kind of item.
        if inner.get("item_type") != item_type or inner.get("item_target") != post_id:
            log.warning(
                "space_public.inbound: space_item %s has no author-bound type — "
                "dropped",
                post_id,
            )
            return
        if not _origin_is_author_household(inner, origin_instance_id):
            log.warning(
                "space_public.inbound: space_item %s names an origin that is not "
                "the author's household — dropped",
                post_id,
            )
            return
        raw_cert = inner.get(WRITER_CERT_FIELD)
        try:
            inner_cert = WriterCert.from_wire(raw_cert)
        except ValueError:
            inner_cert = None
        # The frame carries only the v1 fields (the binding never leaves the
        # ciphertext): they must be exactly the inner cert's.
        if inner_cert is None or inner_cert.v1() != item.writer_cert:
            log.warning(
                "space_public.inbound: space_item %s cert differs from the frame's "
                "— dropped",
                post_id,
            )
            return
        try:
            author_pk = bytes.fromhex(str(inner.get("author_pk") or ""))
        except ValueError:
            return
        if not await self._writer_certs.check_item(
            space,
            raw_cert,
            epoch=item.epoch,
            author_pk=author_pk,
            required_scope=WRITER_SCOPE_WRITE,
        ):
            log.warning(
                "space_public.inbound: writer cert refused for space_item %s in "
                "space %s — dropped",
                post_id,
                item.space_id,
            )
            return
        # v2 user binding: the cert names the household's users that may post
        # — a member-published item requires it, and its author must be one.
        try:
            verify_writer_users(
                inner_cert,
                space_pubkey=bytes.fromhex(space.identity_public_key),
                author_user_id=author_user_id,
            )
        except ValueError as exc:
            log.warning(
                "space_public.inbound: space_item %s author not bound by the "
                "writer cert (%s) — dropped",
                post_id,
                exc,
            )
            return
        # Defence in depth on a member household, which holds the roster and
        # the access levels: the author's own seat must let them post here.
        if (
            self._authorship is not None
            and await self._holds_writer_seat(item.space_id)
            and not await self._authorship.item_access_admits(
                origin_instance_id=origin_instance_id,
                space_id=item.space_id,
                feature="posts",
                author_user_id=author_user_id,
            )
        ):
            log.warning(
                "space_public.inbound: space_item %s refused by the posts access "
                "level / the author's seat here — dropped",
                post_id,
            )
            return
        await self._persist(
            item.space_id, post_id, author_user_id, origin_instance_id, inner
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _holds_writer_seat(self, space_id: str) -> bool:
        """We are a member household here (a local non-follower seat), so we
        hold the roster and the access levels."""
        return any(
            str(m.role) != SpaceRole.SUBSCRIBER.value
            for m in await self._spaces.list_members(space_id)
        )

    async def _writer_cert_ok(
        self, space, inner: dict, *, epoch: int, pinned_pk_hex: str
    ) -> bool:
        try:
            author_pk = bytes.fromhex(str(inner.get("author_pk") or ""))
        except ValueError:
            return False
        # Every frame on this path is authority-signed by a seed holder, which
        # re-stamped the cert at ITS current epoch: the authority signature is
        # the authorizer here, and an old epoch means late delivery, not
        # revocation — so NO freshness gate. Freshness
        # (``SpaceWriterCertService.check_item``) is for member-authorized,
        # cert-only items (PR 2). Any future catch-up / backfill path must not
        # run the freshness gate either: replayed history is old by design.
        return SpaceWriterCertService.check_item_cert(
            space,
            inner.get(WRITER_CERT_FIELD),
            epoch=epoch,
            author_pk=author_pk,
            required_scope=WRITER_SCOPE_WRITE,
            space_pubkey_hex=pinned_pk_hex,
        )

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
        # The card counts only under the author's own signature over it; a
        # relayer can strip it but not forge or alter it.
        try:
            link_preview = wire_link_preview(verified_link_preview(inner))
        except UnsupportedLinkPreviewSigSuite as exc:
            log.warning("relayed post %s: link preview dropped (%s)", post_id, exc)
            link_preview = None
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=inner.get("content"),
            link_preview=link_preview,
            media_url=local_media_ref(inner.get("media_url")),
            image_urls=local_media_refs(
                inner.get("image_urls"), limit=FEED_POST_MAX_IMAGES
            ),
            location=location,
            created_at=parse_iso8601_lenient(inner.get("created_at")),
            hidden_from_feed=bool(inner.get("hidden_from_feed", False)),
        )


def _origin_is_author_household(inner: dict, origin_instance_id: str) -> bool:
    """The inner's ``origin_instance_id`` must be the household whose key
    signed it: an instance id IS the fingerprint of the household identity
    key (§4.1.2), so a signed inner can't be credited to another household
    (which would also dodge the self-echo guard)."""
    try:
        return derive_instance_id(bytes.fromhex(str(inner.get("author_pk") or ""))) == (
            origin_instance_id
        )
    except ValueError:
        return False
