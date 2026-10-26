"""Momentum service — household-broadcast posts (§Momentum).

Orchestrates :class:`AbstractMomentRepo`, :class:`AbstractUserRepo`,
and :class:`PreferencesService` to enforce the spec rules:

* ≤ 1 000 chars text, optional image or ≤ 15-second video.
* 1 top-level moment per author per 15 minutes (replies + reactions
  exempt — otherwise back-and-forth threads would die on the timer).
* Replies are themselves moments; ``parent_moment_id`` links them.
* The author chooses how long followers keep their moments (1–30 days,
  7 by default — ``moments.follower_retention_days``, decision 6d); that
  sets the signed ``expires_at``. The visibility query collapses it to
  24 h for non-followers.

The service publishes :class:`MomentCreated` / :class:`MomentDeleted`
/ :class:`MomentReactionChanged` on the bus so federation outbound +
realtime push pick them up.

**Saves (decision 6b).** A person keeps someone else's moment past its
expiry by saving it — until its author deletes it. Saving is offered only
where that delete can still reach the copy (:meth:`MomentService.can_save`):
a housemate's moment (a local delete), a mesh moment whose origin household
is a paired v_66+ peer (it keeps a delete queued 30 days and sends it to
every paired household), or a GFS-delivered
moment whose GFS proved ``moment_delete_log`` (it keeps the signed delete
certificate 30 days). Your own moments are already archived (decision 6a)
and are never "saved". Saves are local: nothing goes on the wire.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from ..domain.events import (
    MomentCreated,
    MomentDeleted,
    MomentReactionChanged,
)
from ..domain.federation import InstanceSource, PairingStatus
from ..domain.federation_capabilities import FederationCapability
from ..domain.moment import (
    MOMENT_MAX_CONTENT_LEN,
    MOMENT_MAX_VIDEO_MS,
    REACTION_REACH_AUTHOR,
    Moment,
    MomentOwnSaveError,
    MomentReactionInvalidError,
    MomentReactionTotals,
    MomentSaveUnavailableError,
    SavedMoment,
    is_valid_moment_reaction,
    merged_reaction_view,
)
from ..federation.owner_bound_id import MOMENT_KIND, mint_owner_bound_id
from ..media.cleanup import unlink_unreferenced
from ..repositories.media_reference_repo import AbstractMediaReferenceRepo
from .inbound_media_store import optional_local_media_ref
from .user_preferences import parse_moment_preferences

if TYPE_CHECKING:
    import pathlib

    from ..infrastructure.event_bus import EventBus
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.moment_repo import AbstractMomentRepo
    from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)


#: Minimum interval between two top-level moments by the same author.
#: Replies and reactions are exempt — see ``create_moment``.
MOMENT_RATE_WINDOW = timedelta(minutes=15)


#: The one message an unknown moment id and a moment the caller may not
#: read both answer with — the 404 must not tell them apart.
NOT_FOUND_MESSAGE = "moment not found"


def _has_expired(moment: Moment) -> bool:
    """``True`` once the moment's absolute ``expires_at`` has passed."""
    try:
        expires = datetime.fromisoformat(moment.expires_at)
    except TypeError, ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def _expiry_key(value: str) -> datetime:
    """Sort key for an ISO ``expires_at`` (naive = UTC; junk sorts last so
    ``min`` keeps the other)."""
    try:
        when = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return datetime.max.replace(tzinfo=UTC)
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when


class MomentNotFoundError(KeyError):
    """Raised when a moment id can't be found locally — or the caller may
    not read it (the same error, word for word)."""

    def __init__(self, *_: object) -> None:
        super().__init__(NOT_FOUND_MESSAGE)

    def __str__(self) -> str:
        return NOT_FOUND_MESSAGE


class GfsDeleteLog(Protocol):
    """``GfsConnectionService.moment_delete_log_supported`` — whether a GFS
    keeps public moments' delete certificates (``moment_delete_log``)."""

    async def moment_delete_log_supported(self, gfs_id: str) -> bool: ...


class ReactionReach(Protocol):
    """``MomentReactionRouter.reach_for`` — whether a reaction to each
    moment reaches its author's household (v_67)."""

    async def reach_for(self, moments: Iterable[Moment]) -> dict[str, str]: ...


class MomentRateLimitError(Exception):
    """Raised when an author tries to post a top-level moment within
    :data:`MOMENT_RATE_WINDOW` of their last one. Mapped to HTTP 429
    in :class:`BaseView._iter`, with :attr:`next_allowed_at` (UTC ISO
    8601) so the composer can say when the next moment can go.
    """

    def __init__(self, message: str, *, next_allowed_at: str) -> None:
        super().__init__(message)
        self.next_allowed_at = next_allowed_at


class MomentService:
    """Create + react + delete moments and drive their lifecycle."""

    __slots__ = (
        "_bus",
        "_federation_repo",
        "_gfs_delete_log",
        "_media_dir",
        "_media_refs",
        "_moments",
        "_own_instance_id",
        "_reaction_reach",
        "_users",
    )

    def __init__(
        self,
        moment_repo: AbstractMomentRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
        *,
        own_instance_id: str = "",
        media_dir: pathlib.Path | None = None,
        media_refs: AbstractMediaReferenceRepo | None = None,
        federation_repo: AbstractFederationRepo | None = None,
        gfs_delete_log: GfsDeleteLog | None = None,
    ) -> None:
        self._moments = moment_repo
        self._users = user_repo
        self._bus = bus
        self._own_instance_id = own_instance_id
        # When both are set, the backing media file is removed on delete /
        # expiry once no other row references it.
        self._media_dir = media_dir
        self._media_refs = media_refs
        # The save gates (decision 6b). Unset = fail closed: no mesh or
        # GFS copy is savable.
        self._federation_repo = federation_repo
        self._gfs_delete_log = gfs_delete_log
        #: v_67 — where a reaction reaches (``MomentReactionRouter``).
        self._reaction_reach: ReactionReach | None = None

    def attach_reaction_reach(self, reach: ReactionReach) -> None:
        """Wire what decides whether a reaction reaches a moment's author
        (``reactions_reach`` on every API item)."""
        self._reaction_reach = reach

    def attach_instance_id(self, own_instance_id: str) -> None:
        """Late binding for the instance id (set after federation
        identity comes online during app startup)."""
        self._own_instance_id = own_instance_id

    # ── Create ─────────────────────────────────────────────────────────

    async def create_moment(
        self,
        *,
        author_user_id: str,
        content: str,
        media_url: str | None = None,
        media_type: str | None = None,
        duration_ms: int | None = None,
        parent_moment_id: str | None = None,
        is_public: bool = False,
    ) -> Moment:
        content = (content or "").strip()
        # Media is optional; when present it must be a local upload — a
        # remote URL would leak every viewer's IP, and receivers drop it.
        media_url = optional_local_media_ref(media_url)
        if len(content) > MOMENT_MAX_CONTENT_LEN:
            raise ValueError(
                f"Moment text exceeds {MOMENT_MAX_CONTENT_LEN} characters."
            )
        if not content and media_url is None:
            raise ValueError("A moment needs text or media — both can't be empty.")
        if media_type is not None and media_type not in ("image", "video"):
            raise ValueError(f"Unsupported media_type: {media_type!r}")
        if media_type == "video":
            if duration_ms is None or duration_ms <= 0:
                raise ValueError("Video moments must include duration_ms.")
            if duration_ms > MOMENT_MAX_VIDEO_MS:
                raise ValueError(
                    f"Video moments cap at {MOMENT_MAX_VIDEO_MS // 1000} seconds."
                )
        if media_type != "video":
            duration_ms = None

        parent_author_user_id: str | None = None
        root: Moment | None = None
        if parent_moment_id is not None:
            # Replying needs reading: a parent the author may not read is
            # the unknown-id 404, never a way to learn it exists.
            parent = await self._readable(parent_moment_id, author_user_id)
            if parent is None:
                raise MomentNotFoundError(parent_moment_id)
            # Only its author can still read a moment past its expiry (it
            # stays in their archive, decision 6a); the households a reply
            # would go to no longer hold it, so replies close with it.
            if _has_expired(parent):
                raise ValueError(
                    "This moment has ended and is only in your archive now; "
                    "replies are closed."
                )
            # Threads stay flat: a reply to a reply attaches to the
            # original root so list_replies(root) returns the whole
            # conversation in a single query.
            root = parent
            if parent.parent_moment_id is not None:
                root = await self._moments.get(parent.parent_moment_id)
                parent_moment_id = parent.parent_moment_id
                parent_author_user_id = (
                    root.author_user_id if root is not None else parent.author_user_id
                )
            else:
                parent_author_user_id = parent.author_user_id
            # A reply never goes further than the moment it answers: a
            # reply to a moment that isn't public stays in the household
            # whatever the composer asked (fail closed — a stale client
            # that pre-ticks "share publicly" must not leak the thread).
            if root is None or not root.is_public:
                is_public = False

        # Rate-limit only top-level moments (replies are exempt).
        if parent_moment_id is None:
            next_allowed = await self.next_allowed_at(author_user_id)
            if next_allowed is not None:
                raise MomentRateLimitError(
                    "You can post one moment every 15 minutes. "
                    "Replies and reactions don't count.",
                    next_allowed_at=next_allowed,
                )

        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(days=await self._follower_retention_days(author_user_id))
        ).isoformat()
        if parent_moment_id is not None and root is not None:
            # A reply never outlives its thread: once the root is gone from
            # the households that hold it, the reply would stand alone.
            expires_at = min(expires_at, root.expires_at, key=_expiry_key)
        moment = Moment(
            # Owner-bound (v_36): no other household can announce this id
            # first — as a moment or a delete — for its own user.
            id=mint_owner_bound_id(
                MOMENT_KIND, space_id="", owner_user_id=author_user_id
            ),
            author_user_id=author_user_id,
            content=content,
            media_url=media_url,
            media_type=media_type,
            duration_ms=duration_ms,
            parent_moment_id=parent_moment_id,
            origin_instance_id=self._own_instance_id,
            created_at=now.isoformat(),
            expires_at=expires_at,
            is_public=is_public,
            received_via="self",
        )
        await self._moments.save(moment)
        await self._bus.publish(
            MomentCreated(
                moment_id=moment.id,
                author_user_id=moment.author_user_id,
                content=moment.content,
                media_url=moment.media_url,
                media_type=moment.media_type,
                duration_ms=moment.duration_ms,
                parent_moment_id=moment.parent_moment_id,
                parent_author_user_id=parent_author_user_id,
                origin_instance_id=moment.origin_instance_id,
                expires_at=moment.expires_at,
            )
        )
        return moment

    # ── Delete ─────────────────────────────────────────────────────────

    async def delete_moment(
        self,
        moment_id: str,
        *,
        actor_user_id: str,
        actor_is_admin: bool = False,
    ) -> None:
        # An admin moderates every moment held here; anyone else must be
        # able to read it first (else the unknown-id 404), then be its author.
        moment = (
            await self._require(moment_id)
            if actor_is_admin
            else await self.get_for_viewer(moment_id, actor_user_id)
        )
        if moment.author_user_id != actor_user_id and not actor_is_admin:
            raise PermissionError("Only the author or an admin can delete this moment.")
        await self._moments.delete(moment_id)
        await unlink_unreferenced(self._media_dir, self._media_refs, [moment.media_url])
        await self._bus.publish(
            MomentDeleted(
                moment_id=moment_id,
                author_user_id=moment.author_user_id,
                origin_instance_id=moment.origin_instance_id,
                is_public=moment.is_public,
            )
        )

    # ── Reactions ──────────────────────────────────────────────────────

    async def react(
        self,
        moment_id: str,
        *,
        reactor_user_id: str,
        emoji: str,
    ) -> None:
        if not is_valid_moment_reaction(emoji):
            # One emoji of ≤ 32 bytes — the same bound every inbound path
            # (and a routed reaction's seal, v_67) holds a reaction to.
            raise MomentReactionInvalidError()
        moment = await self.get_for_viewer(moment_id, reactor_user_id)
        await self._moments.set_reaction(moment_id, reactor_user_id, emoji)
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=moment.author_user_id,
                emoji=emoji,
            )
        )

    async def clear_reaction(
        self,
        moment_id: str,
        *,
        reactor_user_id: str,
    ) -> None:
        moment = await self.get_for_viewer(moment_id, reactor_user_id)
        await self._moments.clear_reaction(moment_id, reactor_user_id)
        await self._bus.publish(
            MomentReactionChanged(
                moment_id=moment_id,
                reactor_user_id=reactor_user_id,
                author_user_id=moment.author_user_id,
                emoji=None,
            )
        )

    # ── Reads ──────────────────────────────────────────────────────────

    async def next_allowed_at(self, author_user_id: str) -> str | None:
        """When the author may post their next top-level moment (UTC ISO
        8601), or ``None`` when they may post now (decision 13: one per
        :data:`MOMENT_RATE_WINDOW`; replies and reactions are exempt)."""
        since = datetime.now(UTC) - MOMENT_RATE_WINDOW
        last = await self._moments.latest_top_level_at(
            author_user_id, since_iso=since.isoformat()
        )
        if last is None:
            return None
        nxt = datetime.fromisoformat(last) + MOMENT_RATE_WINDOW
        if nxt.tzinfo is None:
            nxt = nxt.replace(tzinfo=UTC)
        return nxt.isoformat() if nxt > datetime.now(UTC) else None

    async def get_moment(self, moment_id: str) -> Moment:
        return await self._require(moment_id)

    async def get_for_viewer(self, moment_id: str, viewer_user_id: str) -> Moment:
        """The moment, iff *viewer_user_id* may read it — the inbox rule
        (:meth:`AbstractMomentRepo.get_readable`: no personal or guardian
        block between them, the 24 h / follower window, the viewer's
        ``max_hops``), or they wrote it. An unguessable id is not
        authorization: anything else raises :class:`MomentNotFoundError`,
        exactly like an unknown id."""
        moment = await self._readable(moment_id, viewer_user_id)
        if moment is None:
            raise MomentNotFoundError(moment_id)
        return moment

    async def _readable(self, moment_id: str, viewer_user_id: str) -> Moment | None:
        return await self._moments.get_readable(
            moment_id, viewer_user_id, max_hops=await self._max_hops(viewer_user_id)
        )

    async def _follower_retention_days(self, author_user_id: str) -> int:
        """How long the author's followers keep a new moment (decision 6d)."""
        author = await self._users.get_by_user_id(author_user_id)
        prefs = parse_moment_preferences(
            author.preferences_json if author is not None else None
        )
        return prefs.follower_retention_days

    async def _max_hops(self, viewer_user_id: str) -> int:
        """The viewer's ``moments.max_hops`` preference (§Momentum-relay-policy)."""
        viewer = await self._users.get_by_user_id(viewer_user_id)
        prefs = parse_moment_preferences(
            viewer.preferences_json if viewer is not None else None
        )
        return prefs.max_hops

    async def list_inbox(
        self,
        *,
        viewer_user_id: str,
        before: str | None = None,
        limit: int = 50,
        tag: str | None = None,
        top_level: bool = False,
    ) -> list[Moment]:
        """The moments *viewer_user_id* sees, newest first; ``top_level``
        leaves replies out (each thread once, under its root — B13)."""
        # Per-user max-hops visibility (§Momentum-relay-policy):
        # parse the viewer's preferences once and forward the cap to
        # the repo so the inbox query hides moments that arrived at
        # a higher hop than the user wants to see.
        return await self._moments.list_visible_to(
            viewer_user_id,
            before=before,
            limit=limit,
            tag=tag,
            max_hops=await self._max_hops(viewer_user_id),
            top_level=top_level,
        )

    async def latest_replies(
        self, moment_ids: list[str], *, viewer_user_id: str
    ) -> dict[str, Moment]:
        """Each root's newest reply *viewer_user_id* may read — the one a
        list card nests under its root (same rule as :meth:`list_replies`)."""
        return await self._moments.latest_replies(
            moment_ids,
            viewer_user_id,
            max_hops=await self._max_hops(viewer_user_id),
        )

    async def list_own_archive(
        self,
        *,
        author_user_id: str,
        before: str | None = None,
        limit: int = 50,
    ) -> list[Moment]:
        """The caller's own archive (decision 6a): every moment and reply
        they wrote here and haven't deleted, live or past expiry, newest
        first, ``before`` cursor paging."""
        return await self._moments.list_own_archive(
            author_user_id, before=before, limit=limit
        )

    async def list_replies(
        self, moment_id: str, *, viewer_user_id: str
    ) -> list[Moment]:
        """The replies *viewer_user_id* may read — the inbox rule, so a
        thread never shows a reply a block (personal or guardian) hides."""
        return await self._moments.list_replies(
            moment_id,
            viewer_user_id=viewer_user_id,
            max_hops=await self._max_hops(viewer_user_id),
        )

    async def count_engagement(
        self, moment_ids: list[str], *, viewer_user_id: str
    ) -> dict[str, dict[str, int]]:
        """Reaction + reply counts per moment; ``reply_count`` counts only
        the replies :meth:`list_replies` would show *viewer_user_id*."""
        return await self._moments.count_engagement_for(
            moment_ids,
            viewer_user_id=viewer_user_id,
            max_hops=await self._max_hops(viewer_user_id),
        )

    async def list_reactions(self, moment_id: str):
        return await self._moments.list_reactions(moment_id)

    async def list_top_hashtags(
        self,
        *,
        viewer_user_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """Trending tags inside the viewer's visibility window.

        The repo aggregates with the same block + retention filter as
        :meth:`list_inbox`, so a blocked author or an out-of-window
        moment doesn't pollute the chip row.
        """
        rows = await self._moments.list_top_hashtags(
            viewer_user_id,
            limit=limit,
        )
        return [{"tag": tag, "count": n} for tag, n in rows]

    # ── Saves (decision 6b) ────────────────────────────────────────────

    async def save_moment(self, moment_id: str, *, user_id: str) -> None:
        """*user_id* keeps *moment_id* past its expiry until its author
        deletes it. Idempotent. The caller must be able to read it (else
        the unknown-id 404); never their own (:class:`MomentOwnSaveError`);
        only where the author's delete can reach the copy
        (:class:`MomentSaveUnavailableError`, see :meth:`can_save`)."""
        moment = await self.get_for_viewer(moment_id, user_id)
        if moment.author_user_id == user_id:
            raise MomentOwnSaveError()
        if await self._moments.saved_ids(user_id, [moment_id]):
            return
        if not await self.can_save(moment, user_id):
            raise MomentSaveUnavailableError()
        if not await self._moments.add_save(user_id, moment_id):
            # Deleted between the read and the write.
            raise MomentNotFoundError(moment_id)

    async def unsave_moment(self, moment_id: str, *, user_id: str) -> None:
        """Forget *user_id*'s save of *moment_id*. Needs no read (a save of
        a moment now hidden by a block can still be removed) and is a no-op
        for an id they never saved — it says nothing about the moment."""
        await self._moments.remove_save(user_id, moment_id)

    async def list_saved(
        self,
        *,
        user_id: str,
        before: str | None = None,
        limit: int = 50,
    ) -> list[SavedMoment]:
        """*user_id*'s saved moments they may still read, newest save first,
        ``before`` = the ``saved_at`` cursor of the previous page."""
        return await self._moments.list_saved(
            user_id,
            before=before,
            limit=limit,
            max_hops=await self._max_hops(user_id),
        )

    async def can_save(
        self,
        moment: Moment,
        viewer_user_id: str,
        *,
        _cache: dict[tuple[str, str], bool] | None = None,
    ) -> bool:
        """Whether *viewer_user_id* may save *moment*: offered only where
        its author's delete can still reach this copy.

        * written here (``received_via='self'``): a housemate's yes (a local
          delete tombstones it); the viewer's own no (already archived);
        * from a household (``'household'``): only when its origin is a
          household we are paired with (a social peer — it sends its
          moments' deletes to us directly) on v_66+
          (``MIN_FOR_LATE_MOMENT_DELETES`` — the ``peer_supports`` rule: it
          keeps the delete queued 30 days). A friend-of-friend origin with
          no pairing here, or one we only share a space with, is not
          offered: its delete reaches us only through relays we can't
          vouch for;
        * through a GFS (``'gfs'``): only when that GFS proved
          ``moment_delete_log`` (it keeps the signed delete certificate 30
          days for followers that reconnect).
        """
        if moment.deleted_at is not None or moment.author_user_id == viewer_user_id:
            return False
        if moment.received_via == "self":
            return True
        cache = _cache if _cache is not None else {}
        if moment.received_via == "household":
            key = ("peer", moment.origin_instance_id)
            if key not in cache:
                cache[key] = await self._origin_keeps_deletes(moment.origin_instance_id)
            return cache[key]
        if moment.received_via == "gfs" and moment.received_via_gfs_id:
            key = ("gfs", moment.received_via_gfs_id)
            if key not in cache:
                cache[key] = self._gfs_delete_log is not None and (
                    await self._gfs_delete_log.moment_delete_log_supported(
                        moment.received_via_gfs_id
                    )
                )
            return cache[key]
        return False

    async def _origin_keeps_deletes(self, instance_id: str) -> bool:
        """A paired (social) household on v_66+: it sends us its moments'
        deletes and keeps them queued 30 days."""
        if self._federation_repo is None or not instance_id:
            return False
        peer = await self._federation_repo.get_instance(instance_id)
        return (
            peer is not None
            and peer.status is PairingStatus.CONFIRMED
            and peer.source is not InstanceSource.SPACE_SESSION
            and peer.proto_version >= FederationCapability.MIN_FOR_LATE_MOMENT_DELETES
        )

    async def save_flags(
        self, moments: list[Moment], *, viewer_user_id: str
    ) -> dict[str, tuple[bool, bool]]:
        """``{moment_id: (saved_by_me, can_save)}`` for a page of moments —
        one saves query, one gate lookup per origin household / GFS. For a
        moment already saved, ``can_save`` says whether saving would be
        offered now; the SPA shows "Saved" either way, so a save can always
        be removed."""
        saved = await self._moments.saved_ids(viewer_user_id, [m.id for m in moments])
        cache: dict[tuple[str, str], bool] = {}
        out: dict[str, tuple[bool, bool]] = {}
        for m in moments:
            out[m.id] = (
                m.id in saved,
                await self.can_save(m, viewer_user_id, _cache=cache),
            )
        return out

    async def reaction_totals(
        self,
        moments: list[Moment],
        *,
        viewer_user_id: str,
        reach: dict[str, str],
    ) -> dict[str, MomentReactionTotals]:
        """What the API shows of each moment's reactions (v_69).

        A moment from another household whose reactions reach its author
        (``reach`` ``author``) shows the totals its author's household last
        sent, when it sent any — exactly, never with the viewer's own
        reaction added on top (:func:`~socialhome.domain.moment
        .merged_reaction_view`: never over-counted, at most one checkpoint
        stale). Everything else — the viewer's own
        household's moments (all reactions are here), a moment whose
        reactions stay here, or no totals yet — shows the reactions held
        here. Always the viewer's own emoji, from the rows here."""
        ids = [m.id for m in moments]
        if not ids:
            return {}
        local = await self._moments.reaction_tallies(ids)
        mine = await self._moments.my_reactions(ids, viewer_user_id)
        summaries = await self._moments.get_reaction_summaries(
            [
                m.id
                for m in moments
                if m.received_via != "self"
                and reach.get(m.id, REACTION_REACH_AUTHOR) == REACTION_REACH_AUTHOR
            ]
        )
        out: dict[str, MomentReactionTotals] = {}
        for m in moments:
            own = mine.get(m.id)
            counts, total, rev = merged_reaction_view(
                summary=summaries.get(m.id),
                local=local.get(m.id, {}),
            )
            out[m.id] = MomentReactionTotals(
                counts=counts,
                total=total,
                totals_rev=rev,
                my_reaction=own[0] if own is not None else None,
            )
        return out

    async def reaction_reach(self, moments: list[Moment]) -> dict[str, str]:
        """``{moment_id: "author" | "household"}`` — whether the caller's
        reaction reaches the author's household (v_67) or stays here. Every
        moment reads ``author`` when nothing is wired (unit stacks)."""
        if self._reaction_reach is None:
            return {m.id: REACTION_REACH_AUTHOR for m in moments}
        return await self._reaction_reach.reach_for(moments)

    # ── Retention scheduler hook ───────────────────────────────────────

    async def expire_due(self) -> int:
        """Drop moments past their ``expires_at``. Reactions cascade."""
        # Collect the backing media URLs before the prune so we can drop
        # the files too (the row delete alone would orphan them on disk).
        urls = (
            await self._moments.list_expired_media_urls()
            if self._media_dir is not None
            else []
        )
        n = await self._moments.prune_expired()
        if n:
            log.info("momentum: pruned %d expired moments", n)
        await unlink_unreferenced(self._media_dir, self._media_refs, urls)
        return n

    # ── Internal helpers ───────────────────────────────────────────────

    async def _require(self, moment_id: str) -> Moment:
        m = await self._moments.get(moment_id)
        if m is None:
            raise MomentNotFoundError(moment_id)
        return m
