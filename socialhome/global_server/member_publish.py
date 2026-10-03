"""Trusted-mode member publish (v_49) — ``POST /gfs/member-publish``.

A space member household publishes its own item over this connection server
with no host signature: it proves its GFS identity (household-signed request,
the same scheme as subscribe / unsubscribe) and carries a writer cert the
space AUTHORITY key signed for it. The wire shape and its codec live in
:mod:`socialhome.domain.gfs_member_publish`.

**What this server learns** — the owner-decided trusted-mode trade-off: which
registered household published into which listed space, at which content
epoch, and when. **What it never learns**: the content or the real item type.
``payload`` is ciphertext under the space content key and the outer event type
is always ``space_item``. That also means the scope this server can check is
only the weakest one (``comment``): it cannot tell a post (needs ``write``)
from a comment. RECEIVERS decrypt, read the real type and enforce the scope it
needs — a ``comment``-scoped follower that dresses a post up as a
``space_item`` is relayed here and dropped by every receiver.

Authorization, in order — every refusal is the same uniform ``403`` at the
route, with the reason at DEBUG:

1. the household signature verifies against the REGISTERED
   ``client_instances.public_key`` of ``instance_id``, its ``ts`` is within
   ±300 s, and the instance is ``active`` (not pending, not banned);
2. the per-(instance, space) rate limit (``429``, after authentication only);
3. the space is published here, not moderator-banned, publicly readable
   (``allow_subscribers``), and has a pinned authority key;
4. :func:`~socialhome.writer_cert.verify_writer_cert` against that pinned key,
   for ``target`` and the request ``epoch``, with ``author_pk`` = the
   publishing household's REGISTERED identity key — so a household can only
   publish with a cert issued to itself;
5. epoch freshness (:meth:`GfsSpaceEpoch.admits`) against the newest content
   epoch this server has seen proven.

Then a content-blind replay guard (BLAKE2b digest of the whole signed body,
TTL'd like the ``ts`` window) and the fan-out: every active subscriber of the
space except the publisher gets ``{type: "relay", **frame}`` on its live
socket, or the frame waits in the 24 h queue (``GfsEnvelopeRelay
.push_or_queue_relay``). The frame never names the publisher.

**Epoch freshness.** A writer cert binds one content epoch; receivers accept
only the newest epoch they hold (or the previous one for 10 minutes). This
server learns epochs only from authority-signed statements — a verified writer
cert (it proves the authority issued that epoch), the plaintext ``epoch`` of an
authority-signed ``space_post_public`` relay, and the authority-signed epoch
NOTICE a seed holder posts to ``POST /gfs/spaces/{id}/epoch`` on rotation
(:meth:`GfsMemberPublishService.note_epoch`). The notice is what makes the
check sound when a removed writer is the only one still publishing: without it
this server would not learn the rotation until someone else published. The
state is cleared whenever the space authority key is re-pinned.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..authority_sig import (
    AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
    UnsupportedAuthoritySuite,
    verify_authority_event,
)
from ..domain.gfs_member_publish import (
    MEMBER_PUBLISH_EPOCH_GRACE_S,
    MEMBER_PUBLISH_ROUTE,
    MemberPublishRequest,
)
from ..domain.writer_cert import MAX_WRITER_CERT_EPOCH, WRITER_SCOPE_COMMENT
from ..writer_cert import (
    InvalidWriterCert,
    UnsupportedWriterCertSuite,
    verify_writer_cert,
)
from .federation import SeenPayloadCache
from .public import ClientIpResolver, SlidingWindowCounter

# The window-limiter factory is module-private in ``.public`` and every
# limiter on this server is built from it (see ``envelope_relay``).
from .public import _build_window_limiter

if TYPE_CHECKING:
    from .domain import GlobalSpace
    from .envelope_relay import GfsEnvelopeRelay
    from .federation import GfsFederationService
    from .repositories import AbstractGfsFederationRepo, AbstractGfsSpaceEpochRepo

log = logging.getLogger(__name__)

#: Accepted member publishes per (household, space) per minute. A household
#: posting by hand stays orders of magnitude below; it bounds what one
#: compromised or buggy member can push into every subscriber's socket/queue.
MEMBER_PUBLISH_MAX_PER_MINUTE: int = 30

#: Per-IP/minute cap on the member-publish and epoch-notice routes, applied
#: BEFORE any signature work (the per-household limit needs a verified
#: identity first).
MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP: int = 120

#: Route of the authority-signed content-epoch notice.
EPOCH_NOTICE_ROUTE: str = "/gfs/spaces/{space_id}/epoch"


class MemberPublishRateLimited(Exception):
    """The household exceeded :data:`MEMBER_PUBLISH_MAX_PER_MINUTE` for this
    space. Raised only after its signature verified."""


class GfsMemberPublishService:
    """Authorize + fan out trusted-mode member publishes; track epochs."""

    __slots__ = (
        "_clock",
        "_epoch_repo",
        "_fed_repo",
        "_federation",
        "_limiter",
        "_relay",
        "_seen",
    )

    def __init__(
        self,
        *,
        federation: "GfsFederationService",
        fed_repo: "AbstractGfsFederationRepo",
        epoch_repo: "AbstractGfsSpaceEpochRepo",
        relay: "GfsEnvelopeRelay",
        limiter: SlidingWindowCounter | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._federation = federation
        self._fed_repo = fed_repo
        self._epoch_repo = epoch_repo
        self._relay = relay
        self._limiter = limiter or SlidingWindowCounter(MEMBER_PUBLISH_MAX_PER_MINUTE)
        self._seen = SeenPayloadCache()
        self._clock = clock

    async def publish(self, req: MemberPublishRequest) -> int:
        """Authorize ``req`` and fan it out. Returns the number of
        subscribers it was pushed or queued to (0 for a suppressed replay).

        Raises :class:`PermissionError` on any authorization failure and
        :class:`MemberPublishRateLimited` past the per-household limit."""
        inst = await self._federation.verify_signed_request(
            req.instance_id, req.signing_payload(), signature=req.signature
        )
        if inst.status != "active":
            raise PermissionError("instance is not active")
        if not self._limiter.allow(f"{inst.instance_id}\x00{req.target}"):
            raise MemberPublishRateLimited()
        space = await self._readable_space(req.target)
        try:
            space_pk = bytes.fromhex(space.identity_public_key)
            author_pk = bytes.fromhex(inst.public_key)
            verify_writer_cert(
                req.writer_cert,
                space_pubkey=space_pk,
                space_id=req.target,
                epoch=req.epoch,
                author_pk=author_pk,
                # The real item type is hidden inside the ciphertext, so the
                # weakest scope is all this server can require; receivers
                # enforce the scope the real type needs.
                required_scope=WRITER_SCOPE_COMMENT,
            )
        except (InvalidWriterCert, UnsupportedWriterCertSuite, ValueError) as exc:
            raise PermissionError(f"writer cert refused: {exc}") from exc

        now = int(self._clock())
        state = await self._epoch_repo.get(req.target)
        if state is not None and not state.admits(
            req.epoch, now=now, grace_s=MEMBER_PUBLISH_EPOCH_GRACE_S
        ):
            raise PermissionError("writer cert epoch is stale")
        if state is None or req.epoch > state.current:
            # The verified cert is itself the authority's statement that this
            # epoch exists.
            await self._epoch_repo.advance(req.target, req.epoch, seen_at=now)

        # Replay guard AFTER authorization (a refused body must never mute a
        # legitimate one). The digest covers the signature, so it is exactly
        # the captured request; the TTL matches the ±300 s ``ts`` window.
        digest = SeenPayloadCache.digest(req.to_wire())
        if self._seen.seen(digest):
            log.debug("gfs.member_publish: suppressing a replayed request")
            return 0
        self._seen.record(digest)

        frame = req.fan_out_frame()
        delivered = 0
        # Sequential: a live push is a local socket write and a queue insert
        # one short transaction; neither waits on a remote household.
        for sub in await self._fed_repo.list_subscribers(req.target):
            if sub.instance_id == inst.instance_id:
                continue  # never echo an item back to its publisher
            if await self._relay.push_or_queue_relay(sub.instance_id, frame):
                delivered += 1
        log.debug(
            "gfs.member_publish: space=%s epoch=%d reached=%d",
            req.target,
            req.epoch,
            delivered,
        )
        return delivered

    async def note_epoch(
        self,
        space_id: str,
        epoch: object,
        authority_sig: str,
        authority_sig_suite: str,
    ) -> None:
        """Record an authority-signed content-epoch notice (monotonic).

        The notice is ``{space_id, epoch}`` signed with the space seed under
        :data:`AUTHORITY_EVENT_SPACE_EPOCH_NOTICE`. It carries no timestamp on
        purpose: replaying one can never move the epoch backwards. Raises
        :class:`PermissionError` on any refusal."""
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or not 0 <= epoch <= MAX_WRITER_CERT_EPOCH
        ):
            raise PermissionError("invalid epoch")
        space = await self._fed_repo.get_space(space_id)
        if space is None or space.status == "banned":
            raise PermissionError("space not published or banned")
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        try:
            ok = verify_authority_event(
                event_type=AUTHORITY_EVENT_SPACE_EPOCH_NOTICE,
                space_id=space_id,
                payload={"space_id": space_id, "epoch": epoch},
                authority_sig=authority_sig,
                authority_sig_suite=authority_sig_suite,
                space_public_key=bytes.fromhex(space.identity_public_key),
            )
        except (UnsupportedAuthoritySuite, ValueError) as exc:
            raise PermissionError(f"epoch notice refused: {exc}") from exc
        if not ok:
            raise PermissionError("invalid authority signature")
        await self._epoch_repo.advance(space_id, epoch, seen_at=int(self._clock()))

    async def _readable_space(self, space_id: str) -> "GlobalSpace":
        space = await self._fed_repo.get_space(space_id)
        if space is None:
            raise PermissionError("space not published")
        if space.status == "banned":
            raise PermissionError("space is banned")
        if not space.allow_subscribers:
            # Listed for discovery but not publicly readable: no readership,
            # and its owner relays no content here either.
            raise PermissionError("space is not publicly readable")
        if not space.identity_public_key:
            raise PermissionError("no pinned authority key for this space")
        return space


def _is_member_publish_path(path: str) -> bool:
    return path == MEMBER_PUBLISH_ROUTE or (
        path.startswith("/gfs/spaces/") and path.endswith("/epoch")
    )


def build_member_publish_rate_limit(resolver: ClientIpResolver):
    """Per-IP limiter for ``POST /gfs/member-publish`` and the epoch notice,
    shedding a flood before any signature verification."""
    return _build_window_limiter(
        resolver,
        MEMBER_PUBLISH_MAX_PER_MINUTE_PER_IP,
        _is_member_publish_path,
    )
