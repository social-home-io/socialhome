"""Host-side authority items for a global space's opaque channel (v_96).

Since v_96 a global space's GFS-borne content rides its opaque channel
(:mod:`socialhome.services.gfs_channel_service`): every member and follower
household holds a channel seat, every writer publishes its own items there
(:mod:`socialhome.services.gfs_member_publish_service`, the OWNER's own posts
included). This service adds what only the space's seed holder can sign, as
AUTHORITY items inside the channel ciphertext (the inner signed by the space
authority, :func:`~socialhome.services.space_public_authority
.channel_authority_plaintext`):

* **Remote-author posts.** A member's post that reaches the OWNER carrying
  the author household's pre-signed inner (``SpacePostCreated.public_relay``,
  built by :func:`build_signed_author_inner` on the author's household) and
  that the author did NOT put on the channel itself (``channel_sent`` in the
  hint — e.g. an author whose household holds no channel seat yet). The
  author's self-cert + signature are re-verified first
  (:func:`verify_signed_author_inner`), the inner's signed ``space_id`` must
  be this space, a member inner naming an authority kind is refused, and
  the author household's writer cert is checked and RE-STAMPED for the epoch
  the item is sealed under — only while that household still holds a seat
  that permits a post. An inner with NO cert is relayed only from an origin
  below v_49 whose pinned key is the inner's ``author_pk`` (the migration
  tripwire). A delegated admin relays no member's post.
* **Approved posts.** A remote member's post released from the moderation
  queue, applied here as the submitter's: the queue kept the submitter's
  author-signed copy and :meth:`SpacePublicOutbound._relay_approved` relays
  it — when it is exactly the published post — marked ``approved_post``,
  with the author household's cert re-stamped at ``comment`` scope at least
  and without follower seats. The approver is never named; without a signed
  copy nothing is relayed.
* **Removals.** Every space post or comment delete the owner applies (a
  moderator's, an admin's or the author's own, local or federated), and
  every one a delegated admin's own household makes, as an
  :class:`~socialhome.domain.space_item.AuthorityRemoval` naming the item
  and its author — never who removed it or why.

A space without a channel here gets nothing from this service: its members
are reached by federation and catch up from the host by sync. The
space-keyed ``POST /gfs/publish`` relay (``space_post_public``) is gone, as
are its padded / compressed copies and the per-server follower coverage.

This service is the encryption boundary: nothing leaves outside the channel
ciphertext (CLAUDE.md Encryption-First Rule). With no content key the seal
raises ``RuntimeError`` rather than degrading to plaintext.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from ..domain.events import CommentDeleted, PostDeleted, SpacePostCreated
from ..domain.space import SpaceType
from ..domain.space_item import (
    AUTHORITY_KIND_FIELD,
    CHANNEL_SENT_FIELD,
    ITEM_TYPE_POST,
    REMOVAL_TARGET_COMMENT,
    REMOVAL_TARGET_POST,
    AuthorityRemoval,
    reader_cert_permits,
)
from ..domain.writer_cert import (
    WRITER_SCOPE_COMMENT,
    WRITER_SCOPE_WRITE,
    scope_permits,
)
from ..federation.space_scope import archive_refusal
from ..infrastructure.event_bus import EventBus
from .space_public_author import verify_signed_author_inner
from .space_public_authority import approved_relay_for, channel_authority_plaintext
from .space_writer_cert_service import WRITER_CERT_FIELD, SpaceWriterCertService

if TYPE_CHECKING:
    from ..domain.space import Space
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .gfs_channel_service import GfsChannelService
    from .space_crypto_service import SpaceContentEncryption

log = logging.getLogger(__name__)


class SpacePublicOutbound:
    """Bus-event → channel authority items of a global space (see the
    module docstring)."""

    __slots__ = (
        "_bus",
        "_channels",
        "_crypto",
        "_own_instance_id",
        "_posts",
        "_spaces",
        "_users",
        "_writer_certs",
    )

    def __init__(
        self,
        *,
        bus: EventBus,
        space_repo: AbstractSpaceRepo,
        space_crypto: SpaceContentEncryption,
        user_repo: AbstractUserRepo,
    ) -> None:
        self._bus = bus
        self._spaces = space_repo
        self._crypto = space_crypto
        #: Only read to tell a local author (whose own post goes into the
        #: channel through the member publisher) from a remote one.
        self._users = user_repo
        self._own_instance_id: str = ""
        #: v_49 writer certs — ``None`` relays an inner without re-stamping.
        self._writer_certs: SpaceWriterCertService | None = None
        #: The space post store — only read to skip the removal of a
        #: calendar-derived post (never relayed, so nothing to remove).
        self._posts: AbstractSpacePostRepo | None = None
        #: v_96 — a GLOBAL space's opaque channel: the only place anything
        #: of this service goes. ``None`` → nothing is sent.
        self._channels: GfsChannelService | None = None

    def attach_channels(self, channels: GfsChannelService) -> None:
        """Wire the channel service (v_96, see :attr:`_channels`)."""
        self._channels = channels

    async def _on_channel(self, space: Space) -> bool:
        """Whether *space* is a GLOBAL space that stores a channel here (the
        owner's own, or a delegated admin's grant) — the one case this
        service sends anything."""
        return (
            self._channels is not None
            and space.space_type is SpaceType.GLOBAL
            and await self._channels.has_channel(space.id)
        )

    def attach_posts(self, posts: AbstractSpacePostRepo) -> None:
        """Wire the space post store (see :attr:`_posts`)."""
        self._posts = posts

    def attach_writer_certs(self, writer_certs: SpaceWriterCertService) -> None:
        """Wire the v_49 writer-cert service (see the module docstring)."""
        self._writer_certs = writer_certs

    def attach_identity(self, *, own_instance_id: str) -> None:
        """Wire our own instance id (who owns a space)."""
        self._own_instance_id = own_instance_id

    def _owns(self, space: Space) -> bool:
        """Whether this household is *space*'s owner (callers check the
        identity is attached first)."""
        return space.owner_instance_id == self._own_instance_id

    def wire(self) -> None:
        self._bus.subscribe(SpacePostCreated, self._on_space_post_created)
        self._bus.subscribe(PostDeleted, self._on_post_deleted)
        self._bus.subscribe(CommentDeleted, self._on_comment_deleted)

    async def _on_space_post_created(self, event: SpacePostCreated) -> None:
        # Inbound (another member's post): the remote-author relay, when it
        # carries a verified, author-signed public_relay hint. Otherwise
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
        # The one local post this service relays: a remote member's post
        # released from the moderation queue (applied here as theirs). Our
        # own authors' posts go into the channel through the member
        # publisher, like any writer's.
        if not event.approved_by:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or not await self._on_channel(space):
            return
        seed = await self._spaces.get_space_seed(event.space_id)
        if seed is None or not self._own_instance_id or not self._owns(space):
            return
        if await self._users.get_by_user_id(post.author) is not None:
            return  # a local author: the member publisher carries it
        await self._relay_approved(event, space)

    async def _relay_remote_authored(self, event: SpacePostCreated) -> None:
        """Put another member household's post into the space's channel as
        an authority item — on the space's OWNER household only, and only
        when the author did not put it there itself (see the module
        docstring). The original author's self-cert + signature are
        verified first — never relay forgeable / unverifiable content."""
        relay = event.public_relay
        if not isinstance(relay, dict):
            return
        if not event.space_id:
            return
        space = await self._spaces.get(event.space_id)
        if space is None or not await self._on_channel(space):
            return
        seed = await self._spaces.get_space_seed(event.space_id)
        if seed is None:
            return  # no seed here — can't authority-sign the relay
        if not self._own_instance_id:
            log.warning(
                "space_public.outbound: identity not attached — cannot relay "
                "remote-authored post for space %s",
                event.space_id,
            )
            return
        if not self._owns(space):
            # A delegated admin relays no member's post: answering each one
            # with an authority-signed copy would set it apart from every
            # other writer household — and a GFS could test a household by
            # handing a post to its seat alone.
            return
        # v_96 — the author put this post on the channel itself: never a
        # second copy there. The mark rides outside the author signature
        # and is never relayed on.
        channel_sent = relay.get(CHANNEL_SENT_FIELD) is True
        relay = {k: v for k, v in relay.items() if k != CHANNEL_SENT_FIELD}
        if channel_sent:
            log.debug(
                "space_public.outbound: author put its post on the channel of "
                "space %s — no host copy",
                event.space_id,
            )
            return
        # Verify the ORIGINAL author's signature + self-cert before relaying —
        # never relay unverifiable / forgeable content (every reader
        # re-verifies, but we fail closed here too).
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
                sealed = await self._encrypt_with_cert(
                    event.space_id,
                    relay,
                    restamp_for,
                    expect_pk=str(relay.get("author_pk") or ""),
                    required=True,
                )
            else:
                sealed = await self._seal(event.space_id, relay)
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
        await self._publish(event.space_id, sealed, what="remote-author post")

    # ── Authority-only items: approved posts and removals ────────────────

    async def _relay_approved(self, event: SpacePostCreated, space: Space) -> None:
        """Relay a remote member's post just released from the moderation
        queue, under its AUTHOR's signature: the submitter's household signed
        the post inner when it submitted the item, and the queue kept that
        copy (``event.public_relay``). It must be exactly the post being
        published (:func:`approved_relay_for`); it goes out marked
        ``approved_post`` with the author household's writer cert re-stamped
        for the relay epoch at ``comment`` scope at least — a plain member of
        a ``MODERATED`` space holds only that — issued without follower seats
        (v_96: never a reader cert, which no receiver takes on a post). The
        approver is never named.
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
            sealed = await self._encrypt_with_cert(
                event.space_id,
                inner,
                origin,
                expect_pk=str(inner.get("author_pk") or ""),
                required=True,
                required_scope=WRITER_SCOPE_COMMENT,
                # A post is never a follower's: re-issue without follower
                # seats, so a member whose household also holds a follower
                # (one reader cert for both) still gets a cert receivers
                # accept on a post — members equal on any path.
                followers=False,
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
        await self._publish(event.space_id, sealed, what=f"approved post {post.id}")

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
            event.space_id,
            REMOVAL_TARGET_POST,
            event.post_id,
            event.post_id,
            author,
            origin=event.origin_instance_id,
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
            origin=event.origin_instance_id,
        )

    async def _relay_removal(
        self,
        space_id: str,
        target: str,
        item_id: str,
        post_id: str,
        author: str,
        *,
        origin: str | None,
    ) -> None:
        """Put a post or comment removal into the space's channel, on this
        seed holder's authority. The OWNER relays every removal applied
        here — a moderator's, an admin's or the author's own, local or
        federated (the host path already authorized it). A delegated admin
        relays only a removal made on its own household (*origin* ``None``):
        one that merely reached it — over federation, by sync or through its
        fan-out seat — gets no answer, or a connection server could hand a
        delete to one household's seat and learn from the reply that it
        holds the seed. Such a removal reaches followers through the owner. The notice names the item and its author (for
        the follower's tombstone rule) — never who removed it or why. Inside
        the ciphertext and padded, so the connection server cannot tell it
        from a short post. A follower that already applied it is unaffected
        by a duplicate."""
        space = await self._spaces.get(space_id)
        if space is None or not await self._on_channel(space):
            return
        if archive_refusal(space, "") is not None:
            return  # read-only: followers drop it anyway
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            return  # only a seed holder speaks for the space
        if not self._own_instance_id:
            log.warning(
                "space_public.outbound: identity not attached — dropping a "
                "%s removal for space %s",
                target,
                space_id,
            )
            return
        if origin is not None and not self._owns(space):
            return
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
            sealed = await self._seal(space_id, removal.to_inner())
        except RuntimeError:
            log.warning(
                "space_public.outbound: no content key for space %s — cannot "
                "relay a %s removal",
                space_id,
                target,
            )
            return
        await self._publish(space_id, sealed, what=f"{target} removal")

    async def _seal(self, space_id: str, body: dict) -> _Sealed:
        """Seal ``body`` as a channel authority item under the current epoch
        (:func:`channel_authority_plaintext`: the inner signed by the space
        authority, binding that epoch). Raises ``RuntimeError`` without the
        seed or a content key."""
        seed = await self._spaces.get_space_seed(space_id)
        if seed is None:
            raise RuntimeError(f"no space seed for {space_id}")
        # The signature binds the epoch the item is sealed under: re-read
        # once if a rotation lands in between.
        for _attempt in range(2):
            current = await self._crypto.get_current_epoch(space_id)
            if current is None:
                raise RuntimeError(f"no content key for space {space_id}")
            plain = channel_authority_plaintext(
                body, space_id=space_id, epoch=current, space_seed=seed
            )
            epoch, ct = await self._crypto.encrypt(space_id, plain)
            if epoch == current:
                return _Sealed(epoch, ct)
        raise RuntimeError(f"content epoch kept moving for space {space_id}")

    async def _publish(self, space_id: str, sealed: _Sealed, *, what: str) -> None:
        """Hand a sealed authority item to the space's channel."""
        if self._channels is None:
            return
        try:
            await self._channels.publish_current(space_id, sealed.epoch, sealed.ct)
        except Exception:
            log.exception(
                "space_public.outbound: %s channel publish failed for space=%s",
                what,
                space_id,
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
            # A remote author's post: never carried by a follower's reader
            # cert (v_96), whatever its scope says.
            item_type=ITEM_TYPE_POST,
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
        item_type: str = ITEM_TYPE_POST,
        followers: bool = True,
    ) -> _Sealed:
        """Encrypt ``inner`` (an ``item_type`` item) with ``instance_id``'s
        writer cert for the epoch it is encrypted under (re-read once if a
        rotation lands in between). Without a cert service, or with no cert
        for that household that permits ``required_scope`` — and, for a
        follower's ``reader`` cert (v_96), ``item_type`` under the space's
        follower switches — the inner goes as is, unless ``required`` (a
        re-stamp), which raises :class:`_NoWriterCert`. ``expect_pk`` pins
        the cert to the author key the inner names. ``followers=False``
        re-issues a remote household's cert without its follower seats (a
        plain cert binding only seated users). Raises ``RuntimeError`` when
        there is no content key.
        """
        if self._writer_certs is None:
            if required:
                raise _NoWriterCert(instance_id)
            return await self._seal(space_id, inner)
        for _attempt in range(2):
            current = await self._crypto.get_current_epoch(space_id)
            if current is None:
                raise RuntimeError(f"no content key for space {space_id}")
            if instance_id == self._own_instance_id and followers:
                cert = await self._writer_certs.own_cert(space_id, current)
            else:
                hint: bytes | None
                try:
                    hint = bytes.fromhex(expect_pk) if expect_pk else None
                except ValueError:
                    hint = None
                cert = await self._writer_certs.issue_for_instance(
                    space_id,
                    instance_id,
                    epoch=current,
                    instance_pk_hint=hint,
                    followers=followers,
                )
            if cert is not None and expect_pk:
                named = SpaceWriterCertService.cert_instance_pk(cert.to_wire())
                if named is None or named.hex() != expect_pk:
                    cert = None
            # Never put a cert on an item it does not authorize (a comment
            # cert on a post).
            if cert is not None and not scope_permits(cert.scope, required_scope):
                cert = None
            # Nor a follower's reader cert on a type every receiver refuses
            # it on (a post, or a comment / reaction whose switch is off).
            if cert is not None and cert.reader:
                space = await self._spaces.get(space_id)
                if space is None or not reader_cert_permits(
                    cert, item_type, space.features
                ):
                    cert = None
            if cert is None and required:
                raise _NoWriterCert(instance_id)
            body = dict(inner)
            body.pop(WRITER_CERT_FIELD, None)
            if cert is not None:
                body[WRITER_CERT_FIELD] = cert.to_wire()
            sealed = await self._seal(space_id, body)
            if cert is None or sealed.epoch == cert.epoch:
                return sealed
        log.warning(
            "space_public.outbound: content epoch kept moving for space %s — "
            "relaying without a writer cert",
            space_id,
        )
        if required:
            raise _NoWriterCert(instance_id)
        body = {k: v for k, v in inner.items() if k != WRITER_CERT_FIELD}
        return await self._seal(space_id, body)


class _Sealed(NamedTuple):
    """One authority item sealed for the space's channel."""

    epoch: int
    ct: str


class _NoWriterCert(Exception):
    """A required writer-cert re-stamp was impossible (the household holds
    no writer seat, or its key does not match the inner's author)."""
