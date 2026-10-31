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
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from weakref import WeakValueDictionary

import orjson

from ..crypto import b64url_decode, derive_instance_id
from ..domain.app_group import (
    CLOSE_FULL,
    CLOSE_REMOVED,
    CLOSE_WITHDRAWN,
    END_CANCELLED,
    END_EXPIRED,
    END_HOST_GONE,
    GROUP_BATCH_MAX_BYTES,
    GROUP_BATCH_MAX_MESSAGES,
    GROUP_COALESCE_SECONDS,
    GROUP_DIRECT_MAX_BYTES,
    GROUP_DIRECT_MAX_PLAINTEXT,
    GROUP_ENDED,
    GROUP_ENDED_KEEP_SECONDS,
    GROUP_HELD_MAX_BYTES,
    GROUP_LOBBY,
    GROUP_LOBBY_DAYS,
    GROUP_LOBBY_DEFAULT_DAYS,
    GROUP_MESSAGE_MAX_BYTES,
    GROUP_MIRROR_GRACE_SECONDS,
    GROUP_MIRRORS_PER_HUB_MAX,
    GROUP_OPEN_TABLES_MAX,
    GROUP_PICK_MAX_PLAYERS,
    GROUP_PLAYING,
    GROUP_ROSTER_EVERY_SECONDS,
    GROUP_ROSTER_MAX,
    GROUP_SEND_BUDGET,
    GROUP_SEQ_WINDOW,
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
    AppGroupBusyError,
    AppGroupCantJoinError,
    AppGroupClosedError,
    AppGroupFullError,
    AppGroupHouseholdTooOldError,
    AppGroupLimitError,
    AppGroupNotFoundError,
    AppGroupNotHostError,
    AppGroupSeatError,
    AppGroupSizeError,
    AppGroupsNotSupportedError,
    AppGroupTooFewError,
    AppGroupTooLargeError,
    GroupMessage,
    GroupSeat,
    GroupSession,
    GroupWireError,
    SeatKey,
    note_ok,
    parse_batch,
    parse_message,
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
from ..domain.events import (
    AppChallengeEnded,
    AppChallengeReceived,
    AppTurn,
    AppTurnsSeen,
    UserBlocked,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..federation.seat_crypto import (
    SeatSealError,
    UnsupportedSeatKemSuite,
    UnsupportedSeatKeySigSuite,
    check_sealed_entry,
    new_msg_id,
    new_seat_keypair,
    open_sealed,
    parse_seat_key,
    seal,
    sign_seat_key,
    verify_seat_key,
)
from .app_federation_service import with_household
from .app_group_budget import TableSendBudget
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..domain.apps import InstalledApp
    from ..domain.events import AppUninstalled, DomainEvent
    from ..domain.user import User
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.key_manager import KeyManager
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_group_repo import AbstractAppGroupRepo
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .app_federation_service import AppFederationService
    from .space_app_table_service import SpaceTableService

log = logging.getLogger(__name__)

#: A table id on the wire — what this household mints (``uuid4().hex``).
_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: The WebSocket frame a local person's SPA gets on every change of a
#: table they have a place at (their own view, :meth:`AppGroupService.view`).
WS_FRAME_TYPE = "app.group"
#: The WebSocket frame a local person's SPA gets for each message at a
#: table delivered to them live.
WS_MESSAGE_FRAME_TYPE = "app.group.message"


def _seat_key_ad(session_id: str) -> bytes:
    """What this household's KEK-wrapped seat key is bound to."""
    return b"socialhome/app_group/seat_key_at_rest|" + session_id.encode()


def _now() -> datetime:
    return datetime.now(UTC)


#: :meth:`AppGroupService._open` found nothing it could open.
_UNOPENED = object()

#: ``msg_id`` s a table's copy remembers (a hub can't replay a message under
#: a new ``seq``).
_SEEN_MSG_IDS_MAX = 2 * GROUP_SEQ_WINDOW


class _Seen:
    """What one table's copy passed on lately: ``seq`` numbers (within
    :data:`GROUP_SEQ_WINDOW` of the newest) and ``(from_seat, msg_id)``."""

    __slots__ = ("msg_ids", "seqs")

    def __init__(self) -> None:
        self.seqs: set[int] = set()
        self.msg_ids: OrderedDict[tuple[int, str], None] = OrderedDict()

    def add(self, seq: int, mid: tuple[int, str], floor: int) -> None:
        self.seqs.add(seq)
        self.seqs = {s for s in self.seqs if s > floor}
        self.msg_ids[mid] = None
        while len(self.msg_ids) > _SEEN_MSG_IDS_MAX:
            self.msg_ids.popitem(last=False)


def _sender_share(households: int) -> int:
    """The envelopes a minute one player may send at a table of
    *households*: three times a fair share, never less than two messages
    to everyone, never more than the table's budget."""
    fair = 3 * GROUP_SEND_BUDGET // max(2, households)
    return min(GROUP_SEND_BUDGET, max(fair, 2 * max(1, households - 1)))


def _sealed_size(msg: GroupMessage) -> int:
    """The ciphertext a directed message carries (b64url characters)."""
    return sum(len(str(e.get("ct", ""))) for e in msg.sealed)


def _batches(msgs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """*msgs* in envelopes of at most :data:`GROUP_BATCH_MAX_MESSAGES`
    messages and :data:`GROUP_BATCH_MAX_BYTES` (a bigger message alone)."""
    out: list[list[dict[str, Any]]] = []
    batch: list[dict[str, Any]] = []
    size = 0
    for msg in msgs:
        n = len(orjson.dumps(msg))
        if batch and (
            len(batch) >= GROUP_BATCH_MAX_MESSAGES or size + n > GROUP_BATCH_MAX_BYTES
        ):
            out.append(batch)
            batch, size = [], 0
        batch.append(msg)
        size += n
    if batch:
        out.append(batch)
    return out


class AppGroupService(ProtectionGateMixin):
    """Group tables: the hub's roster, the mirrors, and the rules each
    household applies for its own people. See the module docstring."""

    __slots__ = (
        "_app_fed",
        "_app_repo",
        "_budgets",
        "_bus",
        "_child_protection",
        "_coalesce_every",
        "_fed_repo",
        "_federation",
        "_key_manager",
        "_locks",
        "_out_due",
        "_outq",
        "_repo",
        "_roster_due",
        "_roster_every",
        "_roster_sent",
        "_seen",
        "_space_tables",
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
        federation_repo: AbstractFederationRepo,
        key_manager: KeyManager,
        bus: EventBus | None = None,
        roster_every: float = GROUP_ROSTER_EVERY_SECONDS,
        coalesce_every: float = GROUP_COALESCE_SECONDS,
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
        self._fed_repo = federation_repo
        self._key_manager = key_manager
        self._coalesce_every = coalesce_every
        #: Messages the hub passes on, gathered per (table, household)
        #: for :data:`GROUP_COALESCE_SECONDS`, and which wait to go.
        self._outq: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._out_due: set[tuple[str, str]] = set()
        #: The send budget of each table (the hub's, or what this
        #: household's own people send to another hub's table).
        self._budgets: dict[str, TableSendBudget] = {}
        #: The ``seq`` numbers each table's copy passed on lately (repeats
        #: are dropped) — bounded per table, least recent table forgotten.
        self._seen: OrderedDict[str, _Seen] = OrderedDict()
        #: Space tables (v_77): the space transport, cards and space rules
        #: (:meth:`attach_space_tables`).
        self._space_tables: SpaceTableService | None = None

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
        """Let the coalesced roster sends that wait go (none is sent);
        messages that wait for their envelope go now (to the outbox)."""
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
            "seq": table.seq,
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
            # The hub's own seat key, on the host's entry.
            key = await self._own_seat_key(table.session_id)
            table = table.with_entry(dataclasses.replace(table.roster[0], seat_key=key))
            await self._repo.save(table)
            hosting = self._seat_row(table, actor_user_id, state=SESSION_ACTIVE)
            await self._app_repo.add_active_session(hosting)
        others = len(picked) - 1
        for entry in table.roster:
            if entry.state != SEAT_INVITED:
                continue
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
        key = await self._own_seat_key(table.session_id)
        if table.hub_instance == self._own:
            table = await self._hub_join(
                table.session_id, self._own, user.username, seat_key=key
            )
        else:
            await self._send(
                table.hub_instance,
                table,
                VERB_JOIN,
                from_user=user.username,
                seat_key=key.to_wire(),
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
        self,
        session_id: str,
        instance: str,
        user: str,
        *,
        seat_key: SeatKey | None = None,
        newcomer: GroupSeat | None = None,
    ) -> GroupSession:
        """Seat an invited person — or say why not (raised for a local
        joiner, sent as ``group_close`` to a remote one). Someone the hub
        never invited, removed or who declined is ignored. *seat_key* is
        their household's (already checked); the first key a household
        brings to a table stays. At a space table (v_77) anyone of the
        space's members may sit: *newcomer* is their entry, checked by
        :class:`SpaceTableService` (a writer seat here, the app still
        allowed) — someone removed never comes back."""
        refusal: str | None = None
        started = False
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            entry = table.entry(instance, user)
            if (
                newcomer is not None
                and table.space_id is not None
                and newcomer.is_person(instance, user)
                and (entry is None or entry.state in (SEAT_LEFT, SEAT_DECLINED))
                # The roster's bound (seated, left and removed travel): a
                # table full of comings and goings takes nobody new.
                and (entry is not None or len(table.roster) < GROUP_ROSTER_MAX)
            ):
                entry = dataclasses.replace(newcomer, state=SEAT_INVITED, seat=0)
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
                        entry,
                        state=SEAT_SEATED,
                        seat=table.next_seat,
                        joined_at=stamp,
                        seat_key=table.seat_key_of(instance) or seat_key,
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
        await self._send_raw(
            to_instance,
            FederationEventType.APP_SESSION,
            payload,
            space_id=table.space_id,
        )

    async def _send_raw(
        self,
        to_instance: str,
        event_type: FederationEventType,
        payload: dict,
        *,
        space_id: str | None = None,
    ) -> None:
        """One sealed event to *to_instance* (best effort: the outbox
        retries; a failure is logged). A space table's (*space_id*) goes
        over the space's own transport instead (v_77)."""
        try:
            if space_id is not None:
                if self._space_tables is None:
                    raise RuntimeError("space tables not attached")
                await self._space_tables.send(
                    space_id, to_instance, event_type, payload
                )
                return
            await self._federation.send_event(
                to_instance_id=to_instance,
                event_type=event_type,
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — the change stands here
            log.warning(
                "app_group: %s for table %s to %s not sent: %s",
                payload.get("verb", "message"),
                payload.get("session_id"),
                to_instance,
                exc,
            )

    # ─── Seat keys ────────────────────────────────────────────────────

    async def _own_seat_key(self, session_id: str) -> SeatKey:
        """This household's seat key at the table — made (and kept,
        KEK-wrapped, on the table's row) the first time one of its people
        takes a seat there. One per household, never per person."""
        stored = await self._own_seat_secret(session_id)
        if stored is None:
            priv, pub = new_seat_keypair()
            wire = sign_seat_key(
                seed=self._federation.own_identity_seed,
                session_id=session_id,
                instance=self._own,
                pub=pub,
            )
            record = {
                **wire,
                "priv": self._key_manager.encrypt(
                    priv, associated_data=_seat_key_ad(session_id)
                ),
            }
            await self._repo.set_seat_key(session_id, orjson.dumps(record).decode())
            # Two joins racing: whichever key was kept first stays.
            stored = await self._own_seat_secret(session_id)
            if stored is None:
                raise AppGroupNotFoundError()
        return stored[1]

    async def _own_seat_secret(self, session_id: str) -> tuple[bytes, SeatKey] | None:
        """This household's seat key at the table: (private, public)."""
        raw = await self._repo.get_seat_key(session_id)
        if raw is None:
            return None
        record = orjson.loads(raw)
        priv = self._key_manager.decrypt(
            record["priv"], associated_data=_seat_key_ad(session_id)
        )
        return priv, SeatKey.from_wire(record)

    async def _identity_pk(self, instance: str, hint: object = None) -> bytes | None:
        """*instance*'s Ed25519 identity key: the one its pairing stores (a
        link-joined household's too); else (v_77) the key *hint* names or a
        member household of a shared space claimed over the mesh — either
        only when it derives to *instance* (a household id IS its identity
        key's fingerprint, so no hub can pass off another key). ``None``
        otherwise."""
        peer = await self._fed_repo.get_instance(instance)
        if peer is not None and peer.remote_identity_pk:
            try:
                return bytes.fromhex(peer.remote_identity_pk)
            except ValueError:
                return None
        candidates: list[object] = [hint]
        claim = await self._fed_repo.get_space_member_version(instance)
        if claim is not None:
            candidates.append(claim[1])
        for raw in candidates:
            if not isinstance(raw, str) or len(raw) != 64:
                continue
            try:
                key = bytes.fromhex(raw)
            except ValueError:
                continue
            if derive_instance_id(key) == instance:
                return key
        return None

    async def _joiner_key(
        self, session_id: str, instance: str, payload: dict
    ) -> SeatKey | None:
        """The seat key a ``group_join`` carries, checked against the
        joining household's identity key — ``None`` (the join is dropped)
        when it is missing, malformed, of an unknown suite or doesn't
        verify. The hub is paired with every household it invites."""
        raw = payload.get("seat_key")
        identity = await self._identity_pk(instance, payload.get("identity_pk"))
        try:
            if identity is None or not verify_seat_key(
                raw, session_id=session_id, instance=instance, identity_pk=identity
            ):
                raise SeatSealError("seat key doesn't verify")
            return SeatKey.from_wire(parse_seat_key(raw))
        except (
            SeatSealError,
            UnsupportedSeatKemSuite,
            UnsupportedSeatKeySigSuite,
            GroupWireError,
        ) as exc:
            log.info(
                "app_group: join for %s from %s dropped: %s", session_id, instance, exc
            )
            return None

    async def _check_seat_keys(self, table: GroupSession) -> None:
        """Every seat key in a hub's snapshot: a suite this build knows;
        a key under this household must be the one it made (none planted
        before it made one); another household's
        must verify against the identity key this household stores for it
        (a household it isn't paired with: the hub's word — the residual
        of design 2.3). Raises :class:`GroupWireError` (the snapshot is
        dropped)."""
        own = self._own
        checked: set[str] = set()
        for entry in table.roster:
            key = entry.seat_key
            if key is None or entry.instance in checked:
                continue
            checked.add(entry.instance)
            wire = key.to_wire()
            try:
                parse_seat_key(wire)
                if entry.instance == own:
                    mine = await self._own_seat_secret(table.session_id)
                    if mine is None or mine[1] != key:
                        raise SeatSealError("not our seat key")
                    continue
                identity = await self._identity_pk(entry.instance, entry.identity_pk)
                if identity is None and table.space_id is not None:
                    # A space table names every household's identity key:
                    # nothing there rests on the hub's word.
                    raise SeatSealError("seat key can't be checked")
                if identity is not None and not verify_seat_key(
                    wire,
                    session_id=table.session_id,
                    instance=entry.instance,
                    identity_pk=identity,
                ):
                    raise SeatSealError("seat key doesn't verify")
            except (
                SeatSealError,
                UnsupportedSeatKemSuite,
                UnsupportedSeatKeySigSuite,
            ) as exc:
                raise GroupWireError(f"seat key of {entry.instance}: {exc}") from exc

    # ─── Messages ─────────────────────────────────────────────────────

    async def send_message(
        self,
        *,
        app_id: str,
        session_id: str,
        actor_user_id: str,
        data: Any,
        to: list[int] | None = None,
        turn: int | None = None,
    ) -> dict:
        """A seated person's app sends *data* to everyone at the table, or
        to the seats in *to* (sealed here to each one's household seat key:
        the hub passes on only ciphertext). *turn* names the seat whose
        turn it is now ("Your turn" there). ``{"seq": n}`` when this
        household is the hub, ``{"seq": None}`` when it went to the hub's.

        :class:`AppGroupClosedError` when the table ended or the caller
        isn't seated, :class:`AppGroupSeatError` for a ``to`` / ``turn``
        seat nobody holds, one's own, or one without a seat key,
        :class:`AppGroupBusyError` past the table's send budget."""
        app = await self._usable(app_id, actor_user_id)
        table, user, row = await self._mine(app_id, session_id, actor_user_id)
        own = self._own
        if table.households() - {own} or table.hub_instance != own:
            app.require_permission(PERMISSION_FEDERATION)
        mine = table.entry(own, user.username)
        if (
            row.state != SESSION_ACTIVE
            or mine is None
            or mine.state != SEAT_SEATED
            or table.state == GROUP_ENDED
        ):
            raise AppGroupClosedError()
        seats = {e.seat: e for e in table.seated}
        targets = None if to is None else tuple(dict.fromkeys(to))
        if any(seat not in seats or seat == mine.seat for seat in targets or ()):
            raise AppGroupSeatError()
        if turn is not None and turn not in seats:
            raise AppGroupSeatError()
        msg_id = new_msg_id()
        if targets is None:
            msg = GroupMessage(msg_id=msg_id, data=data, turn=turn)
        else:
            if not targets:
                raise AppGroupSeatError()
            priv, _pub = await self._own_seat_secret_or_mint(table.session_id)
            plaintext = orjson.dumps(data)
            if len(plaintext) * len(targets) > GROUP_DIRECT_MAX_PLAINTEXT:
                raise AppGroupTooLargeError()
            sealed = []
            for seat in targets:
                key = seats[seat].seat_key
                if key is None:
                    raise AppGroupSeatError()
                try:
                    sealed.append(
                        seal(
                            plaintext,
                            my_priv=priv,
                            peer_pub=b64url_decode(key.pub),
                            session_id=table.session_id,
                            from_seat=mine.seat,
                            to_seat=seat,
                            msg_id=msg_id,
                        )
                    )
                except (SeatSealError, ValueError) as exc:
                    # A seat key that can't be sealed to (the hub's word).
                    raise AppGroupSeatError() from exc
            msg = GroupMessage(
                msg_id=msg_id, to=targets, sealed=tuple(sealed), turn=turn
            )
        now = _now()
        if row.last_change < (now - timedelta(hours=1)).isoformat():
            # The seat's 90-day quiet rule moves on (at most hourly).
            await self._app_repo.touch_session(
                app_id, actor_user_id, session_id, now=now.isoformat()
            )
        if table.hub_instance == own:
            seq = await self._hub_message(session_id, own, user.username, msg)
            return {"seq": seq}
        # This household's own budget for the table, before the hub's.
        self._spend(table, self._recipient_households(table, mine, msg), sender=mine)
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "from_user": user.username,
            "group": msg.to_hub(),
        }
        if targets is None:
            payload["data"] = data
        await self._send_raw(
            table.hub_instance,
            FederationEventType.APP_MESSAGE,
            payload,
            space_id=table.space_id,
        )
        return {"seq": None}

    async def _own_seat_secret_or_mint(self, session_id: str) -> tuple[bytes, SeatKey]:
        await self._own_seat_key(session_id)
        secret = await self._own_seat_secret(session_id)
        assert secret is not None  # kept just now
        return secret

    def _recipient_households(
        self, table: GroupSession, sender: GroupSeat, msg: GroupMessage
    ) -> set[str]:
        """The households (not the hub's own) the hub sends *msg* to: those
        of everyone seated it is for — never the sender themself."""
        return {
            e.instance
            for e in table.seated
            if not e.is_person(sender.instance, sender.user)
            and (msg.to is None or e.seat in msg.to)
        } - {table.hub_instance}

    def _spend(
        self, table: GroupSession, households: set[str], *, sender: GroupSeat
    ) -> None:
        """Spend one envelope per household of the sender's share and of
        the table's send budget — or :class:`AppGroupBusyError`. The share
        (:func:`_sender_share`) keeps one player from using up the table."""
        now = asyncio.get_running_loop().time()
        cost = len(households)
        if cost <= 0:
            return
        mine_key = f"{table.session_id}|{sender.instance}|{sender.user}"
        mine = self._budgets.get(mine_key)
        if mine is None:
            mine = self._budgets[mine_key] = TableSendBudget(
                limit=_sender_share(len(table.households()))
            )
        whole = self._budgets.get(table.session_id)
        if whole is None:
            whole = self._budgets[table.session_id] = TableSendBudget()
        wait = mine.take(cost, now)
        if wait is None:
            wait = whole.take(cost, now)
            if wait is not None:
                mine.refund(cost, now)
        if wait is not None:
            raise AppGroupBusyError(wait)

    async def _hub_message(
        self, session_id: str, instance: str, user: str, msg: GroupMessage
    ) -> int:
        """The hub takes a message from *user* of *instance*: seated now,
        at a table not ended, every ``to`` / ``turn`` seat seated (never
        the sender's own as a target), sealed entries of a suite this build
        knows, within the size and the table's send budget. Numbers it
        (``seq``) and queues it, one envelope per household it is for; the
        hub's own people get it at once. Returns the ``seq``."""
        async with self._lock(session_id):
            table = await self._hub_table(session_id)
            if table is None:
                raise AppGroupNotFoundError()
            sender = table.entry(instance, user)
            if (
                sender is None
                or sender.state != SEAT_SEATED
                or table.state == GROUP_ENDED
            ):
                raise AppGroupClosedError()
            seated = {e.seat for e in table.seated}
            if msg.to is not None and (
                not set(msg.to) <= seated or sender.seat in msg.to
            ):
                raise AppGroupSeatError()
            if msg.turn is not None and msg.turn not in seated:
                raise AppGroupSeatError()
            if msg.to is None:
                if len(orjson.dumps(msg.data)) > GROUP_MESSAGE_MAX_BYTES:
                    raise GroupWireError("message too large")
            else:
                try:
                    for entry in msg.sealed:
                        check_sealed_entry(entry)
                except (SeatSealError, UnsupportedSeatKemSuite) as exc:
                    raise GroupWireError(f"sealed entry: {exc}") from exc
            households = self._recipient_households(table, sender, msg)
            self._spend(table, households, sender=sender)
            if msg.to is not None and _sealed_size(msg) > GROUP_DIRECT_MAX_BYTES:
                raise GroupWireError("sealed entries too large")
            if table.session_id in self._roster_due:
                # Someone sat down and the coalesced roster still waits: it
                # goes first, so every household knows the seats (and their
                # keys) this message names.
                self._roster_sent[session_id] = asyncio.get_running_loop().time()
                await self._broadcast(
                    table, VERB_ROSTER, states=(SEAT_SEATED, SEAT_INVITED)
                )
            seq = table.seq + 1
            stamp = _now().isoformat()
            await self._repo.raise_seq(session_id, seq, now=stamp)
            table = dataclasses.replace(table, seq=seq, updated_at=stamp)
            numbered = dataclasses.replace(msg, seq=seq, from_seat=sender.seat)
            for household in sorted(households):
                for_seats = frozenset(
                    e.seat
                    for e in table.seated
                    if e.instance == household
                    and not e.is_person(instance, user)
                    and (msg.to is None or e.seat in msg.to)
                )
                self._queue_out(table, household, numbered.relayed(for_seats))
            # Under the lock: the hub's own people get them in ``seq`` order.
            await self._deliver_local(table, numbered)
        return seq

    def _queue_out(self, table: GroupSession, household: str, msg: dict) -> None:
        """Gather *msg* for *household* — it goes with whatever else the
        hub passes it on this table within :data:`GROUP_COALESCE_SECONDS`."""
        key = (table.session_id, household)
        self._outq.setdefault(key, []).append(msg)
        if key in self._out_due:
            return
        self._out_due.add(key)
        task = asyncio.create_task(self._flush_later(key, table.app_id, table.space_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _flush_later(
        self, key: tuple[str, str], app_id: str, space_id: str | None = None
    ) -> None:
        session_id, household = key
        try:
            if self._coalesce_every > 0 and not self._stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._coalesce_every
                    )
                except TimeoutError:
                    pass
        finally:
            self._out_due.discard(key)
        # One flush per household and table at a time: batches leave in
        # ``seq`` order.
        async with self._lock(f"out:{session_id}:{household}"):
            msgs = self._outq.pop(key, [])
            for batch in _batches(msgs):
                await self._send_raw(
                    household,
                    FederationEventType.APP_MESSAGE,
                    {
                        "app_id": app_id,
                        "session_id": session_id,
                        "group": {"msgs": batch},
                    },
                    space_id=space_id,
                )

    async def on_inbound_message(self, event: FederationEvent) -> None:
        """An inbound ``APP_MESSAGE`` with a ``group`` object (after the
        §24.11 pipeline): a player's message to this hub, or a hub's batch
        for this household's people. Anything malformed, from a household
        below v_76, or not from who may send it is dropped with a log line
        — nothing goes back. A space table's arrive only through
        :meth:`on_space_message`."""
        if event.space_id is not None:
            log.debug("app_group: space-routed message from %s", event.from_instance)
            return
        if not await self._federation.peer_supports(
            event.from_instance, min_version=FederationCapability.MIN_FOR_APP_GROUPS
        ):
            log.info(
                "app_group: message from %s below v_76 dropped", event.from_instance
            )
            return
        await self._on_message(event, space_id=None)

    async def _on_message(
        self, event: FederationEvent, *, space_id: str | None
    ) -> None:
        p = event.payload
        app_id, session_id = p.get("app_id"), p.get("session_id")
        if (
            not isinstance(app_id, str)
            or not app_id
            or not isinstance(session_id, str)
            or not _SESSION_RE.fullmatch(session_id)
        ):
            log.debug("app_group: malformed message from %s", event.from_instance)
            return
        table = await self._repo.get(session_id)
        if table is None or table.app_id != app_id or table.space_id != space_id:
            # A space table's messages come only over its space, a picked
            # table's never.
            log.debug("app_group: message for unknown table %s", session_id)
            return
        group = p.get("group")
        try:
            if table.hub_instance == self._own:
                actor = p.get("from_user")
                if not isinstance(actor, str) or not actor:
                    raise GroupWireError("from_user missing")
                msg = parse_message(
                    group, data=p.get("data"), has_data="data" in p, relayed=False
                )
                await self._quietly(
                    self._hub_message(session_id, event.from_instance, actor, msg)
                )
            elif table.hub_instance == event.from_instance:
                for msg in parse_batch(group):
                    await self._on_relayed(table, msg)
            else:
                log.info(
                    "app_group: message for %s from %s (not its hub) dropped",
                    session_id,
                    event.from_instance,
                )
        except GroupWireError as exc:
            log.info(
                "app_group: message from %s for %s dropped: %s",
                event.from_instance,
                session_id,
                exc,
            )

    async def _on_relayed(self, table: GroupSession, msg: GroupMessage) -> None:
        """One message the table's hub passed on: once per ``seq`` (a
        repeat, or one far below the newest seen, is dropped), then to
        this household's people it is for."""
        if table.state == GROUP_ENDED:
            return
        seen = self._seen.get(table.session_id)
        if seen is None:
            seen = self._seen[table.session_id] = _Seen()
        self._seen.move_to_end(table.session_id)
        while len(self._seen) > 4 * GROUP_MIRRORS_PER_HUB_MAX:
            self._seen.popitem(last=False)
        newest = max([table.seq, *seen.seqs])
        # A repeat by ``seq`` — or the same sender's ``msg_id`` under a new
        # ``seq`` (a hub replaying a sealed message: ``seq`` isn't sealed).
        mid = (msg.from_seat, msg.msg_id)
        if (
            msg.seq in seen.seqs
            or mid in seen.msg_ids
            or msg.seq <= newest - GROUP_SEQ_WINDOW
        ):
            log.info(
                "app_group: seq %s at %s seen already — dropped",
                msg.seq,
                table.session_id,
            )
            return
        seen.add(msg.seq, mid, max(newest, msg.seq) - GROUP_SEQ_WINDOW)
        await self._repo.raise_seq(table.session_id, msg.seq, now=_now().isoformat())
        try:
            await self._deliver_local(table, msg)
        except Exception:
            log.exception(
                "app_group: seq %s at %s not delivered", msg.seq, table.session_id
            )

    async def _deliver_local(self, table: GroupSession, msg: GroupMessage) -> None:
        """Hand *msg* to each person of this household seated at *table*
        it is for (not the sender): a directed one opened with this
        household's seat key; never to someone who blocked the sender or
        whom a guardian block (or the children's rule) separates from them.
        Live to their app, kept in their mailbox (≤ 16 KiB, the latest
        per sender seat), and "Your turn" when ``turn`` names their seat."""
        sender = table.at_seat(msg.from_seat)
        if sender is None or sender.state != SEAT_SEATED:
            log.info(
                "app_group: message from unknown seat %s at %s dropped",
                msg.from_seat,
                table.session_id,
            )
            return
        app = await self._app_repo.get(table.app_id)
        if app is None:
            return
        own = self._own
        sender_id = await self._person_id(sender)
        secret: tuple[bytes, SeatKey] | None = None
        for user, row in await self._local_people(table):
            entry = table.entry(own, user.username)
            if (
                row.state != SESSION_ACTIVE
                or entry is None
                or entry.state != SEAT_SEATED
                or entry.is_person(sender.instance, sender.user)
                or (msg.to is not None and entry.seat not in msg.to)
            ):
                continue
            if not await self._may_hear(user, sender, sender_id):
                continue
            data = msg.data
            if msg.to is not None:
                if secret is None:
                    secret = await self._own_seat_secret(table.session_id)
                data = self._open(table, msg, entry.seat, sender, secret)
                if data is _UNOPENED:
                    continue
            await self._hand_over(table, app, user, msg, data, sender, sender_id)

    def _open(
        self,
        table: GroupSession,
        msg: GroupMessage,
        seat: int,
        sender: GroupSeat,
        secret: tuple[bytes, SeatKey] | None,
    ) -> Any:
        """The data of the sealed entry for *seat*, or :data:`_UNOPENED`."""
        entry = next((e for e in msg.sealed if e.get("to_seat") == seat), None)
        if entry is None or secret is None or sender.seat_key is None:
            log.info(
                "app_group: directed seq %s at %s can't be opened here",
                msg.seq,
                table.session_id,
            )
            return _UNOPENED
        try:
            raw = open_sealed(
                entry,
                my_priv=secret[0],
                peer_pub=b64url_decode(sender.seat_key.pub),
                session_id=table.session_id,
                from_seat=msg.from_seat,
                msg_id=msg.msg_id,
            )
            return orjson.loads(raw)
        except (SeatSealError, UnsupportedSeatKemSuite, orjson.JSONDecodeError) as exc:
            log.info(
                "app_group: directed seq %s at %s dropped: %s",
                msg.seq,
                table.session_id,
                exc,
            )
            return _UNOPENED

    async def _person_id(self, entry: GroupSeat) -> str | None:
        """The local or ``remote_users`` id of a seat's person, if known."""
        if entry.instance == self._own:
            local = await self._user_repo.get(entry.user)
            return local.user_id if local is not None else None
        remote = await self._user_repo.get_remote_by_member(entry.instance, entry.user)
        return remote.user_id if remote is not None else None

    async def _may_hear(
        self, user: User, sender: GroupSeat, sender_id: str | None
    ) -> bool:
        """Whether *user* takes a message from *sender*: no block either
        way here, no guardian block, and a protected account only from a
        household it may message."""
        if sender.instance != self._own and not await self._may_message_household(
            user.user_id, sender.instance
        ):
            return False
        if sender_id is None:
            return True
        return not (
            await self._user_repo.is_blocked(user.user_id, sender_id)
            or await self._user_repo.is_blocked(sender_id, user.user_id)
            or await self._guardian_blocked(user.user_id, sender_id)
        )

    async def _hand_over(
        self,
        table: GroupSession,
        app: InstalledApp,
        user: User,
        msg: GroupMessage,
        data: Any,
        sender: GroupSeat,
        sender_id: str | None,
    ) -> None:
        if app.enabled:
            await self._ws.broadcast_to_user(
                user.user_id,
                {
                    "type": WS_MESSAGE_FRAME_TYPE,
                    "app_id": table.app_id,
                    "session_id": table.session_id,
                    "seq": msg.seq,
                    "from_seat": msg.from_seat,
                    "direct": msg.direct,
                    "data": data,
                },
            )
        held = {"data": data, "direct": msg.direct}
        kept = False
        if len(orjson.dumps(held)) <= GROUP_HELD_MAX_BYTES:
            kept = await self._repo.hold_group_message(
                table.app_id,
                user.user_id,
                table.session_id,
                from_seat=msg.from_seat,
                seq=msg.seq,
                held=held,
                now=_now().isoformat(),
            )
        mine = table.entry(self._own, user.username)
        if (
            app.enabled
            and msg.turn is not None
            and mine is not None
            and msg.turn == mine.seat
        ):
            await self._emit(
                AppTurn(
                    app_id=table.app_id,
                    session_id=table.session_id,
                    to_user_id=user.user_id,
                    from_user_id=sender_id,
                    from_display=with_household(sender.name, sender.household_name),
                )
            )
        if not kept:
            log.debug(
                "app_group: seq %s at %s live only (too large or not seated)",
                msg.seq,
                table.session_id,
            )

    async def take_held(self, *, app_id: str, actor_user_id: str) -> list[dict]:
        """The person's app opened: each player's latest message at the
        tables they sit at (≤ 16 KiB each), oldest ``seq`` first per table
        — cleared as they are handed over. Their "Your turn" rows for
        those tables read as read."""
        await self._usable(app_id, actor_user_id)
        taken = await self._repo.take_group_held(app_id, actor_user_id)
        if taken:
            await self._emit(
                AppTurnsSeen(
                    app_id=app_id,
                    user_id=actor_user_id,
                    session_ids=tuple(dict.fromkeys(h.session_id for h in taken)),
                )
            )
        return [
            {
                "session_id": h.session_id,
                "seq": h.seq,
                "from_seat": h.from_seat,
                "direct": bool(h.held.get("direct")),
                "data": h.held.get("data"),
            }
            for h in taken
        ]

    # ─── Inbound ──────────────────────────────────────────────────────

    async def on_inbound_event(self, event: FederationEvent) -> None:
        """An inbound ``group_*`` verb (after the §24.11 pipeline). Anything
        malformed, from a household below v_76, or not from who may send
        it is dropped with a log line — nothing goes back. A space table's
        arrive only through :meth:`on_space_session`."""
        if event.space_id is not None:
            log.debug("app_group: space-routed verb from %s", event.from_instance)
            return
        if not await self._federation.peer_supports(
            event.from_instance, min_version=FederationCapability.MIN_FOR_APP_GROUPS
        ):
            log.info("app_group: verb from %s below v_76 dropped", event.from_instance)
            return
        await self._on_session(event, space_id=None)

    async def _on_session(
        self,
        event: FederationEvent,
        *,
        space_id: str | None,
        newcomer: GroupSeat | None = None,
    ) -> None:
        p = event.payload
        app_id, session_id, verb = p.get("app_id"), p.get("session_id"), p.get("verb")
        if (
            not isinstance(app_id, str)
            or not app_id
            or not isinstance(session_id, str)
            or not _SESSION_RE.fullmatch(session_id)
        ):
            log.debug("app_group: malformed %s from %s", verb, event.from_instance)
            return
        try:
            await self._dispatch(
                event,
                app_id,
                session_id,
                str(verb),
                space_id=space_id,
                newcomer=newcomer,
            )
        except GroupWireError as exc:
            log.info(
                "app_group: %s from %s for %s dropped: %s",
                verb,
                event.from_instance,
                session_id,
                exc,
            )

    async def _dispatch(
        self,
        event: FederationEvent,
        app_id: str,
        session_id: str,
        verb: str,
        *,
        space_id: str | None = None,
        newcomer: GroupSeat | None = None,
    ) -> None:
        table = await self._repo.get(session_id)
        if table is not None and (table.app_id != app_id or table.space_id != space_id):
            # A space table speaks only over its space, a picked one never.
            return
        if verb == "group_invite" and space_id is not None:
            return  # a space table invites nobody: members join it
        is_hub = table is not None and table.hub_instance == self._own
        sender = event.from_instance
        actor = event.payload.get("from_user")
        actor = actor if isinstance(actor, str) and actor else None
        match verb:
            case "group_invite":
                await self._on_invite(event, app_id, session_id, table)
            case "group_join" if is_hub and actor:
                key = await self._joiner_key(session_id, sender, event.payload)
                if key is not None:
                    await self._quietly(
                        self._hub_join(
                            session_id, sender, actor, seat_key=key, newcomer=newcomer
                        )
                    )
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
            AppGroupSeatError,
            AppGroupBusyError,
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
        await self._check_seat_keys(table)
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
        # A table's space never changes (the snapshot doesn't carry it).
        table = dataclasses.replace(table, space_id=mirror.space_id)
        await self._check_seat_keys(table)
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
            ids: set[str] = set()
            if e.instance == self._own:
                other = await self._user_repo.get(e.user)
                if other is None:
                    continue
                if await self._user_repo.is_blocked(other.user_id, user.user_id):
                    return False
                ids.add(other.user_id)
            else:
                # D8: at a private space's table a protected account plays
                # with the people it already chats with there (a public
                # space seats no protected account at all).
                if table.space_id is None and not await self._may_message_household(
                    user.user_id, e.instance
                ):
                    return False
                remote = await self._user_repo.get_remote_by_member(e.instance, e.user)
                if remote is not None:
                    ids.add(remote.user_id)
                # At a space table a player is also named by the user id
                # the space knows them by (v_77) — a household this one isn't
                # paired with is known by nothing else.
                if e.user_id is not None:
                    ids.add(e.user_id)
            for other_id in ids:
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
        """Each local person at *table* gets their view (``app.group``);
        a space table's card follows the change (v_77)."""
        if table.space_id is not None and self._space_tables is not None:
            await self._space_tables.on_table_changed(table)
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
        clock = asyncio.get_running_loop().time()
        for sid in list(self._budgets):
            if self._budgets[sid].idle(clock):
                del self._budgets[sid]
        for sid in list(self._seen):
            gone = await self._repo.get(sid)
            if gone is None or gone.state == GROUP_ENDED:
                self._seen.pop(sid, None)
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
        hub, each person from here who sat leaves. Then the tables go —
        except a space table someone else hosts, whose card stays (its
        members may still ask for the app, v_77)."""
        for table in await self._repo.list_for_app(event.app_id):
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
            if table.space_id is not None and table.hub_instance != self._own:
                continue
            await self._repo.delete(table.session_id)
            self._roster_sent.pop(table.session_id, None)
            self._budgets.pop(table.session_id, None)
            self._seen.pop(table.session_id, None)

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

    # ─── Space tables (v_77) — driven by SpaceTableService ────────────

    @property
    def _own_identity_hex(self) -> str:
        return bytes(self._federation.own_identity_pk).hex()

    def attach_space_tables(self, space_tables: SpaceTableService) -> None:
        """Wire space tables: their sends go over the space's own
        transport, and each change of one moves its card on."""
        self._space_tables = space_tables

    async def open_space_table(
        self,
        *,
        app: InstalledApp,
        host: User,
        space_id: str,
        max_players: int,
        lobby_days: int,
        note: str | None,
    ) -> GroupSession:
        """Open a table in *space_id* with *host* in seat 1 — nobody is
        invited: the space's members join it. The caller checked the space
        (a writer seat, the app on its list) and its own caps; this checks
        the table caps of the app (:class:`AppGroupLimitError`)."""
        clean_note = note_ok(note)
        if lobby_days not in GROUP_LOBBY_DAYS:
            raise ValueError("lobby_days must be one of 1, 3, 7, 14")
        async with self._app_fed.invite_lock(app.app_id, host.user_id):
            await self._check_table_caps(app.app_id, host.username)
            now = _now()
            stamp = now.isoformat()
            table = GroupSession(
                session_id=uuid4().hex,
                app_id=app.app_id,
                host_instance=self._own,
                host_user=host.username,
                hub_instance=self._own,
                state=GROUP_LOBBY,
                min_players=app.manifest.players_min,
                max_players=max_players,
                roster=(
                    GroupSeat(
                        instance=self._own,
                        user=host.username,
                        name=host.display_name,
                        state=SEAT_SEATED,
                        seat=1,
                        joined_at=stamp,
                        user_id=host.user_id,
                        identity_pk=self._own_identity_hex,
                    ),
                ),
                roster_rev=1,
                created_at=stamp,
                expires_at=(now + timedelta(days=lobby_days)).isoformat(),
                updated_at=stamp,
                space_id=space_id,
                late_join=app.manifest.late_join,
                note=clean_note,
                opener_user=host.username,
            )
            await self._repo.save(table)
            key = await self._own_seat_key(table.session_id)
            table = table.with_entry(dataclasses.replace(table.roster[0], seat_key=key))
            await self._repo.save(table)
            await self._app_repo.add_active_session(
                self._seat_row(table, host.user_id, state=SESSION_ACTIVE)
            )
        return table

    async def join_space_table(
        self, *, table: GroupSession, user: User, name: str | None = None
    ) -> dict:
        """*user* takes a seat at a space table this household knows (its
        card, or a copy). The caller checked the space side; this checks
        the person's own rules (:class:`AppGroupCantJoinError`, no reason)
        and asks the hub — at once when the hub is here
        (:class:`AppGroupFullError` / :class:`AppGroupClosedError`)."""
        if table.space_id is None or not table.is_open:
            raise AppGroupClosedError()
        if not await self._may_play(user, table):
            raise AppGroupCantJoinError()
        existing = await self._app_repo.get_pending_session(
            table.app_id, user.user_id, table.session_id
        )
        if existing is not None and existing.is_group:
            return self.view(table, user, existing)
        key = await self._own_seat_key(table.session_id)
        newcomer = GroupSeat(
            instance=self._own,
            user=user.username,
            name=user.display_name,
            state=SEAT_INVITED,
            user_id=user.user_id,
            identity_pk=self._own_identity_hex,
        )
        row = self._seat_row(table, user.user_id, state=SESSION_ACTIVE)
        if table.hub_instance == self._own:
            table = await self._hub_join(
                table.session_id,
                self._own,
                user.username,
                seat_key=key,
                newcomer=newcomer,
            )
            await self._app_repo.add_active_session(row)
        else:
            await self._app_repo.add_active_session(row)
            await self._send(
                table.hub_instance,
                table,
                VERB_JOIN,
                from_user=user.username,
                user_id=user.user_id,
                name=name or user.display_name,
                identity_pk=self._own_identity_hex,
                seat_key=key.to_wire(),
                snap=False,
            )
        await self._push(table)
        return self.view(table, user, row)

    async def drop_space_player(self, session_id: str, entry: GroupSeat) -> None:
        """Take *entry* out of a space table this household hosts because
        they left the space (removed, banned, made a follower): the host
        person as if they left, anyone else as if the host removed them."""
        table = await self._hub_table(session_id)
        if table is None or table.state == GROUP_ENDED:
            return
        current = table.entry(entry.instance, entry.user)
        if current is None or current.state != SEAT_SEATED:
            return
        if table.is_host(entry.instance, entry.user):
            await self._hub_leave(
                session_id, entry.instance, entry.user, declined=False
            )
            return
        await self._quietly(
            self._hub_remove(
                session_id, table.host_instance, table.host_user, current.seat
            )
        )

    async def end_space_table(
        self, table: GroupSession, reason: str, *, only_lobby: bool = False
    ) -> None:
        """End a space table this household hosts for everyone (a
        moderator closed it, its app left the space's list). With
        *only_lobby*, a table that started meanwhile plays on (D10)."""
        if only_lobby:
            current = await self._hub_table(table.session_id)
            if current is None or current.state != GROUP_LOBBY:
                return
        await self._hub_end(table, reason)

    async def on_space_session(
        self, event: FederationEvent, *, newcomer: GroupSeat | None = None
    ) -> None:
        """A space table's ``group_*`` verb that came over its space —
        :class:`SpaceTableService` checked the space, the sender's writer
        seat and (for a join) *newcomer*."""
        await self._on_session(event, space_id=event.space_id, newcomer=newcomer)

    async def on_space_message(self, event: FederationEvent) -> None:
        """A space table's group message that came over its space."""
        await self._on_message(event, space_id=event.space_id)

    async def local_people(
        self, table: GroupSession
    ) -> list[tuple[User, AppPendingSession]]:
        """The people of this household with a place at *table*."""
        return await self._local_people(table)
