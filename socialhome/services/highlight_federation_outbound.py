"""Outbound federation for personal Highlights (§Highlights).

Subscribes to :class:`HighlightFrameAdded`, :class:`HighlightFrameRemoved` and
:class:`HighlightRemoved` and translates the local-write events into the
matching ``HIGHLIGHT_*`` federation envelopes targeted at the audience.

Audience routing — Highlights are *not* space-scoped:

* ``audience_kind = 'all_paired'`` → fan to every confirmed peer.
* ``audience_kind = 'households'`` → fan to the listed peer instance ids
  only (already in instance-id form on the event payload).
* ``audience_kind = 'users'``      → resolve each ``audience`` user id
  to its home instance via :class:`AbstractUserRepo` and dedup.

:class:`HighlightAudienceChanged` ("Change for all of today") is told with
the same events: ``HIGHLIGHT_DELETED`` to households that drop out, a
re-sent ``HIGHLIGHT_CREATED`` to households whose slice changed, and the
day's frames to newly added households only — see
:meth:`HighlightFederationOutbound._on_audience_changed`.

Each household's envelope carries only its own slice of the audience
(its own picked people, itself, or nothing) — never who else the
highlight was for. See :meth:`HighlightFederationOutbound._audience_per_target`.

**Echo-loop guard.** :class:`FederationInboundService` republishes
``HighlightFrameAdded`` / ``HighlightRemoved`` / ``HighlightFrameRemoved`` on the
local bus so :class:`RealtimeService` can fan to local viewers. To
avoid re-fanning that event back to every peer, this subscriber
gates each event on "is the author a local user on *this* instance?"
via :func:`AbstractUserRepo.get_instance_for_user`. Remote-author
events flow through the bus untouched here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.events import (
    HighlightAudienceChanged,
    HighlightFrameAdded,
    HighlightFrameReactionChanged,
    HighlightFrameRemoved,
    HighlightFrameViewed,
    HighlightRemoved,
)
from ..domain.federation import FederationEventType
from ..domain.highlight import Highlight, HighlightFrame
from ..infrastructure.event_bus import EventBus
from .peer_outbound import ConfirmedPeerBroadcaster, SingleTargetSender
from .protection_gate import ProtectionGateMixin
from .visibility import VisibilityMixin

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.peer_user_visibility_repo import AbstractPeerUserVisibilityRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


class HighlightFederationOutbound(
    VisibilityMixin,
    ConfirmedPeerBroadcaster,
    SingleTargetSender,
    ProtectionGateMixin,
):
    """Publish Highlight mutations to paired peer instances."""

    __slots__ = (
        "_bus",
        "_child_protection",
        "_federation",
        "_federation_repo",
        "_user_repo",
    )

    # Narrow the mixins' optional ``_federation`` — this service requires it
    # at construction, so direct attribute access elsewhere is non-None.
    _federation: FederationService

    def __init__(
        self,
        *,
        bus: EventBus,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
        user_repo: AbstractUserRepo,
        visibility_repo: AbstractPeerUserVisibilityRepo | None = None,
    ) -> None:
        self._bus = bus
        self._federation = federation_service
        self._federation_repo = federation_repo
        self._user_repo = user_repo
        self._visibility_repo = visibility_repo
        self._child_protection = None

    def wire(self) -> None:
        """Subscribe handlers on the bus. Idempotent."""
        self._bus.subscribe(HighlightFrameAdded, self._on_frame_added)
        self._bus.subscribe(HighlightFrameRemoved, self._on_frame_removed)
        self._bus.subscribe(HighlightRemoved, self._on_highlight_removed)
        self._bus.subscribe(HighlightAudienceChanged, self._on_audience_changed)
        # Back-channel events flow viewer → author. The echo-loop guard
        # gates on "is the *actor* (viewer / reactor) local?" — same
        # idea as the author-side gate above, just on a different field.
        self._bus.subscribe(HighlightFrameViewed, self._on_frame_viewed)
        self._bus.subscribe(HighlightFrameReactionChanged, self._on_reaction_changed)

    # ── Bus handlers ─────────────────────────────────────────────────────

    async def _on_frame_added(self, event: HighlightFrameAdded) -> None:
        if not await self._is_local_author(event.author_user_id):
            return  # echo from inbound or remote-only — never re-fan
        ev_type = (
            FederationEventType.HIGHLIGHT_CREATED
            if event.is_first_frame
            else FederationEventType.HIGHLIGHT_FRAME_APPENDED
        )
        payload = {
            "highlight_id": event.highlight_id,
            "frame_id": event.frame_id,
            "author_user_id": event.author_user_id,
            "highlight_date": event.highlight_date,
            "sequence": event.sequence,
            "audience_kind": event.audience_kind,
            "audience": list(event.audience),
            "frame_type": event.frame_type,
            "media_url": event.media_url,
            "caption_text": event.caption_text,
            "caption_emoji": event.caption_emoji,
            "duration_ms": event.duration_ms,
            "expires_at": event.expires_at,
            "occurred_at": event.occurred_at.isoformat(),
        }
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=ev_type,
            payload=payload,
            author_user_id=event.author_user_id,
        )

    async def _on_frame_removed(self, event: HighlightFrameRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.HIGHLIGHT_FRAME_DELETED,
            payload={
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
            author_user_id=event.author_user_id,
        )

    async def _on_highlight_removed(self, event: HighlightRemoved) -> None:
        if not await self._is_local_author(event.author_user_id):
            return
        await self._fan_to_audience(
            audience_kind=event.audience_kind,
            audience=event.audience,
            event_type=FederationEventType.HIGHLIGHT_DELETED,
            payload={
                "highlight_id": event.highlight_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
            author_user_id=event.author_user_id,
        )

    async def _on_audience_changed(self, event: HighlightAudienceChanged) -> None:
        """Change for all of today, told to each household with the events
        it already understands (no new event type, no protocol version):

        * a household that **drops out** gets ``HIGHLIGHT_DELETED`` — the
          receiver deletes the highlight and its frames, and no later frame
          is addressed to it;
        * a household that **stays** but whose slice changed (the kind, or
          its own picked people) gets ``HIGHLIGHT_CREATED`` re-sent with its
          new slice — receivers upsert the audience on it
          (``save_highlight``) and re-gate their users at read time;
        * a household that is **new** gets the day: ``HIGHLIGHT_CREATED``
          with the first frame, then ``HIGHLIGHT_FRAME_APPENDED`` for each
          later one (≤ ``MAX_FRAMES_PER_HIGHLIGHT``), in order.

        A household whose slice is unchanged gets nothing. Cost per change:
        one small envelope per dropped or re-gated household, plus ≤ 30
        frame envelopes per newly added household — once per author action.
        """
        if not await self._is_local_author(event.author_user_id):
            return
        before = await self._audience_per_target(
            event.old_audience_kind, event.old_audience
        )
        after = await self._audience_per_target(event.audience_kind, event.audience)
        kind_changed = event.old_audience_kind != event.audience_kind
        frames = event.frames
        for instance_id in before.keys() - after.keys():
            if not await self._may_send(event.author_user_id, instance_id):
                continue
            await self.send_to_instance(
                instance_id,
                FederationEventType.HIGHLIGHT_DELETED,
                {
                    "highlight_id": event.highlight_id,
                    "author_user_id": event.author_user_id,
                    "occurred_at": event.occurred_at.isoformat(),
                },
            )
        for instance_id, visible in after.items():
            is_new = instance_id not in before
            if (
                not is_new
                and not kind_changed
                and (set(before[instance_id]) == set(visible))
            ):
                continue
            if is_new and not frames:
                # Nothing to show yet: a later frame's APPENDED carries the
                # parent fields and the receiver creates it then.
                continue
            if not await self._may_send(event.author_user_id, instance_id):
                continue
            # New: the whole day. Kept: the row again (with its first frame,
            # so the envelope is the CREATED every receiver already knows).
            sending = frames if is_new else frames[:1]
            if not sending:
                await self.send_to_instance(
                    instance_id,
                    FederationEventType.HIGHLIGHT_CREATED,
                    self._highlight_payload(event, visible),
                )
                continue
            for n, frame in enumerate(sending):
                await self.send_to_instance(
                    instance_id,
                    FederationEventType.HIGHLIGHT_CREATED
                    if n == 0
                    else FederationEventType.HIGHLIGHT_FRAME_APPENDED,
                    {
                        **self._highlight_payload(event, visible),
                        **self._frame_payload(frame),
                    },
                )

    @staticmethod
    def _highlight_payload(
        event: HighlightAudienceChanged, visible: tuple[str, ...]
    ) -> dict:
        """The highlight-row fields of a CREATED / APPENDED body, with the
        receiving household's own slice of the new audience."""
        return {
            "highlight_id": event.highlight_id,
            "author_user_id": event.author_user_id,
            "highlight_date": event.highlight_date,
            "audience_kind": event.audience_kind,
            "audience": list(visible),
            "expires_at": event.expires_at,
            "occurred_at": event.occurred_at.isoformat(),
        }

    @staticmethod
    def _frame_payload(frame: HighlightFrame) -> dict:
        return {
            "frame_id": frame.id,
            "sequence": frame.sequence,
            "frame_type": frame.frame_type.value,
            "media_url": frame.media_url,
            "caption_text": frame.caption_text,
            "caption_emoji": frame.caption_emoji,
            "duration_ms": frame.duration_ms,
        }

    # ── Back-channel: viewer/reactor → author ────────────────────────────

    async def _on_frame_viewed(self, event: HighlightFrameViewed) -> None:
        if not await self._is_local_user(event.viewer_user_id):
            return  # echo from inbound or the author's own view — drop
        # Author-only delivery: the only peer that needs the view
        # receipt is the author's home instance. If the author *is*
        # local (i.e. local viewer + local author), there's no
        # federation work — the local realtime layer already fanned.
        target = await self._home_instance_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        hidden = await self.hidden_for_peer(target)
        if event.viewer_user_id in hidden:
            return
        await self.send_to_instance(
            target,
            FederationEventType.HIGHLIGHT_FRAME_VIEWED,
            {
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "viewer_user_id": event.viewer_user_id,
                "author_user_id": event.author_user_id,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    async def _on_reaction_changed(self, event: HighlightFrameReactionChanged) -> None:
        if not await self._is_local_user(event.reactor_user_id):
            return
        target = await self._home_instance_or_none(event.author_user_id)
        if target is None or target == self._federation.own_instance_id:
            return
        hidden = await self.hidden_for_peer(target)
        if event.reactor_user_id in hidden:
            return
        if event.emoji is None:
            ev_type = FederationEventType.HIGHLIGHT_FRAME_REACTION_REMOVED
        else:
            ev_type = FederationEventType.HIGHLIGHT_FRAME_REACTED
        await self.send_to_instance(
            target,
            ev_type,
            {
                "highlight_id": event.highlight_id,
                "frame_id": event.frame_id,
                "reactor_user_id": event.reactor_user_id,
                "author_user_id": event.author_user_id,
                "emoji": event.emoji,
                "occurred_at": event.occurred_at.isoformat(),
            },
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _is_local_author(self, author_user_id: str) -> bool:
        return await self._is_local_user(author_user_id)

    async def _is_local_user(self, user_id: str) -> bool:
        """Generic 'does this user_id live on *this* instance?' check.

        Used by both the author-side gate (frame added / removed /
        highlight removed) and the actor-side gate (frame viewed / reaction
        changed). Same semantics — different field on the event.
        """
        return (
            await self._home_instance_or_none(user_id)
            == self._federation.own_instance_id
        )

    async def _home_instance_or_none(self, user_id: str) -> str | None:
        try:
            return await self._user_repo.get_instance_for_user(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("highlight-outbound: user lookup failed: %s", exc, exc_info=True)
            return None

    async def _resolve_audience(
        self,
        kind: str,
        audience: tuple[str, ...],
    ) -> set[str]:
        """Return the set of peer instance_ids to send the event to."""
        return set(await self._audience_per_target(kind, audience))

    async def _audience_per_target(
        self,
        kind: str,
        audience: tuple[str, ...],
    ) -> dict[str, tuple[str, ...]]:
        """Each peer the event goes to → the slice of the audience it may see.

        A household learns only its own part of who a highlight is for:
        the picked people homed on it (``users``), itself (``households``)
        or nobody (``all_paired``). Receivers read the list only to gate
        their own local users (``highlight_repo._IN_AUDIENCE_SQL``), so the
        slice is exactly what they need — another household's people, the
        author's housemates and the other picked households stay home.
        """
        own = self._federation.own_instance_id
        if kind == "all_paired":
            return {iid: () for iid in await self.list_confirmed_peer_ids()}
        if kind == "households":
            return {iid: (iid,) for iid in audience if iid and iid != own}
        if kind == "users":
            per_target: dict[str, list[str]] = {}
            for uid in audience:
                try:
                    home = await self._user_repo.get_instance_for_user(uid)
                except Exception as exc:  # pragma: no cover — defensive
                    log.debug(
                        "highlight-outbound: instance lookup for %s failed: %s",
                        uid,
                        exc,
                        exc_info=True,
                    )
                    continue
                if home and home != own:
                    per_target.setdefault(home, []).append(uid)
            return {iid: tuple(uids) for iid, uids in per_target.items()}
        log.debug("highlight-outbound: unknown audience_kind %r", kind)
        return {}

    async def reaches(
        self, highlight: Highlight, *, instance_id: str, user_id: str
    ) -> bool:
        """Whether *highlight* is delivered to *user_id* on household
        *instance_id* — the fan-out rule below, asked in reverse.

        The receiver of a remote view or reaction (``HIGHLIGHT_FRAME_VIEWED``
        / ``_REACTED`` / ``_REACTION_REMOVED``) uses it to honour only
        someone the highlight actually reached: the household is one the
        audience resolves to, a ``users`` audience names this very user, the
        author is not hidden from that household (§Privacy) and no guardian
        block covers it (§CP.F2).
        """
        kind = str(highlight.audience_kind)
        if kind == "users" and user_id not in highlight.audience:
            return False
        if instance_id not in await self._resolve_audience(kind, highlight.audience):
            return False
        if highlight.author_user_id in await self.hidden_for_peer(instance_id):
            return False
        return not await self._guardian_blocks_household(
            highlight.author_user_id, instance_id
        )

    async def _may_send(self, author_user_id: str, instance_id: str) -> bool:
        """The author isn't hidden from *instance_id* (§Privacy) and no
        guardian block covers someone homed there (§CP.F2)."""
        if author_user_id in await self.hidden_for_peer(instance_id):
            return False
        return not await self._guardian_blocks_household(author_user_id, instance_id)

    async def _fan_to_audience(
        self,
        *,
        audience_kind: str,
        audience: tuple[str, ...],
        event_type: FederationEventType,
        payload: dict,
        author_user_id: str,
    ) -> None:
        targets = await self._audience_per_target(audience_kind, audience)
        for instance_id, visible in targets.items():
            if not await self._may_send(author_user_id, instance_id):
                continue
            body = payload
            if "audience" in payload:
                # Per-target copy: never the whole list (see
                # :meth:`_audience_per_target`).
                body = {**payload, "audience": list(visible)}
            await self.send_to_instance(instance_id, event_type, body)
