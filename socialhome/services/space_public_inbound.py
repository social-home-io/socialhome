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

**Authority kinds** (:mod:`socialhome.services.space_public_authority`).
A host-relay inner with NO ``author_sig`` can only be a **removal** notice,
its envelope's authority signature (step 1) the only authorizer
(soft-delete or tombstone, through
:meth:`SpaceItemInbound.apply_authority_removal`); anything else unsigned is
dropped. An author-signed post the seed holder marked ``approved_post``
(released from the moderation queue) gets every post check, and its writer
cert is REQUIRED but may be ``comment``-scope.

Member-published ``space_item`` frames (v_49) carry posts AND comments,
comment edits / deletes, the author's own post edits / deletes and
reactions; :meth:`SpacePublicInbound._on_space_item` runs the checks every
type shares and applies posts and post edits here, the rest through
:class:`~socialhome.services.space_item_inbound.SpaceItemInbound`.

Attribution comes from the **encrypted, authority-signed inner only**. The
fan-out frame carries no household identity (the GFS never learns which
household relayed); a frame from an older GFS may still carry an outer
``from_instance``, and that value is ignored — never read, never logged.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

import orjson
from cryptography.exceptions import InvalidTag

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    UnsupportedAuthoritySuite,
    strip_authority_sig_fields,
    verify_authority_event,
)
from ..crypto import derive_instance_id
from ..domain.events import PostEdited, SpacePostCreated
from ..domain.gfs_member_publish import (
    SPACE_ITEM_EVENT_TYPE,
    InvalidMemberPublish,
    SpaceItemFrame,
)
from ..domain.link_preview import card_survives_edit
from ..domain.post import FEED_POST_MAX_IMAGES, LocationData, Post, PostType
from ..domain.presence import truncate_coord
from ..domain.space import SpaceRole
from ..domain.space_item import (
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    ITEM_TYPE_AUTHOR_PROFILE,
    ITEM_TYPE_POST_EDIT,
    POST_SHAPED_ITEM_TYPES,
    AuthorityRemoval,
    item_stamp,
    required_scope,
    stamp_to_db,
)
from ..domain.writer_cert import WRITER_SCOPE_COMMENT, WRITER_SCOPE_WRITE, WriterCert
from ..federation.pending_parent_buffer import (
    AwaitingParent,
    PendingParentBuffer,
    Replay,
)
from ..federation.space_scope import archive_refusal
from ..infrastructure.event_bus import EventBus
from ..utils.datetime import parse_iso8601_lenient
from ..writer_cert import verify_writer_users
from .gfs_member_publish_service import parse_item_plaintext
from .inbound_media_store import local_media_ref, local_media_refs
from .link_preview_service import wire_link_preview
from .space_crypto_service import MissingEpochKey
from .space_item_author import (
    UnsupportedItemAuthorSigSuite,
    verify_signed_item_inner,
)
from .space_item_inbound import SpaceItemInbound
from .space_public_author import (
    UnsupportedLinkPreviewSigSuite,
    verified_link_preview,
    verify_signed_author_inner,
)
from .space_public_authority import is_approved, is_authority_inner
from .space_writer_cert_service import WRITER_CERT_FIELD, SpaceWriterCertService

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..federation.space_authorship import SpaceAuthorship
    from ..repositories.space_author_profile_repo import (
        AbstractSpaceAuthorProfileRepo,
    )
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from .pending_decrypts_cache import PendingDecryptsCache
    from .space_authority_pin import AuthorityPinRefresher
    from .space_crypto_service import SpaceContentEncryption
    from .space_mentions import SpaceMentionResolver

log = logging.getLogger(__name__)

#: The most relayed frames held for a missing space key at once (#5b),
#: oldest dropped first. A follower needs a handful: the frames of one
#: rotation it was offline for.
RELAY_KEY_WAIT_MAX_FRAMES: int = 64


class SpacePublicInbound:
    """GFS-relay → local-persist consumer for public/global space posts."""

    __slots__ = (
        "_authorship",
        "_bus",
        "_crypto",
        "_items",
        "_key_pull",
        "_mentions",
        "_own_instance_id",
        "_pending_decrypts",
        "_pending_parents",
        "_pin_refresher",
        "_posts",
        "_spaces",
        "_writer_certs",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        space_post_repo: AbstractSpacePostRepo,
        mention_resolver: SpaceMentionResolver | None = None,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        self._posts = space_post_repo
        self._own_instance_id: str = ""
        #: Resolves @-mentions in the decrypted relayed post against this
        #: household's view of the space's members. ``None`` → no mentions.
        self._mentions = mention_resolver
        self._pin_refresher: AuthorityPinRefresher | None = None
        #: v_49 — the writer-cert service. Host-signed frames (all of them
        #: today) skip its epoch-freshness gate; member-authorized items
        #: (PR 2) will run it.
        self._writer_certs: SpaceWriterCertService | None = None
        #: v_49 — a member household's own view of the access levels and the
        #: roster, run on member-published items as defence in depth.
        self._authorship: SpaceAuthorship | None = None
        #: v_49 — applies member-published comments, deletes and reactions.
        self._items = SpaceItemInbound(
            bus=bus,
            space_repo=space_repo,
            space_post_repo=space_post_repo,
            mention_resolver=mention_resolver,
        )
        #: Holds an item whose post (or parent comment) has not landed here
        #: yet and re-runs its frame when it does. ``None`` drops instead.
        self._pending_parents: PendingParentBuffer | None = None
        #: #5b — asks a server that seats us for a space key we miss, and
        #: holds the frame until that key lands. ``None``: dropped.
        self._key_pull: Callable[[str], Awaitable[bool]] | None = None
        self._pending_decrypts: PendingDecryptsCache | None = None

    def attach_key_pull(
        self,
        pull: Callable[[str], Awaitable[bool]],
        pending: PendingDecryptsCache,
    ) -> None:
        """Wire the #5b key pull: a frame under an epoch we hold no key for
        is held in *pending* (replayed on :class:`SpaceContentKeyImported`)
        and *pull* asks for the key (``GfsSpaceMirrorService
        .request_space_key`` — rate-limited, only where we hold a seat)."""
        self._key_pull = pull
        self._pending_decrypts = pending

    async def _await_key(
        self, space_id: str, epoch: int, replay: Callable[[], Awaitable[None]]
    ) -> None:
        """Hold *replay* until *space_id*'s *epoch* key lands, and ask for
        it. Fail-soft: a failed request still leaves the frame held (a key
        that arrives another way replays it).

        Only for an epoch AFTER the newest key we hold (or with none held):
        the owner seals its current key, so an older missing epoch would
        never come — and the frame epoch is plaintext, so a relay could
        otherwise fill the hold with frames no key will ever open. The hold
        is this path's own bounded cache, never the sync one."""
        if self._pending_decrypts is None or self._key_pull is None:
            return
        held = await self._crypto.get_current_epoch(space_id)
        if held is not None and epoch <= held:
            return
        self._pending_decrypts.stash(space_id, epoch, replay)
        try:
            await self._key_pull(space_id)
        except Exception:
            log.exception(
                "space_public.inbound: asking for the key of space %s failed",
                space_id,
            )

    def attach_pending_parents(self, buffer: PendingParentBuffer) -> None:
        """Wire the parent-wait buffer (shared with the federated path)."""
        self._pending_parents = buffer

    def attach_authorship(self, authorship: SpaceAuthorship) -> None:
        """Wire the access-level / seat check for ``space_item``."""
        self._authorship = authorship

    def attach_author_profiles(self, repo: AbstractSpaceAuthorProfileRepo) -> None:
        """Wire the v_60 follower-side author profile cache."""
        self._items.attach_author_profiles(repo)

    def attach_writer_certs(self, writer_certs: SpaceWriterCertService) -> None:
        """Wire the v_49 writer-cert service."""
        self._writer_certs = writer_certs

    def attach_pin_refresher(self, refresher: AuthorityPinRefresher) -> None:
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

    async def handle_channel_item(
        self, space_id: str, *, epoch: int, payload: str
    ) -> None:
        """A member item relayed through a PRIVATE space's opaque channel
        (v_51). The channel frame names no space — the caller mapped its
        channel id to ``space_id`` locally — and carries no cert; the item
        then gets exactly the checks of a strict-mode ``space_item`` (the
        writer cert from inside the ciphertext against the pinned space key,
        the inner's signed ``space_id`` equal to this space, epoch
        freshness, the author signature, the roster)."""
        await self._on_space_item(
            {
                "space_id": space_id,
                "event_type": SPACE_ITEM_EVENT_TYPE,
                "epoch": epoch,
                "payload": payload,
            }
        )

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
        if not self._verify_authority(space_id, envelope, pinned_pk_hex):
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
            space = healed
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
        except MissingEpochKey:
            log.info(
                "space_public.inbound: no key for space %s epoch %s yet — "
                "holding the frame",
                space_id,
                epoch,
            )
            await self._await_key(
                space_id, epoch, functools.partial(self._on_relay, envelope)
            )
            return
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
            inner = orjson.loads(pt)
        except orjson.JSONDecodeError:
            log.warning(
                "space_public.inbound: undecodable inner payload for space %s",
                space_id,
            )
            return
        if not isinstance(inner, dict):
            return
        # No author signature → an authority removal notice, authorized by
        # the envelope's authority signature verified above. An inner WITH an
        # author signature is always a post, whatever else it claims.
        if is_authority_inner(inner):
            try:
                await self._on_authority_inner(space, inner)
            except AwaitingParent as wait:
                self._hold_for_parent(
                    wait,
                    space_id=space_id,
                    token=_digest("relay", ciphertext),
                    # The relay frame names no household: the notice is
                    # charged to the space's host, its only signer.
                    sender=space.owner_instance_id or space_id,
                    size=_retained_bytes(envelope),
                    replay=functools.partial(self._on_relay, envelope),
                    label=f"space_public removal in space {space_id}",
                )
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
        # A post a seed holder released from the moderation queue: its author
        # household may hold only a comment-scope cert (a plain member of a
        # MODERATED space), and the authority's mark — outside the author
        # signature, under the authority signature — vouches for the
        # approval. The cert is REQUIRED then: it proves the author's
        # household holds a seat this epoch.
        approved = is_approved(inner)
        if approved and inner.get(WRITER_CERT_FIELD) is None:
            log.warning(
                "space_public.inbound: approved post %s without a writer cert "
                "— dropped",
                post_id,
            )
            return
        # v_49 — a v_49 author's item carries its household's writer cert;
        # present means it MUST hold (an absent one is a pre-v_49 author).
        if inner.get(WRITER_CERT_FIELD) is not None and not await self._writer_cert_ok(
            space,
            inner,
            epoch=epoch,
            pinned_pk_hex=pinned_pk_hex,
            required_scope=WRITER_SCOPE_COMMENT if approved else WRITER_SCOPE_WRITE,
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

    # ── Authority-only notices (host relay) ──────────────────────────────

    async def _on_authority_inner(self, space: Space, inner: dict) -> None:
        """An inner with no author signature, under a verified authority
        signature: a removal notice (see
        :mod:`~socialhome.services.space_public_authority`). Anything else is
        dropped — an approved post always carries its author's signature."""
        if str(inner.get("space_id") or "") != space.id:
            log.warning(
                "space_public.inbound: authority notice names another space "
                "(inner=%s envelope=%s) — dropped",
                inner.get("space_id"),
                space.id,
            )
            return
        kind = inner.get(AUTHORITY_KIND_FIELD)
        if kind == AUTHORITY_KIND_REMOVAL:
            try:
                removal = AuthorityRemoval.from_inner(inner)
            except ValueError as exc:
                log.warning(
                    "space_public.inbound: malformed removal for space %s (%s) "
                    "— dropped",
                    space.id,
                    exc,
                )
                return
            await self._items.apply_authority_removal(space=space, removal=removal)
            return
        log.warning(
            "space_public.inbound: relayed inner for space %s has no author "
            "signature and is no removal — dropped",
            space.id,
        )

    # ── Member-published items (v_49 trusted mode) ───────────────────────

    async def _on_space_item(self, frame: dict[str, Any]) -> None:
        """A ``space_item`` a member household published itself over the GFS.

        No authority signature — the authorizer is the author household's
        writer cert, checked HERE with epoch freshness and the scope the real
        item type needs (the GFS can see neither): decrypt; read the real
        type and the author-signed inner; drop our own echo; verify the
        author signature + self-cert (the post-shaped inner of a ``post`` /
        ``post_edit`` also its owner-bound id; the generic inner of every
        other type its own domain and suite); require the author-bound
        ``item_type`` (and for a post-shaped inner ``item_target ==
        post_id``); require the origin to be the author's household and the
        inner cert (identical to the frame's) valid for this space, the
        frame's epoch (a strict-mode frame, v_50, carries no plaintext cert —
        the inner one is then the only one), the inner's ``author_pk`` and
        :func:`~socialhome.domain.space_item.required_scope`, at an epoch
        still open here, with a v2 user binding naming the author. Then
        apply by type: a post dedupes by id; a post edit lands last-writer-
        wins (:meth:`_apply_post_edit`); every other type goes to
        :class:`SpaceItemInbound`."""
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
        except MissingEpochKey:
            log.info(
                "space_public.inbound: no key for space %s epoch %s yet — "
                "holding the space_item",
                item.space_id,
                item.epoch,
            )
            await self._await_key(
                item.space_id,
                item.epoch,
                functools.partial(self._on_space_item, frame),
            )
            return
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
        verified = await self._verified_item(
            space,
            epoch=item.epoch,
            item_type=item_type,
            inner=inner,
            frame_cert=item.writer_cert,
        )
        if verified is None:
            return
        inner_cert, item_id, author_user_id, origin_instance_id = verified
        post_shaped = item_type in POST_SHAPED_ITEM_TYPES
        member_household = await self._holds_writer_seat(item.space_id)
        if not post_shaped:
            try:
                await self._items.apply(
                    space=space,
                    item_type=item_type,
                    inner=inner,
                    origin_instance_id=origin_instance_id,
                    cert_scope=inner_cert.scope,
                    member_household=member_household,
                    authorship=self._authorship,
                    epoch=item.epoch,
                )
            except AwaitingParent as wait:
                # Re-run the whole frame when the parent lands: decrypt,
                # writer cert, epoch, author signature and roster again.
                self._hold_for_parent(
                    wait,
                    space_id=item.space_id,
                    token=_digest("item", item.payload),
                    sender=origin_instance_id,
                    size=_retained_bytes(frame),
                    replay=functools.partial(self._on_space_item, frame),
                    label=f"space_item {item_type} {item_id} from {origin_instance_id}",
                )
            return
        # Defence in depth on a member household, which holds the roster and
        # the access levels: the author's own seat must let them post here
        # (a post edit needs the same ``write`` right as the post).
        if (
            self._authorship is not None
            and member_household
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
                item_id,
            )
            return
        if item_type == ITEM_TYPE_POST_EDIT:
            await self._apply_post_edit(
                item.space_id, item_id, author_user_id, origin_instance_id, inner
            )
            return
        await self._persist(
            item.space_id, item_id, author_user_id, origin_instance_id, inner
        )

    async def _verified_item(
        self,
        space: Space,
        *,
        epoch: int,
        item_type: str,
        inner: dict[str, Any],
        frame_cert: object | None,
        fresh_epoch: bool = True,
    ) -> tuple[WriterCert, str, str, str] | None:
        """Every check a member-published item gets before it is applied
        (see :meth:`_on_space_item`), shared with the v_61 new-follower
        bundle (:meth:`apply_bundled_profiles`): ``(inner_cert, item_id,
        author_user_id, origin_instance_id)``, or ``None`` (logged)."""
        if self._writer_certs is None:
            return None
        post_shaped = item_type in POST_SHAPED_ITEM_TYPES
        item_id = str(
            (inner.get("post_id") if post_shaped else inner.get("item_target")) or ""
        )
        author_user_id = str(inner.get("author_user_id") or "")
        origin_instance_id = str(inner.get("origin_instance_id") or "")
        if origin_instance_id and origin_instance_id == self._own_instance_id:
            log.debug("space_public.inbound: self-echo for item %s — dropped", item_id)
            return None
        if not _item_author_verified(inner, post_shaped=post_shaped):
            log.warning(
                "space_public.inbound: author verification failed for space_item "
                "%s in space %s",
                item_id,
                space.id,
            )
            return None
        if str(inner.get("space_id") or "") != space.id:
            log.warning(
                "space_public.inbound: space_item %s names another space — dropped",
                item_id,
            )
            return None
        # The real type and its target are bound inside the author signature
        # (verified above): nobody holding the content key can re-wrap a
        # signed item as another kind of item, or point it at another row.
        if inner.get("item_type") != item_type or (
            post_shaped and inner.get("item_target") != item_id
        ):
            log.warning(
                "space_public.inbound: space_item %s has no author-bound type — "
                "dropped",
                item_id,
            )
            return None
        if not _origin_is_author_household(inner, origin_instance_id):
            log.warning(
                "space_public.inbound: space_item %s names an origin that is not "
                "the author's household — dropped",
                item_id,
            )
            return None
        raw_cert = inner.get(WRITER_CERT_FIELD)
        try:
            inner_cert = WriterCert.from_wire(raw_cert)
        except ValueError:
            inner_cert = None
        # A trusted-mode frame carries only the v1 fields (the binding never
        # leaves the ciphertext): they must be exactly the inner cert's. A
        # strict-mode frame (v_50) carries no cert at all — the inner one is
        # the only one, and it gets every check below either way.
        if inner_cert is None or (
            frame_cert is not None and inner_cert.v1() != frame_cert
        ):
            log.warning(
                "space_public.inbound: space_item %s cert differs from the frame's "
                "— dropped",
                item_id,
            )
            return None
        try:
            author_pk = bytes.fromhex(str(inner.get("author_pk") or ""))
        except ValueError:
            return None
        # A bundled item (``fresh_epoch=False``) is checked at the epoch it
        # was published at, with no freshness: the bundle is authority-signed
        # and sealed to us, and its sender, a seed holder, could mint a
        # current cert anyway. A key rotation then never forces a republish.
        cert_ok = (
            await self._writer_certs.check_item(
                space,
                raw_cert,
                epoch=epoch,
                author_pk=author_pk,
                required_scope=required_scope(item_type),
            )
            if fresh_epoch
            else self._writer_certs.check_item_cert(
                space,
                raw_cert,
                epoch=epoch,
                author_pk=author_pk,
                required_scope=required_scope(item_type),
            )
        )
        if not cert_ok:
            log.warning(
                "space_public.inbound: writer cert refused for space_item %s (%s) "
                "in space %s — dropped",
                item_id,
                item_type,
                space.id,
            )
            return None
        # v2 user binding: the cert names the household's users that hold its
        # scope — a member-published item requires it, and its author must
        # be one.
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
                item_id,
                exc,
            )
            return None
        return inner_cert, item_id, author_user_id, origin_instance_id

    async def apply_bundled_profiles(
        self,
        space_id: str,
        *,
        items: list[tuple[int, dict]],
        pictures: Sequence[tuple[str, str, str]] = (),
    ) -> int:
        """Apply one opened part of a v_61 new-follower bundle. The part was
        opened and authority-verified by
        :class:`~socialhome.services.space_subscriber_key_inbound.SpaceSubscriberKeyInbound`.

        ``items`` are ``(epoch, author-signed author_profile item)``. Each
        gets every check a relayed item gets: author signature, author-bound
        type, origin, and a writer cert with a v2 user binding at ITS OWN
        epoch, though not a fresh one (see :meth:`_verified_item`). Anything
        but a profile is refused. ``pictures`` are
        ``(author, signed hash, base64 WebP)``, kept only when the bytes hash
        to the held profile's signed hash. Returns how many profiles and
        pictures were stored. Never raises."""
        space = await self._spaces.get(space_id)
        if space is None or not space.identity_public_key or self._writer_certs is None:
            return 0
        stored = 0
        for epoch, raw in items:
            if not isinstance(raw, dict):
                continue
            if raw.get("item_type") != ITEM_TYPE_AUTHOR_PROFILE:
                log.warning(
                    "space_public.inbound: a follower bundle for space %s carries "
                    "a %r item — dropped",
                    space_id,
                    raw.get("item_type"),
                )
                continue
            try:
                verified = await self._verified_item(
                    space,
                    epoch=epoch,
                    item_type=ITEM_TYPE_AUTHOR_PROFILE,
                    inner=raw,
                    frame_cert=None,
                    fresh_epoch=False,
                )
                if verified is None:
                    continue
                inner_cert, _id, _author, origin = verified
                if await self._items.apply(
                    space=space,
                    item_type=ITEM_TYPE_AUTHOR_PROFILE,
                    inner=raw,
                    origin_instance_id=origin,
                    cert_scope=inner_cert.scope,
                    member_household=await self._holds_writer_seat(space_id),
                    authorship=self._authorship,
                    epoch=epoch,
                ):
                    stored += 1
            except Exception:
                log.exception(
                    "space_public.inbound: a bundled profile for space %s failed",
                    space_id,
                )
        for author, picture_hash, picture in pictures:
            try:
                if await self._items.attach_bundled_picture(
                    space_id, author, picture_hash, picture
                ):
                    stored += 1
            except Exception:
                log.exception(
                    "space_public.inbound: a bundled picture for space %s failed",
                    space_id,
                )
        return stored

    async def _apply_post_edit(
        self,
        space_id: str,
        post_id: str,
        author_user_id: str,
        origin_instance_id: str,
        inner: dict,
    ) -> None:
        """The author's own edit of their post (v_49 ``post_edit``): the full
        signed snapshot plus its signed ``edited_at``.

        * Held here: only the post's author, in this space, on a live post,
          and only over an older stored stamp (last writer wins).
        * Not held yet — the edit overtook its create: the snapshot IS the
          post, at its newest content, admitted under the post's own rules
          (already checked by the caller); the later create is a duplicate.
        * A deleted post stays deleted.
        """
        stamp = item_stamp(inner.get("edited_at"))
        content = inner.get("content")
        if stamp is None or not isinstance(content, str):
            log.warning(
                "space_public.inbound: post edit %s without a valid stamp or "
                "content — dropped",
                post_id,
            )
            return
        got = await self._posts.get(post_id)
        if got is None:
            post = self._post_from_inner(post_id, author_user_id, inner)
            if await self._posts.save(space_id, post) is None:
                log.warning(
                    "space_public.inbound: post %s already exists in another space "
                    "— refusing the relayed write",
                    post_id,
                )
                return
            await self._posts.edit(
                post_id, content, space_id=space_id, edited_at=stamp_to_db(stamp)
            )
            refreshed = await self._posts.get(post_id)
            if refreshed is None:
                return
            await self._bus.publish(
                SpacePostCreated(
                    post=refreshed[1],
                    space_id=space_id,
                    mentions=(
                        await self._mentions.resolve(
                            space_id, content, author_id=author_user_id
                        )
                        if self._mentions is not None
                        else ()
                    ),
                    origin_instance_id=origin_instance_id,
                )
            )
            return
        row_space, row = got
        if row_space != space_id or row.author != author_user_id:
            log.warning(
                "space_public.inbound: post edit %s by someone other than its "
                "author, or for another space — dropped",
                post_id,
            )
            return
        if row.deleted:
            log.debug(
                "space_public.inbound: post %s is deleted — edit dropped", post_id
            )
            return
        if not await self._posts.edit(
            post_id,
            content,
            space_id=space_id,
            clear_link_preview=row.link_preview is not None
            and not card_survives_edit(row.content, content),
            edited_at=stamp_to_db(stamp),
        ):
            log.debug(
                "space_public.inbound: post %s holds a newer edit — dropped", post_id
            )
            return
        refreshed = await self._posts.get(post_id)
        if refreshed is None:
            return
        await self._bus.publish(
            PostEdited(
                post=refreshed[1],
                space_id=space_id,
                origin_instance_id=origin_instance_id,
                new_mentions=(
                    await self._mentions.added(
                        space_id, row.content, content, author_id=author_user_id
                    )
                    if self._mentions is not None
                    else ()
                ),
            )
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _hold_for_parent(
        self,
        wait: AwaitingParent,
        *,
        space_id: str,
        token: str,
        sender: str,
        size: int,
        replay: Replay,
        label: str,
    ) -> None:
        """Hold a frame whose parent is not here yet; dropped (logged at
        INFO — by the buffer when it is full) when it cannot be held."""
        if self._pending_parents is None:
            log.info("%s: %s — dropped", label, wait)
            return
        self._pending_parents.hold(
            space_id=space_id,
            kind=wait.kind,
            parent_id=wait.parent_id,
            token=token,
            sender=sender,
            size=size,
            replay=replay,
            label=label,
        )

    async def _holds_writer_seat(self, space_id: str) -> bool:
        """We are a member household here (a local non-follower seat), so we
        hold the roster and the access levels."""
        return any(
            str(m.role) != SpaceRole.SUBSCRIBER.value
            for m in await self._spaces.list_members(space_id)
        )

    async def _writer_cert_ok(
        self,
        space,
        inner: dict,
        *,
        epoch: int,
        pinned_pk_hex: str,
        required_scope: str = WRITER_SCOPE_WRITE,
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
            required_scope=required_scope,
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


def _retained_bytes(frame: dict) -> int:
    """What a held replay keeps alive: the whole frame it re-runs."""
    try:
        return len(orjson.dumps(frame))
    except TypeError, ValueError:
        return len(repr(frame))


def _digest(kind: str, ciphertext: str) -> str:
    """A short dedupe token for a held frame — two copies of one frame (from
    two GFSs) share it; the buffer keeps 64 hex chars, not the ciphertext."""
    return f"{kind}:{hashlib.sha256(ciphertext.encode()).hexdigest()}"


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


def _item_author_verified(inner: dict, *, post_shaped: bool) -> bool:
    """The inner's author signature and self-cert, by its shape: the post
    inner (``space_public_author``, incl. the owner-bound post id) for a
    ``post`` / ``post_edit``, the generic item inner
    (``space_item_author``, its own domain and suite) for every other type.
    An unknown generic suite is a refusal, never a fallback."""
    if post_shaped:
        return verify_signed_author_inner(inner)
    try:
        return verify_signed_item_inner(inner)
    except UnsupportedItemAuthorSigSuite:
        return False
