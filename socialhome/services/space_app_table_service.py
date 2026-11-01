"""SpaceTableService — game tables opened in a space (group play §3, v_77).

A member with a writing seat opens a table of an app on the space's games
list ("Games allowed here", ``SpaceFeatures.allowed_apps``) and any member
with a writing seat may take a seat; the host person presses Start (a
two-player table starts when the second seat fills). The table itself is
an ordinary group session run by :class:`AppGroupService` — its *hub* is
the household of the person who opened it. This service adds the space:

* **The card** (``SPACE_APP_TABLE``): what the "Open games" strip shows of
  a table — the app, who opened it, the note, the players range, how many
  sit, open / full / playing / closed, the expiry. Never the roster. The
  hub sends it to the member households holding a writer seat only
  (``broadcast_to_space_members`` narrowed with ``only_instances``) — on
  open, on start / close, when it turns full or not, and with a new seat
  count at most once per :data:`CARD_COUNT_EVERY_SECONDS`. A household
  keeps the card of a table it has nobody at
  (:func:`~socialhome.domain.space_app_table.card_table`).
* **The space's transport** (``SPACE_APP_SESSION``): every ``group_*``
  verb and group message of a space table goes between the hub and ONE
  player's household as ``broadcast_to_space_members`` narrowed to that
  household — direct, ``SPACE_ROUTED`` (sealed end to end) across a
  relay, or the link-joined relay seat. Households that aren't paired play
  together: being in the space and pressing Join is the consent (D7).
* **The space's rules**: only writer seats see, open or join tables
  (followers never); never a global space, never an archived one; the app
  must be on the list (an app taken off it closes its waiting tables —
  games being played finish, and take nobody more: D10); the owner, admins
  and moderators may close any table (``closed_by_moderator``); the opener
  may "let everyone know" (one in-app row per member, ≤ 1 per person per
  space per day, D5).

Receiver checks (every household): a card only from a household whose
named host holds a live writer seat there, for an app on this household's
copy of the list, at most :data:`SPACE_TABLES_PER_PERSON_DAY` new tables
per person per space per day (counted by this household's clock), at most
:data:`GROUP_MIRRORS_PER_HUB_MAX` open tables per hub; a newer ``rev``
only. A join at the hub only from a live writer seat on the sending
household, not banned, for an app still on the list. A moderator's close
only from a content-authority seat
(:meth:`~socialhome.federation.space_authorship.SpaceAuthorship.moderates_as`).

**Not in the §25.6 sync stream.** A household that joins the space later
hears of a table with its next card (any start / close / full change, or
the seat count) — tables live days, not years, and the outbox already
covers a household that was offline.

Cost per card: one sealed ≈ 1 KB envelope per writer household. A 200-
household local community at the caps (50 people × 3 tables a day, ~4 card
updates each): 150 × 4 × 200 × 1 KB ≈ 120 MB a day across the space, ≈ 600
KB per household. Every session envelope goes to ONE household.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from ..crypto import derive_instance_id
from ..domain.app_group import (
    END_APP_REMOVED,
    END_CLOSED_BY_MODERATOR,
    GROUP_ENDED,
    GROUP_LOBBY,
    GROUP_LOBBY_DEFAULT_DAYS,
    GROUP_MIRRORS_PER_HUB_MAX,
    GROUP_NAME_MAX,
    GROUP_REF_MAX,
    SEAT_INVITED,
    AppGroupClosedError,
    AppGroupFullError,
    AppGroupNotFoundError,
    AppGroupNotHostError,
    AppGroupSizeError,
    AppGroupsNotSupportedError,
    GroupSeat,
    GroupSession,
    GroupWireError,
    TableCard,
)
from ..domain.apps import PERMISSION_FEDERATION, PERMISSION_PEOPLE, SESSION_INVITED
from ..domain.events import (
    AppTableAnnounced,
    AppTableClosed,
    RemoteSpaceMemberBanned,
    SpaceConfigChanged,
    SpaceFeaturesApplied,
    SpaceMemberLeft,
    SpaceRemoteSeatEnded,
    SpaceRemoteSeatLive,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.space import (
    CONTENT_AUTHORITY_ROLES,
    WRITER_ROLES,
    Space,
    SpaceMember,
    SpaceType,
)
from ..domain.space_app_table import (
    CARD_CLOSED,
    CARD_COUNT_EVERY_SECONDS,
    CARD_FULL,
    CARD_OPEN,
    SPACE_TABLE_ANNOUNCES_PER_DAY,
    SPACE_TABLE_MAX_PLAYERS,
    SPACE_TABLES_PER_PERSON_DAY,
    SPACE_TABLES_SHOWN,
    SpaceAppNotAllowedError,
    SpaceHostTooOldError,
    SpaceTableLimitError,
    card_state,
    card_table,
    card_wire,
    parse_card,
)
from ..federation.space_scope import resolve_space_id
from .bus_publisher import BusPublisherMixin
from .space_service import SpaceService
from .space_writer_households import SpaceWriterHouseholds, holds_member_seat

if TYPE_CHECKING:
    from ..domain.apps import InstalledApp
    from ..domain.user import User
    from ..federation.space_authorship import SpaceAuthorship
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_group_repo import AbstractAppGroupRepo
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .app_federation_service import AppFederationService
    from .app_group_service import AppGroupService

log = logging.getLogger(__name__)

#: The WebSocket frame a member's SPA gets when a card of a space it is in
#: changes (the "Open games" strip, the Games section).
WS_FRAME_TYPE = "space.app_table"

_WRITER_VALUES: frozenset[str] = frozenset(r.value for r in WRITER_ROLES)
_CONTENT_VALUES: frozenset[str] = frozenset(r.value for r in CONTENT_AUTHORITY_ROLES)
#: What a space session carries inside: an ``APP_SESSION`` group verb or a
#: group ``APP_MESSAGE``.
_KIND_SESSION = "session"
_KIND_MESSAGE = "message"
#: Tables whose "closed for you" row went out (bounded).
_TOLD_MAX = 512


def _now() -> datetime:
    return datetime.now(UTC)


def _line(raw: object, limit: int) -> str | None:
    """A one-line string of at most *limit* characters, else ``None``."""
    if (
        isinstance(raw, str)
        and raw.strip()
        and len(raw) <= limit
        and not any(ch in raw for ch in "\r\n\x00")
    ):
        return raw
    return None


class SpaceTableService(BusPublisherMixin):
    """Game tables in a space: cards, the space's transport and its rules.
    See the module docstring."""

    __slots__ = (
        "_app_fed",
        "_app_repo",
        "_authorship",
        "_bus",
        "_card_due",
        "_card_every",
        "_card_last",
        "_federation",
        "_groups",
        "_locks",
        "_repo",
        "_seats",
        "_spaces",
        "_stop",
        "_tasks",
        "_told",
        "_user_repo",
        "_writers",
        "_ws",
    )

    def __init__(
        self,
        *,
        groups: AppGroupService,
        group_repo: AbstractAppGroupRepo,
        app_repo: AbstractAppRepo,
        app_federation: AppFederationService,
        space_repo: AbstractSpaceRepo,
        remote_member_repo: AbstractSpaceRemoteMemberRepo,
        user_repo: AbstractUserRepo,
        federation: Any,
        authorship: SpaceAuthorship,
        ws: WebSocketManager,
        bus: EventBus | None = None,
        card_every: float = CARD_COUNT_EVERY_SECONDS,
    ) -> None:
        self._groups = groups
        self._repo = group_repo
        self._app_repo = app_repo
        self._app_fed = app_federation
        self._spaces = space_repo
        self._seats = remote_member_repo
        self._user_repo = user_repo
        self._federation = federation
        self._authorship = authorship
        self._ws = ws
        self._bus = bus
        self._card_every = card_every
        self._writers = SpaceWriterHouseholds(
            remote_member_repo, own_instance_id=str(federation.own_instance_id)
        )
        #: Per hub table: the card state and seat count last sent, and when.
        self._card_last: dict[str, tuple[str, int, float]] = {}
        #: Hub tables whose next seat count waits for its turn.
        self._card_due: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        #: One change at a time per table / opener / hub (dropped when idle).
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._told: dict[str, None] = {}

    @property
    def _own(self) -> str:
        return str(self._federation.own_instance_id)

    def attach_to(self, federation_service: Any) -> None:
        """Register the two inbound handlers."""
        federation_service._event_registry.register(
            FederationEventType.SPACE_APP_TABLE, self._on_card
        )
        federation_service._event_registry.register(
            FederationEventType.SPACE_APP_SESSION, self._on_session
        )

    def wire(self, bus: EventBus) -> None:
        """Follow the space's games list and its members leaving."""
        bus.subscribe(SpaceFeaturesApplied, self._on_space_config)
        bus.subscribe(SpaceConfigChanged, self._on_space_config)
        bus.subscribe(SpaceMemberLeft, self._on_member_left)
        bus.subscribe(RemoteSpaceMemberBanned, self._on_space_config)
        # A remote seat removed, left or demoted (a follower is live too).
        bus.subscribe(SpaceRemoteSeatEnded, self._on_space_config)
        bus.subscribe(SpaceRemoteSeatLive, self._on_space_config)

    async def stop(self) -> None:
        """Let the card sends that wait go (none is sent)."""
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ─── Guards ───────────────────────────────────────────────────────

    async def _writer(self, space_id: str, user_id: str) -> tuple[Space, SpaceMember]:
        """The space and the caller's seat — a writer seat (a follower is
        refused, :class:`~socialhome.domain.space.SubscriberReadOnlyError`);
        no seat at all reads as no such space."""
        space = await self._spaces.get(space_id)
        member = await self._spaces.get_member(space_id, user_id)
        if space is None or space.dissolved or member is None:
            raise KeyError(f"space {space_id!r} not found")
        SpaceService.assert_writable_member(member, action="post", space=space)
        if await self._spaces.is_banned(space_id, user_id):
            raise KeyError(f"space {space_id!r} not found")
        return space, member

    @staticmethod
    def _games_here(space: Space, app_id: str | None = None) -> None:
        """A space that may have tables at all — never a global space (its
        members reach each other only anonymously through GFSs) nor an
        archived one — and, with *app_id*, one whose list holds it."""
        if space.space_type is SpaceType.GLOBAL or space.archived:
            raise SpaceAppNotAllowedError()
        if app_id is not None and app_id not in space.features.allowed_apps:
            raise SpaceAppNotAllowedError()

    async def _usable(self, app_id: str, user_id: str) -> InstalledApp:
        """The app here, on, allowed to reach other households and old
        enough for *user_id* — else the app errors (the SPA then offers
        "Ask an admin", the D10 request)."""
        app = await self._app_fed.require_enabled(app_id)
        app.require_permission(PERMISSION_PEOPLE)
        app.require_permission(PERMISSION_FEDERATION)
        await self._app_fed.assert_age_allowed(app_id, user_id, app=app)
        if not app.manifest.tables:
            raise AppGroupsNotSupportedError()
        return app

    async def _local_user(self, user_id: str) -> User:
        user = await self._user_repo.get_by_user_id(user_id)
        if user is None:
            raise AppGroupNotFoundError()
        return user

    async def _space_table(self, space_id: str, session_id: str) -> GroupSession:
        table = await self._repo.get(session_id)
        if table is None or table.space_id != space_id:
            raise AppGroupNotFoundError()
        return table

    def _card_only(self, table: GroupSession) -> bool:
        """A table this household knows only from its card."""
        return table.hub_instance != self._own and table.card is not None

    # ─── Reading ──────────────────────────────────────────────────────

    def card_view(self, table: GroupSession) -> dict:
        """A table as its card shows it — no roster, no usernames."""
        host = table.entry(table.host_instance, table.host_user)
        seated = (
            table.card.seated
            if self._card_only(table) and table.card is not None
            else len(table.seated)
        )
        state = card_state(table)
        if self._card_only(table) and state in (CARD_OPEN, CARD_FULL):
            state = CARD_FULL if seated >= table.max_players else CARD_OPEN
        return {
            "session_id": table.session_id,
            "space_id": table.space_id,
            "app_id": table.app_id,
            "host_name": host.name if host is not None else None,
            "host_is_local": table.host_instance == self._own,
            "note": table.note,
            "min": table.min_players,
            "max": table.max_players,
            "seated": seated,
            "state": state,
            "end_reason": table.end_reason,
            "late_join": table.late_join,
            "created_at": table.created_at,
            "started_at": table.started_at,
            "expires_at": table.expires_at,
        }

    async def list_tables(self, *, space_id: str, actor_user_id: str) -> dict:
        """The space's tables as *actor_user_id* sees them: every table
        they have a place at, and the open ones of apps on the list (the
        newest :data:`SPACE_TABLES_SHOWN`) whose host they don't block or
        aren't blocked by, for apps their age allows. Each with the app's
        state here (``installed`` / ``enabled``) — Join or "Ask an admin"."""
        space, member = await self._writer(space_id, actor_user_id)
        user = await self._local_user(actor_user_id)
        allowed = set(space.features.allowed_apps)
        now = _now().isoformat()
        tables: list[dict] = []
        shown = 0
        if space.space_type is not SpaceType.GLOBAL:
            # Newest first by when this household heard of each (a hub
            # can't jump the strip with a future ``created_at``).
            rows = sorted(
                await self._repo.list_for_space(space_id),
                key=lambda t: (
                    t.card.seen_at if self._card_only(t) and t.card else t.created_at
                ),
                reverse=True,
            )
            for table in rows:
                row = await self._app_repo.get_pending_session(
                    table.app_id, user.user_id, table.session_id
                )
                mine = row is not None and row.is_group
                if not mine and (
                    table.app_id not in allowed
                    or space.archived
                    or table.state == GROUP_ENDED
                    or (table.state == GROUP_LOBBY and table.expires_at < now)
                    or shown >= SPACE_TABLES_SHOWN
                    or await self._host_blocked(user, table)
                ):
                    continue
                app = await self._app_repo.get(table.app_id)
                if app is not None and not await self._age_ok(app, user):
                    continue
                if not mine:
                    shown += 1
                view = self.card_view(table)
                view["app"] = {
                    "installed": app is not None,
                    "enabled": bool(app is not None and app.enabled),
                }
                view["group"] = (
                    await self._groups.view_of(table, user, row)
                    if mine and row is not None
                    else None
                )
                tables.append(view)
        return {
            "space_id": space_id,
            "allowed_apps": list(space.features.allowed_apps),
            "games_possible": space.space_type is not SpaceType.GLOBAL
            and not space.archived,
            "can_edit_games": member.role in _CONTENT_VALUES,
            "host_supports": await self._host_supports(space),
            "tables": tables,
        }

    async def _host_supports(self, space: Space) -> bool:
        """Whether the space's host household keeps a games list (v_77)."""
        host = space.owner_instance_id
        if not host or host == self._own:
            return True
        return bool(
            await self._federation.space_member_supports(
                host, min_version=FederationCapability.MIN_FOR_SPACE_APP_TABLES
            )
        )

    async def _host_blocked(self, user: User, table: GroupSession) -> bool:
        host = table.entry(table.host_instance, table.host_user)
        other = host.user_id if host is not None else None
        if other is None or other == user.user_id:
            return False
        return await self._user_repo.is_blocked(
            user.user_id, other
        ) or await self._user_repo.is_blocked(other, user.user_id)

    async def _age_ok(self, app: InstalledApp, user: User) -> bool:
        return bool(
            await self._app_fed.age_filter_recipients(
                app.app_id, [user.user_id], app=app
            )
        )

    # ─── Opening, joining, closing (a local person) ───────────────────

    async def open_table(
        self,
        *,
        space_id: str,
        actor_user_id: str,
        app_id: str,
        max_players: int | None = None,
        lobby_days: int = GROUP_LOBBY_DEFAULT_DAYS,
        note: str | None = None,
        announce: bool = False,
    ) -> dict:
        """Open a table of *app_id* in the space, the caller in seat 1.

        The app must be on the space's list
        (:class:`SpaceAppNotAllowedError`), here, on and able to play as a
        table; ``max_players`` within its players range (at most 30,
        :class:`AppGroupSizeError`). Caps: the app's table caps
        (:class:`~socialhome.domain.app_group.AppGroupLimitError`) and
        :data:`SPACE_TABLES_PER_PERSON_DAY` a day in this space, of which
        :data:`SPACE_TABLE_ANNOUNCES_PER_DAY` may let everyone know
        (:class:`SpaceTableLimitError`)."""
        space, _member = await self._writer(space_id, actor_user_id)
        self._games_here(space, app_id)
        app = await self._usable(app_id, actor_user_id)
        lo = app.manifest.players_min
        hi = min(app.manifest.players_max, SPACE_TABLE_MAX_PLAYERS)
        seats = hi if max_players is None else max_players
        if not lo <= seats <= hi:
            raise AppGroupSizeError(lo, hi)
        user = await self._local_user(actor_user_id)
        async with self._lock(f"open:{space_id}:{user.username}"):
            day_cut = (_now() - timedelta(days=1)).isoformat()
            today = [
                t
                for t in await self._repo.list_for_space(space_id)
                if t.hub_instance == self._own
                and t.opener_user == user.username
                and t.created_at >= day_cut
            ]
            if len(today) >= SPACE_TABLES_PER_PERSON_DAY:
                raise SpaceTableLimitError()
            if announce and (
                sum(1 for t in today if t.card is not None and t.card.announce)
                >= SPACE_TABLE_ANNOUNCES_PER_DAY
            ):
                raise SpaceTableLimitError(announce=True)
            table = await self._groups.open_space_table(
                app=app,
                host=user,
                space_id=space_id,
                max_players=seats,
                lobby_days=lobby_days,
                note=note,
            )
            if announce:
                # The hub keeps its own "let everyone know" on the card
                # field (its view always reads the roster).
                table = dataclasses.replace(
                    table, card=TableCard(seated=0, announce=True, seen_at="")
                )
                await self._repo.save(table)
        if announce:
            await self._announce_locally(
                table, space, host_display=user.display_name, skip={user.user_id}
            )
        await self.on_table_changed(table)
        row = await self._app_repo.get_pending_session(
            app_id, user.user_id, table.session_id
        )
        view = self.card_view(table)
        view["group"] = await self._groups.view_of(table, user, row) if row else None
        return view

    async def join(self, *, space_id: str, session_id: str, actor_user_id: str) -> dict:
        """Take a seat at a table in the space. The app must still be on
        the list (D10) and usable here — else the app's own errors, which
        the SPA answers with "Ask an admin"."""
        space, _member = await self._writer(space_id, actor_user_id)
        table = await self._space_table(space_id, session_id)
        self._games_here(space, table.app_id)
        if (
            self._card_only(table)
            and table.card is not None
            and table.card.seated >= table.max_players
        ):
            raise AppGroupFullError()
        await self._usable(table.app_id, actor_user_id)
        if table.hub_instance != self._own and not (
            await self._federation.space_member_supports(
                table.hub_instance,
                min_version=FederationCapability.MIN_FOR_SPACE_APP_TABLES,
            )
        ):
            raise SpaceHostTooOldError()
        user = await self._local_user(actor_user_id)
        return await self._groups.join_space_table(table=table, user=user)

    async def close(
        self, *, space_id: str, session_id: str, actor_user_id: str
    ) -> None:
        """Close a table: its host person ("Close this game") or, for any
        table in the space, the owner, an admin or a moderator ("Close
        game" — ``closed_by_moderator``, everyone seated hears why)."""
        _space, member = await self._writer(space_id, actor_user_id)
        table = await self._space_table(space_id, session_id)
        if table.state == GROUP_ENDED:
            raise AppGroupClosedError()
        user = await self._local_user(actor_user_id)
        if table.is_host(self._own, user.username):
            await self._groups.close(
                app_id=table.app_id, session_id=session_id, actor_user_id=actor_user_id
            )
            return
        if member.role not in _CONTENT_VALUES:
            raise AppGroupNotHostError()
        if table.hub_instance == self._own:
            await self._groups.end_space_table(table, END_CLOSED_BY_MODERATOR)
            return
        await self.send(
            space_id,
            table.hub_instance,
            FederationEventType.APP_SESSION,
            {
                "app_id": table.app_id,
                "session_id": session_id,
                "verb": "group_close",
                "from_user": user.username,
                "user_id": user.user_id,
                "by_role": True,
            },
        )

    # ─── The space's transport ────────────────────────────────────────

    async def send(
        self,
        space_id: str,
        to_instance: str,
        event_type: FederationEventType,
        payload: dict,
    ) -> None:
        """One space table's ``APP_SESSION`` verb or group ``APP_MESSAGE``
        to ONE member household, over the space (``SPACE_APP_SESSION``)."""
        kind = (
            _KIND_MESSAGE
            if event_type is FederationEventType.APP_MESSAGE
            else _KIND_SESSION
        )
        result = await self._federation.broadcast_to_space_members(
            space_id,
            FederationEventType.SPACE_APP_SESSION,
            {"space_id": space_id, "kind": kind, "body": payload},
            only_instances={to_instance},
            min_proto_version=FederationCapability.MIN_FOR_SPACE_APP_TABLES,
        )
        if getattr(result, "attempted", 1) == 0:
            log.warning(
                "space_tables: %s for %s not sent — %s is no member household of "
                "%s at v_77",
                payload.get("verb", "message"),
                payload.get("session_id"),
                to_instance,
                space_id,
            )

    # ─── Cards (the hub) ──────────────────────────────────────────────

    async def on_table_changed(self, table: GroupSession) -> None:
        """A space table changed here: the members of this household see
        it at once; the hub sends the card when it says something new
        (state at once, a seat count at most every
        :data:`CARD_COUNT_EVERY_SECONDS`); people from here at a table a
        moderator or the games list closed hear why."""
        if table.space_id is None:
            return
        await self._push_card(table)
        await self._tell_closed(table)
        if table.hub_instance != self._own:
            return
        sid = table.session_id
        state, seated = card_state(table), len(table.seated)
        loop = asyncio.get_running_loop()
        last = self._card_last.get(sid)
        if last is None or last[0] != state:
            await self._send_card(table)
            return
        if last[1] == seated or sid in self._card_due:
            return
        wait = self._card_every - (loop.time() - last[2])
        if wait <= 0:
            await self._send_card(table)
            return
        self._card_due.add(sid)
        task = asyncio.create_task(self._card_later(sid, wait))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _card_later(self, session_id: str, wait: float) -> None:
        try:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return  # stopping: nothing more goes out
            except TimeoutError:
                pass
        finally:
            self._card_due.discard(session_id)
        table = await self._repo.get(session_id)
        last = self._card_last.get(session_id)
        if table is None or table.hub_instance != self._own or last is None:
            return
        if (last[0], last[1]) != (card_state(table), len(table.seated)):
            await self._send_card(table)

    async def _send_card(self, table: GroupSession) -> None:
        space_id = table.space_id
        assert space_id is not None
        state = card_state(table)
        loop = asyncio.get_running_loop()
        if state == CARD_CLOSED:
            self._card_last.pop(table.session_id, None)
        else:
            self._card_last[table.session_id] = (state, len(table.seated), loop.time())
        # The hub's own "let everyone know" rides only the first card it
        # sends — once, for good: ``seen_at`` marks it sent (a restart or a
        # later card never announces again).
        announce = (
            table.card is not None and table.card.announce and not table.card.seen_at
        )
        if announce:
            async with self._lock(table.session_id):
                current = await self._repo.get(table.session_id)
                if current is not None and current.card is not None:
                    await self._repo.save(
                        dataclasses.replace(
                            current,
                            card=dataclasses.replace(
                                current.card, seen_at=_now().isoformat()
                            ),
                        )
                    )
        targets = await self._writers.writer_households(space_id)
        if not targets:
            return
        try:
            await self._federation.broadcast_to_space_members(
                space_id,
                FederationEventType.SPACE_APP_TABLE,
                card_wire(table, space_id, announce=announce),
                only_instances=targets,
                min_proto_version=FederationCapability.MIN_FOR_SPACE_APP_TABLES,
            )
        except Exception:
            log.exception("space_tables: card for %s not sent", table.session_id)

    async def _local_writers(self, space_id: str) -> list[str]:
        local = set(await self._spaces.list_local_member_user_ids(space_id))
        return [
            m.user_id
            for m in await self._spaces.list_members(space_id)
            if m.user_id in local and m.role in _WRITER_VALUES
        ]

    async def _push_card(self, table: GroupSession) -> None:
        """The card to each member here who may see it — as
        :meth:`list_tables` would show it to them (the app on the list,
        their age, no block either way with the host)."""
        assert table.space_id is not None
        space = await self._spaces.get(table.space_id)
        if space is None:
            return
        app = await self._app_repo.get(table.app_id)
        frame = {
            "type": WS_FRAME_TYPE,
            "space_id": table.space_id,
            "table": self.card_view(table),
        }
        listed = table.app_id in space.features.allowed_apps
        for user_id in await self._local_writers(table.space_id):
            user = await self._user_repo.get_by_user_id(user_id)
            if user is None:
                continue
            row = await self._app_repo.get_pending_session(
                table.app_id, user_id, table.session_id
            )
            if row is None or not row.is_group:
                if not listed or await self._host_blocked(user, table):
                    continue
                if app is not None and not await self._age_ok(app, user):
                    continue
            await self._ws.broadcast_to_user(user_id, frame)

    async def _tell_closed(self, table: GroupSession) -> None:
        """Each person from here with a seat hears why a table a moderator
        or the games list closed is over — once."""
        if (
            table.state != GROUP_ENDED
            or table.end_reason not in (END_CLOSED_BY_MODERATOR, END_APP_REMOVED)
            or table.session_id in self._told
            or table.space_id is None
        ):
            return
        self._told[table.session_id] = None
        while len(self._told) > _TOLD_MAX:
            self._told.pop(next(iter(self._told)))
        space = await self._spaces.get(table.space_id)
        name = space.name if space is not None else ""
        for user, row in await self._groups.local_people(table):
            if row.state == SESSION_INVITED:
                continue
            await self._emit(
                AppTableClosed(
                    app_id=table.app_id,
                    session_id=table.session_id,
                    space_id=table.space_id,
                    to_user_id=user.user_id,
                    reason=str(table.end_reason),
                    space_name=name,
                )
            )

    async def _announce_locally(
        self,
        table: GroupSession,
        space: Space,
        *,
        host_display: str,
        host_user_id: str | None = None,
        skip: set[str] | None = None,
    ) -> None:
        """ "Let everyone know": one in-app row for each member with a
        writing seat here (old enough for the app; blocks are the
        notification gate's)."""
        users = [
            u for u in await self._local_writers(space.id) if u not in (skip or ())
        ]
        app = await self._app_repo.get(table.app_id)
        if app is not None:
            users = await self._app_fed.age_filter_recipients(
                table.app_id, users, app=app
            )
        host = table.entry(table.host_instance, table.host_user)
        for user_id in users:
            await self._emit(
                AppTableAnnounced(
                    app_id=table.app_id,
                    session_id=table.session_id,
                    space_id=space.id,
                    to_user_id=user_id,
                    host_display=host_display,
                    space_name=space.name,
                    host_user_id=host_user_id or (host.user_id if host else None),
                )
            )

    # ─── Inbound ──────────────────────────────────────────────────────

    async def _space_for(self, event: FederationEvent) -> Space | None:
        """The space an inbound card / session is for — held here, not
        dissolved, not global, and with a writer seat of this household."""
        space_id = resolve_space_id(event)
        if not space_id:
            return None
        space = await self._spaces.get(space_id)
        if (
            space is None
            or space.dissolved
            or space.space_type is SpaceType.GLOBAL
            or not await holds_member_seat(self._spaces, space_id)
        ):
            return None
        return space

    async def _writer_seat(self, space_id: str, instance: str, user_id: str) -> bool:
        """*user_id* holds a live writer seat on household *instance*, and
        isn't banned."""
        if not user_id or await self._spaces.is_banned(space_id, user_id):
            return False
        seat = await self._seats.get(space_id, instance, user_id)
        return seat is not None and not seat.tombstoned and seat.role in _WRITER_VALUES

    async def _on_card(self, event: FederationEvent) -> None:
        """A table's card from its hub. Dropped (logged) unless the space
        is ours to hear it in, the named host holds a writer seat on the
        sending household, the app is on this household's copy of the
        list, and the caps hold."""
        space = await self._space_for(event)
        sender = event.from_instance
        if space is None:
            log.info("space_tables: card from %s for no space of ours", sender)
            return
        try:
            card = parse_card(event.payload, now=_now())
        except GroupWireError as exc:
            log.info("space_tables: card from %s dropped: %s", sender, exc)
            return
        existing = await self._repo.get(card.table_id)
        if existing is not None:
            if (
                existing.hub_instance != sender
                or existing.space_id != space.id
                or existing.app_id != card.app_id
            ):
                log.warning(
                    "space_tables: card %s from %s refused", card.table_id, sender
                )
                return
            if await self._groups.local_people(existing):
                return  # someone from here sits there: the roster says it
            seen = existing.card
            kept = card_table(
                card,
                space_id=space.id,
                hub_instance=sender,
                seen_at=seen.seen_at if seen is not None else _now().isoformat(),
            )
            assert kept.card is not None
            kept = dataclasses.replace(
                kept,
                opener_user=existing.opener_user,
                updated_at=_now().isoformat(),
                card=dataclasses.replace(
                    kept.card, announce=bool(seen and seen.announce)
                ),
            )
            async with self._lock(card.table_id):
                if await self._repo.save_newer(kept):
                    await self._push_card(kept)
            return
        if (
            card.state == CARD_CLOSED
            or card.app_id not in space.features.allowed_apps
            or space.archived
            or not await self._writer_seat(space.id, sender, card.host_user_id)
        ):
            log.info("space_tables: card %s from %s not taken", card.table_id, sender)
            return
        now = _now()
        day_cut = (now - timedelta(days=1)).isoformat()
        async with self._lock(f"hub:{sender}"):
            if await self._repo.get(card.table_id) is not None:
                return  # another card for it got here first: the next one updates it
            from_host = [
                t
                for t in await self._repo.list_for_space(space.id)
                if t.hub_instance == sender
                and t.opener_user == card.host_user_id
                and t.card is not None
                and t.card.seen_at >= day_cut
            ]
            if len(from_host) >= SPACE_TABLES_PER_PERSON_DAY:
                log.warning(
                    "space_tables: too many tables from %s of %s today",
                    card.host_user,
                    sender,
                )
                return
            if await self._repo.count_open_from(sender) >= GROUP_MIRRORS_PER_HUB_MAX:
                log.warning("space_tables: too many open tables from %s", sender)
                return
            announce = (
                card.announce
                and card.state == CARD_OPEN
                and sum(1 for t in from_host if t.card is not None and t.card.announce)
                < SPACE_TABLE_ANNOUNCES_PER_DAY
            )
            kept = card_table(
                card, space_id=space.id, hub_instance=sender, seen_at=now.isoformat()
            )
            kept = dataclasses.replace(
                kept,
                card=TableCard(
                    seated=card.seated, announce=announce, seen_at=now.isoformat()
                ),
            )
            await self._repo.save(kept)
        if announce:
            await self._announce_locally(
                kept,
                space,
                host_display=card.host_name,
                host_user_id=card.host_user_id,
            )
        await self._push_card(kept)

    async def _on_session(self, event: FederationEvent) -> None:
        """A space table's verb or message over the space. Taken only for a
        table of that space this household knows, from its hub or from a
        household holding a writer seat; a join only for a live writer seat
        while the app is on the list; a moderator's close only from a
        content-authority seat."""
        space = await self._space_for(event)
        sender = event.from_instance
        payload = event.payload if isinstance(event.payload, dict) else {}
        body, kind = payload.get("body"), payload.get("kind")
        if (
            space is None
            or not isinstance(body, dict)
            or kind
            not in (
                _KIND_SESSION,
                _KIND_MESSAGE,
            )
        ):
            log.info("space_tables: session from %s dropped", sender)
            return
        session_id = body.get("session_id")
        table = (
            await self._repo.get(session_id) if isinstance(session_id, str) else None
        )
        if table is None or table.space_id != space.id:
            log.info("space_tables: session for a table not here from %s", sender)
            return
        if sender != table.hub_instance and not any(
            s.role in _WRITER_VALUES
            for s in await self._seats.list_for_instance(
                space.id, sender, include_tombstoned=False
            )
        ):
            log.warning("space_tables: %s holds no writer seat in %s", sender, space.id)
            return
        if (
            table.hub_instance == self._own
            and sender != self._own
            and not await self._actor_still_seated(space, table, sender, body)
        ):
            log.info(
                "space_tables: %s from %s by someone no longer in %s dropped",
                body.get("verb", "message"),
                sender,
                space.id,
            )
            return
        inner = FederationEvent(
            msg_id=event.msg_id,
            event_type=(
                FederationEventType.APP_MESSAGE
                if kind == _KIND_MESSAGE
                else FederationEventType.APP_SESSION
            ),
            from_instance=sender,
            to_instance=event.to_instance,
            timestamp=event.timestamp,
            payload=body,
            space_id=space.id,
        )
        if kind == _KIND_MESSAGE:
            await self._groups.on_space_message(inner)
            return
        verb = body.get("verb")
        hub_here = table.hub_instance == self._own
        if verb == "group_join" and hub_here:
            newcomer = await self._newcomer(space, table, sender, body)
            if newcomer is None:
                log.info(
                    "space_tables: join for %s from %s refused", session_id, sender
                )
                return
            await self._groups.on_space_session(inner, newcomer=newcomer)
            return
        if verb == "group_close" and body.get("by_role") is True:
            user_id = body.get("user_id")
            if (
                hub_here
                and table.state != GROUP_ENDED
                and isinstance(user_id, str)
                and await self._authorship.moderates_as(event, space.id, user_id)
            ):
                await self._groups.end_space_table(table, END_CLOSED_BY_MODERATOR)
            else:
                log.info(
                    "space_tables: close for %s from %s refused", session_id, sender
                )
            return
        await self._groups.on_space_session(inner)

    async def _actor_still_seated(
        self, space: Space, table: GroupSession, sender: str, body: dict
    ) -> bool:
        """At the hub: the person a verb or message from *sender* is by
        still holds a live writer seat there and isn't banned (a leave is
        always taken). Someone not at the table yet passes (a join is
        checked on its own)."""
        if body.get("verb") == "group_leave":
            return True
        actor = body.get("from_user")
        entry = table.entry(sender, actor) if isinstance(actor, str) else None
        if entry is None or entry.user_id is None:
            return True
        return await self._writer_seat(space.id, sender, entry.user_id)

    async def _newcomer(
        self, space: Space, table: GroupSession, sender: str, body: dict
    ) -> GroupSeat | None:
        """The roster entry of someone joining from *sender* — or ``None``:
        no live writer seat there, banned, the app off the list or the
        space archived, or an identity key that isn't *sender*'s."""
        user = _line(body.get("from_user"), GROUP_REF_MAX)
        user_id = _line(body.get("user_id"), GROUP_REF_MAX)
        if (
            user is None
            or user_id is None
            or space.archived
            or table.app_id not in space.features.allowed_apps
            or not await self._writer_seat(space.id, sender, user_id)
        ):
            return None
        identity = body.get("identity_pk")
        try:
            if (
                not isinstance(identity, str)
                or derive_instance_id(bytes.fromhex(identity)) != sender
            ):
                return None
        except ValueError:
            return None
        # One person, one place: the same user id under another name is
        # refused (else one seat on the space fills every seat).
        if any(
            e.user_id == user_id and not e.is_person(sender, user) for e in table.roster
        ):
            return None
        seat = await self._seats.get(space.id, sender, user_id)
        name = _line(body.get("name"), GROUP_NAME_MAX) or (
            seat.display_name if seat is not None and seat.display_name else user
        )
        return GroupSeat(
            instance=sender,
            user=user,
            name=name[:GROUP_NAME_MAX],
            household_name=await self._app_fed.household_name(sender),
            state=SEAT_INVITED,
            user_id=user_id,
            identity_pk=identity,
        )

    # ─── The space's list and its members ─────────────────────────────

    async def _on_space_config(
        self,
        event: SpaceFeaturesApplied
        | SpaceConfigChanged
        | RemoteSpaceMemberBanned
        | SpaceMemberLeft
        | SpaceRemoteSeatEnded
        | SpaceRemoteSeatLive,
    ) -> None:
        """The space changed (its config, its roster, a ban): the tables
        this household hosts in it follow.

        * An app left the list (D10) — or the space turned global or was
          archived (D6: no games there): the tables still waiting for
          players close (``app_removed``); games being played finish and
          take nobody more (the join checks).
        * A seated player who no longer holds a live writer seat there
          (removed, banned, made a follower) is taken out — the host
          person's seat as if they left (a waiting table closes, a game
          passes to the player seated longest)."""
        space = await self._spaces.get(event.space_id)
        if space is None:
            return
        allowed = set(space.features.allowed_apps)
        no_games = space.space_type is SpaceType.GLOBAL or space.archived
        for table in await self._repo.list_for_space(space.id):
            if table.hub_instance != self._own or table.state == GROUP_ENDED:
                continue
            if table.state == GROUP_LOBBY and (no_games or table.app_id not in allowed):
                await self._groups.end_space_table(
                    table, END_APP_REMOVED, only_lobby=True
                )
                continue
            for entry in table.seated:
                if entry.user_id is None or await self._still_in_space(space, entry):
                    continue
                log.info(
                    "space_tables: %s left %s — out of table %s",
                    entry.user_id,
                    space.id,
                    table.session_id,
                )
                await self._groups.drop_space_player(table.session_id, entry)

    async def _still_in_space(self, space: Space, entry: GroupSeat) -> bool:
        if entry.instance != self._own:
            assert entry.user_id is not None
            return await self._writer_seat(space.id, entry.instance, entry.user_id)
        member = await self._spaces.get_member(space.id, str(entry.user_id))
        return (
            member is not None
            and member.role in _WRITER_VALUES
            and not await self._spaces.is_banned(space.id, str(entry.user_id))
        )

    async def _on_member_left(self, event: SpaceMemberLeft) -> None:
        """Someone left the space (or was removed). One of ours leaves
        every table of it (a waiting table they host closes, a game being
        played passes to the player seated longest); with no writer seat
        left here, the space's cards go too. Anyone else is taken out of
        the tables this household hosts there."""
        if await self._user_repo.get_by_user_id(event.user_id) is None:
            await self._on_space_config(event)
            return
        for table in await self._repo.list_for_space(event.space_id):
            row = await self._app_repo.get_pending_session(
                table.app_id, event.user_id, table.session_id
            )
            if row is None or not row.is_group or table.state == GROUP_ENDED:
                continue
            try:
                if row.state == SESSION_INVITED:
                    continue
                await self._groups.leave(
                    app_id=table.app_id,
                    session_id=table.session_id,
                    actor_user_id=event.user_id,
                )
            except (AppGroupClosedError, AppGroupNotFoundError) as exc:
                log.info("space_tables: leave on space exit: %s", exc.code)
        if not await holds_member_seat(self._spaces, event.space_id):
            for table in await self._repo.list_for_space(event.space_id):
                if self._card_only(table):
                    await self._repo.delete(table.session_id)
