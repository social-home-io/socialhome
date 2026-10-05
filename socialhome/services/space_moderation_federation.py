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
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.federation import FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    ContentAction,
    HostTooOldError,
    PUBLIC_SPACE_TIERS,
    ModerationStatus,
    Space,
    SpaceFeatureAccess,
    SpaceModerationItem,
    SpaceRole,
    WRITER_ROLES,
)
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
from ..domain.post import BAZAAR_MAX_IMAGES, FEED_POST_MAX_IMAGES
from ..domain.presence import truncate_coord
from ..federation.space_scope import resolve_space_id
from ..repositories.base import dump_json
from ..utils.datetime import parse_iso8601_optional
from .inbound_media_store import verbatim_local_media_ref
from .space_post_moderation import post_from_queue_payload
from .space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)
from .space_moderation_service import (
    MAX_PAYLOAD_BYTES,
    MAX_PENDING_PER_SPACE,
    MAX_PENDING_PER_SUBMITTER,
    MAX_REJECT_REASON,
    MODERATION_TTL,
    feature_on,
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

#: The owner-bound id kind of a create's target, per (feature, entity).
_CREATE_KINDS: dict[tuple[str, str], str] = {
    ("posts", "post"): SPACE_POST_KIND,
    ("pages", "page"): SPACE_PAGE_KIND,
    ("tasks", "task"): SPACE_TASK_KIND,
    ("tasks", "list"): SPACE_TASK_LIST_KIND,
    ("stickies", "sticky"): SPACE_STICKY_KIND,
    ("calendar", "event"): SPACE_CALENDAR_EVENT_KIND,
}


class SpaceModerationFederation:
    """Send and receive moderation items and decisions (v_43)."""

    __slots__ = (
        "_federation",
        "_spaces",
        "_seats",
        "_authorship",
        "_media_sync",
        "_moderation",
        "_own_pk",
        "_own_seed",
        "_users",
    )

    def __init__(
        self,
        *,
        federation_service: "FederationService",
        space_repo: "AbstractSpaceRepo",
        remote_member_repo: "AbstractSpaceRemoteMemberRepo",
        authorship: "SpaceAuthorship",
        media_sync: "SpaceMediaSyncService | None" = None,
    ) -> None:
        self._federation = federation_service
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._authorship = authorship
        self._media_sync = media_sync
        self._moderation: "SpaceModerationService | None" = None
        #: Our household identity + local users — to author-sign a queued
        #: public post's inner for the GFS followers (see
        #: :meth:`_public_relay_for`). Unset → no signed copy is attached.
        self._own_pk: bytes = b""
        self._own_seed: bytes = b""
        self._users: "AbstractUserRepo | None" = None

    def attach_identity(
        self,
        *,
        own_instance_pk: bytes,
        own_identity_seed: bytes,
        user_repo: "AbstractUserRepo",
    ) -> None:
        """Wire what author-signing a queued public post needs."""
        self._own_pk = own_instance_pk
        self._own_seed = own_identity_seed
        self._users = user_repo

    def bind(self, moderation: "SpaceModerationService") -> None:
        """Wire both directions: the queue sends through us, and what we
        receive is stored through it."""
        self._moderation = moderation
        moderation.attach_federation(self)

    def attach_to(self, federation_service: "FederationService") -> None:
        registry = federation_service._event_registry
        registry.register(FET.SPACE_MODERATION_SUBMITTED, self._on_submitted)
        registry.register(FET.SPACE_MODERATION_DECIDED, self._on_decided)

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
            if await self._federation.peer_supports(
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
        self, space: Space, item: SpaceModerationItem, targets: list[str]
    ) -> None:
        """One targeted, sealed send per reviewer household — never a
        broadcast — and the item's media to those households only."""
        relay = await self._public_relay_for(space, item)
        payload = {
            # Also in the sealed payload: the routing field is absent on a
            # mesh-relayed envelope, and the space-writer gate needs it.
            "space_id": space.id,
            "item_id": item.id,
            "feature": item.feature,
            "action": item.action,
            "target_id": (item.payload or {}).get("target_id"),
            "submitted_by": item.submitted_by,
            "payload": item.payload,
            "snapshot": item.current_snapshot,
            "submitted_at": _aware(item.submitted_at).isoformat(),
            "expires_at": _aware(item.expires_at).isoformat(),
        }
        if relay is not None:
            payload["public_relay"] = relay
        for iid in targets:
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=FET.SPACE_MODERATION_SUBMITTED,
                payload=payload,
                space_id=space.id,
            )
            if not result.ok:
                log.warning(
                    "moderation: item %s did not reach reviewer household %s (%s)",
                    item.id,
                    iid,
                    result.error,
                )
        await self._send_media(space, item, targets)

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
        if self._media_sync is None or item.feature != "posts" or not targets:
            return
        urls = _post_media_urls(item.payload or {})
        if not urls:
            return
        try:
            await self._media_sync.enqueue_for_post(
                post_id=str((item.payload or {}).get("target_id") or ""),
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
        payload: dict = {
            "space_id": space.id,
            "item_id": item.id,
            "decision": decision.value,
            "decided_by": decided_by,
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
        if reason:
            payload["reason"] = reason
        for iid in targets:
            if not await self._federation.peer_supports(
                iid, min_version=FederationCapability.MIN_FOR_FEDERATED_MODERATION
            ):
                continue
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=iid,
                event_type=FET.SPACE_MODERATION_DECIDED,
                payload=payload,
                space_id=space.id,
            )
            if not result.ok:
                log.warning(
                    "moderation: decision on item %s did not reach %s (%s)",
                    item.id,
                    iid,
                    result.error,
                )

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
                "decided_at": datetime.now(timezone.utc).isoformat(),
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

    # ── Inbound ──────────────────────────────────────────────────────────

    async def _on_submitted(self, event: "FederationEvent") -> None:
        item = await self._admit_submission(event)
        if item is None or self._moderation is None:
            return
        if await self._moderation.store_received(item):
            log.info(
                "moderation: holding %s %s item %s from %s for review",
                item.feature,
                item.action,
                item.id,
                event.from_instance,
            )

    async def _admit_submission(
        self, event: "FederationEvent"
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
        # 2. The submitter is the sender's own writer, not banned.
        if not await self._authorship.acts_for(event, space_id, submitted_by):
            return self._refuse(
                event,
                space_id,
                item_id,
                f"{submitted_by!r} holds no writer seat on the sending household",
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
            return self._refuse(event, space_id, item_id, f"invalid payload ({exc})")
        relay = _signed_copy(
            p.get(PUBLIC_RELAY_KEY),
            space=space,
            feature=feature,
            action=action,
            target_id=target_id,
            submitted_by=submitted_by,
        )
        if relay is not None:
            clean[PUBLIC_RELAY_KEY] = relay
        # The "before" values a reviewer compares against come from OUR copy
        # of the target, never the sender's (whose snapshot could carry
        # anything, a remote image URL included).
        snapshot = await self._moderation.local_snapshot(
            space_id, feature, action, clean
        )
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
                space_id, str(event.from_instance or "")
            )
            >= MAX_PENDING_PER_HOUSEHOLD
        ):
            return self._refuse(event, space_id, item_id, "household cap reached")
        if await self._spaces.count_pending(space_id) >= MAX_PENDING_PER_SPACE:
            return self._refuse(event, space_id, item_id, "space queue full")
        now = datetime.now(timezone.utc)
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

    async def _reviews_here(self, space: Space) -> bool:
        """This household is the host, or one of its own people holds a
        content-authority seat in the space."""
        if space.owner_instance_id == self._own:
            return True
        return any(
            m.role in CONTENT_AUTHORITY_ROLES
            for m in await self._spaces.list_members(space.id)
        )

    async def _on_decided(self, event: "FederationEvent") -> None:
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
        self, event: "FederationEvent", space_id: str, item_id: str, decided_by: str
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
            self._refuse(event, space_id, item_id, f"not released: {exc!r}")

    @staticmethod
    def _refuse(
        event: "FederationEvent", space_id: str, item_id: str, reason: str
    ) -> None:
        log.warning(
            "%s from %s: moderation item %r in space %r refused — %s",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            item_id,
            space_id,
            reason,
        )
        return None


#: The queue-payload key of a queued public post's author-signed inner — the
#: copy the host relays to GFS followers once it approves the post
#: (``space_public_outbound``; ``docs/protocol/moderation.md``).
PUBLIC_RELAY_KEY: str = "public_relay"


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


def _local_refs(values: object, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [v for v in values if verbatim_local_media_ref(v)][:limit]


def sanitize_remote_payload(feature: str, payload: dict) -> dict:
    """A remote submission's payload under the inbound media rules: every
    media field a local media reference or nothing, coordinates at 4
    decimal places. Everything else is the handler's ``validate``."""
    out = dict(payload)
    match feature:
        case "posts":
            out["media_url"] = verbatim_local_media_ref(out.get("media_url"))
            out["image_urls"] = _local_refs(
                out.get("image_urls"), limit=FEED_POST_MAX_IMAGES
            )
            fm = out.get("file_meta")
            out["file_meta"] = (
                dict(fm)
                if isinstance(fm, dict) and verbatim_local_media_ref(fm.get("url"))
                else None
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
            if isinstance(lp, dict) and lp.get("thumbnail_url"):
                if not verbatim_local_media_ref(lp.get("thumbnail_url")):
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
                out["cover_image_url"] = verbatim_local_media_ref(
                    out["cover_image_url"]
                )
            patch = out.get("patch")
            if isinstance(patch, dict) and "cover_image_url" in patch:
                out["patch"] = {
                    **patch,
                    "cover_image_url": verbatim_local_media_ref(
                        patch["cover_image_url"]
                    ),
                }
        case "calendar":
            if "cover_url" in out:
                out["cover_url"] = verbatim_local_media_ref(out["cover_url"])
            patch = out.get("patch")
            if isinstance(patch, dict) and "cover_url" in patch:
                out["patch"] = {
                    **patch,
                    "cover_url": verbatim_local_media_ref(patch["cover_url"]),
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
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _post_media_urls(payload: dict) -> list[str]:
    """Every local media file a queued post (and its listing) references."""
    urls: list[str] = []
    for url in (payload.get("media_url"), *(payload.get("image_urls") or ())):
        if isinstance(url, str) and url:
            urls.append(url)
    fm = payload.get("file_meta")
    if isinstance(fm, dict) and isinstance(fm.get("url"), str) and fm["url"]:
        urls.append(fm["url"])
    lp = payload.get("link_preview")
    if isinstance(lp, dict) and isinstance(lp.get("thumbnail_url"), str):
        if lp["thumbnail_url"]:
            urls.append(lp["thumbnail_url"])
    bazaar = (payload.get("attachments") or {}).get("bazaar")
    if isinstance(bazaar, dict):
        for url in bazaar.get("image_urls") or ():
            if isinstance(url, str) and url:
                urls.append(url)
    return urls
