"""Federated moderation: pending items and decisions between households (v_43).

Under ``MODERATED`` a member's write waits for review (§4.3). The item may
be reviewed on any household holding a content-authority seat in the
space, so it travels — but ONLY there:

* :data:`~socialhome.domain.federation.FederationEventType.SPACE_MODERATION_SUBMITTED`
  goes from the submitter's household to the space's host and to every
  household holding a live ``admin`` or ``moderator`` seat
  (``list_instances_with_roles``) — one targeted send each over the
  pairwise session (:meth:`FederationService.send_with_mesh_fallback`;
  sealed end-to-end under ``SPACE_ROUTED`` when it must cross a relay). It
  is never broadcast: a plain member household, a relay and the GFS never
  see pending content. An approver household below v_43 is skipped; a host
  below it fails the submit (``HOST_TOO_OLD``).
* :data:`~socialhome.domain.federation.FederationEventType.SPACE_MODERATION_DECIDED`
  goes from the deciding household to the host, the approver households
  and the submitter's household.

Plaintext on the envelope is the routing ``space_id`` only; the item
(feature, action, target, submitter, payload, snapshot, timestamps) and the
decision ride inside the sealed payload (encryption-first, §25.8.21).

The approved CONTENT never rides these events, and only the HOST applies
an item: a reviewer household's approve sends ``SPACE_MODERATION_DECIDED
{approved}`` to the host alone; the host verifies the approver's live seat,
runs every gate and applies the item from ITS OWN stored copy through the
feature's normal persist path, which federates the ordinary ``SPACE_*``
event to the space's members with the approval block
(:mod:`.moderation_release`), then announces the decision to the reviewers
and the submitter's household. Receivers accept a block only from the host
(``SpaceAuthorship.may_author_approved``). A rejection is decided wherever
it is made and announced to every holder.

Receiver checks, in order, for a submission (each refusal is a WARNING and
stores nothing):

1. this household is the host or holds ≥1 LOCAL content-authority member
   of the space (a misdirected item is dropped);
2. the submitter holds a live writer seat on the SENDING household
   (``acts_for``) and is not banned;
3. a create's target id is owner-bound to the submitter in this space;
4. the feature is on, its level here is ``MODERATED``, the space is not
   archived;
5. the feature's handler validates the payload with the live codecs/caps;
6. caps: 20 pending per (space, submitter), 50 per (space, sending
   household), 500 per space, 256 KiB per item; ``expires_at`` clamped to
   at most 14 days ahead, ``submitted_at`` to at most 5 minutes ahead and
   at most 14 days before ``expires_at``;
7. stored once under the item's id (a replay changes nothing).

For a decision: the sender has content authority and the named decider is
a content-authority user seated on it (``moderates_as``, live seats). An
approval from a non-host is acted on by the host only (it publishes); one
for an item not held here yet leaves a tombstone, so the late submission is
never stored as pending.

**Catch-up.** A reviewer household can miss a submission or a decision (it
was offline past the sender's retries, it only started reviewing after the
item was submitted, or it purged its copies while it did not review). The
host holds every pending item, so it is the one source: a reviewer
household's §25.6 ``SPACE_SYNC_BEGIN`` to the host echoes what it holds for
the space, by state (``mod_held``,
:meth:`SpaceModerationFederation.held_echo` — the periodic BEGIN, and one
sent the moment one of its people is given a content-authority seat), and
the host answers with exactly the difference
(:meth:`SpaceModerationFederation.on_sync_begin`): each pending item the
household lacks or holds purged, re-sent as ``SPACE_MODERATION_SUBMITTED``
naming the submitter's own household (``submitter_instance_id`` — believed
only from the host, and the submitter must hold a live writer seat there),
with its files to that household alone; and the decision of each item it
holds pending that the host has since approved or rejected. A rejection
made by the household's own people that the host never got rides the echo
itself and is applied like the decision. Only for a household holding a
live admin / moderator seat, at most once per :data:`CATCH_UP_INTERVAL_S`
per (household, space), at most :data:`CATCH_UP_MAX_BYTES` of items per
answer, and the same item to the same household at most once per
:data:`RESEND_SAME_INTERVAL_S` (a role change starts over). Nothing of the
queue enters the §25.6 stream itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.file_hashes import parse_file_hashes
from ..domain.media_chunking import RELAY_JSON_CHUNK_BYTES
from ..domain.post import BAZAAR_MAX_IMAGES, FEED_POST_MAX_IMAGES
from ..domain.presence import truncate_coord
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    PUBLIC_SPACE_TIERS,
    WRITER_ROLES,
    ContentAction,
    HostTooOldError,
    ModerationStatus,
    Space,
    SpaceFeatureAccess,
    SpaceModerationItem,
    SpaceRole,
)
from ..domain.space_media_ref import rewrite_page_body_media
from ..federation.owner_bound_id import (
    SPACE_CALENDAR_EVENT_KIND,
    SPACE_PAGE_KIND,
    SPACE_POST_KIND,
    SPACE_STICKY_KIND,
    SPACE_TASK_KIND,
    SPACE_TASK_LIST_KIND,
    is_owner_bound,
    owner_bound_id_refused,
)
from ..federation.space_scope import resolve_space_id
from ..repositories.base import dump_json, load_json
from ..utils.datetime import parse_iso8601_optional
from .file_hashes_sender import FileHashesMixin
from .inbound_media_store import media_basename, space_media_ref
from .space_media_names import SpaceMediaNamer, feature_media_urls, renamed_media
from .space_moderation_service import (
    MAX_PAYLOAD_BYTES,
    MAX_PENDING_PER_SPACE,
    MAX_PENDING_PER_SUBMITTER,
    MAX_REJECT_REASON,
    MODERATION_TTL,
    feature_on,
    item_payload_snapshot,
)
from .space_post_moderation import post_from_queue_payload
from .space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent
    from ..federation.federation_service import FederationService
    from ..federation.space_authorship import SpaceAuthorship
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .space_media_sync_service import SpaceMediaSyncService
    from .space_moderation_service import SpaceModerationService

log = logging.getLogger(__name__)

FET = FederationEventType

#: The seats that review: the households a submission goes to.
REVIEWER_SEATS: frozenset[str] = frozenset(
    {SpaceRole.ADMIN.value, SpaceRole.MODERATOR.value}
)
#: A remote seat that writes (a remote seat is never ``owner``).
_WRITER_SEATS: frozenset[str] = frozenset(
    r.value for r in WRITER_ROLES if r is not SpaceRole.OWNER
)
#: Pending items one sending household may hold here per space.
MAX_PENDING_PER_HOUSEHOLD = 50
#: The furthest a received item's review window may reach.
MAX_EXPIRY = timedelta(days=14)
#: How far in the future a received ``submitted_at`` may be (clock skew).
MAX_FUTURE_SKEW = timedelta(minutes=5)
#: Bounds on ids read from the wire.
_MAX_ID = 128
#: The ``SPACE_SYNC_BEGIN`` field a reviewer household echoes what it
#: holds for the space under (additive: an older host ignores it):
#: ``{"p": [ids], "x": [ids], "d": [ids], "r": [{id, by, reason?}]}`` —
#: pending; expired / purged; decided (approved, or rejected by somebody
#: elsewhere); rejected by one of the household's own people (the decision
#: itself, so a host that never got it applies it). Empty lists are left
#: out; ``{}`` holds nothing.
HELD_ECHO_KEY = "mod_held"
ECHO_PENDING, ECHO_EXPIRED, ECHO_DECIDED, ECHO_REJECTED = "p", "x", "d", "r"
#: Ids one echo names at most (pending first, then the newest) — a longer
#: one is ignored — and how many of them may be own rejections.
MAX_ECHO_IDS = 2 * MAX_PENDING_PER_SPACE
MAX_ECHO_REJECTIONS = 100
#: The household a re-sent item's submitter belongs to — set by the host
#: on a catch-up re-send, believed only from the host.
SUBMITTER_INSTANCE_KEY = "submitter_instance_id"
#: The host answers a household's echo for one space at most this often
#: (a role change of that household's people starts over).
CATCH_UP_INTERVAL_S: float = 600.0
#: Item payload bytes one catch-up answer carries (at least one item); the
#: rest follows on the household's next sync, which echoes what arrived.
CATCH_UP_MAX_BYTES = 1024 * 1024
#: The host re-sends the SAME item (or decision) to the same household at
#: most this often — a household that keeps refusing it (its copy of the
#: space says the feature is not reviewed, a cap) is not sent it on every
#: sync. A role change of that household's people starts over.
RESEND_SAME_INTERVAL_S: float = 24 * 3600.0
#: An item whose ``SPACE_MODERATION_SUBMITTED`` payload is larger than this
#: does not fit a JSON envelope on the GFS relay (191 KiB, after encrypt +
#: base64 twice — the bound media chunks use). To a household seated from
#: an invite link the host then sends it as ONE binary relay frame (v_87,
#: up to 528 KiB): the same event, the content as the frame's bytes.
RELAY_JSON_ITEM_BYTES: int = RELAY_JSON_CHUNK_BYTES
#: The keys of the wire payload that travel as a relay frame's bytes.
_FRAME_BODY_KEYS: tuple[str, ...] = ("payload", "public_relay")

#: The owner-bound id kind of a create's target, per (feature, entity).
_CREATE_KINDS: dict[tuple[str, str], str] = {
    ("posts", "post"): SPACE_POST_KIND,
    ("pages", "page"): SPACE_PAGE_KIND,
    ("tasks", "task"): SPACE_TASK_KIND,
    ("tasks", "list"): SPACE_TASK_LIST_KIND,
    ("stickies", "sticky"): SPACE_STICKY_KIND,
    ("calendar", "event"): SPACE_CALENDAR_EVENT_KIND,
}


class SpaceModerationFederation(FileHashesMixin):
    """Send and receive moderation items and decisions (v_43)."""

    __slots__ = (
        "_authorship",
        "_caught_up_at",
        "_clock",
        "_federation",
        "_file_hasher",
        "_media_names",
        "_media_sync",
        "_moderation",
        "_own_pk",
        "_own_seed",
        "_page_media",
        "_resent",
        "_seat_stamps",
        "_seats",
        "_spaces",
        "_users",
    )

    def __init__(
        self,
        *,
        federation_service: FederationService,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        authorship: SpaceAuthorship,
        media_sync: SpaceMediaSyncService | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        #: (space, household) → when the host last answered its echo.
        self._caught_up_at: dict[tuple[str, str], float] = {}
        #: (space, household) → {item or decision key: when the host last
        #: re-sent it there} (:data:`RESEND_SAME_INTERVAL_S`).
        self._resent: dict[tuple[str, str], dict[str, float]] = {}
        #: (space, household) → its reviewer seats as last seen on a BEGIN
        #: (:func:`_reviewer_stamp`): a different one starts over.
        self._seat_stamps: dict[tuple[str, str], tuple] = {}
        self._federation = federation_service
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._authorship = authorship
        self._media_sync = media_sync
        #: v_85 — a submission's files ride with their SHA-256.
        self._file_hasher = None
        #: The files a held submission proposes take this household's
        #: names, bound to the submitting household (which ships them).
        self._media_names = SpaceMediaNamer()
        #: v_82 — every file a page shows here (version, history, sides).
        self._page_media: Callable[[str, str], Awaitable[list[str]]] | None = None
        self._moderation: SpaceModerationService | None = None
        #: Our household identity + local users — to author-sign a queued
        #: public post's inner for the GFS followers (see
        #: :meth:`_public_relay_for`). Unset → no signed copy is attached.
        self._own_pk: bytes = b""
        self._own_seed: bytes = b""
        self._users: AbstractUserRepo | None = None

    def attach_identity(
        self,
        *,
        own_instance_pk: bytes,
        own_identity_seed: bytes,
        user_repo: AbstractUserRepo,
    ) -> None:
        """Wire what author-signing a queued public post needs."""
        self._own_pk = own_instance_pk
        self._own_seed = own_identity_seed
        self._users = user_repo

    def attach_page_media(
        self, held: Callable[[str, str], Awaitable[list[str]]]
    ) -> None:
        """Wire ``held(space_id, page_id)`` — every file a page shows here,
        its history and open sides included (v_82)."""
        self._page_media = held

    def attach_media_names(self, namer: SpaceMediaNamer) -> None:
        """Wire the shared namer (it knows this household's instance id)."""
        self._media_names = namer

    def bind(self, moderation: SpaceModerationService) -> None:
        """Wire both directions: the queue sends through us, and what we
        receive is stored through it."""
        self._moderation = moderation
        moderation.attach_federation(self)

    def attach_to(self, federation_service: FederationService) -> None:
        registry = federation_service._event_registry
        registry.register(FET.SPACE_MODERATION_SUBMITTED, self._on_submitted)
        registry.register(FET.SPACE_MODERATION_DECIDED, self._on_decided)
        # The host's catch-up answers a reviewer household's §25.6 BEGIN.
        registry.register(FET.SPACE_SYNC_BEGIN, self.on_sync_begin)

    @property
    def _own(self) -> str:
        return str(self._federation.own_instance_id or "")

    # ── Outbound ─────────────────────────────────────────────────────────

    async def reviewer_households(self, space: Space) -> list[str]:
        """The host first, then every household with a live admin /
        moderator seat — each once, never this household."""
        out: list[str] = []
        for iid in (
            space.owner_instance_id,
            *await self._seats.list_instances_with_roles(space.id, REVIEWER_SEATS),
        ):
            if iid and iid != self._own and iid not in out:
                out.append(iid)
        return out

    async def submission_targets(self, space: Space) -> list[str]:
        """The households a new item goes to: every reviewer household on
        v_43+. A host below v_43 cannot hold it: :class:`HostTooOldError`."""
        targets: list[str] = []
        for iid in await self.reviewer_households(space):
            # ``space_member_supports``: a reviewer household reached only
            # over the mesh has no ``remote_instances`` row — it is judged
            # by the version it claimed as a member of the space.
            if await self._federation.space_member_supports(
                iid, min_version=FederationCapability.MIN_FOR_FEDERATED_MODERATION
            ):
                targets.append(iid)
            elif iid == space.owner_instance_id:
                raise HostTooOldError(iid)
            else:
                log.info(
                    "moderation: approver household %s in space %s is below "
                    "v_43 — not sent the submission",
                    iid,
                    space.id,
                )
        return targets

    async def send_submitted(
        self,
        space: Space,
        item: SpaceModerationItem,
        targets: list[str],
        *,
        submitter_instance_id: str | None = None,
        relay_frames: bool = False,
    ) -> list[str]:
        """One targeted, sealed send per reviewer household — never a
        broadcast — and the item's media to those households only.
        Returns the households the transport took it for.

        ``submitter_instance_id`` is set by the host re-sending an item it
        holds to a reviewer household that lacks it (:meth:`on_sync_begin`):
        the household the submitter belongs to. ``relay_frames``: the
        targets are known to run this revision (they echoed), so an item
        too large for a JSON relay envelope goes to a household seated from
        an invite link as one binary relay frame."""
        payload, relay = await self._wire(space, item)
        if submitter_instance_id:
            payload[SUBMITTER_INSTANCE_KEY] = submitter_instance_id
        # What a relay frame would carry as bytes. Only a content this
        # large is framed: a smaller one fits the JSON envelope, and
        # ``send_media_chunk`` would fall back to a JSON chunk for it.
        content = dump_json(
            {k: payload[k] for k in _FRAME_BODY_KEYS if k in payload}
        ).encode()
        framed = relay_frames and len(content) > RELAY_JSON_ITEM_BYTES
        # v_85 — the proposed files with their hashes, to a v_85 reviewer
        # household only. A page's files stay page-scoped (``sp-``) on
        # every household, so a page submission carries none; nor does a
        # post with an author-signed copy for followers: the host matches
        # that copy's file names against the files it holds by their
        # sender-bound names (``approved_relay_for``). Its release from the
        # host carries the hashes of the host's own copies either way.
        hashed = (
            await self._with_file_hashes(payload, _changed_media_urls(item))
            if item.feature != "pages" and relay is None
            else payload
        )
        reached: list[str] = []
        for iid in targets:
            if framed and await self._federation.is_space_session_peer(iid):
                # The existing v_87 frame: the small signed metadata names
                # the item, its content rides as the frame's bytes.
                result = await self._federation.send_media_chunk(
                    to_instance_id=iid,
                    event_type=FET.SPACE_MODERATION_SUBMITTED,
                    payload={
                        k: v for k, v in payload.items() if k not in _FRAME_BODY_KEYS
                    },
                    raw_chunk=content,
                    space_id=space.id,
                    mesh_fallback=True,
                )
                if result.ok:
                    reached.append(iid)
                else:
                    log.warning(
                        "moderation: large item %s did not reach reviewer "
                        "household %s over its relay (%s)",
                        item.id,
                        iid,
                        result.error,
                    )
                continue
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=FET.SPACE_MODERATION_SUBMITTED,
                payload=(
                    hashed
                    if hashed is not payload
                    and await self._federation.space_member_supports(
                        iid,
                        min_version=FederationCapability.MIN_FOR_CONTENT_FILE_HASHES,
                    )
                    else payload
                ),
                space_id=space.id,
            )
            if result.ok:
                reached.append(iid)
            else:
                log.warning(
                    "moderation: item %s did not reach reviewer household %s (%s)",
                    item.id,
                    iid,
                    result.error,
                )
        await self._send_media(space, item, targets)
        return reached

    async def _wire(
        self, space: Space, item: SpaceModerationItem
    ) -> tuple[dict, dict | None]:
        """``item`` as ``SPACE_MODERATION_SUBMITTED`` carries it, and the
        author-signed copy inside it (``None`` when it has none)."""
        # A held copy keeps the author's signed inner in its payload; on
        # the wire it rides next to the payload, never inside it.
        held = item.payload or {}
        body = {k: v for k, v in held.items() if k != PUBLIC_RELAY_KEY}
        relay = held.get(PUBLIC_RELAY_KEY) or (
            await self._public_relay_for(space, item)
        )
        payload: dict = {
            # Also in the sealed payload: the routing field is absent on a
            # mesh-relayed envelope, and the space-writer gate needs it.
            "space_id": space.id,
            "item_id": item.id,
            "feature": item.feature,
            "action": item.action,
            "target_id": body.get("target_id"),
            "submitted_by": item.submitted_by,
            "payload": body,
            # Every receiver rebuilds the "before" values from its own copy
            # of the target and ignores the sender's: never sent (half of
            # an edit's or a delete's bytes).
            "snapshot": None,
            "submitted_at": _aware(item.submitted_at).isoformat(),
            "expires_at": _aware(item.expires_at).isoformat(),
        }
        if relay is not None:
            payload[PUBLIC_RELAY_KEY] = relay
        return payload, relay

    async def _public_relay_for(
        self, space: Space, item: SpaceModerationItem
    ) -> dict | None:
        """The submitter's author-signed inner of a queued post in a
        PUBLIC / GLOBAL space that admits followers — so the host, once it
        approves the post, can relay it to the GFS followers under the
        AUTHOR's signature (a seed holder can't forge authorship). Signed
        over the post as queued, ``created_at`` = the submission time.
        ``None`` for anything else: other features and actions, a private
        space (never relayed to a connection server), a space without
        followers, or a submitter that is not one of our users."""
        if (
            not (self._own_pk and self._own_seed and self._own)
            or self._users is None
            or item.feature != "posts"
            or item.action != ContentAction.CREATE.value
            or (item.payload or {}).get("entity") != "post"
            or space.space_type not in PUBLIC_SPACE_TIERS
            or not space.features.allow_subscribers
        ):
            return None
        author = await self._users.get_by_user_id(item.submitted_by)
        if author is None:
            return None
        try:
            post = post_from_queue_payload(item, created_at=_aware(item.submitted_at))
        except ValueError:
            return None
        return build_signed_author_inner(
            post=post,
            space_id=space.id,
            author_username=author.username,
            author_pk=self._own_pk,
            author_identity_seed=self._own_seed,
            origin_instance_id=self._own,
            author_identity_anchor=author.identity_anchor,
        )

    async def _send_media(
        self, space: Space, item: SpaceModerationItem, targets: list[str]
    ) -> None:
        if self._media_sync is None or not targets:
            return
        urls = _changed_media_urls(item)
        if not urls:
            return
        try:
            await self._media_sync.enqueue_for_post(
                # Correlated by the queue item, never the target row: a
                # reviewer household checks the files against the item it
                # holds (``moderation_media_urls``). The target row there
                # still references its current media, so an edit's new
                # cover or picture would be refused against it.
                post_id=item.id,
                target_instance_ids=list(targets),
                media_urls=urls,
                space_id=space.id,
            )
        except Exception:
            log.exception("moderation: media of item %s not enqueued", item.id)

    async def send_decided(
        self,
        space: Space,
        item: SpaceModerationItem,
        *,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
    ) -> None:
        """To the host, the approver households and the submitter's
        household (each v_43+), one targeted send each."""
        targets = await self.reviewer_households(space)
        seat = await self._seats.get_including_tombstones(
            space.id, "", item.submitted_by
        )
        if seat is not None and seat.instance_id not in (self._own, *targets):
            targets.append(seat.instance_id)
        await self._send_decision(
            space,
            item,
            targets,
            decision=decision,
            decided_by=decided_by,
            reason=reason,
            decided_at=datetime.now(UTC),
        )

    async def _send_decision(
        self,
        space: Space,
        item: SpaceModerationItem,
        targets: list[str],
        *,
        decision: ModerationStatus,
        decided_by: str,
        reason: str | None,
        decided_at: datetime,
    ) -> list[str]:
        """Returns the households the transport took it for."""
        reached: list[str] = []
        payload: dict = {
            "space_id": space.id,
            "item_id": item.id,
            "decision": decision.value,
            "decided_by": decided_by,
            "decided_at": _aware(decided_at).isoformat(),
        }
        if reason:
            payload["reason"] = reason
        for iid in targets:
            if not await self._federation.space_member_supports(
                iid, min_version=FederationCapability.MIN_FOR_FEDERATED_MODERATION
            ):
                continue
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=FET.SPACE_MODERATION_DECIDED,
                payload=payload,
                space_id=space.id,
            )
            if result.ok:
                reached.append(iid)
            else:
                log.warning(
                    "moderation: decision on item %s did not reach %s (%s)",
                    item.id,
                    iid,
                    result.error,
                )
        return reached

    async def send_release_request(
        self, space: Space, item: SpaceModerationItem, *, decided_by: str
    ) -> None:
        """A reviewer household's approval goes to the HOST only, which
        applies the item from its own copy and announces the decision."""
        host = space.owner_instance_id
        if not host or host == self._own:
            return
        result = await self._federation.send_with_mesh_fallback(
            to_instance_id=host,
            event_type=FET.SPACE_MODERATION_DECIDED,
            payload={
                "space_id": space.id,
                "item_id": item.id,
                "decision": ModerationStatus.APPROVED.value,
                "decided_by": decided_by,
                "decided_at": datetime.now(UTC).isoformat(),
            },
            space_id=space.id,
        )
        if not result.ok:
            log.warning(
                "moderation: approval of item %s did not reach the host %s (%s)",
                item.id,
                host,
                result.error,
            )

    async def display_name(self, space_id: str, user_id: str) -> str | None:
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        return seat.display_name if seat is not None else None

    async def remote_role(self, space_id: str, user_id: str) -> str | None:
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        return seat.role if seat is not None and not seat.tombstoned else None

    async def is_remote_writer(self, space_id: str, user_id: str) -> bool:
        seat = await self._seats.get_including_tombstones(space_id, "", user_id)
        return (
            seat is not None
            and not seat.tombstoned
            and seat.role in _WRITER_SEATS
            and seat.instance_id != self._own
        )

    # ── Catch-up (the §25.6 BEGIN echo) ──────────────────────────────────

    async def held_echo(self, space_id: str, to_instance_id: str) -> dict | None:
        """What this household holds for ``space_id``, by state — what its
        §25.6 ``SPACE_SYNC_BEGIN`` to the space's host echoes
        (:data:`HELD_ECHO_KEY`), so the host sends what is missing or was
        decided meanwhile, and hears of a rejection made here that it never
        got. ``None`` (no field) unless ``to_instance_id`` is the space's
        host, somewhere else, and one of our people holds a
        content-authority seat in the space."""
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or not to_instance_id
            or to_instance_id != space.owner_instance_id
            or to_instance_id == self._own
            or not await self._reviews_here(space)
        ):
            return None
        ours = set(await self._spaces.list_local_member_user_ids(space_id))
        echo: dict[str, list] = {}
        for item in await self._open_rows(space_id):
            if item.status is ModerationStatus.PENDING:
                echo.setdefault(ECHO_PENDING, []).append(item.id)
            elif item.status is ModerationStatus.EXPIRED:
                echo.setdefault(ECHO_EXPIRED, []).append(item.id)
            elif (
                item.status is ModerationStatus.REJECTED
                and item.reviewed_by in ours
                and len(echo.get(ECHO_REJECTED, ())) < MAX_ECHO_REJECTIONS
            ):
                own: dict[str, str] = {"id": item.id, "by": item.reviewed_by}
                if item.rejection_reason:
                    own["reason"] = item.rejection_reason
                echo.setdefault(ECHO_REJECTED, []).append(own)
            else:
                echo.setdefault(ECHO_DECIDED, []).append(item.id)
        return echo

    async def _open_rows(self, space_id: str) -> list[SpaceModerationItem]:
        """Every item of the space held here whose review window is open —
        one read, pending first."""
        return await self._spaces.list_moderation_open(
            space_id, datetime.now(UTC), limit=MAX_ECHO_IDS
        )

    def forget_household(self, space_id: str, instance_id: str) -> None:
        """A seat of ``instance_id`` in ``space_id`` just changed role on
        this host: its next echo is answered at once and in full (what it
        purged while it did not review is sent again)."""
        self._caught_up_at.pop((space_id, instance_id), None)
        self._resent.pop((space_id, instance_id), None)
        self._seat_stamps.pop((space_id, instance_id), None)

    async def on_sync_begin(self, event: FederationEvent) -> None:
        """Host side of the catch-up: a reviewer household's §25.6 BEGIN
        echoes what it holds. Send it exactly the difference — the pending
        items it lacks or holds purged, and the decision of each item it
        holds pending that was approved or rejected meanwhile — and apply
        the rejections its own people made that never reached us. Only for
        a household holding a live admin / moderator seat; no echo (an
        older household, a plain member one) → nothing. Never raises: the
        BEGIN's own handler (the sync itself) is not this catch-up's to
        fail."""
        try:
            await self._catch_up(event)
        except Exception:
            log.warning(
                "moderation: catch-up for %s failed",
                getattr(event, "from_instance", "?"),
                exc_info=True,
            )

    async def _catch_up(self, event: FederationEvent) -> None:
        p = event.payload if isinstance(event.payload, dict) else {}
        space_id = str(event.space_id or p.get("space_id") or "")
        sender = str(event.from_instance or "")
        states = _echo_states(p.get(HELD_ECHO_KEY))
        if states is None or not space_id or not sender or sender == self._own:
            return
        # The rate limit comes before any read: a household that asks more
        # often than it is answered costs one dict lookup.
        key = (space_id, sender)
        now = self._clock()
        if now - self._caught_up_at.get(key, float("-inf")) < CATCH_UP_INTERVAL_S:
            return
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved or space.owner_instance_id != self._own:
            return
        # Pending content goes to a household that reviews, and nowhere else.
        stamp = _reviewer_stamp(
            await self._seats.list_for_instance(
                space_id, sender, include_tombstoned=False
            )
        )
        if not stamp:
            self.forget_household(space_id, sender)
            return
        if self._seat_stamps.get(key, stamp) != stamp:
            # Its reviewer seats are not the ones last answered — somebody
            # left and was seated again, with or without a role change:
            # what was sent before may be gone there, so start over.
            self._resent.pop(key, None)
        self._seat_stamps[key] = stamp
        held, rejections = states
        rows = await self._open_rows(space_id)
        resent = self._resent.setdefault(key, {})
        heard = False
        # Forget what is no longer open here, so the memory stays bounded.
        open_ids = {i.id for i in rows}
        for stale in [k for k in resent if k.removeprefix("d:") not in open_ids]:
            del resent[stale]

        def _fresh(mark: str) -> bool:
            return now - resent.get(mark, float("-inf")) >= RESEND_SAME_INTERVAL_S

        missing: list[tuple[SpaceModerationItem, str]] = []
        decided: list[SpaceModerationItem] = []
        for item in reversed(rows):  # oldest first
            state = held.get(item.id)
            if item.status is ModerationStatus.PENDING:
                if state == ECHO_REJECTED:
                    # Rejected there by its own people, and we never heard:
                    # the echo is that decision (checked like one).
                    heard = True
                    await self._on_decided(
                        replace(
                            event,
                            event_type=FET.SPACE_MODERATION_DECIDED,
                            payload={
                                "space_id": space_id,
                                "item_id": item.id,
                                "decision": ModerationStatus.REJECTED.value,
                                **rejections[item.id],
                            },
                        )
                    )
                elif (
                    state in (None, ECHO_EXPIRED)
                    and _reviewable(space, item)
                    and _fresh(item.id)
                ):
                    origin = await self._submitter_household(space_id, item)
                    # The submitter's own household holds its item already.
                    if origin and origin != sender:
                        missing.append((item, origin))
            elif (
                (
                    state == ECHO_PENDING
                    or (
                        state == ECHO_REJECTED
                        and item.status is ModerationStatus.APPROVED
                    )
                )
                and _fresh(f"d:{item.id}")
                and await self._decision_stands(space, item)
            ):
                decided.append(item)
        if not (missing or decided or heard):
            return
        # Spent by anything the echo made us do — an echoed rejection too,
        # so its checks do not run again on every BEGIN that still names it.
        self._caught_up_at[key] = now
        sent = size = 0
        for item, origin in missing:
            size += len(dump_json(item.payload).encode())
            if sent and size > CATCH_UP_MAX_BYTES:
                break
            # Marked only when it got out: a send the transport refused is
            # tried again on the next answer.
            if await self.send_submitted(
                space,
                item,
                [sender],
                submitter_instance_id=origin,
                # It echoed: it runs this revision and reads a relay frame.
                relay_frames=True,
            ):
                resent[item.id] = now
            sent += 1
        for verdict in decided:
            if await self._send_decision(
                space,
                verdict,
                [sender],
                decision=verdict.status,
                decided_by=verdict.reviewed_by or "",
                reason=verdict.rejection_reason,
                decided_at=verdict.reviewed_at or datetime.now(UTC),
            ):
                resent[f"d:{verdict.id}"] = now
        log.info(
            "moderation: caught %s up on space %s — %d of %d missing item(s), "
            "%d decision(s)",
            sender,
            space_id,
            sent,
            len(missing),
            len(decided),
        )

    async def _submitter_household(
        self, space_id: str, item: SpaceModerationItem
    ) -> str | None:
        """The household ``item``'s submitter holds a live seat on: ours,
        the one the roster mirror names, or ``None`` (they left)."""
        if item.submitted_by in await self._spaces.list_local_member_user_ids(space_id):
            return self._own
        seat = await self._seats.get_including_tombstones(
            space_id, "", item.submitted_by
        )
        if seat is None or seat.tombstoned:
            return None
        return seat.instance_id

    async def _decision_stands(self, space: Space, item: SpaceModerationItem) -> bool:
        """``item`` was approved or rejected here by somebody who still
        holds content authority — a receiver refuses a decision naming
        anybody else, so it is not sent."""
        if not item.reviewed_by or item.status not in (
            ModerationStatus.APPROVED,
            ModerationStatus.REJECTED,
        ):
            return False
        member = await self._spaces.get_member(space.id, item.reviewed_by)
        role = (
            str(member.role)
            if member is not None
            else await self.remote_role(space.id, item.reviewed_by)
        )
        return role in _AUTHORITY_ROLES

    # ── Inbound ──────────────────────────────────────────────────────────

    async def _on_submitted(self, event: FederationEvent) -> None:
        if event.media_bytes is not None:
            # A relay frame (a large item to a household behind a GFS): the
            # signed metadata is the payload, the content its bytes.
            body = _frame_body(event.media_bytes)
            if body is None:
                self._refuse(
                    event,
                    resolve_space_id(event) or "",
                    _bounded(event.payload.get("item_id")),
                    "malformed relay frame content",
                )
                return
            event = replace(event, payload={**event.payload, **body}, media_bytes=None)
        item = await self._admit_submission(event)
        if item is None or self._moderation is None:
            return
        space = await self._spaces.get(item.space_id)
        from_host = space is not None and event.from_instance == space.owner_instance_id
        if await self._moderation.store_received(item, from_host=from_host):
            log.info(
                "moderation: holding %s %s item %s from %s for review",
                item.feature,
                item.action,
                item.id,
                event.from_instance,
            )

    async def _admit_submission(
        self, event: FederationEvent
    ) -> SpaceModerationItem | None:
        """The item an inbound submission describes, or ``None`` (WARNING)
        when any receiver check fails — see the module docstring."""
        p = event.payload
        space_id = resolve_space_id(event) or ""
        item_id = _bounded(p.get("item_id"))
        feature = str(p.get("feature") or "")
        submitted_by = _bounded(p.get("submitted_by"))
        body = p.get("payload")
        target_id = _bounded(p.get("target_id"))
        try:
            action = ContentAction(str(p.get("action") or ""))
        except ValueError:
            action = None
        if (
            not space_id
            or not item_id
            or not submitted_by
            or not target_id
            or not isinstance(body, dict)
            or body.get("target_id") != target_id
            or action
            not in (ContentAction.CREATE, ContentAction.EDIT, ContentAction.DELETE)
            or self._moderation is None
        ):
            return self._refuse(event, space_id, item_id, "malformed submission")
        assert action is not None
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            return self._refuse(event, space_id, item_id, "unknown space")
        # 1. Only a household that reviews may hold pending content.
        if not await self._reviews_here(space):
            return self._refuse(
                event,
                space_id,
                item_id,
                "this household holds no content-authority seat (misdirected)",
            )
        # 2. The submitter is the sender's own writer, not banned. Only
        # the HOST may name another household as the submitter's: it holds
        # every pending item and re-sends one to a reviewer household that
        # lacks it (``on_sync_begin``). From anybody else the field is
        # ignored and the sender is the origin.
        origin = _bounded(p.get(SUBMITTER_INSTANCE_KEY))
        relayed = bool(
            origin
            and event.from_instance == space.owner_instance_id
            and origin not in (self._own, event.from_instance)
        )
        if not (
            await self._writes_on(space_id, origin, submitted_by)
            if relayed
            else await self._authorship.acts_for(event, space_id, submitted_by)
        ):
            return self._refuse(
                event,
                space_id,
                item_id,
                f"{submitted_by!r} holds no writer seat on "
                + (f"household {origin}" if relayed else "the sending household"),
            )
        if await self._spaces.is_banned(space_id, submitted_by):
            return self._refuse(event, space_id, item_id, "the submitter is banned")
        # 3. A create's id is the submitter's own.
        if action is ContentAction.CREATE:
            kind = _CREATE_KINDS.get((feature, str(body.get("entity") or "")))
            if (
                kind is None
                or not is_owner_bound(target_id)
                or owner_bound_id_refused(
                    kind,
                    target_id,
                    space_id=space_id,
                    owner_user_id=submitted_by,
                    context=f"{event.event_type} from {event.from_instance}",
                )
            ):
                return self._refuse(
                    event,
                    space_id,
                    item_id,
                    f"create target {target_id!r} is not bound to the submitter",
                )
        # 4. Reviewable here, now.
        try:
            level = space.features.access_level(feature)
        except ValueError:
            return self._refuse(
                event, space_id, item_id, f"unknown feature {feature!r}"
            )
        if level is not SpaceFeatureAccess.MODERATED:
            return self._refuse(event, space_id, item_id, f"{feature} is not reviewed")
        if space.archived:
            return self._refuse(event, space_id, item_id, "the space is archived")
        if not feature_on(space, feature, body):
            return self._refuse(event, space_id, item_id, f"{feature} is switched off")
        # 5. The live codecs and caps — and the inbound media rules: what a
        # reviewer's preview renders is never a remote URL (a beacon).
        # The author's signed copy of a queued public post travels next to
        # the payload, never inside it: anything under that key in the body
        # is dropped, and only a copy that verifies is kept (below).
        body = {k: v for k, v in body.items() if k != PUBLIC_RELAY_KEY}
        try:
            clean = self._moderation.validate_payload(
                space, feature, action, sanitize_remote_payload(feature, body)
            )
        except Exception as exc:
            log.debug(
                "SpaceModerationFederation._admit_submission: fail-soft error ignored",
                exc_info=True,
            )
            return self._refuse(event, space_id, item_id, f"invalid payload ({exc})")
        relay = _signed_copy(
            p.get(PUBLIC_RELAY_KEY),
            space=space,
            feature=feature,
            action=action,
            target_id=target_id,
            submitted_by=submitted_by,
        )
        # The "before" values a reviewer compares against come from OUR copy
        # of the target, never the sender's (whose snapshot could carry
        # anything, a remote image URL included).
        snapshot = await self._moderation.local_snapshot(
            space_id, feature, action, clean
        )
        # The submitter's household ships the files the item proposes
        # (``_send_media``): they take its names here, like any received
        # space file - so another household can't fill them first. A file
        # the target already shows here (an edited page's pictures, which
        # its patch repeats) keeps the name it has.
        held = load_json(snapshot, {}) if snapshot else {}
        held_urls = feature_media_urls(feature, held) if isinstance(held, dict) else []
        if (
            feature == "pages"
            and action is ContentAction.EDIT
            and self._page_media is not None
        ):
            # A reviewed resolution or revert repeats a side's or a history
            # version's files: they keep the names the page shows.
            held_urls = [*held_urls, *await self._page_media(space_id, target_id)]
        names = self._media_names.localize(
            space_id,
            str(event.from_instance or ""),
            feature_media_urls(feature, clean),
            held=held_urls,
            # v_85 — hashed files take their content names (never a page's:
            # page files stay page-scoped).
            hashes=parse_file_hashes(p) if feature != "pages" else None,
        )
        clean = renamed_media(feature, clean, names.url)
        if relay is not None:
            clean[PUBLIC_RELAY_KEY] = relay
        # 6. Caps.
        size = len(dump_json(clean).encode()) + len((snapshot or "").encode())
        if size > MAX_PAYLOAD_BYTES:
            return self._refuse(event, space_id, item_id, f"too large ({size} bytes)")
        if (
            await self._spaces.count_pending(space_id, submitted_by=submitted_by)
            >= MAX_PENDING_PER_SUBMITTER
        ):
            return self._refuse(event, space_id, item_id, "submitter cap reached")
        if (
            await self._spaces.count_pending_from_instance(
                space_id, origin if relayed else str(event.from_instance or "")
            )
            >= MAX_PENDING_PER_HOUSEHOLD
        ):
            return self._refuse(event, space_id, item_id, "household cap reached")
        if await self._spaces.count_pending(space_id) >= MAX_PENDING_PER_SPACE:
            return self._refuse(event, space_id, item_id, "space queue full")
        now = datetime.now(UTC)
        expires_at = min(
            _when(p.get("expires_at")) or now + MODERATION_TTL, now + MAX_EXPIRY
        )
        if expires_at <= now:
            return self._refuse(event, space_id, item_id, "already expired")
        # At most a little in the future (clock skew), never older than a
        # whole review window before its expiry.
        submitted_at = min(_when(p.get("submitted_at")) or now, now + MAX_FUTURE_SKEW)
        submitted_at = max(submitted_at, expires_at - MAX_EXPIRY)
        return SpaceModerationItem(
            id=item_id,
            space_id=space_id,
            feature=feature,
            action=action.value,
            submitted_by=submitted_by,
            payload=clean,
            current_snapshot=snapshot,
            submitted_at=submitted_at,
            expires_at=expires_at,
            status=ModerationStatus.PENDING,
        )

    async def _writes_on(self, space_id: str, instance_id: str, user_id: str) -> bool:
        """``user_id`` holds a live writer seat in ``space_id`` on household
        ``instance_id`` — :meth:`SpaceAuthorship.acts_for` for a submitter
        the host names on another household."""
        seat = await self._seats.get(space_id, instance_id, user_id)
        return seat is not None and seat.role in _WRITER_SEATS

    async def _reviews_here(self, space: Space) -> bool:
        """This household is the host, or one of its own people holds a
        content-authority seat in the space."""
        if space.owner_instance_id == self._own:
            return True
        # ``space_members`` on a member household also mirrors the host's
        # people (the §25.6 ``members`` resource) — the host's owner among
        # them. Only this household's own people count.
        ours = set(await self._spaces.list_local_member_user_ids(space.id))
        return any(
            m.role in CONTENT_AUTHORITY_ROLES and m.user_id in ours
            for m in await self._spaces.list_members(space.id)
        )

    async def _on_decided(self, event: FederationEvent) -> None:
        p = event.payload
        space_id = resolve_space_id(event) or ""
        item_id = _bounded(p.get("item_id"))
        decided_by = _bounded(p.get("decided_by"))
        raw_decision = str(p.get("decision") or "")
        reason = p.get("reason")
        if (
            not space_id
            or not item_id
            or not decided_by
            or raw_decision
            not in (ModerationStatus.APPROVED.value, ModerationStatus.REJECTED.value)
            or (reason is not None and not isinstance(reason, str))
            or self._moderation is None
        ):
            self._refuse(event, space_id, item_id, "malformed decision")
            return
        decision = ModerationStatus(raw_decision)
        if not await self._authorship.has_content_authority(event, space_id):
            self._refuse(
                event,
                space_id,
                item_id,
                "the sending household has no content authority",
            )
            return
        if not await self._authorship.approver_holds(event, space_id, decided_by):
            self._refuse(
                event,
                space_id,
                item_id,
                f"{decided_by!r} holds no content-authority seat there",
            )
            return
        clean_reason = (reason or "").strip()[:MAX_REJECT_REASON] or None
        if decision is not ModerationStatus.REJECTED:
            clean_reason = None
        space = await self._spaces.get(space_id)
        if space is None:
            return
        sender_is_host = event.from_instance == space.owner_instance_id
        if decision is ModerationStatus.APPROVED and not sender_is_host:
            # An approval from a reviewer household is a request for the
            # HOST to publish the item — only the host acts on it.
            if space.owner_instance_id == self._own:
                await self._release_for(event, space_id, item_id, decided_by)
            else:
                self._refuse(
                    event, space_id, item_id, "an approval only the host may announce"
                )
            return
        item = await self._spaces.get_moderation_item(item_id)
        if item is not None and item.space_id != space_id:
            return
        if item is None:
            # The decision overtook the submission: keep a tombstone so a
            # late SUBMITTED never stores it as pending.
            await self._moderation.record_early_decision(
                item_id=item_id,
                space_id=space_id,
                decision=decision,
                decided_by=decided_by,
                reason=clean_reason,
                from_instance=str(event.from_instance or ""),
            )
            return
        await self._moderation.apply_decision(
            item, decision=decision, decided_by=decided_by, reason=clean_reason
        )

    async def _release_for(
        self, event: FederationEvent, space_id: str, item_id: str, decided_by: str
    ) -> None:
        """The host applies a remote moderator's approval (their seat on the
        sender is verified above) — from its own copy of the item."""
        assert self._moderation is not None
        item = await self._spaces.get_moderation_item(item_id)
        if item is None or item.space_id != space_id or not item.feature:
            self._refuse(event, space_id, item_id, "no such item held by the host")
            return
        seat = await self._seats.get(
            space_id, str(event.from_instance or ""), decided_by
        )
        if seat is None or seat.tombstoned:
            self._refuse(event, space_id, item_id, "the approver holds no live seat")
            return
        try:
            await self._moderation.release_remote(
                item, approved_by=decided_by, role=seat.role
            )
        except Exception as exc:
            # Every gate of a local approve applies; a refusal is final for
            # this request (the reviewer may approve again).
            log.debug(
                "SpaceModerationFederation._release_for: fail-soft error ignored",
                exc_info=True,
            )
            self._refuse(event, space_id, item_id, f"not released: {exc!r}")

    @staticmethod
    def _refuse(
        event: FederationEvent, space_id: str, item_id: str, reason: str
    ) -> None:
        log.warning(
            "%s from %s: moderation item %r in space %r refused — %s",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            item_id,
            space_id,
            reason,
        )


#: The queue-payload key of a queued public post's author-signed inner — the
#: copy the host relays to GFS followers once it approves the post
#: (``space_public_outbound``; ``docs/protocol/moderation.md``).
PUBLIC_RELAY_KEY: str = "public_relay"


#: Roles (local or mirrored) whose decision a receiver accepts.
_AUTHORITY_ROLES: frozenset[str] = frozenset(r.value for r in CONTENT_AUTHORITY_ROLES)


def _reviewable(space: Space, item: SpaceModerationItem) -> bool:
    """Would a reviewer household admit ``item`` now (receiver check 4)?"""
    try:
        level = space.features.access_level(item.feature)
    except ValueError:
        return False
    return (
        level is SpaceFeatureAccess.MODERATED
        and not space.archived
        and feature_on(space, item.feature, item.payload or {})
    )


def _frame_body(raw: bytes) -> dict | None:
    """The content a relay frame carries for a large item — only the keys
    that travel as bytes, each an object — or ``None``."""
    try:
        body = load_json(raw.decode("utf-8"), None)
    except UnicodeDecodeError:
        return None
    if not isinstance(body, dict) or not isinstance(body.get("payload"), dict):
        return None
    relay = body.get("public_relay")
    if relay is not None and not isinstance(relay, dict):
        return None
    return {k: body[k] for k in _FRAME_BODY_KEYS if k in body}


def _reviewer_stamp(seats: list) -> tuple:
    """The live admin / moderator seats of one household in one space, as
    a comparable value: who, in which role, at which roster version (a
    removal and a re-seat each advance ``member_version``). Empty: the
    household does not review."""
    return tuple(
        sorted(
            (s.user_id, s.role, s.member_version)
            for s in seats
            if s.role in REVIEWER_SEATS and not s.tombstoned
        )
    )


def _echo_states(
    raw: object,
) -> tuple[dict[str, str], dict[str, dict[str, str]]] | None:
    """A ``mod_held`` echo as ``({item id: state}, {item id: the own
    rejection it names})`` — or ``None`` for a missing, malformed or
    oversized one (ignored whole: a half-read echo would make the host
    re-send what the household holds)."""
    if not isinstance(raw, dict) or not set(raw) <= {
        ECHO_PENDING,
        ECHO_EXPIRED,
        ECHO_DECIDED,
        ECHO_REJECTED,
    }:
        return None
    if not all(isinstance(v, list) for v in raw.values()) or (
        sum(len(v) for v in raw.values()) > MAX_ECHO_IDS
        or len(raw.get(ECHO_REJECTED, ())) > MAX_ECHO_REJECTIONS
    ):
        return None
    states: dict[str, str] = {}
    rejections: dict[str, dict[str, str]] = {}
    for state in (ECHO_PENDING, ECHO_EXPIRED, ECHO_DECIDED):
        for item_id in raw.get(state, ()):
            if not _bounded(item_id):
                return None
            states[item_id] = state
    for own in raw.get(ECHO_REJECTED, ()):
        if not isinstance(own, dict):
            return None
        item_id, by, reason = _bounded(own.get("id")), _bounded(own.get("by")), None
        if "reason" in own:
            reason = own["reason"]
            if not isinstance(reason, str):
                return None
        if not item_id or not by:
            return None
        states[item_id] = ECHO_REJECTED
        rejections[item_id] = {"decided_by": by}
        if reason:
            rejections[item_id]["reason"] = reason[:MAX_REJECT_REASON]
    return states, rejections


def _signed_copy(
    raw: object,
    *,
    space: Space,
    feature: str,
    action: ContentAction,
    target_id: str,
    submitted_by: str,
) -> dict | None:
    """The submitter's signed copy of a queued post worth keeping: a post
    create in a space whose content may reach followers, author-signed by
    the submitter for this space and this post id. Whether it matches the
    post is checked again when the approved post is relayed."""
    if (
        not isinstance(raw, dict)
        or feature != "posts"
        or action is not ContentAction.CREATE
        or space.space_type not in PUBLIC_SPACE_TIERS
        or not space.features.allow_subscribers
    ):
        return None
    if (
        raw.get("space_id") != space.id
        or raw.get("post_id") != target_id
        or raw.get("author_user_id") != submitted_by
        or not verify_signed_author_inner(raw)
    ):
        log.info(
            "moderation: signed copy of queued post %s does not verify — "
            "kept without it (its approval will not reach GFS followers)",
            target_id,
        )
        return None
    return dict(raw)


def _space_ref(value: object) -> str | None:
    """*value* unchanged when it names a space file (:func:`space_media_ref`:
    a local media reference, never a name another receiving path owns —
    a DM attachment, GFS moment media, a GFS reader copy), else ``None``.
    Kept byte for byte: an author's signed copy may cover it."""
    return value if isinstance(value, str) and space_media_ref(value) else None


def _space_body(text: object) -> object:
    """A page body with every picture that is no space file emptied."""
    if not isinstance(text, str) or not text:
        return text
    return rewrite_page_body_media(text, lambda ref: _space_ref(ref) or "")


def _local_refs(values: object, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [v for v in values if _space_ref(v)][:limit]


def sanitize_remote_payload(feature: str, payload: dict) -> dict:
    """A remote submission's payload under the inbound media rules: every
    media field (a page body's pictures included) a space file
    (:func:`_space_ref`) or nothing, coordinates at 4
    decimal places. Everything else is the handler's ``validate``."""
    out = dict(payload)
    match feature:
        case "posts":
            out["media_url"] = _space_ref(out.get("media_url"))
            out["image_urls"] = _local_refs(
                out.get("image_urls"), limit=FEED_POST_MAX_IMAGES
            )
            fm = out.get("file_meta")
            out["file_meta"] = (
                dict(fm) if isinstance(fm, dict) and _space_ref(fm.get("url")) else None
            )
            loc = out.get("location")
            if isinstance(loc, dict):
                try:
                    out["location"] = {
                        "lat": truncate_coord(float(loc["lat"])),
                        "lon": truncate_coord(float(loc["lon"])),
                        "label": loc.get("label"),
                    }
                except KeyError, TypeError, ValueError:
                    out["location"] = None
            else:
                out["location"] = None
            lp = out.get("link_preview")
            if (
                isinstance(lp, dict)
                and lp.get("thumbnail_url")
                and not _space_ref(lp.get("thumbnail_url"))
            ):
                out["link_preview"] = {**lp, "thumbnail_url": None}
            attachments = out.get("attachments")
            if isinstance(attachments, dict) and isinstance(
                attachments.get("bazaar"), dict
            ):
                bazaar = dict(attachments["bazaar"])
                bazaar["image_urls"] = _local_refs(
                    bazaar.get("image_urls"), limit=BAZAAR_MAX_IMAGES
                )
                out["attachments"] = {**attachments, "bazaar": bazaar}
        case "pages":
            if "cover_image_url" in out:
                out["cover_image_url"] = _space_ref(out["cover_image_url"])
            for key in ("content", "merged_content"):
                if key in out:
                    out[key] = _space_body(out[key])
            patch = out.get("patch")
            if isinstance(patch, dict):
                patch = dict(patch)
                if "cover_image_url" in patch:
                    patch["cover_image_url"] = _space_ref(patch["cover_image_url"])
                if "content" in patch:
                    patch["content"] = _space_body(patch["content"])
                out["patch"] = patch
        case "calendar":
            if "cover_url" in out:
                out["cover_url"] = _space_ref(out["cover_url"])
            patch = out.get("patch")
            if isinstance(patch, dict) and "cover_url" in patch:
                out["patch"] = {
                    **patch,
                    "cover_url": _space_ref(patch["cover_url"]),
                }
    return out


def _bounded(value: object) -> str:
    """A non-empty id-shaped string from the wire, else ``""``."""
    if not isinstance(value, str) or not value or len(value) > _MAX_ID:
        return ""
    return value


def _when(value: object) -> datetime | None:
    when = parse_iso8601_optional(value) if isinstance(value, str) else None
    if when is None:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def moderation_media_urls(item: SpaceModerationItem) -> list[str]:
    """Every local media file a queued item proposes — a post's (and its
    listing's) files, or a calendar create's / edit's cover. Both the
    submitter (what to send) and a reviewer household (what a
    ``SPACE_MEDIA_BLOB`` correlated by the item may write) use it."""
    return feature_media_urls(item.feature, item.payload or {})


def _changed_media_urls(item: SpaceModerationItem) -> list[str]:
    """:func:`moderation_media_urls` minus the files the target already
    has (``current_snapshot``: an edited event's cover, an edited post's
    pictures) — every reviewer holds those already."""
    urls = moderation_media_urls(item)
    snapshot = item_payload_snapshot(item)
    if not snapshot:
        return urls
    have = {media_basename(u) for u in feature_media_urls(item.feature, snapshot)}
    return [u for u in urls if media_basename(u) not in have]
