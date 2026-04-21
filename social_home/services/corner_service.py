"""My Corner — personal dashboard aggregator (§23).

Bundles the slices a signed-in user's landing page needs into a single
payload so the frontend renders with one round-trip instead of 4-6
parallel calls.

No state of its own: every slice delegates to the owning service/repo
(notifications, DMs, calendar, presence, tasks, bazaar). If a slice
fails, the error is logged and the field becomes an empty list/zero —
a single widget failure never fails the whole dashboard.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..domain.calendar import CalendarEvent
from ..domain.post import BazaarStatus
from ..domain.presence import PersonPresence
from ..domain.task import Task

if TYPE_CHECKING:
    from ..repositories.bazaar_repo import AbstractBazaarRepo
    from ..repositories.calendar_repo import AbstractCalendarRepo
    from ..repositories.conversation_repo import AbstractConversationRepo
    from ..repositories.notification_repo import AbstractNotificationRepo
    from ..repositories.space_post_repo import AbstractSpacePostRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.task_repo import AbstractTaskRepo
    from ..repositories.user_repo import AbstractUserRepo
    from ..services.presence_service import PresenceService

log = logging.getLogger(__name__)


#: Cap the per-slice list sizes so a power-user's dashboard stays light.
MAX_EVENTS = 8
MAX_TASKS = 8
UPCOMING_DAYS = 7

#: Followed-spaces widget caps — see the plan. 10 spaces × 5 posts each,
#: trimmed to the newest 15 globally.
MAX_FOLLOWED_SPACES = 10
POSTS_PER_FOLLOWED = 5
MAX_FOLLOWED_POSTS = 15


@dataclass(slots=True, frozen=True)
class BazaarCornerSummary:
    """Seller-side snapshot for the dashboard bazaar widget."""

    active_listings: int
    pending_offers: int  # bids awaiting accept/reject on my listings
    ending_soon: int  # my active listings closing within 24 h


@dataclass(slots=True, frozen=True)
class FollowedSpacePost:
    """One row for the "Spaces you follow" widget.

    A slim projection of :class:`Post` + its parent space's display
    metadata, so the frontend can render a chip + one-line snippet
    without an extra round-trip.
    """

    post_id: str
    space_id: str
    space_name: str
    space_emoji: str | None
    author: str
    type: str
    content: str | None
    created_at: str  # ISO string; already timezone-aware


@dataclass(slots=True, frozen=True)
class CornerBundle:
    """Personal dashboard payload — everything :mod:`DashboardPage` needs."""

    unread_notifications: int
    unread_conversations: int
    upcoming_events: tuple[CalendarEvent, ...]
    presence: tuple[PersonPresence, ...]
    tasks_due_today: tuple[Task, ...]
    bazaar: BazaarCornerSummary
    followed_space_ids: tuple[str, ...]
    followed_spaces_feed: tuple[FollowedSpacePost, ...]


class CornerService:
    """Assemble a :class:`CornerBundle` for the caller."""

    __slots__ = (
        "_notifications",
        "_conversations",
        "_calendar",
        "_presence",
        "_tasks",
        "_bazaar",
        "_users",
        "_spaces",
        "_space_posts",
    )

    def __init__(
        self,
        *,
        notification_repo: "AbstractNotificationRepo",
        conversation_repo: "AbstractConversationRepo",
        calendar_repo: "AbstractCalendarRepo",
        presence_service: "PresenceService",
        task_repo: "AbstractTaskRepo",
        bazaar_repo: "AbstractBazaarRepo",
        user_repo: "AbstractUserRepo",
        space_repo: "AbstractSpaceRepo",
        space_post_repo: "AbstractSpacePostRepo",
    ) -> None:
        self._notifications = notification_repo
        self._conversations = conversation_repo
        self._calendar = calendar_repo
        self._presence = presence_service
        self._tasks = tasks_or_none(task_repo)
        self._bazaar = bazaar_repo
        self._users = user_repo
        self._spaces = space_repo
        self._space_posts = space_post_repo

    async def build(
        self,
        *,
        user_id: str,
        username: str,
    ) -> CornerBundle:
        unread_notifications = await _safe_int(
            "notifications.count_unread",
            self._notifications.count_unread(user_id),
        )
        unread_conversations = await self._count_unread_dms(username)
        upcoming_events = await self._upcoming_events(username)
        presence = await self._presence_list()
        tasks_due = await self._tasks_due_today(user_id)
        bazaar_summary = await self._bazaar_summary(user_id)
        followed_ids, followed_feed = await self._followed_spaces_feed(
            username, user_id
        )
        return CornerBundle(
            unread_notifications=unread_notifications,
            unread_conversations=unread_conversations,
            upcoming_events=upcoming_events,
            presence=presence,
            tasks_due_today=tasks_due,
            bazaar=bazaar_summary,
            followed_space_ids=followed_ids,
            followed_spaces_feed=followed_feed,
        )

    # ── Per-slice helpers ──────────────────────────────────────────────

    async def _count_unread_dms(self, username: str) -> int:
        try:
            convos = await self._conversations.list_for_user(username)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: list DMs failed: %s", exc)
            return 0
        total = 0
        for c in convos:
            try:
                total += await self._conversations.count_unread(
                    c.id,
                    username,
                )
            except Exception:  # pragma: no cover — defensive
                continue
        return total

    async def _upcoming_events(
        self,
        username: str,
    ) -> tuple[CalendarEvent, ...]:
        now = datetime.now(timezone.utc)
        window_end = now + timedelta(days=UPCOMING_DAYS)
        try:
            events = await self._calendar.list_events_for_user_in_range(
                username,
                start=now,
                end=window_end,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: upcoming events failed: %s", exc)
            return ()
        events.sort(key=lambda e: e.start)
        return tuple(events[:MAX_EVENTS])

    async def _presence_list(self) -> tuple[PersonPresence, ...]:
        try:
            rows = await self._presence.list_presence()
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: presence failed: %s", exc)
            return ()
        return tuple(rows)

    async def _tasks_due_today(self, user_id: str) -> tuple[Task, ...]:
        if self._tasks is None:
            return ()
        try:
            tasks = await self._tasks.list_by_assignee(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: tasks failed: %s", exc)
            return ()
        today = date.today()
        relevant = [
            t
            for t in tasks
            if t.status != "done" and t.due_date is not None and t.due_date <= today
        ]
        relevant.sort(key=lambda t: (t.due_date, t.position))
        return tuple(relevant[:MAX_TASKS])

    async def _followed_spaces_feed(
        self,
        username: str,
        user_id: str,
    ) -> tuple[tuple[str, ...], tuple[FollowedSpacePost, ...]]:
        """Merge recent posts from spaces the user has asked to follow.

        Silently filters spaces the user is no longer a member of —
        a stale preference never breaks the dashboard. Capped at
        :data:`MAX_FOLLOWED_POSTS` globally; per-space pull is
        :data:`POSTS_PER_FOLLOWED`.
        """
        try:
            user = await self._users.get(username)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: user lookup failed: %s", exc)
            return (), ()
        if user is None:
            return (), ()

        try:
            prefs = json.loads(user.preferences_json or "{}")
        except json.JSONDecodeError:
            prefs = {}
        raw_ids = prefs.get("followed_space_ids", [])
        if not isinstance(raw_ids, list):
            return (), ()
        # Preserve the user's stated order; cap to MAX_FOLLOWED_SPACES.
        wanted_ids = tuple(
            str(s) for s in raw_ids[:MAX_FOLLOWED_SPACES] if isinstance(s, str)
        )
        if not wanted_ids:
            return (), ()

        rows: list[FollowedSpacePost] = []
        for space_id in wanted_ids:
            try:
                member = await self._spaces.get_member(space_id, user_id)
            except Exception:  # pragma: no cover — defensive
                member = None
            if member is None:
                # Stale preference — user left or was removed.
                continue
            try:
                space = await self._spaces.get(space_id)
            except Exception:  # pragma: no cover — defensive
                space = None
            if space is None:
                continue
            try:
                posts = await self._space_posts.list_feed(
                    space_id,
                    limit=POSTS_PER_FOLLOWED,
                )
            except Exception as exc:  # pragma: no cover — defensive
                log.debug(
                    "corner: list_feed(%s) failed: %s",
                    space_id,
                    exc,
                )
                continue
            for p in posts:
                if p.deleted:
                    continue
                rows.append(
                    FollowedSpacePost(
                        post_id=p.id,
                        space_id=space_id,
                        space_name=space.name,
                        space_emoji=space.emoji,
                        author=p.author,
                        type=p.type.value if hasattr(p.type, "value") else str(p.type),
                        content=p.content,
                        created_at=(
                            p.created_at.isoformat()
                            if hasattr(p.created_at, "isoformat")
                            else str(p.created_at)
                        ),
                    )
                )

        # Global newest-first sort + cap.
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return wanted_ids, tuple(rows[:MAX_FOLLOWED_POSTS])

    async def _bazaar_summary(self, user_id: str) -> BazaarCornerSummary:
        try:
            mine = await self._bazaar.list_by_seller(user_id)
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("corner: bazaar list failed: %s", exc)
            return BazaarCornerSummary(0, 0, 0)

        active = [lst for lst in mine if lst.status is BazaarStatus.ACTIVE]
        ending_soon_cutoff = datetime.now(timezone.utc) + timedelta(hours=24)
        ending_soon = 0
        pending_offers = 0
        for lst in active:
            try:
                end = datetime.fromisoformat(
                    lst.end_time.replace("Z", "+00:00"),
                )
                if end.tzinfo is None:
                    end = end.replace(tzinfo=timezone.utc)
                if end <= ending_soon_cutoff:
                    ending_soon += 1
            except ValueError, AttributeError:
                pass
            try:
                bids = await self._bazaar.list_bids(lst.post_id)
            except Exception:  # pragma: no cover — defensive
                bids = []
            pending_offers += sum(
                1 for b in bids if not b.accepted and not b.rejected and not b.withdrawn
            )
        return BazaarCornerSummary(
            active_listings=len(active),
            pending_offers=pending_offers,
            ending_soon=ending_soon,
        )


async def _safe_int(label: str, coro) -> int:
    try:
        return int(await coro)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("corner: %s failed: %s", label, exc)
        return 0


def tasks_or_none(repo):
    """Allow ``CornerService`` to be constructed before the task repo is
    wired (e.g. in unit tests). Returns the repo as-is in production."""
    return repo
