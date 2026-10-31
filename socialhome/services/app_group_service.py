"""AppGroupService — group sessions ("tables") for Social Home Apps (v_76).

A table seats 2–30 people (up to :data:`GROUP_PICK_MAX_PLAYERS` when opened
by picking people, the only way in until space tables, v_77). Its **hub**
— the household of the person who opened it — keeps the roster
(:class:`~socialhome.domain.app_group.GroupSession`) and is the only
household the others talk to about it; every other household with a
player keeps a copy (a *mirror*) as the hub last sent it. Households never
talk to each other because of a table.

On the wire (all sealed inside ``APP_SESSION``, v_76 households only):

* hub → an invitee's household: ``group_invite`` (one person; who else was
  invited never travels — only how many);
* a player's household → hub: ``group_join``, ``group_leave`` (a leave
  before joining is a decline);
* hub → the joiner's household: ``group_seat``; hub → the seated
  households: ``group_roster`` (at most one per
  :data:`GROUP_ROSTER_EVERY_SECONDS` per table, coalesced),
  ``group_start``, ``group_close`` (with ``to_user``: one person's place is
  over — removed, invite withdrawn, no seat left);
* the host person → hub, when the host person lives in another household
  after a hand-over: ``group_start``, ``group_remove``, ``group_close``.

Every hub message carries the table snapshot; a mirror keeps it only from
the table's hub and only when newer (``rev``).

Security invariants
-------------------
* The hub decides who sits (invited people only, never past ``max``, never
  someone removed) and acts on commands only from the host person's own
  household (the signature-proven sender).
* A mirror takes snapshots only from the household that invited it (the
  hub) — no other household can speak for a table.
* **Blocks stay unseen** (design 2.11, D12): each household enforces its
  own people's personal and guardian blocks — it hides an invite, refuses
  a Join, and takes its person out with an ordinary leave when someone
  they can't play with sits down. The hub is never told why.
* **Children** (§CP, D8): a protected account sits only at a table where
  every player is someone it may already message (its own household, or a
  household it paired with directly) — checked at the invite, at Join and
  on every roster.
* People are seats to the app: :meth:`view` gives seat numbers, names and
  household names, never a username.

Each local person at a table has an ``app_pending_sessions`` row marked
``group`` (``invited`` → ``active``): it keeps Needs you, the TTLs and the
uninstall cascade; the 1:1 paths of
:class:`~socialhome.services.app_federation_service.AppFederationService`
leave such rows alone.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from weakref import WeakValueDictionary

from ..domain.app_group import (
    CLOSE_FULL,
    CLOSE_REMOVED,
    CLOSE_WITHDRAWN,
    END_CANCELLED,
    END_EXPIRED,
    END_HOST_GONE,
    GROUP_ENDED,
    GROUP_ENDED_KEEP_SECONDS,
    GROUP_LOBBY,
    GROUP_LOBBY_DAYS,
    GROUP_LOBBY_DEFAULT_DAYS,
    GROUP_MIRROR_GRACE_SECONDS,
    GROUP_MIRRORS_PER_HUB_MAX,
    GROUP_OPEN_TABLES_MAX,
    GROUP_PICK_MAX_PLAYERS,
    GROUP_PLAYING,
    GROUP_ROSTER_EVERY_SECONDS,
    GROUP_TABLES_PER_DAY_MAX,
    PERSON_CLOSE_REASONS,
    SEAT_DECLINED,
    SEAT_INVITED,
    SEAT_LEFT,
    SEAT_REMOVED,
    SEAT_SEATED,
    VERB_CLOSE,
    VERB_INVITE,
    VERB_JOIN,
    VERB_LEAVE,
    VERB_REMOVE,
    VERB_ROSTER,
    VERB_SEAT,
    VERB_START,
    WIRE_SEAT_STATES,
    AppGroupCantJoinError,
    AppGroupClosedError,
    AppGroupFullError,
    AppGroupHouseholdTooOldError,
    AppGroupLimitError,
    AppGroupNotFoundError,
    AppGroupNotHostError,
    AppGroupSizeError,
    AppGroupsNotSupportedError,
    AppGroupTooFewError,
    GroupSeat,
    GroupSession,
    GroupWireError,
    note_ok,
    parse_table,
)
from ..domain.apps import (
    ACTIVE_SESSION_TTL_SECONDS,
    PENDING_SESSION_TTL_SECONDS,
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    SESSION_ACTIVE,
    SESSION_INVITED,
    AppPendingSession,
)
from ..domain.events import AppChallengeEnded, AppChallengeReceived, UserBlocked
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from .app_federation_service import with_household
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..domain.apps import InstalledApp
    from ..domain.events import AppUninstalled, DomainEvent
    from ..domain.user import User
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_group_repo import AbstractAppGroupRepo
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .app_federation_service import AppFederationService

log = logging.getLogger(__name__)

#: A table id on the wire — what this household mints (``uuid4().hex``).
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: The WebSocket frame a local person's SPA gets on every change of a
#: table they have a place at (their own view, :meth:`AppGroupService.view`).
WS_FRAME_TYPE = "app.group"


def _now() -> datetime:
    return datetime.now(UTC)


class AppGroupService(ProtectionGateMixin):
    """Group tables: the hub's roster, the mirrors, and the rules each
    household applies for its own people. See the module docstring."""

    __slots__ = (
        "_app_fed",
        "_app_repo",
        "_bus",
        "_child_protection",
        "_federation",
        "_locks",
        "_repo",
        "_roster_due",
        "_roster_every",
        "_roster_sent",
        "_stop",
        "_tasks",
        "_user_repo",
        "_ws",
    )

    def __init__(
        self,
        *,
        group_repo: AbstractAppGroupRepo,
        app_repo: AbstractAppRepo,
        user_repo: AbstractUserRepo,
        app_federation: AppFederationService,
        federation: Any,
        ws: WebSocketManager,
        bus: EventBus | None = None,
        roster_every: float = GROUP_ROSTER_EVERY_SECONDS,
    ) -> None:
        self._repo = group_repo
        self._app_repo = app_repo
        self._user_repo = user_repo
        self._app_fed = app_federation
        self._federation = federation
        self._ws = ws
        self._bus = bus
        self._child_protection = None
        self._roster_every = roster_every
        #: One change at a time per table (read, change, save, send).
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        #: Coalesced roster sends: when each table's last went out, which
        #: wait for their turn, and the tasks that send them.
        self._roster_sent: dict[str, float] = {}
        self._roster_due: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._stop = asyncio.Event()

    @property
    def _own(self) -> str:
        return str(self._federation.own_instance_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def _emit(self, event: DomainEvent) -> None:
        """Publish fail-soft: a bus failure never breaks a table change."""
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft publish
            log.warning("app_group: bus publish failed: %s", exc)

    async def stop(self) -> None:
        """Let the coalesced roster sends that wait go (none is sent)."""
        self._stop.set()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # ─── Reading ──────────────────────────────────────────────────────

    def view(self, table: GroupSession, user: User, row: AppPendingSession) -> dict:
        """*table* as *user* sees it (the lobby, the coming SDK's
        ``GroupSession``): seats, names and household names — never a
        username. ``me`` is their seat (``None`` until the hub seated
        them), ``my_state`` their place: ``invited``, ``joining`` (Join
        sent, the hub hasn't answered), ``seated``, ``left`` or
        ``removed``."""
        own = self._own
        mine = table.entry(own, user.username)
        if mine is not None and mine.state in WIRE_SEAT_STATES:
            my_state = mine.state
        elif row.state == SESSION_INVITED:
            my_state = SEAT_INVITED
        else:
            my_state = "joining"
        return {
            "session_id": table.session_id,
            "app_id": table.app_id,
            "state": table.state,
            "end_reason": table.end_reason,
            "space_id": table.space_id,
            "rev": table.roster_rev,
            "host": table.host_seat or None,
            "is_host": table.is_host(own, user.username),
            "me": mine.seat if mine is not None and mine.seat else None,
            "my_state": my_state,
            "min": table.min_players,
            "max": table.max_players,
            "late_join": table.late_join,
            "note": table.note,
            "created_at": table.created_at,
            "started_at": table.started_at,
            "expires_at": table.expires_at,
            # Who else was asked is the host's own business (hub only).
            "invited": table.pending_invites if table.hub_instance == own else None,
            "players": [
                {
                    "seat": e.seat,
                    "name": e.name,
                    "household": e.household_name,
                    "is_local": e.instance == own,
                    "state": e.state,
                }
                for e in table.roster
                if e.state in WIRE_SEAT_STATES
            ],
        }

    async def list_tables(self, *, app_id: str, actor_user_id: str) -> list[dict]:
        """The tables *actor_user_id* has a place at in *app_id* (invited,
        joining, seated, or one they left that hasn't been pruned), newest
        change first."""
        await self._usable(app_id, actor_user_id)
        user = await self._local_user(actor_user_id)
        out: list[dict] = []
        for row in await self._app_repo.list_sessions(app_id, actor_user_id):
            if not row.is_group:
                continue
            table = await self._repo.get(row.session_id)
            if table is not None and table.app_id == app_id:
                out.append(self.view(table, user, row))
        return out

    async def get_table(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> dict:
        """One table the caller has a place at — else
        :class:`AppGroupNotFoundError`."""
        await self._usable(app_id, actor_user_id)
        table, user, row = await self._mine(app_id, session_id, actor_user_id)
        return self.view(table, user, row)

    async def invite_usage(self, app_id: str, user_id: str) -> tuple[int, int]:
        """(open, today): the people *user_id*'s tables in *app_id* invited
        who haven't answered (tables still waiting for players), and the
        people invited in the last 24 hours — their share of the 1:1 invite
        caps (one per person, design 2.5)."""
        user = await self._user_repo.get_by_user_id(user_id)
        if user is None:
            return 0, 0
        now = _now()
        day_cut = (now - timedelta(days=1)).isoformat()
        open_ = today = 0
        for t in await self._repo.list_hosted(
            app_id,
            self._own,
            user.username,
            since=(now - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)).isoformat(),
        ):
            if t.state == GROUP_LOBBY:
                open_ += t.pending_invites
            if t.created_at >= day_cut:
                today += t.invited_total
        return open_, today

    # ─── Opening a table (the invite sheet) ───────────────────────────

    async def open_table(
        self,
        *,
        app_id: str,
        actor_user_id: str,
        targets: list[dict],
        max_players: int | None = None,
        lobby_days: int = GROUP_LOBBY_DEFAULT_DAYS,
        note: str | None = None,
    ) -> dict:
        """Open a table with the people picked in Social Home's invite
        sheet (D3) — each *target* one of
        :meth:`AppFederationService.list_invitable` — and invite them.

        The opener is the host (seat 1). ``max_players`` defaults to the
        app's (at most :data:`GROUP_PICK_MAX_PLAYERS`); the picked people
        plus the host must reach the app's fewest players and fit
        ``max_players`` (:class:`AppGroupSizeError`). The table waits
        *lobby_days* for players. Refused past the table caps
        (:class:`AppGroupLimitError`) and the invite caps — one invite per
        person picked (:class:`~socialhome.domain.apps.AppInviteLimitError`).
        A person whose household runs a Social Home below v_76 is refused
        (:class:`AppGroupHouseholdTooOldError`): it would never hear of it.
        """
        app = await self._usable(app_id, actor_user_id)
        if not app.manifest.tables:
            raise AppGroupsNotSupportedError()
        host = await self._local_user(actor_user_id)
        own = self._own
        lo = app.manifest.players_min
        hi = min(app.manifest.players_max, GROUP_PICK_MAX_PLAYERS)
        seats = hi if max_players is None else max_players
        picked = self._distinct(targets)
        if not lo <= seats <= hi or not lo <= len(picked) + 1 <= seats:
            raise AppGroupSizeError(lo, hi)
        if lobby_days not in GROUP_LOBBY_DAYS:
            raise ValueError("lobby_days must be one of 1, 3, 7, 14")
        clean_note = note_ok(note)
        for target in picked:
            if not target.get("is_local"):
                app.require_permission(PERMISSION_FEDERATION)
            await self._app_fed.assert_invitable(actor_user_id, target, app=app)
            if not target.get("is_local") and not await self._federation.peer_supports(
                str(target["instance_id"]),
                min_version=FederationCapability.MIN_FOR_APP_GROUPS,
            ):
                raise AppGroupHouseholdTooOldError()
        async with self._app_fed.invite_lock(app_id, actor_user_id):
            await self._check_table_caps(app_id, host.username)
            await self._app_fed.check_invite_caps(
                app_id, actor_user_id, adding=len(picked)
            )
            now = _now()
            stamp = now.isoformat()
            roster = [
                GroupSeat(
                    instance=own,
                    user=host.username,
                    name=host.display_name,
                    state=SEAT_SEATED,
                    seat=1,
                    joined_at=stamp,
                )
            ]
            for target in picked:
                roster.append(await self._invitee(target))
            table = GroupSession(
                session_id=uuid4().hex,
                app_id=app_id,
                host_instance=own,
                host_user=host.username,
                hub_instance=own,
                state=GROUP_LOBBY,
                min_players=lo,
                max_players=seats,
                roster=tuple(roster),
                roster_rev=1,
                created_at=stamp,
                expires_at=(now + timedelta(days=lobby_days)).isoformat(),
                updated_at=stamp,
                late_join=app.manifest.late_join,
                note=clean_note,
                opener_user=host.username,
            )
            await self._repo.save(table)
            hosting = self._seat_row(table, actor_user_id, state=SESSION_ACTIVE)
            await self._app_repo.add_active_session(hosting)
        others = len(picked) - 1
        for entry in table.roster[1:]:
            if entry.instance == own:
                await self._invite_local(table, app, entry, host, actor_user_id)
            else:
                await self._send(
                    entry.instance,
                    table,
                    VERB_INVITE,
                    to_user=entry.user,
                    from_user=host.username,
                    others=others,
                )
        await self._push(table)
        return self.view(table, host, hosting)

    @staticmethod
    def _distinct(targets: list[dict]) -> list[dict]:
        """Each person once, in the order picked."""
        seen: set[tuple[str, str, bool]] = set()
        out: list[dict] = []
        for t in targets:
            key = (
                str(t.get("instance_id")),
                str(t.get("user_ref")),
                bool(t.get("is_local")),
            )
            if key not in seen:
                seen.add(key)
                out.append(t)
        return out

    async def _check_table_caps(self, app_id: str, username: str) -> None:
        now = _now()
        hosted = await self._repo.list_hosted(
            app_id,
            self._own,
            username,
            since=(now - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)).isoformat(),
        )
        stamp = now.isoformat()
        waiting = sum(
            1 for t in hosted if t.state == GROUP_LOBBY and t.expires_at > stamp
        )
        if waiting >= GROUP_OPEN_TABLES_MAX:
            raise AppGroupLimitError(daily=False)
        day_cut = (now - timedelta(days=1)).isoformat()
        if (
            sum(1 for t in hosted if t.created_at >= day_cut)
            >= GROUP_TABLES_PER_DAY_MAX
        ):
            raise AppGroupLimitError(daily=True)

    async def _invitee(self, target: dict) -> GroupSeat:
        """A picked person's roster entry, named as this household knows
        them."""
        if target.get("is_local"):
            user = await self._user_repo.get_by_user_id(str(target["user_ref"]))
            if user is None:  # gone since the sheet listed them
                raise AppGroupNotFoundError()
            return GroupSeat(
                instance=self._own,
                user=user.username,
                name=user.display_name,
                state=SEAT_INVITED,
            )
        instance = str(target["instance_id"])
        remote = await self._user_repo.get_remote_by_member(
            instance, str(target["user_ref"])
        )
        if remote is None:  # gone since the sheet listed them
            raise AppGroupNotFoundError()
        return GroupSeat(
            instance=instance,
            user=remote.remote_username,
            name=remote.display_name or remote.remote_username,
            household_name=await self._app_fed.household_name(instance),
            state=SEAT_INVITED,
        )

    def _seat_row(
        self, table: GroupSession, user_id: str, *, state: str = SESSION_INVITED
    ) -> AppPendingSession:
        """A local person's place at *table* as an ``app_pending_sessions``
        row (marked ``group``; the other side is the hub's host person)."""
        stamp = _now().isoformat()
        return AppPendingSession(
            app_id=table.app_id,
            user_id=user_id,
            session_id=table.session_id,
            from_instance=table.hub_instance,
            from_user=table.host_user,
            payload={
                "app_id": table.app_id,
                "session_id": table.session_id,
                "verb": VERB_INVITE,
                "group": True,
            },
            created_at=stamp,
            state=state,
            updated_at=stamp,
        )

    async def _invite_local(
        self,
        table: GroupSession,
        app: InstalledApp,
        entry: GroupSeat,
        host: User,
        host_user_id: str,
    ) -> None:
        """Someone in the hub's own household was picked: their invite
        waits in Needs you (age gate first, as for a 1:1 invite)."""
        user = await self._user_repo.get(entry.user)
        if user is None:
            return
        if not await self._app_fed.age_filter_recipients(
            table.app_id, [user.user_id], app=app
        ):
            return
        await self._app_repo.add_pending_session(self._seat_row(table, user.user_id))
        await self._emit(
            AppChallengeReceived(
                app_id=table.app_id,
                session_id=table.session_id,
                to_user_id=user.user_id,
                from_display=host.display_name,
                from_user_id=host_user_id,
            )
        )

    # ─── A local person answers ───────────────────────────────────────

    async def join(self, *, app_id: str, session_id: str, actor_user_id: str) -> dict:
        """Join a table one is invited to ("Join" in Needs you).

        This household checks first (design 2.11): the app here and on,
        the age gate, and that nobody at the table is someone the person
        can't play with (a block either way, a guardian block, a protected
        account and a player it may not message) —
        :class:`AppGroupCantJoinError`, no reason given. Then the hub
        decides (a seat left, the table still open); a refusal arrives as
        ``group_close`` to this person. On the hub itself it is decided at
        once (:class:`AppGroupFullError` / :class:`AppGroupClosedError`)."""
        app = await self._usable(app_id, actor_user_id)
        table, user, row = await self._mine(app_id, session_id, actor_user_id)
        if table.hub_instance != self._own:
            app.require_permission(PERMISSION_FEDERATION)
        if row.state != SESSION_INVITED or not table.is_open:
            raise AppGroupClosedError()
        if not await self._may_play(user, table):
            raise AppGroupCantJoinError()
        if table.hub_instance == self._own:
            table = await self._hub_join(table.session_id, self._own, user.username)
        else:
            await self._send(
                table.hub_instance,
                table,
                VERB_JOIN,
                from_user=user.username,
                snap=False,
            )
        taken = await self._app_repo.activate_pending_session(
            app_id, actor_user_id, session_id, now=_now().isoformat()
        )
        if taken is not None:
            await self._emit(
                AppChallengeEnded(
                    app_id=app_id,
                    user_id=actor_user_id,
                    session_ids=(session_id,),
                    outcome="accepted",
                )
            )
        row = dataclasses.replace(row, state=SESSION_ACTIVE)
        await self._push(table)
        return self.view(table, user, row)

    async def decline(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> None:
        """Decline a table invite: it stops waiting here, and the hub hears
        a leave (it marks the invite declined — the players aren't told)."""
        table, user, _row = await self._mine(app_id, session_id, actor_user_id)
        taken = await self._app_repo.take_pending_session(
            app_id, actor_user_id, session_id
        )
        if taken is None:
            raise AppGroupClosedError()
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome="declined",
            )
        )
        await self._leave_table(table, user, declined=True)

    async def leave(self, *, app_id: str, session_id: str, actor_user_id: str) -> None:
        """Leave a table one joined (before Start the seat is simply free
        again). The host person leaving hands the game to the player seated
        longest — or, before Start, closes the table."""
        table, user, row = await self._mine(app_id, session_id, actor_user_id)
        if row.state != SESSION_ACTIVE:
            raise AppGroupClosedError()
        await self._app_repo.delete_pending_session(app_id, actor_user_id, session_id)
        await self._leave_table(table, user, declined=False)
        await self._push_to(table, user, change="left")

    async def _leave_table(
        self, table: GroupSession, user: User, *, declined: bool
    ) -> None:
        if table.hub_instance == self._own:
            await self._hub_leave(
                table.session_id, self._own, user.username, declined=declined
            )
            return
        if table.state == GROUP_ENDED:
            return
        await self._send(
            table.hub_instance,
            table,
            VERB_LEAVE,
            from_user=user.username,
            reason=SEAT_DECLINED if declined else SEAT_LEFT,
            snap=False,
        )

    # ─── The host person ──────────────────────────────────────────────

    async def start(self, *, app_id: str, session_id: str, actor_user_id: str) -> dict:
        """Start the game (host person only, once the app's fewest players
        are seated). Invites nobody answered are withdrawn — unless the app
        takes late joiners."""
        await self._usable(app_id, actor_user_id)
        table, user, row = await self._host_only(app_id, session_id, actor_user_id)
        if not table.can_start:
            if table.state != GROUP_LOBBY:
                raise AppGroupClosedError()
            raise AppGroupTooFewError(table.min_players)
        if table.hub_instance == self._own:
            table = await self._hub_start(session_id, self._own, user.username)
        else:
            await self._send(
                table.hub_instance,
                table,
                VERB_START,
                from_user=user.username,
                snap=False,
            )
        return self.view(table, user, row)

    async def remove(
        self, *, app_id: str, session_id: str, actor_user_id: str, seat: int
    ) -> dict:
        """Take a player out (host person only, before or after Start):
        they can't come back to this table."""
        await self._usable(app_id, actor_user_id)
        table, user, row = await self._host_only(app_id, session_id, actor_user_id)
        target = table.at_seat(seat)
        if (
            target is None
            or target.state != SEAT_SEATED
            or table.is_host(target.instance, target.user)
            or table.state == GROUP_ENDED
        ):
            raise AppGroupClosedError()
        if table.hub_instance == self._own:
            table = await self._hub_remove(session_id, self._own, user.username, seat)
        else:
            await self._send(
                table.hub_instance,
                table,
                VERB_REMOVE,
                from_user=user.username,
                seat=seat,
                snap=False,
            )
        return self.view(table, user, row)

    async def close(self, *, app_id: str, session_id: str, actor_user_id: str) -> None:
        """Close the table (host person only): the game is over for
        everyone (``cancelled``)."""
        table, user, _row = await self._host_only(app_id, session_id, actor_user_id)
        if table.state == GROUP_ENDED:
            raise AppGroupClosedError()
        if table.hub_instance == self._own:
            await self._hub_close(session_id, self._own, user.username)
        else:
            await self._send(
                table.hub_instance,
                table,
                VERB_CLOSE,
                from_user=user.username,
                snap=False,
            )

    async def _host_only(
        self, app_id: str, session_id: str, actor_user_id: str
    ) -> tuple[GroupSession, User, AppPendingSession]:
        table, user, row = await self._mine(app_id, session_id, actor_user_id)
        if not table.is_host(self._own, user.username):
            raise AppGroupNotHostError()
        return table, user, row

    # ─── The hub ──────────────────────────────────────────────────────

    async def _hub_table(self, session_id: str) -> GroupSession | None:
        table = await self._repo.get(session_id)
        if table is None or table.hub_instance != self._own:
            return None
        return table

    async def _hub_join(
        self, session_id: str, instance: str, user: str
    ) -> GroupSession:
        """Seat an invited person — or say why not (raised for a local
        joiner, sent as ``group_close`` to a remote one). Someone the hub
        never invited, removed or who declined is ignored."""
        refusal: str | None = None
        started = False
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            entry = table.entry(instance, user)
            if entry is None or entry.state not in (SEAT_INVITED, SEAT_SEATED):
                raise AppGroupNotFoundError()
            if entry.state != SEAT_SEATED:
                if not table.is_open:
                    refusal = CLOSE_WITHDRAWN
                elif table.is_full:
                    refusal = CLOSE_FULL
                else:
                    stamp = _now().isoformat()
                    seated = dataclasses.replace(
                        entry, state=SEAT_SEATED, seat=table.next_seat, joined_at=stamp
                    )
                    changes: dict[str, Any] = {}
                    if (
                        table.state == GROUP_LOBBY
                        and table.max_players == 2
                        and len(table.seated) == 1
                    ):
                        # Two-player tables start on their own (D11).
                        changes = {"state": GROUP_PLAYING, "started_at": stamp}
                        started = True
                    table = table.with_entry(seated).bumped(stamp, **changes)
                    await self._repo.save(table)
        if refusal is not None:
            if instance == self._own:
                raise (
                    AppGroupFullError()
                    if refusal == CLOSE_FULL
                    else AppGroupClosedError()
                )
            await self._send(instance, table, VERB_CLOSE, to_user=user, reason=refusal)
            return table
        if instance != self._own:
            await self._send(instance, table, VERB_SEAT, to_user=user)
        if started:
            await self._broadcast(table, VERB_START, skip={instance})
        else:
            self._schedule_roster(table)
        await self._recheck_local(table)
        await self._push(table)
        return table

    async def _hub_leave(
        self, session_id: str, instance: str, user: str, *, declined: bool
    ) -> None:
        """A person leaves (or, before joining, declines). The host person
        leaving closes a table still waiting for players, and hands a game
        being played to the player seated longest."""
        ended = False
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None or table.state == GROUP_ENDED:
                return
            entry = table.entry(instance, user)
            if entry is None:
                return
            stamp = _now().isoformat()
            if entry.state == SEAT_INVITED:
                table = table.with_entry(
                    dataclasses.replace(entry, state=SEAT_DECLINED)
                ).bumped(stamp)
                await self._repo.save(table)
                await self._push(table)
                return
            if entry.state != SEAT_SEATED or declined:
                return
            table = table.with_entry(dataclasses.replace(entry, state=SEAT_LEFT))
            changes: dict[str, Any] = {}
            if table.is_host(instance, user):
                heir = table.successor() if table.state == GROUP_PLAYING else None
                if heir is None:
                    changes = {"state": GROUP_ENDED, "end_reason": END_CANCELLED}
                    ended = True
                else:
                    changes = {"host_instance": heir.instance, "host_user": heir.user}
            table = table.bumped(stamp, **changes)
            await self._repo.save(table)
        await self._after_change(table, ended=ended)

    async def _hub_start(
        self, session_id: str, instance: str, user: str
    ) -> GroupSession:
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            if not table.is_host(instance, user):
                raise AppGroupNotHostError()
            if not table.can_start:
                if table.state != GROUP_LOBBY:
                    raise AppGroupClosedError()
                raise AppGroupTooFewError(table.min_players)
            stamp = _now().isoformat()
            withdrawn: list[GroupSeat] = []
            if not table.late_join:
                for entry in table.roster:
                    if entry.state == SEAT_INVITED:
                        withdrawn.append(entry)
                        table = table.with_entry(
                            dataclasses.replace(entry, state=SEAT_DECLINED)
                        )
            table = table.bumped(stamp, state=GROUP_PLAYING, started_at=stamp)
            await self._repo.save(table)
        for entry in withdrawn:
            if entry.instance == self._own:
                await self._withdraw_local(table, entry.user)
            else:
                await self._send(
                    entry.instance,
                    table,
                    VERB_CLOSE,
                    to_user=entry.user,
                    reason=CLOSE_WITHDRAWN,
                )
        # With late joiners the invites stay open: their households hear
        # the start too (else their copy would run out as a lobby).
        await self._broadcast(table, VERB_START, states=(SEAT_SEATED, SEAT_INVITED))
        await self._push(table)
        return table

    async def _hub_remove(
        self, session_id: str, instance: str, user: str, seat: int
    ) -> GroupSession:
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            if not table.is_host(instance, user):
                raise AppGroupNotHostError()
            target = table.at_seat(seat)
            if (
                target is None
                or target.state != SEAT_SEATED
                or table.is_host(target.instance, target.user)
                or table.state == GROUP_ENDED
            ):
                raise AppGroupClosedError()
            table = table.with_entry(
                dataclasses.replace(target, state=SEAT_REMOVED)
            ).bumped(_now().isoformat())
            await self._repo.save(table)
        if target.instance == self._own:
            await self._gone_local(table, target.user, change=CLOSE_REMOVED)
        else:
            await self._send(
                target.instance,
                table,
                VERB_CLOSE,
                to_user=target.user,
                reason=CLOSE_REMOVED,
            )
        self._schedule_roster(table)
        await self._push(table)
        return table

    async def _hub_close(self, session_id: str, instance: str, user: str) -> None:
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            if not table.is_host(instance, user):
                raise AppGroupNotHostError()
            if table.state == GROUP_ENDED:
                raise AppGroupClosedError()
            table = table.bumped(
                _now().isoformat(), state=GROUP_ENDED, end_reason=END_CANCELLED
            )
            await self._repo.save(table)
        await self._after_change(table, ended=True)

    async def _hub_end(self, table: GroupSession, reason: str) -> None:
        """End a hub table for everyone (expired, the app gone)."""
        async with self._lock(table.session_id):
            current = await self._hub_table(table.session_id)
            if current is None or current.state == GROUP_ENDED:
                return
            table = current.bumped(
                _now().isoformat(), state=GROUP_ENDED, end_reason=reason
            )
            await self._repo.save(table)
        await self._after_change(table, ended=True)

    async def _after_change(self, table: GroupSession, *, ended: bool) -> None:
        """Tell the households and the local people about a hub change: an
        end goes to every household with someone seated or invited, at
        once; anything else rides the coalesced roster."""
        if ended:
            await self._broadcast(
                table, VERB_CLOSE, states=(SEAT_SEATED, SEAT_INVITED, SEAT_LEFT)
            )
            self._roster_sent.pop(table.session_id, None)
            await self._reconcile_local(table)
        else:
            self._schedule_roster(table)
            await self._recheck_local(table)
        await self._push(table)

    async def _broadcast(
        self,
        table: GroupSession,
        verb: str,
        *,
        states: tuple[str, ...] = (SEAT_SEATED,),
        skip: set[str] | None = None,
    ) -> None:
        """One snapshot to each household with someone in *states* — one
        envelope per household, never per person; none to this one."""
        for instance in sorted(
            table.households(*states) - {self._own} - (skip or set())
        ):
            await self._send(instance, table, verb)

    def _schedule_roster(self, table: GroupSession) -> None:
        """Send the seated households the roster now, or — inside
        :data:`GROUP_ROSTER_EVERY_SECONDS` of the last — once that runs out
        (the newest roster then: changes in between coalesce)."""
        sid = table.session_id
        if sid in self._roster_due:
            return
        loop = asyncio.get_running_loop()
        wait = self._roster_every - (loop.time() - self._roster_sent.get(sid, -1e9))
        self._roster_due.add(sid)
        task = asyncio.create_task(self._roster_later(sid, max(0.0, wait)))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _roster_later(self, session_id: str, wait: float) -> None:
        try:
            if wait > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    return  # stopping: nothing more goes out
                except TimeoutError:
                    pass
        finally:
            self._roster_due.discard(session_id)
        table = await self._hub_table(session_id)
        if table is None or table.state == GROUP_ENDED:
            return
        self._roster_sent[session_id] = asyncio.get_running_loop().time()
        # Invitees' households too: whoever sits down decides whether their
        # people may still join (blocks, children — design 2.11).
        await self._broadcast(table, VERB_ROSTER, states=(SEAT_SEATED, SEAT_INVITED))

    async def _send(
        self,
        to_instance: str,
        table: GroupSession,
        verb: str,
        *,
        snap: bool = True,
        **fields: Any,
    ) -> None:
        """One sealed ``APP_SESSION`` *verb* about *table* to *to_instance*
        (best effort: the outbox retries; a failure is logged)."""
        payload: dict[str, Any] = {
            "app_id": table.app_id,
            "session_id": table.session_id,
            "verb": verb,
            **fields,
        }
        if snap:
            payload["table"] = table.to_wire()
        try:
            await self._federation.send_event(
                to_instance_id=to_instance,
                event_type=FederationEventType.APP_SESSION,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — the change stands here
            log.warning(
                "app_group: %s for table %s to %s not sent: %s",
                verb,
                table.session_id,
                to_instance,
                exc,
            )

    # ─── Inbound ──────────────────────────────────────────────────────

    async def on_inbound_event(self, event: FederationEvent) -> None:
        """An inbound ``group_*`` verb (after the §24.11 pipeline). Anything
        malformed, from a household below v_76, or not from who may send
        it is dropped with a log line — nothing goes back."""
        p = event.payload
        app_id, session_id, verb = p.get("app_id"), p.get("session_id"), p.get("verb")
        if (
            not isinstance(app_id, str)
            or not app_id
            or not isinstance(session_id, str)
            or not _SESSION_RE.fullmatch(session_id)
            or event.space_id is not None  # space tables: v_77
        ):
            log.debug("app_group: malformed %s from %s", verb, event.from_instance)
            return
        if not await self._federation.peer_supports(
            event.from_instance, min_version=FederationCapability.MIN_FOR_APP_GROUPS
        ):
            log.info(
                "app_group: %s from %s below v_76 dropped", verb, event.from_instance
            )
            return
        try:
            await self._dispatch(event, app_id, session_id, str(verb))
        except GroupWireError as exc:
            log.info(
                "app_group: %s from %s for %s dropped: %s",
                verb,
                event.from_instance,
                session_id,
                exc,
            )

    async def _dispatch(
        self, event: FederationEvent, app_id: str, session_id: str, verb: str
    ) -> None:
        table = await self._repo.get(session_id)
        if table is not None and table.app_id != app_id:
            return
        is_hub = table is not None and table.hub_instance == self._own
        sender = event.from_instance
        actor = event.payload.get("from_user")
        actor = actor if isinstance(actor, str) and actor else None
        match verb:
            case "group_invite":
                await self._on_invite(event, app_id, session_id, table)
            case "group_join" if is_hub and actor:
                await self._quietly(self._hub_join(session_id, sender, actor))
            case "group_leave" if is_hub and actor:
                await self._hub_leave(
                    session_id,
                    sender,
                    actor,
                    declined=event.payload.get("reason") == SEAT_DECLINED,
                )
            case "group_start" if is_hub and actor:
                await self._quietly(self._hub_start(session_id, sender, actor))
            case "group_remove" if is_hub and actor:
                seat = event.payload.get("seat")
                if isinstance(seat, int) and not isinstance(seat, bool):
                    await self._quietly(
                        self._hub_remove(session_id, sender, actor, seat)
                    )
            case "group_close" if is_hub and actor:
                await self._quietly(self._hub_close(session_id, sender, actor))
            case "group_seat" | "group_roster" | "group_start" | "group_close" if (
                table is not None and not is_hub and table.hub_instance == sender
            ):
                await self._on_snapshot(event, app_id, table)
            case _:
                log.debug(
                    "app_group: %s for %s from %s not ours to take",
                    verb,
                    session_id,
                    sender,
                )

    @staticmethod
    async def _quietly(step: Any) -> None:
        """Run a hub step for another household: a refusal is not answered
        (it was checked on the way out of the sender's household)."""
        try:
            await step
        except (
            AppGroupNotFoundError,
            AppGroupNotHostError,
            AppGroupClosedError,
            AppGroupTooFewError,
            AppGroupFullError,
        ) as exc:
            log.info("app_group: remote step refused: %s", exc.code)

    async def _on_invite(
        self,
        event: FederationEvent,
        app_id: str,
        session_id: str,
        known: GroupSession | None,
    ) -> None:
        """A hub invited one person here. Kept (Needs you) only after every
        rule a 1:1 invite passes and the group ones: the app here, on and
        allowed to reach other households, the age gate, the host a known
        person of the sending household, nobody at the table the invitee
        can't play with, a seat id this person has no row for, and at most
        :data:`GROUP_MIRRORS_PER_HUB_MAX` open tables from that hub."""
        p = event.payload
        hub = event.from_instance
        to_user, from_user = p.get("to_user"), p.get("from_user")
        if not (isinstance(to_user, str) and to_user and isinstance(from_user, str)):
            return
        table = parse_table(
            p.get("table"),
            session_id=session_id,
            app_id=app_id,
            hub_instance=hub,
            now=_now(),
        )
        if (
            not table.is_open
            or table.host_instance != hub
            or table.host_user != from_user
            or table.max_players > GROUP_PICK_MAX_PLAYERS
        ):
            return
        if known is not None and known.hub_instance != hub:
            return
        user = await self._user_repo.get(to_user)
        if user is None or not user.is_active():
            return
        app = await self._app_repo.get(app_id)
        if (
            app is None
            or not app.enabled
            or PERMISSION_FEDERATION not in app.permissions
            or not app.manifest.tables
        ):
            log.info("app_group: invite for app %r not usable here dropped", app_id)
            return
        if not await self._app_fed.age_filter_recipients(
            app_id, [user.user_id], app=app
        ):
            return
        if await self._app_repo.get_pending_session(app_id, user.user_id, session_id):
            return
        host = await self._user_repo.get_remote_by_member(hub, from_user)
        if host is None or not await self._may_play(user, table):
            log.info("app_group: invite for %s dropped by its household", session_id)
            return
        async with self._lock(f"hub:{hub}"):
            if (
                known is None
                and await self._repo.count_open_from(hub) >= GROUP_MIRRORS_PER_HUB_MAX
            ):
                log.warning("app_group: too many open tables from %s", hub)
                return
            await self._repo.save_newer(table)
        await self._app_repo.add_pending_session(self._seat_row(table, user.user_id))
        await self._emit(
            AppChallengeReceived(
                app_id=app_id,
                session_id=session_id,
                to_user_id=user.user_id,
                from_display=with_household(
                    host.display_name or from_user,
                    await self._app_fed.household_name(hub),
                ),
                from_user_id=host.user_id,
            )
        )
        await self._push(await self._repo.get(session_id) or table)

    async def _on_snapshot(
        self, event: FederationEvent, app_id: str, mirror: GroupSession
    ) -> None:
        """The hub's newer view of a table this household keeps a copy of
        (seat, roster, start, close). A ``group_close`` with ``to_user``
        also ends that one person's place (removed, invite withdrawn, no
        seat left)."""
        p = event.payload
        table = parse_table(
            p.get("table"),
            session_id=mirror.session_id,
            app_id=app_id,
            hub_instance=mirror.hub_instance,
            now=_now(),
        )
        if (table.min_players, table.max_players) != (
            mirror.min_players,
            mirror.max_players,
        ):
            # The players range is fixed when the table opens.
            raise GroupWireError("players range changed")
        to_user = p.get("to_user")
        if p.get("verb") == VERB_SEAT and isinstance(to_user, str) and to_user:
            await self._unwanted_seat(table, to_user)
        if p.get("verb") == VERB_CLOSE and isinstance(to_user, str) and to_user:
            reason = p.get("reason")
            if reason in PERSON_CLOSE_REASONS:
                await self._gone_local(table, to_user, change=str(reason))
        async with self._lock(mirror.session_id):
            kept = await self._repo.save_newer(table)
        if kept:
            if table.state == GROUP_ENDED:
                await self._reconcile_local(table)
            else:
                await self._recheck_local(table)
            await self._push(table)

    async def _unwanted_seat(self, table: GroupSession, username: str) -> None:
        """The hub seated someone from here who no longer wants the seat
        (their household took them out, or the app went, before the
        seat arrived — two paths can reorder): answer with a leave so
        no ghost sits at the table."""
        user = await self._user_repo.get(username)
        if user is None:
            return
        row = await self._app_repo.get_pending_session(
            table.app_id, user.user_id, table.session_id
        )
        if row is not None and row.is_group:
            return
        await self._send(
            table.hub_instance,
            table,
            VERB_LEAVE,
            from_user=username,
            reason=SEAT_LEFT,
            snap=False,
        )

    # ─── This household's own people ──────────────────────────────────

    async def _may_play(self, user: User, table: GroupSession) -> bool:
        """Whether *user* may sit at *table* (design 2.11, D8): nobody
        seated (nor the host) is someone they blocked, who blocked them
        here, or a guardian block separates them from; and a protected
        account only with people of households it may message."""
        others = [
            e
            for e in table.roster
            if (e.state == SEAT_SEATED or table.is_host(e.instance, e.user))
            and not e.is_person(self._own, user.username)
        ]
        for e in others:
            if e.instance == self._own:
                other = await self._user_repo.get(e.user)
                if other is None:
                    continue
                other_id = other.user_id
                if await self._user_repo.is_blocked(other_id, user.user_id):
                    return False
            else:
                if not await self._may_message_household(user.user_id, e.instance):
                    return False
                remote = await self._user_repo.get_remote_by_member(e.instance, e.user)
                if remote is None:
                    continue
                other_id = remote.user_id
            if await self._user_repo.is_blocked(
                user.user_id, other_id
            ) or await self._guardian_blocked(user.user_id, other_id):
                return False
        return True

    async def _local_people(
        self, table: GroupSession
    ) -> list[tuple[User, AppPendingSession]]:
        out: list[tuple[User, AppPendingSession]] = []
        for row in await self._app_repo.list_session_rows(
            table.app_id, table.session_id
        ):
            if not row.is_group:
                continue
            user = await self._user_repo.get_by_user_id(row.user_id)
            if user is not None:
                out.append((user, row))
        return out

    async def _recheck_local(self, table: GroupSession) -> None:
        """Someone sat down: each local person seated (or joining) whom a
        block or a children's rule now separates from someone at the table
        leaves — an ordinary leave, so nothing more reaches the hub — and
        only they are told why. An invite still waiting is hidden instead:
        it stops waiting here and nothing goes back (the hub's invite just
        runs out), so not even a decline follows the newcomer."""
        for user, row in await self._local_people(table):
            if row.state != SESSION_ACTIVE:
                if not await self._may_play(user, table):
                    await self._withdraw_local(table, user.username, outcome="done")
                continue
            entry = table.entry(self._own, user.username)
            if entry is not None and entry.state in (SEAT_LEFT, SEAT_REMOVED):
                continue
            if await self._may_play(user, table):
                continue
            log.info(
                "app_group: %s leaves table %s (household rule)",
                user.user_id,
                table.session_id,
            )
            await self._app_repo.delete_pending_session(
                table.app_id, user.user_id, table.session_id
            )
            await self._leave_table(table, user, declined=False)
            await self._push_to(table, user, change="left", reason="cant_play")

    async def _reconcile_local(self, table: GroupSession) -> None:
        """The table ended: invites still waiting here end with it (their
        Needs-you rows read as withdrawn / expired); a seat stays known so
        the app can still say how it ended, until the prune."""
        outcome = "expired" if table.end_reason == END_EXPIRED else "withdrawn"
        for user, row in await self._local_people(table):
            if row.state == SESSION_INVITED:
                await self._withdraw_local(table, user.username, outcome=outcome)

    async def _withdraw_local(
        self, table: GroupSession, username: str, *, outcome: str = "withdrawn"
    ) -> None:
        user = await self._user_repo.get(username)
        if user is None:
            return
        taken = await self._app_repo.take_pending_session(
            table.app_id, user.user_id, table.session_id
        )
        if taken is not None:
            await self._emit(
                AppChallengeEnded(
                    app_id=table.app_id,
                    user_id=user.user_id,
                    session_ids=(table.session_id,),
                    outcome=outcome,
                )
            )
            await self._push_to(table, user, change="withdrawn")

    async def _gone_local(
        self, table: GroupSession, username: str, *, change: str
    ) -> None:
        """One local person's place is over (removed, invite withdrawn, no
        seat left): their row goes and only they are told."""
        user = await self._user_repo.get(username)
        if user is None:
            return
        row = await self._app_repo.get_pending_session(
            table.app_id, user.user_id, table.session_id
        )
        if row is None or not row.is_group:
            return
        if row.state == SESSION_INVITED:
            await self._withdraw_local(table, username)
            return
        await self._app_repo.delete_pending_session(
            table.app_id, user.user_id, table.session_id
        )
        await self._push_to(table, user, change=change)

    async def _push(self, table: GroupSession) -> None:
        """Each local person at *table* gets their view (``app.group``)."""
        for user, row in await self._local_people(table):
            await self._ws.broadcast_to_user(
                user.user_id,
                {
                    "type": WS_FRAME_TYPE,
                    "app_id": table.app_id,
                    "session_id": table.session_id,
                    "group": self.view(table, user, row),
                },
            )

    async def _push_to(
        self,
        table: GroupSession,
        user: User,
        *,
        change: str,
        reason: str | None = None,
    ) -> None:
        """Tell one person their place at *table* is over (their row is
        gone, so :meth:`_push` no longer reaches them)."""
        await self._ws.broadcast_to_user(
            user.user_id,
            {
                "type": WS_FRAME_TYPE,
                "app_id": table.app_id,
                "session_id": table.session_id,
                "change": change,
                "reason": reason,
            },
        )

    # ─── Guards ───────────────────────────────────────────────────────

    async def _usable(self, app_id: str, actor_user_id: str) -> InstalledApp:
        app = await self._app_fed.require_enabled(app_id)
        app.require_permission(PERMISSION_PEOPLE)
        await self._app_fed.assert_age_allowed(app_id, actor_user_id, app=app)
        return app

    async def _local_user(self, user_id: str) -> User:
        user = await self._user_repo.get_by_user_id(user_id)
        if user is None:
            raise AppGroupNotFoundError()
        return user

    async def _mine(
        self, app_id: str, session_id: str, actor_user_id: str
    ) -> tuple[GroupSession, User, AppPendingSession]:
        """The table and the caller's place at it — else
        :class:`AppGroupNotFoundError` (the same for a table that doesn't
        exist and one they have no place at)."""
        row = await self._app_repo.get_pending_session(
            app_id, actor_user_id, session_id
        )
        table = await self._repo.get(session_id)
        if row is None or not row.is_group or table is None or table.app_id != app_id:
            raise AppGroupNotFoundError()
        return table, await self._local_user(actor_user_id), row

    # ─── Housekeeping ─────────────────────────────────────────────────

    async def sweep(self) -> int:
        """Hourly: tables still waiting for players past their time end
        (``expired``; a hub tells everyone, a copy ends on its own after
        :data:`GROUP_MIRROR_GRACE_SECONDS` in case the hub is gone), then
        tables that ended :data:`GROUP_ENDED_KEEP_SECONDS` ago, or nobody
        touched for the 90-day game TTL, are forgotten. Returns how many
        were forgotten."""
        now = _now()
        for table in await self._repo.list_lobby_expired(before=now.isoformat()):
            if table.hub_instance == self._own:
                await self._hub_end(table, END_EXPIRED)
                continue
            grace = (now - timedelta(seconds=GROUP_MIRROR_GRACE_SECONDS)).isoformat()
            if table.expires_at < grace:
                ended = dataclasses.replace(
                    table,
                    state=GROUP_ENDED,
                    end_reason=END_EXPIRED,
                    updated_at=now.isoformat(),
                )
                await self._repo.save(ended)
                await self._reconcile_local(ended)
                await self._push(ended)
        for sid in list(self._roster_sent):
            if await self._repo.get(sid) is None:
                self._roster_sent.pop(sid, None)
        return await self._repo.prune(
            ended_before=(
                now - timedelta(seconds=GROUP_ENDED_KEEP_SECONDS)
            ).isoformat(),
            quiet_before=(
                now - timedelta(seconds=ACTIVE_SESSION_TTL_SECONDS)
            ).isoformat(),
        )

    async def on_app_uninstalled(self, event: AppUninstalled) -> None:
        """The app is gone here (its people's rows went with it): a table
        this household hosts ends for everyone (``host_gone``); at another
        hub, each person from here who sat leaves. Then the tables go."""
        for table in await self._repo.list_for_app(event.app_id):
            if table.space_id is not None:
                continue  # a space table's card stays (v_77)
            if table.state != GROUP_ENDED:
                if table.hub_instance == self._own:
                    await self._hub_end(table, END_HOST_GONE)
                else:
                    for entry in table.roster:
                        if entry.instance == self._own and entry.state == SEAT_SEATED:
                            await self._send(
                                table.hub_instance,
                                table,
                                VERB_LEAVE,
                                from_user=entry.user,
                                reason=SEAT_LEFT,
                                snap=False,
                            )
            await self._repo.delete(table.session_id)
            self._roster_sent.pop(table.session_id, None)

    async def on_account_protected(self, user_id: str) -> None:
        """§CP: *user_id* just became protected — the children's rule now
        holds at every table they have a place at."""
        await self._recheck_person(user_id)

    async def on_guardian_block(self, minor_user_id: str, blocked_user_id: str) -> None:
        """§CP.F2: a guardian blocked someone for *minor_user_id* — the
        minor leaves every table where that person sits."""
        await self._recheck_person(minor_user_id)

    async def on_user_blocked(self, event: UserBlocked) -> None:
        """A person here blocked someone: they leave every table where
        that person sits (and a waiting invite to one is hidden)."""
        await self._recheck_person(event.blocker_user_id)

    async def _recheck_person(self, user_id: str) -> None:
        for app in await self._app_repo.list_installed():
            for row in await self._app_repo.list_sessions(app.app_id, user_id):
                if not row.is_group:
                    continue
                table = await self._repo.get(row.session_id)
                if table is not None and table.state != GROUP_ENDED:
                    await self._recheck_local(table)
