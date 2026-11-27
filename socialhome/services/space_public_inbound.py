"""Recipient-side consumer for a space's opaque channel items (v_51 / v_96).

Since v_96 the only GFS-borne space content a household takes is what rides
a space's opaque channel (:mod:`socialhome.services.gfs_channel_service`
maps the channel id to a space locally and calls
:meth:`SpacePublicInbound.handle_channel_item`). The space-keyed relay
(``space_post_public`` / ``space_item`` frames naming a ``space_id``) is
gone: a GFS still sending one has it dropped before parsing
(``app.dispatch_gfs_relay_frame``).

The pipeline is defence-in-depth — the channel and the GFS are never
trusted by the receiver:

1. **Space + archive** — a space we hold no pinned key for is dropped; an
   **archived** (or dissolved) space is a read-only snapshot, the item is
   dropped before any further work.
2. **Decrypt** — under the per-space content key for the stated epoch. An
   epoch we hold no key for yet is HELD (:meth:`attach_key_wait`) and
   replayed when the rekey lands by federation.
3. **Authority items** (v_96) — an item whose plaintext is a channel
   authority item carries the space authority signature over its inner
   (``space_public_authority.open_channel_authority``), binding this space
   and the frame's epoch. A **removal** is applied (soft-delete or
   tombstone, :meth:`SpaceItemInbound.apply_authority_removal`); a
   remote-author or ``approved_post`` copy gets every post check below,
   its writer cert REQUIRED for an approved post (``comment`` scope may
   do), optional for a pre-v_49 author (the migration tripwire).
4. **Member items** — the author-signed inner, its author-bound type and
   target, the origin being the author's household, the writer cert from
   inside the ciphertext (against the pinned space key, this space, the
   frame's epoch, the inner's ``author_pk``, the scope the real type needs,
   epoch freshness, a v2 user binding naming the author; a follower's
   ``reader`` cert only for what the receiver's own switches allow).
5. **Self-echo guard** — an inner ``origin_instance_id`` equal to our own
   instance id is dropped at DEBUG before any write.
6. **Dedupe + persist** — posts dedupe by id, post edits land
   last-writer-wins, every other type goes through
   :class:`~socialhome.services.space_item_inbound.SpaceItemInbound`; a
   saved post publishes :class:`SpacePostCreated` with
   ``origin_instance_id`` set (the federation bridge's loop guard skips
   re-fanning it).

Attribution comes from the **encrypted, signed inner only**; the channel
frame names no space and no household.
"""

from __future__ import annotations

import functools
import hashlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import orjson
from cryptography.exceptions import InvalidTag

from ..crypto import derive_instance_id
from ..domain.events import PostEdited, SpacePostCreated
from ..domain.gfs_channel import SPACE_ITEM_EVENT_TYPE
from ..domain.link_preview import card_survives_edit
from ..domain.media_constraints import space_media_ref
from ..domain.post import FEED_POST_MAX_IMAGES, LocationData, Post, PostType
from ..domain.presence import truncate_coord
from ..domain.space import SpaceRole
from ..domain.space_item import (
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
    ITEM_TYPE_POST,
    ITEM_TYPE_POST_EDIT,
    POST_SHAPED_ITEM_TYPES,
    AuthorityRemoval,
    item_stamp,
    required_scope,
    stamp_to_db,
)
from ..domain.space_media_fields import replace_item_media
from ..domain.space_media_ref import SpaceMediaLocalization, post_media_urls
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
from .inbound_media_store import media_basename, space_media_refs
from .link_preview_service import (
    space_wire_link_preview,
)
from .pending_decrypts_cache import KEY_WAIT_MAX_AHEAD
from .space_crypto_service import MissingEpochKey
from .space_item_author import (
    UnsupportedItemAuthorSigSuite,
    verify_signed_item_inner,
)
from .space_item_inbound import SpaceItemInbound
from .space_public_author import (
    UnsupportedFilesSigSuite,
    UnsupportedLinkPreviewSigSuite,
    UnsupportedMediaRefsSigSuite,
    signed_media_refs,
    verified_file_hashes,
    verified_link_preview,
    verify_signed_author_inner,
)
from .space_public_authority import (
    is_approved,
    is_authority_inner,
    is_channel_authority,
    open_channel_authority,
)
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
    from .space_crypto_service import SpaceContentEncryption
    from .space_media_gfs_fetcher import SpaceMediaGfsFetcher
    from .space_mentions import SpaceMentionResolver

log = logging.getLogger(__name__)

#: The most relayed frames held for a missing space key at once (#5b),
#: oldest dropped first. A reader needs a handful: the items that beat one
#: rekey to this household.
RELAY_KEY_WAIT_MAX_FRAMES: int = 64


class SpacePublicInbound:
    """Opaque-channel item → local-persist consumer (private and global spaces)."""

    __slots__ = (
        "_authorship",
        "_bus",
        "_crypto",
        "_gfs_media",
        "_items",
        "_mentions",
        "_own_instance_id",
        "_pending_decrypts",
        "_pending_parents",
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
            space_post_repo=space_post_repo,
            mention_resolver=mention_resolver,
        )
        #: Holds an item whose post (or parent comment) has not landed here
        #: yet and re-runs its frame when it does. ``None`` drops instead.
        self._pending_parents: PendingParentBuffer | None = None
        #: #5b — holds a frame under an epoch we hold no key for yet until
        #: that key lands by federation. ``None``: dropped.
        self._pending_decrypts: PendingDecryptsCache | None = None
        #: v_80 — keeps a relayed post's ``media_refs`` (its pictures in the
        #: GFS media cache, sealed under the space key) and fetches them.
        self._gfs_media: SpaceMediaGfsFetcher | None = None

    def attach_gfs_media(self, fetcher: SpaceMediaGfsFetcher) -> None:
        """Wire the v_80 GFS media fetcher."""
        self._gfs_media = fetcher

    def attach_key_wait(self, pending: PendingDecryptsCache) -> None:
        """Wire the #5b key wait: a frame under an epoch we hold no key for
        yet is held in *pending* and replayed on
        :class:`SpaceContentKeyImported` — the rekey reaches every member
        household by federation, a channel item can simply beat it here."""
        self._pending_decrypts = pending

    async def _await_key(
        self, space_id: str, epoch: int, replay: Callable[[], Awaitable[None]]
    ) -> None:
        """Hold *replay* until *space_id*'s *epoch* key lands (the key
        arrives by federation; nothing is asked for here).

        Only for an epoch AFTER the newest key we hold (or with none held):
        a rekey delivers the new current key, so an older missing epoch would
        never come — and at most :data:`KEY_WAIT_MAX_AHEAD` past it: the
        frame epoch is plaintext, so a relay could otherwise fill the hold
        with frames no key will open soon. The hold
        is this path's own bounded cache, never the sync one."""
        if self._pending_decrypts is None:
            return
        held = await self._crypto.get_current_epoch(space_id)
        if held is not None and (epoch <= held or epoch > held + KEY_WAIT_MAX_AHEAD):
            log.debug(
                "space_public.inbound: space %s epoch %d not held (ours %d, at "
                "most %d ahead) — sync covers it",
                space_id,
                epoch,
                held,
                KEY_WAIT_MAX_AHEAD,
            )
            return
        self._pending_decrypts.stash(space_id, epoch, replay)

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

    def attach_identity(self, *, own_instance_id: str) -> None:
        """Wire our own instance id — the self-echo guard's only input."""
        self._own_instance_id = own_instance_id

    async def handle_channel_item(
        self, space_id: str, *, epoch: int, payload: str
    ) -> None:
        """An item relayed through a space's opaque channel (v_51 private,
        v_96 global) — the only GFS-borne space content this household
        takes. The channel frame names no space — the caller mapped its
        channel id to ``space_id`` locally — and carries no cert; a member
        item then gets every check of a ``space_item`` (the writer cert from
        inside the ciphertext against the pinned space key, the inner's
        signed ``space_id`` equal to this space, epoch freshness, the author
        signature, the roster), an authority item the space authority
        signature over its inner."""
        await self._on_space_item(
            {
                "space_id": space_id,
                "event_type": SPACE_ITEM_EVENT_TYPE,
                "epoch": epoch,
                "payload": payload,
            }
        )

    async def _apply_authority_relayed(
        self,
        space: Space,
        *,
        epoch: int,
        inner: dict,
        pinned_pk_hex: str,
        token: str,
        size: int,
        replay: Callable[[], Awaitable[None]],
    ) -> None:
        """An inner a seed holder vouched for: an authority item on the
        space's opaque channel (v_96), its authority signature already
        verified by the caller."""
        space_id = space.id
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
                    token=token,
                    # The relay frame names no household: the notice is
                    # charged to the space's host, its only signer.
                    sender=space.owner_instance_id or space_id,
                    size=size,
                    replay=replay,
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
            # A post — never carried by a follower's reader cert (v_96), not
            # even an approved one.
            item_type=ITEM_TYPE_POST,
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
        held = await self._posts.get(post_id)
        if held is not None:
            log.debug("space_public.inbound: duplicate post %s — dropped", post_id)
            await self._heal_media(
                space_id, held, author_user_id, origin_instance_id, inner
            )
            return

        post = self._post_from_inner(post_id, author_user_id, inner)
        media = await self._localize_media(space_id, post, origin_instance_id, inner)
        post = _only_described_files(media.post(post), media)
        if await self._posts.save(space_id, post) is None:
            log.warning(
                "space_public.inbound: post %s already exists in another space "
                "— refusing the relayed write",
                post_id,
            )
            return
        await self._keep_media(media)
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

    # ── Member-published items (v_49) ────────────────────────────────────

    async def _on_space_item(self, frame: dict[str, Any]) -> None:
        """An item from a space's opaque channel: one a member household
        published itself, or (v_96) an authority item.

        No authority signature — the authorizer is the author household's
        writer cert, checked HERE with epoch freshness and the scope the real
        item type needs (the GFS can see neither): decrypt; read the real
        type and the author-signed inner; drop our own echo; verify the
        author signature + self-cert (the post-shaped inner of a ``post`` /
        ``post_edit`` also its owner-bound id; the generic inner of every
        other type its own domain and suite); require the author-bound
        ``item_type`` (and for a post-shaped inner ``item_target ==
        post_id``); require the origin to be the author's household and the
        inner cert (the only one — a frame carries none) valid for this
        space, the frame's epoch, the inner's ``author_pk`` and
        :func:`~socialhome.domain.space_item.required_scope`, at an epoch
        still open here, with a v2 user binding naming the author. Then
        apply by type: a post dedupes by id; a post edit lands last-writer-
        wins (:meth:`_apply_post_edit`); every other type goes to
        :class:`SpaceItemInbound`."""
        # Built by :meth:`handle_channel_item` from a parsed channel frame
        # (``ChannelItemFrame``) and the space its channel id maps to here.
        space_id = str(frame["space_id"])
        epoch = int(frame["epoch"])
        payload = str(frame["payload"])
        space = await self._spaces.get(space_id)
        if space is None or not space.identity_public_key:
            log.info(
                "space_public.inbound: space_item for unknown space %s — dropped",
                space_id,
            )
            return
        if archive_refusal(space, "") is not None:
            return
        if self._writer_certs is None:
            return
        try:
            pt = await self._crypto.decrypt(space_id, epoch, payload)
        except MissingEpochKey:
            log.info(
                "space_public.inbound: no key for space %s epoch %s yet — "
                "holding the space_item",
                space_id,
                epoch,
            )
            await self._await_key(
                space_id,
                epoch,
                functools.partial(self._on_space_item, frame),
            )
            return
        except (RuntimeError, ValueError, InvalidTag) as exc:
            log.info(
                "space_public.inbound: cannot decrypt space_item for %s epoch %s: %s",
                space_id,
                epoch,
                exc,
            )
            return
        opened = pt
        if is_channel_authority(opened):
            # v_96 — an authority item on the space's opaque channel: the
            # inner carries the space authority signature (the frame has
            # none). Verified against the pin, then the host-relay checks.
            await self._on_channel_authority(
                space, epoch=epoch, opened=opened, frame=frame
            )
            return
        try:
            parsed = parse_item_plaintext(opened)
        except ValueError as exc:
            log.warning(
                "space_public.inbound: undecodable space_item for space %s "
                "(%s) — dropped",
                space_id,
                exc,
            )
            return
        if parsed is None:
            log.warning(
                "space_public.inbound: unsupported space_item for space %s — dropped",
                space_id,
            )
            return
        item_type, inner = parsed
        verified = await self._verified_item(
            space,
            epoch=epoch,
            item_type=item_type,
            inner=inner,
        )
        if verified is None:
            return
        _cert, item_id, author_user_id, origin_instance_id = verified
        post_shaped = item_type in POST_SHAPED_ITEM_TYPES
        member_household = await self._holds_writer_seat(space_id)
        if not post_shaped:
            try:
                await self._items.apply(
                    space=space,
                    item_type=item_type,
                    inner=inner,
                    origin_instance_id=origin_instance_id,
                    member_household=member_household,
                    authorship=self._authorship,
                    epoch=epoch,
                )
            except AwaitingParent as wait:
                # Re-run the whole frame when the parent lands: decrypt,
                # writer cert, epoch, author signature and roster again.
                self._hold_for_parent(
                    wait,
                    space_id=space_id,
                    token=_digest("item", payload),
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
                space_id=space_id,
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
                space_id, item_id, author_user_id, origin_instance_id, inner
            )
            return
        await self._persist(
            space_id, item_id, author_user_id, origin_instance_id, inner
        )

    async def _on_channel_authority(
        self, space: Space, *, epoch: int, opened: bytes, frame: dict[str, Any]
    ) -> None:
        """An authority item from the space's channel (v_96): the inner's
        space-authority signature must verify against the pinned key for
        THIS space (the channel id was mapped to it locally); then the
        host-relay checks apply (:meth:`_apply_authority_relayed`)."""
        inner = open_channel_authority(
            opened,
            space_id=space.id,
            epoch=epoch,
            space_pubkey_hex=space.identity_public_key,
        )
        if inner is None:
            log.warning(
                "space_public.inbound: channel authority item for space %s "
                "failed its authority signature or epoch — dropped",
                space.id,
            )
            return
        await self._apply_authority_relayed(
            space,
            epoch=epoch,
            inner=inner,
            pinned_pk_hex=space.identity_public_key,
            token=_digest("channel-authority", str(frame.get("payload") or "")),
            size=_retained_bytes(frame),
            replay=functools.partial(self._on_space_item, frame),
        )

    async def _verified_item(
        self,
        space: Space,
        *,
        epoch: int,
        item_type: str,
        inner: dict[str, Any],
    ) -> tuple[WriterCert, str, str, str] | None:
        """Every check a member-published item gets before it is applied
        (see :meth:`_on_space_item`): ``(inner_cert, item_id,
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
        # The frame carries no cert: the inner one is the only one, and it
        # gets every check below.
        if inner_cert is None:
            log.warning(
                "space_public.inbound: space_item %s carries no valid cert — dropped",
                item_id,
            )
            return None
        try:
            author_pk = bytes.fromhex(str(inner.get("author_pk") or ""))
        except ValueError:
            return None
        cert_ok = await self._writer_certs.check_item(
            space,
            raw_cert,
            epoch=epoch,
            author_pk=author_pk,
            required_scope=required_scope(item_type),
            item_type=item_type,
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

    async def _localize_media(
        self, space_id: str, post: Post, origin_instance_id: str, inner: dict
    ) -> SpaceMediaLocalization:
        """v_80 — the post's files in the GFS media cache, under THIS
        household's names, bound to the author household the signed inner
        proves (``origin_instance_id``). Only a descriptor the author SIGNED
        is taken (:func:`signed_media_refs`): one a re-publisher swapped, or
        added unsigned, is dropped and the post shows without those
        pictures."""
        if self._gfs_media is None:
            return SpaceMediaLocalization()
        try:
            refs = signed_media_refs(inner)
        except UnsupportedMediaRefsSigSuite as exc:
            log.warning(
                "space_public.inbound: post %s media_refs signature suite %s "
                "unsupported — dropped",
                post.id,
                exc,
            )
            refs = None
        if refs is None and inner.get("media_refs") is not None:
            log.warning(
                "space_public.inbound: post %s carries media_refs its author did "
                "not sign — dropped",
                post.id,
            )
        # The author-signed file hashes: with them each file takes
        # its content name (``sc-``), the one name it has on every path, so
        # a member that gets this copy before the host's direct one names
        # it exactly as that one would (and can still ask the host or a
        # helper for it, ``SPACE_MEDIA_WANTED``).
        try:
            hashes = verified_file_hashes(inner)
        except UnsupportedFilesSigSuite as exc:
            log.warning(
                "space_public.inbound: post %s file hashes signature suite %s "
                "unsupported — dropped",
                post.id,
                exc,
            )
            hashes = {}
        return await self._gfs_media.localize(
            space_id,
            refs,
            post_media_urls(post),
            item_id=post.id,
            origin=origin_instance_id,
            hashes=hashes,
        )

    async def _heal_media(
        self,
        space_id: str,
        held: tuple[str, Post],
        author_user_id: str,
        origin_instance_id: str,
        inner: dict,
    ) -> None:
        """Add-only heal of a post already stored here WITHOUT its GFS
        pictures (#1309 round 4).

        A member can re-publish the author's inner WITHOUT its signed
        ``media_refs`` (the author signature still verifies) and win the race
        to a GFS: this household stores the post pictureless and would drop
        the author's real copy as a duplicate. A later copy — or edit — of
        the SAME author's post in the same space that carries a descriptor
        the author signed heals it: the stored post's files take this
        household's names and the rows are kept. Never for an item that has
        rows already, so it never replaces refs or names."""
        held_space, post = held
        if (
            self._gfs_media is None
            or inner.get("media_refs") is None
            or held_space != space_id
            or post.author != author_user_id
            or post.deleted
            or await self._gfs_media.has_item_refs(space_id, post.id)
        ):
            return
        # The files come from the copy that carries them: the stored post kept
        # only described files (none, for a stripped copy).
        incoming = self._post_from_inner(post.id, author_user_id, inner)
        media = await self._localize_media(
            space_id, incoming, origin_instance_id, inner
        )
        if not media.refs:
            return
        files = _only_described_files(media.post(incoming), media)
        preview = post.link_preview
        healed = replace(
            post,
            media_url=files.media_url,
            image_urls=files.image_urls,
            file_meta=files.file_meta,
            link_preview=(
                replace(
                    preview,
                    thumbnail_url=(
                        files.link_preview.thumbnail_url
                        if files.link_preview is not None
                        else None
                    ),
                )
                if preview is not None
                else None
            ),
        )
        if not await self._posts.set_media(healed, space_id=space_id):
            return
        await self._keep_media(media)
        log.info(
            "space_public.inbound: post %s stored without its pictures — healed "
            "from a copy its author signed",
            post.id,
        )

    async def _keep_media(self, media: SpaceMediaLocalization) -> None:
        if self._gfs_media is not None:
            await self._gfs_media.keep(media)

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
            # The edit IS the post (the later create is a duplicate): its
            # signed descriptor is the only one this post will get.
            media = await self._localize_media(
                space_id, post, origin_instance_id, inner
            )
            post = _only_described_files(media.post(post), media)
            if await self._posts.save(space_id, post) is None:
                log.warning(
                    "space_public.inbound: post %s already exists in another space "
                    "— refusing the relayed write",
                    post_id,
                )
                return
            await self._keep_media(media)
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
        await self._heal_media(space_id, got, author_user_id, origin_instance_id, inner)
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
        hold the roster and the access levels. Our own people only:
        ``space_members`` also mirrors the host's (the §25.6 ``members``
        resource)."""
        ours = set(await self._spaces.list_local_member_user_ids(space_id))
        return any(
            m.user_id in ours and str(m.role) != SpaceRole.SUBSCRIBER.value
            for m in await self._spaces.list_members(space_id)
        )

    async def _writer_cert_ok(
        self,
        space,
        inner: dict,
        *,
        epoch: int,
        pinned_pk_hex: str,
        item_type: str,
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
            item_type=item_type,
            space_pubkey_hex=pinned_pk_hex,
        )

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
            link_preview = space_wire_link_preview(verified_link_preview(inner))
        except UnsupportedLinkPreviewSigSuite as exc:
            log.warning("relayed post %s: link preview dropped (%s)", post_id, exc)
            link_preview = None
        return Post(
            id=post_id,
            author=author,
            type=post_type,
            content=inner.get("content"),
            link_preview=link_preview,
            media_url=space_media_ref(inner.get("media_url")),
            image_urls=space_media_refs(
                inner.get("image_urls"), limit=FEED_POST_MAX_IMAGES
            ),
            location=location,
            created_at=parse_iso8601_lenient(inner.get("created_at")),
            hidden_from_feed=bool(inner.get("hidden_from_feed", False)),
        )


def _only_described_files(post: Post, media: SpaceMediaLocalization) -> Post:
    """``post`` with only the file references a signed GFS descriptor
    renamed to this household's name for the GFS copy — a reader name
    (``sg-``), or the content name (``sc-``) when the author-signed hashes
    cover the file: exactly the names *media* produced. On the relay path
    that is the only way the author's bytes reach this household; any other
    name — an ``sd-`` file received from some household over federation, a
    raw ``sc-`` name, or a plain name that may be one of this household's
    own uploads, from another space even — would show a file the relayed
    author never sent. Apply after the localization. The link card stays;
    only its picture goes.

    Every field the space media-field registry lists for a post
    (:func:`replace_item_media`)."""
    described = set(media.names.values())

    def keep(url: str | None) -> bool:
        return bool(url) and media_basename(url) in described

    return replace_item_media("posts", post, lambda u: u if keep(u) else None)


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
