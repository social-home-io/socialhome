"""AppFederationService — cross-household Social Home Apps federation bridge.

Translates between the federation layer (inbound events + binary frames from
``fed-app-v1``) and the SPA layer (WebSocket pushes to local users).

Security invariants
-------------------
* ``require_enabled`` gates every outbound — an uninstalled or disabled app
  cannot initiate or send federation messages.
* Inbound delivery is fail-soft: if the app is not installed or not enabled,
  nothing reaches the app and nothing goes back to the peer (no error, no
  "unavailable"). An invite addressed to one person still reaches that
  person (D10): it waits as their "Needs you" row with "Ask an admin"
  (:meth:`_keep_waiting`) and becomes a normal invite once the app is
  added or turned on (:meth:`on_app_usable`). A group game's invite
  (v_76) waits the same way (:meth:`keep_group_waiting`; the table's
  service keeps its copy of the table and makes it a Join once the app
  is here). Anything else is dropped after a debug log.
* No application payload ever appears in plaintext in a federation envelope.
  Both the binary and JSON paths rely on :class:`FederationService` for
  encryption; this service only passes dicts down to the send methods and
  never touches the wire format.

The ``session_id`` returned by :meth:`open_session` is the de-facto game /
whiteboard / mini-app session namespace.  All messages carry it so the SPA can
dispatch to the correct in-page component without any server-side state.

Person-addressed sessions: :meth:`open_session` and :meth:`send_message` both
target a specific person (local or remote).  A local-loopback delivers a frame
straight to the target and initiator over WebSocket (no federation send); a
remote open sends an ``APP_SESSION`` event and a remote message goes via
``send_app_message``, both carrying ``to_user`` / ``from_user`` to v_18+ peers
(gated on :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING`) so the peer
can route to the addressed person.

Inbound routing: when an inbound JSON event or binary ``fed-app-v1`` frame
carries a non-empty ``to_user``, :meth:`_deliver` delivers only to that local
user (and names the sender as ``from_user`` on the frame); without one (an
older sender) it falls back to the household fan-out and the app's
``session_id`` scopes the semantics for the SPA.

Game invites: an open addressed to one person is kept for them
(``app_pending_sessions``, local and remote opens alike) until the invitee
plays it (:meth:`accept_session` — "Play" in "Needs you",
one invite at a time — "accepted"; only Play, never a message in it;
opening the app accepts nothing), the invitee declines it
(:meth:`decline_session`), the challenger withdraws it
(:meth:`cancel_session`, or an inbound ``APP_SESSION {verb:"close"}`` from
the household and person that opened it) or it expires
(:data:`~socialhome.domain.apps.PENDING_SESSION_TTL_SECONDS`). Each end but
the expiry publishes :class:`AppChallengeEnded`, which takes the "Needs you"
item down. A decline / withdrawal tells the other side with an
``APP_SESSION {verb:"close", reason}`` — the existing session-control verb.

Invites go through Social Home (D3). The app's bridge offers no way to
open a session: only Social Home's invite sheet does, with the people
:meth:`list_invitable` lists (the roster never reaches the app). An open
is refused (``AppContactNotFoundError``) for anyone not in that list and
(``AppInviteLimitError``) past the invite caps — at most
:data:`~socialhome.domain.apps.INVITES_OPEN_MAX` unanswered invites and
:data:`~socialhome.domain.apps.INVITES_PER_DAY_MAX` a day per person and
app, counted from the session rows. An app knows only who was picked and
who the person plays with (:meth:`list_contacts`: the other side of the
sessions this household knows for them), and :meth:`send_message` /
:meth:`cancel_session` reach only those sessions
(``AppSessionUnknownError``): never a new session id to an old opponent.
A household-addressed target (``user_ref == ""``) names nobody and is
refused. A protected account (§CP.F3, D5) plays only with people it could
already message — its picker, its sends and what reaches it.

Guardian blocks (§CP.F2, via :class:`ProtectionGateMixin`): like calls and
DMs, apps never connect two people a guardian block separates — they are
missing from each other's contacts, an open / move / withdrawal between them
is refused on the way out, an inbound one is dropped on receipt (per-person
and household fan-out alike), and an invite that waited from before the
block is dropped when the app drains it.

The mailbox (D11): the household keeps, per person and per game it knows
(``app_pending_sessions``: an invite they got, a game they played or
sent), the **latest** message from the other side of that game that their
app hasn't taken yet — a move, a decline, a withdrawal. It is kept whether
the app is open, closed or turned off (the server can't see whether a tab
has the app mounted), and handed over when the app next opens
(:meth:`take_held`), then cleared. A decline also gives the challenger a
bell row. Nothing more goes over the wire: the mailbox only keeps what was
sent anyway. A message for a session the household doesn't know for that
person — or from anyone but its other side — is never kept.

"Your turn" (owner decision 2026-10-30): an app may flag a message
``turn: true`` (:meth:`send_message`) — the move hands the other
player the turn. The flag rides the sealed message beside
``to_user`` (an older household ignores it). When such a message is
kept in the person's mailbox for a game they play (``active``) with
exactly that sender, in an app turned on here, :class:`AppTurn` is
published (one "Your turn" row per game); when their app takes its
held messages, :class:`AppTurnsSeen` names the games it took. Without
the flag a message is informational and raises nothing.

Uninstall (D9): before the rows go with the app (FK cascade),
:meth:`sessions_ending` lists every game and invite a person here has
with someone in another household; :meth:`preview_uninstall` turns them
into the dialog's counts and :meth:`tell_opponents` tells each of those
households — ONE ``APP_SESSION {verb: "close", reason: "ended", sessions:
[...]}`` per household (v_72, :data:`FederationCapability.MIN_FOR_APP_SESSION_ENDED`),
or, below v_72, the per-session ``withdrawn`` close it already knows for
each pending invite and nothing for a game. The receiver ends only
sessions its own person plays with exactly that household and person,
and tells the player once per opponent (:class:`AppGameEnded`).

Per move, the sender's session row stands in for the roster scan: a
message in a game this household knows with that very person needs one
primary-key read plus the block checks, not the whole contact list.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4
from weakref import WeakValueDictionary

import orjson

from ..domain.app_group import GROUP_VERBS, AppGroupSessionError
from ..domain.apps import (
    ACTIVE_SESSION_TTL_SECONDS,
    CLOSE_DECLINED,
    CLOSE_ENDED,
    CLOSE_WITHDRAWN,
    ENDED_SESSIONS_MAX,
    HELD_MESSAGE_MAX_BYTES,
    INVITES_OPEN_MAX,
    INVITES_PER_DAY_MAX,
    PENDING_SESSION_TTL_SECONDS,
    PERMISSION_FEDERATION,
    PERMISSION_PEOPLE,
    SESSION_ACTIVE,
    SESSION_INVITED,
    SESSION_MARKS,
    SESSION_VERB_CONTINUE,
    UNINSTALL_GAME_RECENT_SECONDS,
    UNINSTALL_NAMES_MAX,
    WAITING_SENDERS_MAX,
    AppAgeRestrictedError,
    AppChallengeHandledError,
    AppContactNotFoundError,
    AppInviteLimitError,
    AppNotEnabledError,
    AppNotFoundError,
    AppPendingSession,
    AppSessionTakenError,
    AppSessionUnknownError,
    AppUninstallPreview,
)
from ..domain.events import (
    AppChallengeDeclined,
    AppChallengeEnded,
    AppChallengeReceived,
    AppChallengeWaiting,
    AppEnabledChanged,
    AppGameEnded,
    AppInstalled,
    AppTurn,
    AppTurnsSeen,
)
from ..domain.federation import FederationEvent, FederationEventType
from ..domain.federation_capabilities import FederationCapability
from ..domain.notification import NotificationObject, object_key
from ..domain.space import WRITER_ROLES, SpaceType
from ..repositories._spec import Spec
from .protection_gate import ProtectionGateMixin

if TYPE_CHECKING:
    from ..domain.apps import AppCatalogEntry, InstalledApp
    from ..domain.events import DomainEvent
    from ..domain.notification import Notification
    from ..domain.user import RemoteUser, User
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_repo import AbstractAppRepo
    from ..repositories.cp_repo import AbstractCpRepo
    from ..repositories.federation_repo import AbstractFederationRepo
    from ..repositories.notification_repo import AbstractNotificationRepo
    from ..repositories.space_remote_member_repo import (
        AbstractSpaceRemoteMemberRepo,
    )
    from ..repositories.space_repo import AbstractSpaceRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .app_group_service import AppGroupService
    from .app_service import AppService
    from .notification_delivery_worker import NotificationDeliveryWorker

#: The "Needs you" row of a game invite that waits for its app (D10).
WAITING_INVITE_TYPE = "app_challenge_waiting"

#: A session id a waiting invite may carry — what this household's own
#: senders mint (``uuid4().hex``) and the SPA's ``?session=`` accepts; it
#: becomes part of the row's subject (``app_session:<app>/<session>``).
_WAITING_SESSION_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: Cap on the bounded inbound de-dupe of ``APP_SESSION`` opens — keeps memory
#: flat (LRU eviction of the oldest session id) while still suppressing the
#: realistic double-delivery window (WebRTC + HTTPS fallback, retransmits).
_SEEN_OPEN_SESSIONS_MAX = 4096

#: A game's row has its TTL moved on by the person's own moves at most once
#: in this long — one write an hour per game, not one per move.
_TOUCH_EVERY = timedelta(hours=1)

log = logging.getLogger(__name__)


def with_household(person: str, household: str | None) -> str:
    """``"Ben · Familie Okafor"`` — a person from another household, named
    with it so two people called "Admin" in two households read apart.
    Just *person* when the household is unknown or has the same name."""
    if not household or household == person:
        return person
    return f"{person} · {household}"


def _is_decline(kind: str, payload: dict) -> bool:
    """Whether an app frame is the invitee's "no": ``{verb: "close",
    reason: "declined"}`` on the session channel."""
    return (
        kind == "session"
        and payload.get("verb") == "close"
        and payload.get("reason") == "declined"
    )


def _ending_kind(
    row: AppPendingSession, own_instance: str, recent_cut: str
) -> str | None:
    """What an uninstall ends in *row*: ``"game"`` (being played),
    ``"invite"`` (waiting, either way) or ``None`` — a session with
    someone in this household (the uninstall reaches them itself), an
    invite this person took back, a game the other side already closed
    (its close waits in the mailbox), or a game nobody moved in since
    *recent_cut* (:data:`UNINSTALL_GAME_RECENT_SECONDS`: Social Home can't
    tell a finished game from a resting one, so a quiet game is left to
    the session TTL instead of counted and announced as "ended")."""
    if row.from_instance == own_instance or not row.from_user or row.is_group:
        # A group table ends through its own service (v_76).
        return None
    if row.state == SESSION_INVITED:
        return "invite"
    if row.withdrawn:
        return None
    if row.closed_by_other:
        return None
    if row.sent and not row.answered:
        return "invite"
    if row.last_change < recent_cut:
        return None
    return "game"


def _target_permission(target: dict) -> str:
    """The permission a game with *target* takes: ``people`` for someone
    in this household, ``federation`` for anyone in another one."""
    return PERMISSION_PEOPLE if target.get("is_local") else PERMISSION_FEDERATION


class AppFederationService(ProtectionGateMixin):
    """Bridge between the federation layer and Social Home Apps.

    Constructor parameters are injected; no I/O of its own beyond what the
    injected services provide.

    Parameters
    ----------
    app_repo:
        Installed-apps registry — used to validate that an app exists
        and is enabled before any outbound or inbound delivery.
    user_repo:
        Local user directory — used to enumerate user ids for WebSocket
        fan-out on inbound messages.
    ws:
        WebSocket manager — delivers ``app.message`` frames to all open
        connections.
    federation:
        The core :class:`FederationService` — used for outbound sends
        (``send_event`` for session control, ``send_app_message`` for
        data messages).
    federation_repo:
        Federation peer directory — used by :meth:`list_peers`.
    notifications:
        The notification rows — read only, to find the invites that wait
        for their app (D10, ``app_challenge_waiting``); ``None`` keeps
        dropping such invites as before.
    catalog:
        The app service whose catalog names an app this household doesn't
        have and says whether its age gate lets the invitee see it.
    spaces / space_remote_members:
        The caller's spaces and their seats (local / other households') —
        read only, for the invite sheet's "pick everyone from a space"
        (:meth:`invite_sheet`); without them the sheet offers no spaces.
    """

    __slots__ = (
        "_app_repo",
        "_bus",
        "_catalog",
        "_child_protection",
        "_cp_repo",
        "_delivery",
        "_federation",
        "_federation_repo",
        "_groups",
        "_invite_locks",
        "_notifs",
        "_seen_open_sessions",
        "_space_remote_members",
        "_spaces",
        "_user_repo",
        "_ws",
    )

    def __init__(
        self,
        *,
        app_repo: AbstractAppRepo,
        user_repo: AbstractUserRepo,
        ws: WebSocketManager,
        federation: Any,
        federation_repo: AbstractFederationRepo,
        cp_repo: AbstractCpRepo | None = None,
        bus: EventBus | None = None,
        notifications: AbstractNotificationRepo | None = None,
        catalog: AppService | None = None,
        spaces: AbstractSpaceRepo | None = None,
        space_remote_members: AbstractSpaceRemoteMemberRepo | None = None,
    ) -> None:
        self._app_repo = app_repo
        self._spaces = spaces
        self._space_remote_members = space_remote_members
        self._user_repo = user_repo
        self._ws = ws
        self._federation = federation
        self._federation_repo = federation_repo
        self._cp_repo = cp_repo
        self._bus = bus
        self._notifs = notifications
        self._catalog = catalog
        self._child_protection = None
        #: Group sessions (v_76): its ``group_*`` verbs and its invites'
        #: share of the invite caps (:meth:`attach_groups`).
        self._groups: AppGroupService | None = None
        #: Where the waiting-invite steps (D10) run in order with the jobs
        #: that write their rows (:meth:`attach_delivery_worker`).
        self._delivery: NotificationDeliveryWorker | None = None
        #: Bounded LRU of inbound ``APP_SESSION`` open ``session_id``s already
        #: handled — guards against a double federation delivery (WebRTC +
        #: HTTPS fallback / retransmit) seating two invites + two notifications
        #: for the same challenge. Scoped to inbound only; outbound
        #: ``open_session`` always mints a fresh uuid so it never dedupes.
        self._seen_open_sessions: OrderedDict[tuple[str, str], None] = OrderedDict()
        #: One invite at a time per (app, person): the cap check, the send
        #: and the session row happen together, so parallel requests can't
        #: slip past the caps. Weak values: a lock goes once nobody holds it.
        self._invite_locks: WeakValueDictionary[tuple[str, str], asyncio.Lock] = (
            WeakValueDictionary()
        )

    def attach_delivery_worker(self, worker: NotificationDeliveryWorker) -> None:
        """Run the waiting-invite steps (D10) on *worker*.

        A waiting invite is a notification row, written (arrival) and
        resolved (decline, withdrawal, the app arriving) by jobs on the
        notification delivery worker — after the publisher returns. A step
        that reads those rows to decide must not run ahead of the jobs
        queued before it, so it is queued behind them (:meth:`_in_order`).
        Read inline, a decline still in the queue left its row open and the
        app arriving made the declined invite playable again; a withdrawal
        right behind its invite found no row and was lost; and a burst of
        invites slipped past the per-person bound of
        :data:`WAITING_SENDERS_MAX` senders."""
        self._delivery = worker

    def attach_groups(self, groups: AppGroupService) -> None:
        """Wire group sessions (v_76): inbound ``APP_SESSION`` verbs in
        :data:`~socialhome.domain.app_group.GROUP_VERBS` go to *groups*,
        and the people a table invites count towards the invite caps."""
        self._groups = groups

    async def _in_order(
        self, step: Callable[..., Awaitable[None]], /, *args: Any, **kwargs: Any
    ) -> None:
        """Run a waiting-invite *step* after every delivery job queued so
        far (inline without a worker)."""
        if self._delivery is None:
            await step(*args, **kwargs)
            return
        await self._delivery.call(partial(step, *args, **kwargs))

    async def _emit(self, event: DomainEvent) -> None:
        """Publish ``event`` fail-soft; no-op without a bus.

        Mirrors :meth:`socialhome.services.bus_publisher.BusPublisherMixin._emit`
        but additionally swallows + logs any publish error: a notification /
        bus failure must NEVER break the WebSocket delivery of an app frame.
        """
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft publish
            log.warning("app_federation: bus publish failed: %s", exc)

    # ─── Public API ───────────────────────────────────────────────────────────

    async def list_peers(self) -> list[dict]:
        """Return confirmed instances as ``[{instance_id, display_name}]``.

        The SPA uses this to populate the peer picker when an app wants
        to start a cross-household session.
        """
        # §D2b: social surface — a ``space_session`` row (a household we
        # only share a space with, via an invite link) is not a social
        # peer, so read the social list, not every CONFIRMED row.
        instances = await self._federation_repo.list_social_instances()
        return [
            {
                "instance_id": inst.id,
                "display_name": inst.effective_display_name,
            }
            for inst in instances
        ]

    async def list_invitable(self, *, app_id: str, self_user_id: str) -> list[dict]:
        """The people the invite sheet lists — whom *self_user_id* may
        invite to a game in *app_id* (D3).

        Only Social Home's own sheet reads this (``GET /invitable``); the
        app's bridge never offers it, so an app never sees the roster. The
        app learns only who the person picked (:meth:`open_session`) and
        who they play with (:meth:`list_contacts`).

        The roster DMs and ``/friends`` expose — active members here
        (minus the caller) and known people of paired households — minus
        personal blocks and guardian blocks (§CP.F2), and further:

        * people of other households only when the app has ``federation``
          ("sends to other households");
        * a protected account (§CP.F3, D5) sees only people it can already
          message: households its own household paired with directly;
        * a member here below the app's age rating is left out (their
          invite would never reach them).

        Each person is ``{instance_id, user_ref, display_name, is_local,
        online, person_name, household_name}``. Two households can each
        have a "Ben": a person from another household reads "Ben · Familie
        Okafor" in ``display_name`` (:func:`with_household`);
        ``person_name`` is the bare name and ``household_name`` the
        household's (``None`` for your own). Remote ``online`` is always
        ``False`` (remote presence isn't wired in).
        """
        app = await self.require_enabled(app_id)
        await self.assert_age_allowed(app_id, self_user_id, app=app)
        app.require_permission(PERMISSION_PEOPLE)
        return [person for person, _id in await self._roster(self_user_id, app)]

    async def invite_sheet(
        self, *, app_id: str, self_user_id: str
    ) -> tuple[list[dict], list[dict]]:
        """What Social Home's invite sheet shows (``GET /invitable``):
        :meth:`list_invitable`'s people, and the spaces to pick everyone
        from at once (group play, owner decision D2) — ``[{space_id, name,
        people}]``, ``people`` the indexes into the people list of whom
        hold a writing seat there.

        A space is offered when the caller holds a writing seat in it
        (owner, admin, moderator or member — never a follower's), it is
        not a global space and not archived, and at least one person the
        sheet lists writes there too (local seats by user id, another
        household's by their ``remote_users`` id). Followers are never
        offered. Sorted by name. The SPA's sheet only — the app's bridge
        never sees it (D3)."""
        app = await self.require_enabled(app_id)
        await self.assert_age_allowed(app_id, self_user_id, app=app)
        app.require_permission(PERMISSION_PEOPLE)
        roster = await self._roster(self_user_id, app)
        people = [person for person, _id in roster]
        return people, await self._sheet_spaces(
            self_user_id, [pid for _person, pid in roster]
        )

    async def _sheet_spaces(
        self, self_user_id: str, person_ids: list[str]
    ) -> list[dict]:
        if self._spaces is None or not person_ids:
            return []
        index = {pid: i for i, pid in enumerate(person_ids)}
        out: list[dict] = []
        for space in await self._spaces.list_for_user(self_user_id):
            if space.space_type == SpaceType.GLOBAL or space.archived:
                continue
            writers = {
                m.user_id
                for m in await self._spaces.list_members(space.id)
                if m.role in WRITER_ROLES
            }
            if self_user_id not in writers:
                continue
            if self._space_remote_members is not None:
                writers |= {
                    m.user_id
                    for m in await self._space_remote_members.list_for_space(space.id)
                    if m.role in WRITER_ROLES
                }
            picks = sorted(index[uid] for uid in writers if uid in index)
            if picks:
                out.append({"space_id": space.id, "name": space.name, "people": picks})
        out.sort(key=lambda s: (s["name"].casefold(), s["space_id"]))
        return out

    async def _roster(
        self, self_user_id: str, app: InstalledApp
    ) -> list[tuple[dict, str]]:
        """:meth:`list_invitable` without the app checks — each person with
        their local or ``remote_users`` id (never sent)."""
        own = self._federation.own_instance_id
        connected = self._ws.connected_users()

        # Personal blocks (§Privacy) — same filter as /friends and DM roster.
        blocked_ids = {
            uid for uid, _ in await self._user_repo.list_blocked(self_user_id)
        }
        # §CP.F2: nobody a guardian block separates from the caller (either
        # way, local or remote) — same as calls and DMs.
        blocked_ids |= await self._guardian_block_counterparts(self_user_id)

        out: list[tuple[dict, str]] = []

        local = [
            u
            for u in await self._user_repo.list_active()
            if u.user_id != self_user_id and u.user_id not in blocked_ids
        ]
        # A member below the app's age rating never gets the invite.
        allowed = set(
            await self.age_filter_recipients(
                app.app_id, [u.user_id for u in local], app=app
            )
        )
        for u in local:
            if u.user_id not in allowed:
                continue
            # Someone here who blocked the caller isn't offered either — a
            # Send that then fails would reveal the block.
            if await self._user_repo.is_blocked(u.user_id, self_user_id):
                continue
            out.append(
                (
                    {
                        "instance_id": own,
                        "user_ref": u.user_id,
                        "display_name": u.display_name,
                        "is_local": True,
                        "online": u.user_id in connected,
                        "person_name": u.display_name,
                        "household_name": None,
                        "groups": True,
                    },
                    u.user_id,
                )
            )

        if PERMISSION_FEDERATION not in app.permissions:
            return out

        households: dict[str, str | None] = {}
        reachable: dict[str, bool] = {}
        groups: dict[str, bool] = {}
        for r in await self._user_repo.list_all_known_remote():
            if r.user_id in blocked_ids:
                continue
            if r.instance_id not in reachable:
                # §CP.F3 (D5): a protected account only reaches people it
                # could already message. A household too old to address one
                # person (pre-v_18) would hand an invite to everyone there:
                # nobody there is offered.
                reachable[r.instance_id] = await self._may_message_household(
                    self_user_id, r.instance_id
                ) and await self._federation.peer_supports(
                    r.instance_id,
                    min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
                )
            if not reachable[r.instance_id]:
                continue
            if r.instance_id not in households:
                households[r.instance_id] = await self.household_name(r.instance_id)
                # Group tables (v_76) reach only households that know them.
                groups[r.instance_id] = await self._federation.peer_supports(
                    r.instance_id,
                    min_version=FederationCapability.MIN_FOR_APP_GROUPS,
                )
            out.append(
                (
                    {
                        **self._remote_contact(r, households[r.instance_id]),
                        "groups": groups[r.instance_id],
                    },
                    r.user_id,
                )
            )

        return out

    @staticmethod
    def _remote_contact(r: RemoteUser, household: str | None) -> dict:
        """A person of another household, in the contact shape."""
        person = r.display_name or r.remote_username
        return {
            "instance_id": r.instance_id,
            "user_ref": r.remote_username,
            "display_name": with_household(person, household),
            "person_name": person,
            "household_name": household,
            "is_local": False,
            # Remote presence isn't wired into this service yet.
            "online": False,
        }

    async def list_contacts(self, *, app_id: str, self_user_id: str) -> list[dict]:
        """The people an app may know about for *self_user_id* (D3): who
        they picked in the invite sheet and who they play with in **this**
        app — the other side of every game this household knows for them
        (a sent invite, a game they took in with Play), latest first. Never
        an invite still waiting in Needs you: the app learns about an
        inviter only once the person plays it.

        The same shape as :meth:`list_invitable`, and the same rules: a
        person a block (personal or guardian) now separates, one the
        person may no longer message (§CP.F3), or another household's
        person once the app lost ``federation`` is left out. Sessions past
        their TTL are left out before the hourly prune removes them.
        """
        app = await self.require_enabled(app_id)
        await self.assert_age_allowed(app_id, self_user_id, app=app)
        app.require_permission(PERMISSION_PEOPLE)
        own = self._federation.own_instance_id
        connected = self._ws.connected_users()
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=ACTIVE_SESSION_TTL_SECONDS)
        ).isoformat()
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for row in await self._app_repo.list_sessions(app_id, self_user_id):
            if row.state != SESSION_ACTIVE or not row.from_user:
                continue
            if row.is_group:
                # People at a table are seats, never contacts (v_76).
                continue
            if row.last_change < cutoff:
                continue
            key = (row.from_instance, row.from_user)
            if key in seen:
                continue
            seen.add(key)
            is_local = row.from_instance == own
            if not is_local and PERMISSION_FEDERATION not in app.permissions:
                continue
            target = {
                "instance_id": row.from_instance,
                "user_ref": row.from_user,
                "is_local": is_local,
            }
            if not await self._person_allowed(self_user_id, target):
                continue
            if is_local:
                user = await self._user_repo.get_by_user_id(row.from_user)
                if user is None:
                    continue
                out.append(
                    {
                        "instance_id": own,
                        "user_ref": user.user_id,
                        "display_name": user.display_name,
                        "is_local": True,
                        "online": user.user_id in connected,
                        "person_name": user.display_name,
                        "household_name": None,
                    }
                )
                continue
            remote = await self._user_repo.get_remote_by_member(
                row.from_instance, row.from_user
            )
            if remote is None:
                continue
            out.append(
                self._remote_contact(
                    remote, await self.household_name(row.from_instance)
                )
            )
        return out

    async def invite_counts(self, *, app_id: str, user_id: str) -> dict:
        """What the invite caps leave *user_id* in *app_id*: ``{open,
        max_open, today, max_per_day}`` — for the sheet to say so before
        Send, not after."""
        open_, today = await self._invite_usage(app_id, user_id)
        return {
            "open": open_,
            "max_open": INVITES_OPEN_MAX,
            "today": today,
            "max_per_day": INVITES_PER_DAY_MAX,
        }

    async def _invite_usage(self, app_id: str, user_id: str) -> tuple[int, int]:
        """(open, today): invites *user_id* sent in *app_id* that nobody
        answered or they withdrew (within the TTL), and invites sent in
        the last 24 hours. From the session rows — at most 50 per (app,
        person), so this is one small indexed read."""
        now = datetime.now(UTC)
        ttl_cut = (now - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)).isoformat()
        day_cut = (now - timedelta(days=1)).isoformat()
        open_ = today = 0
        for row in await self._app_repo.list_sessions(app_id, user_id):
            if not row.sent:
                continue
            if row.created_at >= day_cut:
                today += 1
            if not row.answered and not row.withdrawn and row.last_change >= ttl_cut:
                open_ += 1
        if self._groups is not None:
            # A table counts as one invite, however many it asked (v_76,
            # AppGroupService.invite_usage).
            g_open, g_today = await self._groups.invite_usage(app_id, user_id)
            open_ += g_open
            today += g_today
        return open_, today

    async def check_invite_caps(
        self, app_id: str, user_id: str, *, adding: int = 1
    ) -> None:
        """Raise :class:`AppInviteLimitError` when *adding* more invites
        would pass a cap (open first: it is the one the person can act on —
        withdraw an invite). A table opened by picking people adds one,
        however many people it asks (owner decision D2)."""
        open_, today = await self._invite_usage(app_id, user_id)
        if open_ + adding > INVITES_OPEN_MAX:
            raise AppInviteLimitError(daily=False)
        if today + adding > INVITES_PER_DAY_MAX:
            raise AppInviteLimitError(daily=True)

    async def open_session(
        self,
        *,
        app_id: str,
        target: dict,
        actor_user_id: str,
    ) -> str:
        """Send one invite (:meth:`_open_session`), one at a time per (app,
        person), so the invite caps hold under parallel requests."""
        async with self.invite_lock(app_id, actor_user_id):
            return await self._open_session(
                app_id=app_id, target=target, actor_user_id=actor_user_id
            )

    def invite_lock(self, app_id: str, user_id: str) -> asyncio.Lock:
        """The one lock for *user_id*'s invites in *app_id* (open and
        continue), kept while anyone holds it."""
        key = (app_id, user_id)
        lock = self._invite_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._invite_locks[key] = lock
        return lock

    async def _open_session(
        self,
        *,
        app_id: str,
        target: dict,
        actor_user_id: str,
    ) -> str:
        """Open an app session with a specific *person* (local or remote).

        ``target`` is ``{"instance_id": str, "user_ref": str, "is_local":
        bool}`` — one of :meth:`list_invitable`. Only Social Home's invite
        sheet calls this (D3): the app's bridge doesn't offer it, so an
        invite always names a person someone picked in the sheet. Refused
        past the invite caps (:class:`AppInviteLimitError`).

        * ``is_local`` — the target lives on this household; ``user_ref`` is
          a local ``user_id``.  We deliver the ``kind="session"`` open frame
          straight to the target over WebSocket — no federation send, no
          fan-out to every local user, no echo to the initiator.
        * remote — ``instance_id`` is the target household and ``user_ref``
          is the target's remote username on that household.  We send an
          ``APP_SESSION`` event there.

        Validates that the app is installed and enabled and that the actor
        passes the age gate, allocates a ``session_id``, and returns it so
        the caller can hand it back to the SPA.

        §FIX-I2 (relaxed, proto v_18): per-user identity on the wire
        (``to_user`` / ``from_user``) is now permitted because the
        challengeable roster is exactly the pairing-scoped DM / ``/friends``
        set — a consensual, not covert, audience.  ``to_user`` lets the peer
        route the open to the addressed person instead of fanning to every
        local user; ``from_user`` is the initiator's username so the
        recipient can show who challenged them.  Both fields are gated on
        :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING` and are omitted
        for sub-v_18 peers, which fall back to the legacy household-addressed
        fan-out (no ``to_user``).  Session control always rides the JSON
        event path regardless of peer version — the binary channel is only
        used for :meth:`send_message`.
        """
        app = await self.require_enabled(app_id, target)
        await self.assert_age_allowed(app_id, actor_user_id, app=app)
        await self.assert_invitable(actor_user_id, target, app=app)
        await self.check_invite_caps(app_id, actor_user_id)
        session_id = uuid4().hex
        own = self._federation.own_instance_id

        if target.get("is_local"):
            # Local loopback — no live frame into anyone's app (D3: the
            # invite waits in Needs you until Play, which hands it over as
            # the launch context). Keep the invite for the target and raise
            # a bell row + push for them only — if they pass the age gate.
            recipients = await self.age_filter_recipients(app_id, [target["user_ref"]])
            if target["user_ref"] in recipients:
                await self._app_repo.add_pending_session(
                    AppPendingSession(
                        app_id=app_id,
                        user_id=target["user_ref"],
                        session_id=session_id,
                        from_instance=own,
                        from_user=actor_user_id,
                        payload={
                            "app_id": app_id,
                            "session_id": session_id,
                            "verb": "open",
                        },
                        created_at=datetime.now(UTC).isoformat(),
                    )
                )
                initiator = await self._user_repo.get_by_user_id(actor_user_id)
                from_display = (
                    initiator.display_name if initiator is not None else actor_user_id
                )
                await self._emit(
                    AppChallengeReceived(
                        app_id=app_id,
                        session_id=session_id,
                        to_user_id=target["user_ref"],
                        from_display=from_display,
                        from_user_id=actor_user_id,
                    )
                )
            await self._know_game(app_id, actor_user_id, session_id, own, target)
            return session_id

        # Remote — address the peer household; include per-user routing fields
        # only when the peer is v_18+ (else legacy household fan-out).
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "verb": "open",
        }
        supported = await self._federation.peer_supports(
            target["instance_id"],
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        )
        if supported:
            me = await self._user_repo.get_by_user_id(actor_user_id)
            from_user = me.username if me is not None else actor_user_id
            payload["to_user"] = target["user_ref"]
            payload["from_user"] = from_user
        await self._federation.send_event(
            to_instance_id=target["instance_id"],
            event_type=FederationEventType.APP_SESSION,
            payload=payload,
        )
        await self._know_game(
            app_id, actor_user_id, session_id, target["instance_id"], target
        )
        return session_id

    async def continue_session(
        self,
        *,
        app_id: str,
        session_id: str,
        target: dict,
        actor_user_id: str,
    ) -> str:
        """Pick a game this household no longer knows up again (owner
        decision 2026-10-29) — only through Social Home's sheet ("Continue
        game with Ben"), so D3 holds: the person confirms who.

        *session_id* is the app's own id for the game (started before the
        mailbox, or quiet past
        :data:`~socialhome.domain.apps.ACTIVE_SESSION_TTL_SECONDS`), and
        *target* one of :meth:`list_invitable`. On this side the session
        becomes known (an ``active`` row with *target*, marked ``sent`` —
        it counts towards the invite caps). The other side gets
        ``APP_SESSION {verb: "continue"}`` and keeps it as an invite in
        Needs you ("Ben wants to continue your Chess game"): their app
        hears of it, and of the moves held meanwhile, only after Play.

        Never moves a session id to another opponent: a session this
        person already knows with someone else (or that waits as an invite)
        is :class:`AppSessionTakenError`; the same one with *target* is a
        no-op. The receiving household keeps a continue only for a session
        id its person has no row for, and only from the person who sent it
        (see :meth:`_deliver`)."""
        async with self.invite_lock(app_id, actor_user_id):
            app = await self.require_enabled(app_id, target)
            await self.assert_age_allowed(app_id, actor_user_id, app=app)
            await self.assert_invitable(actor_user_id, target, app=app)
            own = self._federation.own_instance_id
            is_local = bool(target.get("is_local"))
            peer = own if is_local else str(target["instance_id"])
            user_ref = str(target["user_ref"])
            known = await self._app_repo.get_pending_session(
                app_id, actor_user_id, session_id
            )
            if known is not None:
                if (
                    known.state == SESSION_ACTIVE
                    and known.is_with(peer, user_ref)
                    and not known.withdrawn
                ):
                    return session_id
                raise AppSessionTakenError()
            await self.check_invite_caps(app_id, actor_user_id)
            payload: dict[str, Any] = {
                "app_id": app_id,
                "session_id": session_id,
                "verb": SESSION_VERB_CONTINUE,
            }
            if is_local:
                recipients = await self.age_filter_recipients(
                    app_id, [user_ref], app=app
                )
                theirs = await self._app_repo.get_pending_session(
                    app_id, user_ref, session_id
                )
                # Only a session id they have no row for (no re-pointing).
                if user_ref in recipients and theirs is None:
                    await self._app_repo.add_pending_session(
                        AppPendingSession(
                            app_id=app_id,
                            user_id=user_ref,
                            session_id=session_id,
                            from_instance=own,
                            from_user=actor_user_id,
                            payload=payload,
                            created_at=datetime.now(UTC).isoformat(),
                        )
                    )
                    me = await self._user_repo.get_by_user_id(actor_user_id)
                    await self._emit(
                        AppChallengeReceived(
                            app_id=app_id,
                            session_id=session_id,
                            to_user_id=user_ref,
                            from_display=(
                                me.display_name if me is not None else actor_user_id
                            ),
                            from_user_id=actor_user_id,
                            continued=True,
                        )
                    )
            else:
                me = await self._user_repo.get_by_user_id(actor_user_id)
                payload["to_user"] = user_ref
                payload["from_user"] = me.username if me is not None else actor_user_id
                await self._federation.send_event(
                    to_instance_id=peer,
                    event_type=FederationEventType.APP_SESSION,
                    payload=payload,
                )
            await self._know_game(app_id, actor_user_id, session_id, peer, target)
            return session_id

    async def send_message(
        self,
        *,
        app_id: str,
        target: dict,
        session_id: str,
        payload: dict,
        actor_user_id: str,
        turn: bool = False,
    ) -> None:
        """Send an app-layer message to a specific *person* (local or remote).

        Mirrors :meth:`open_session`: ``target`` is
        ``{"instance_id": str, "user_ref": str, "is_local": bool}`` — the
        other side of *session_id*, a session this household knows for the
        actor (else :class:`AppSessionUnknownError`).

        * ``is_local`` — the target lives on this household; ``user_ref`` is a
          local ``user_id``.  We deliver a ``kind="message"`` frame straight
          to the target *and* the initiator over WebSocket (age-filtered,
          de-duped) — no federation send, no fan-out to every local user.
        * remote — delegate to :meth:`FederationService.send_app_message`,
          which selects the binary ``fed-app-v1`` channel (v_17+ confirmed
          peers) or falls back to the JSON ``APP_MESSAGE`` federation event
          path — in both cases the ``payload`` dict is AES-256-GCM-sealed and
          never sent in plaintext.

        Per-user routing (``to_user`` / ``from_user``) is gated on
        :data:`FederationCapability.MIN_FOR_APP_USER_ROUTING` (v_18) and rides
        both paths, sealed: the JSON ``APP_MESSAGE`` payload or the binary
        frame's envelope metadata (see
        :meth:`FederationService.send_app_message`).

        ``turn`` — the app says this move hands the other player the
        turn: kept for them while their app is closed, it raises "Your
        turn" (:class:`AppTurn`). Sealed beside ``to_user``; an older
        household ignores it.
        """
        app = await self.require_enabled(app_id, target)
        await self.assert_age_allowed(app_id, actor_user_id, app=app)
        own = self._federation.own_instance_id
        # D3: only in a game this household knows the actor plays with this
        # very person — one they sent through the invite sheet or took in
        # with Play — and not one they withdrew. Never a new session id to
        # an old opponent. One primary-key read (C#31), then the checks
        # the roster would apply to that one person.
        known = await self._known_session(app_id, actor_user_id, session_id, target)
        if known.withdrawn:
            raise AppSessionUnknownError()
        await self._assert_person_allowed(actor_user_id, target)
        await self._mark_played(app_id, actor_user_id, session_id, known)

        if target.get("is_local"):
            # Local loopback — deliver the message frame to the addressed
            # person and the initiator only, never a fan-out to all local
            # users; to the person only once they play it (D3: not while the
            # invite still waits in Needs you — it is held for after Play).
            recipients = await self.age_filter_recipients(
                app_id, [target["user_ref"], actor_user_id], app=app
            )
            live = [
                uid
                for uid in recipients
                if uid == actor_user_id
                or await self._plays_with(app_id, uid, session_id, own, actor_user_id)
            ]
            await self._emit_frame(
                live,
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
                kind="message",
                payload=payload,
            )
            if target["user_ref"] in recipients and target["user_ref"] != actor_user_id:
                held = await self._hold(
                    app_id,
                    target["user_ref"],
                    session_id,
                    from_instance=own,
                    from_user=actor_user_id,
                    kind="message",
                    payload=payload,
                )
                if held and turn:
                    await self._your_turn(
                        app_id,
                        session_id,
                        target["user_ref"],
                        from_instance=own,
                        from_user=actor_user_id,
                        from_user_id=actor_user_id,
                    )
            return

        # Remote — include per-user routing fields only for v_18+ peers.
        to_user: str | None = None
        from_user: str | None = None
        if await self._federation.peer_supports(
            target["instance_id"],
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        ):
            me = await self._user_repo.get_by_user_id(actor_user_id)
            from_user = me.username if me is not None else actor_user_id
            to_user = target["user_ref"]
        await self._federation.send_app_message(
            to_instance_id=target["instance_id"],
            app_id=app_id,
            session_id=session_id,
            payload=payload,
            to_user=to_user,
            from_user=from_user,
            turn=turn,
        )

    # ─── Inbound federation dispatch ─────────────────────────────────────────

    async def on_inbound_event(self, event: FederationEvent) -> None:
        """Handle an inbound ``APP_SESSION`` or ``APP_MESSAGE`` JSON event.

        Called by the :class:`FederationService` event registry after the
        §24.11 pipeline has validated and decrypted the envelope.  Extracts
        the app-specific fields and fans out via :meth:`_deliver`.
        """
        if not isinstance(event.payload, dict):
            log.debug("app_federation: inbound event with non-dict payload — dropping")
            return
        if event.event_type is FederationEventType.APP_MESSAGE and isinstance(
            event.payload.get("group"), dict
        ):
            # A message at a group table (v_76): its own service.
            if self._groups is not None:
                await self._groups.on_inbound_message(event)
            return
        if (
            event.event_type is FederationEventType.APP_SESSION
            and event.payload.get("verb") in GROUP_VERBS
        ):
            # A group table (v_76): its own service, never the 1:1 paths.
            if self._groups is not None:
                await self._groups.on_inbound_event(event)
            return
        if (
            event.event_type is FederationEventType.APP_SESSION
            and event.payload.get("verb") == "close"
            and event.payload.get("reason") == CLOSE_ENDED
            and "sessions" in event.payload
        ):
            await self._on_ended(event)
            return
        app_id = event.payload.get("app_id")
        session_id = event.payload.get("session_id")
        if not app_id or not session_id:
            log.debug(
                "app_federation: inbound event missing app_id/session_id — dropping"
            )
            return

        if event.event_type is FederationEventType.APP_SESSION:
            # Pass the whole payload as the session info dict.
            app_payload: dict = dict(event.payload)
            kind = "session"
            # Idempotency: suppress a re-delivered open (WebRTC + HTTPS
            # fallback / retransmit) so we never seat two invites or raise two
            # notifications for the same challenge.  Scoped to opens — a
            # future ``verb`` ("close") is not a session-creating event.
            verb = event.payload.get("verb")
            if verb in ("open", SESSION_VERB_CONTINUE) and self._seen_open_session(
                event.from_instance,
                str(session_id) if verb == "open" else f"{verb}:{session_id}",
            ):
                log.debug(
                    "app_federation: duplicate inbound open for session %r — skipping",
                    session_id,
                )
                return
        else:
            # APP_MESSAGE: the application data is nested under "data".
            app_payload = event.payload.get("data") or {}
            kind = "message"

        # Per-user routing (v_18+): a non-empty ``to_user`` addresses one local
        # user.  An empty string is the legacy household-addressed open (a
        # back-compat open maps ``user_ref`` → "") — treat it as absent so it
        # falls back to the household fan-out and is never looked up.
        raw_to_user = event.payload.get("to_user")
        to_user = raw_to_user if isinstance(raw_to_user, str) and raw_to_user else None

        raw_from_user = event.payload.get("from_user")
        from_user = (
            raw_from_user if isinstance(raw_from_user, str) and raw_from_user else None
        )
        # A household that addresses people (v_18+) always names the user on
        # the JSON path; the whole-household fan-out is only for older peers.
        if to_user is None and await self._federation.peer_supports(
            event.from_instance,
            min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
        ):
            log.warning(
                "app_federation: %s from %s refused — no addressee (session=%s)",
                kind,
                event.from_instance,
                session_id,
            )
            return

        await self._deliver(
            app_id,
            session_id,
            from_instance=event.from_instance,
            payload=app_payload,
            kind=kind,
            to_user=to_user,
            from_user=from_user,
            notify_open=(
                kind == "session"
                and event.payload.get("verb") in ("open", SESSION_VERB_CONTINUE)
            ),
            continued=(
                kind == "session" and event.payload.get("verb") == SESSION_VERB_CONTINUE
            ),
            withdraw=(kind == "session" and event.payload.get("verb") == "close"),
            turn=(kind == "message" and event.payload.get("turn") is True),
        )

    async def on_inbound_message(
        self,
        instance_id: str,
        app_id: str,
        session_id: str,
        payload: dict,
        *,
        to_user: str | None = None,
        from_user: str | None = None,
        turn: bool = False,
    ) -> None:
        """Handle an inbound binary app frame from ``fed-app-v1``.

        Called by :meth:`FederationService._app_inbound_handler` after the
        §24.11 pipeline validates the envelope and the payload is decrypted.
        ``to_user`` / ``from_user`` come from the frame's sealed metadata
        (v_18+ senders); routed exactly like the JSON ``APP_MESSAGE``. A
        frame without them (an older sender) keeps the household fan-out and
        the app disambiguates by ``session_id``. A household that addresses
        people (v_18+) must name the person here too — never the
        whole-household fan-out.
        """
        if not to_user and await self._federation.peer_supports(
            instance_id, min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING
        ):
            log.warning(
                "app_federation: binary message from %s refused — no addressee "
                "(session=%s)",
                instance_id,
                session_id,
            )
            return
        await self._deliver(
            app_id,
            session_id,
            from_instance=instance_id,
            payload=payload,
            kind="message",
            to_user=to_user or None,
            from_user=from_user or None,
            turn=turn,
        )

    # ─── Internal helpers ─────────────────────────────────────────────────────

    def _seen_open_session(self, from_instance: str, session_id: str) -> bool:
        """Return whether ``session_id`` from ``from_instance`` was already
        handled inbound.

        Keyed per sending household, so one household can never pre-empt
        another's open by sending the same session id first.

        First sight records it (LRU, capped at
        :data:`_SEEN_OPEN_SESSIONS_MAX`, oldest evicted) and returns ``False``;
        a repeat returns ``True`` so the caller can skip both delivery and the
        challenge publish. Synchronous — the OrderedDict mutation is atomic
        under the single-threaded asyncio loop.
        """
        key = (from_instance, session_id)
        if key in self._seen_open_sessions:
            return True
        self._seen_open_sessions[key] = None
        if len(self._seen_open_sessions) > _SEEN_OPEN_SESSIONS_MAX:
            self._seen_open_sessions.popitem(last=False)
        return False

    async def _resolve_remote_initiator(
        self, from_instance: str, from_user: str | None
    ) -> RemoteUser | None:
        """Resolve the remote challenge initiator's local ``RemoteUser`` row.

        Returns ``None`` when ``from_user`` is absent or the remote can't be
        found (legacy/unknown sender) — callers treat that as "can't enforce a
        block, deliver as before" (fail-soft).
        """
        if not from_user:
            return None
        return await self._user_repo.get_remote_by_member(from_instance, from_user)

    async def _remote_open_display(
        self,
        from_instance: str,
        from_user: str | None,
        *,
        remote: RemoteUser | None = None,
    ) -> str:
        """Resolve a human label for a remote challenge initiator.

        Order: the remote user's federated ``display_name`` with their
        household (``"Ben · Familie Okafor"``, :func:`with_household`, when
        ``from_user`` is known) → the peer household's display name → the
        raw ``from_instance`` id as a last resort. Never a stable
        cross-household id beyond the fallback. ``remote`` lets a caller
        that already resolved the initiator (e.g. for a block check) pass
        the row to avoid a second lookup.
        """
        if remote is None and from_user:
            remote = await self._user_repo.get_remote_by_member(
                from_instance, from_user
            )
        household = await self.household_name(from_instance)
        if remote is not None and remote.display_name:
            return with_household(remote.display_name, household)
        return household or from_instance

    async def household_name(self, instance_id: str) -> str | None:
        """A paired household's name as this household shows it (its local
        alias, else its own display name), or ``None`` if unknown."""
        inst = await self._federation_repo.get_instance(instance_id)
        if inst is None:
            return None
        return inst.effective_display_name.strip() or None

    async def require_enabled(
        self, app_id: str, target: dict | None = None
    ) -> InstalledApp:
        """The installed, enabled app — else raise :class:`AppNotFoundError`
        or :class:`AppNotEnabledError`.

        With *target* (a game with that person), the app must also have the
        permission it takes (:func:`_target_permission`) — else
        :class:`~socialhome.domain.apps.AppPermissionDeniedError`, before
        anything is sent."""
        app = await self._app_repo.get(app_id)
        if app is None:
            raise AppNotFoundError(app_id)
        if not app.enabled:
            raise AppNotEnabledError(app_id)
        if target is not None:
            app.require_permission(_target_permission(target))
        return app

    async def assert_age_allowed(
        self, app_id: str, actor_user_id: str, *, app: InstalledApp | None = None
    ) -> None:
        """Raise :class:`AppAgeRestrictedError` for protected minors below min_age.

        Fail-closed: if the app is not found or has no age restriction,
        this is a no-op.  Only blocks when the app exists, has a min_age > 0,
        cp_repo is configured, and the user's protection record shows a
        declared_age below the threshold.
        """
        if self._cp_repo is None:
            return
        if app is None:
            app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0:
            return
        p = await self._cp_repo.get_user_protection(actor_user_id)
        if p is None:
            return
        if not p.get("child_protection_enabled"):
            return
        declared = int(p.get("declared_age") or 0)
        if declared < app.min_age:
            raise AppAgeRestrictedError(
                f"This app is restricted to ages {app.min_age}+."
            )

    async def assert_invitable(
        self, actor_user_id: str, target: dict, *, app: InstalledApp
    ) -> None:
        """Raise :class:`AppContactNotFoundError` unless *target* is one of
        the people the invite sheet lists for the actor
        (:meth:`list_invitable`) — paired-household members minus blocks,
        a protected account only reaching people it can message, another
        household only with ``federation``.

        A household-addressed target (``user_ref == ""``, from before
        per-person addressing) names nobody anyone picked: refused.
        """
        refused = AppContactNotFoundError(
            "target is not a contact of the requesting user"
        )
        if not target.get("user_ref"):
            raise refused
        key = (
            target.get("instance_id"),
            target.get("user_ref"),
            bool(target.get("is_local")),
        )
        for c, _id in await self._roster(actor_user_id, app):
            if (c["instance_id"], c["user_ref"], c["is_local"]) == key:
                break
        else:
            raise refused
        # A local person who blocked the actor — as a remote recipient's
        # household drops a blocked initiator's frames on receipt. Same
        # answer as a non-contact, so the block isn't revealed.
        if target.get("is_local") and await self._user_repo.is_blocked(
            str(target["user_ref"]), actor_user_id
        ):
            raise refused

    async def _known_session(
        self, app_id: str, actor_user_id: str, session_id: str, target: dict
    ) -> AppPendingSession:
        """The session row this household keeps for the actor's game
        *session_id* with *target* — else :class:`AppSessionUnknownError`.

        ``is_local`` must agree with where *target* lives (this household
        or another), so a game can't be steered between the WebSocket and
        the federation path by a mislabelled target."""
        own = self._federation.own_instance_id
        instance = str(target.get("instance_id") or "")
        user_ref = str(target.get("user_ref") or "")
        if not user_ref or bool(target.get("is_local")) != (instance == own):
            raise AppSessionUnknownError()
        known = await self._app_repo.get_pending_session(
            app_id, actor_user_id, session_id
        )
        if known is not None and known.is_group:
            # A seat at a group table is played through the table (v_76).
            raise AppGroupSessionError()
        # An invite that still waits in Needs you is not the app's yet: only
        # Play takes it in (D3/D12) — never a message the app sends in it.
        if (
            known is None
            or known.state != SESSION_ACTIVE
            or not known.is_with(instance, user_ref)
        ):
            raise AppSessionUnknownError()
        return known

    async def _plays_with(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        from_instance: str,
        from_user: str | None,
    ) -> bool:
        """Whether *user_id* plays *session_id* (an ``active`` row) with
        exactly *from_instance* / *from_user* — the only frames their app
        may see live (D3). One primary-key read."""
        row = await self._app_repo.get_pending_session(app_id, user_id, session_id)
        return (
            row is not None
            and row.state == SESSION_ACTIVE
            and not row.is_group
            and row.is_with(from_instance, from_user)
        )

    async def _person_allowed(self, actor_user_id: str, target: dict) -> bool:
        """Whether *target* is still someone the actor may play with: an
        active member here, or a known remote user, neither blocked by the
        actor nor — locally — blocking them, no guardian block between
        them, and (§CP.F3) a household a protected actor may message. A
        handful of primary-key reads, no roster scan."""
        ref = str(target.get("user_ref") or "")
        if target.get("is_local"):
            user = await self._user_repo.get_by_user_id(ref)
            if user is None or not user.is_active() or ref == actor_user_id:
                return False
            other = ref
            if await self._user_repo.is_blocked(other, actor_user_id):
                return False
        else:
            instance = str(target.get("instance_id") or "")
            remote = await self._user_repo.get_remote_by_member(instance, ref)
            if remote is None or remote.deprovisioned_at is not None:
                return False
            if not await self._may_message_household(actor_user_id, instance):
                return False
            # A pre-v_18 household can't address one person: never a
            # household-wide fan-out of a game.
            if not await self._federation.peer_supports(
                instance, min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING
            ):
                return False
            other = remote.user_id
        return not (
            await self._user_repo.is_blocked(actor_user_id, other)
            or await self._guardian_blocked(actor_user_id, other)
        )

    async def _assert_person_allowed(self, actor_user_id: str, target: dict) -> None:
        """:meth:`_person_allowed`, raising :class:`AppContactNotFoundError`
        (the same answer as a non-contact, so no block is revealed)."""
        if not await self._person_allowed(actor_user_id, target):
            raise AppContactNotFoundError(
                "target is not a contact of the requesting user"
            )

    async def _know_game(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        peer_instance: str,
        target: dict,
    ) -> None:
        """*user_id* sent an invite to *target* through the invite sheet:
        keep the session known for them (an ``active`` row marked
        ``sent``), so they can message that game, the other side's moves
        wait in their mailbox, and it counts towards the invite caps until
        it is answered."""
        now = datetime.now(UTC).isoformat()
        await self._app_repo.add_active_session(
            AppPendingSession(
                app_id=app_id,
                user_id=user_id,
                session_id=session_id,
                from_instance=peer_instance,
                from_user=str(target["user_ref"]),
                payload={
                    "app_id": app_id,
                    "session_id": session_id,
                    "verb": "open",
                    "sent": True,
                },
                created_at=now,
                state=SESSION_ACTIVE,
                updated_at=now,
            )
        )

    async def _mark_played(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        known: AppPendingSession,
    ) -> None:
        """*user_id* sent a message in *session_id*: the game's TTL moves on
        (at most hourly)."""
        now = datetime.now(UTC)
        if known.last_change < (now - _TOUCH_EVERY).isoformat():
            await self._app_repo.touch_session(
                app_id, user_id, session_id, now=now.isoformat()
            )

    async def _hold(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str | None,
        kind: str,
        payload: dict,
    ) -> bool:
        """The mailbox (D11): keep *payload* as the latest message of
        *session_id* for *user_id* until their app opens — only for a
        session known for them whose other side is ``from_instance`` /
        ``from_user``, and only up to :data:`HELD_MESSAGE_MAX_BYTES`.
        ``True`` when kept."""
        if not from_user:
            return False
        held = {"kind": kind, "payload": payload}
        if len(orjson.dumps(held)) > HELD_MESSAGE_MAX_BYTES:
            log.info(
                "app_federation: %s for session %s too large to hold — live only",
                kind,
                session_id,
            )
            return False
        return await self._app_repo.hold_message(
            app_id,
            user_id,
            session_id,
            from_instance=from_instance,
            from_user=from_user,
            held=held,
            now=datetime.now(UTC).isoformat(),
        )

    async def _your_turn(
        self,
        app_id: str,
        session_id: str,
        user_id: str,
        *,
        from_instance: str,
        from_user: str | None,
        from_user_id: str,
        from_display: str | None = None,
    ) -> None:
        """A move flagged ``turn`` was kept for *user_id*: tell them it's
        their turn — only in a game they play (``active``, never an
        invite still waiting in Needs you) with exactly that sender.
        One primary-key read."""
        if not await self._plays_with(
            app_id, user_id, session_id, from_instance, from_user
        ):
            return
        await self._emit(
            AppTurn(
                app_id=app_id,
                session_id=session_id,
                to_user_id=user_id,
                from_user_id=from_user_id,
                from_display=from_display,
            )
        )

    async def _emit_frame(
        self,
        recipient_user_ids: list[str],
        *,
        app_id: str,
        session_id: str,
        from_instance: str,
        from_user: str,
        kind: str,
        payload: dict,
    ) -> None:
        """Push an ``app.message`` frame to each recipient over WebSocket.

        Builds the frame once and delivers it per-user via
        :meth:`WebSocketManager.broadcast_to_user` — never a fan-out to all
        local users.  ``from_user`` rides the frame so the recipient's SPA can
        show who initiated the session / sent the message.  ``kind`` is
        ``"session"`` for an open frame, ``"message"`` for an in-session
        message; ``payload`` is the app-specific dict the SPA relays into the
        iframe.  Reused by the local-loopback open and message paths.
        """
        frame = {
            "type": "app.message",
            "app_id": app_id,
            "session_id": session_id,
            "from_instance": from_instance,
            "kind": kind,
            "from_user": from_user,
            "payload": payload,
        }
        for uid in recipient_user_ids:
            await self._ws.broadcast_to_user(uid, frame)

    async def age_filter_recipients(
        self, app_id: str, user_ids: list[str], *, app: InstalledApp | None = None
    ) -> list[str]:
        """Drop protected minors below the app's ``min_age`` from ``user_ids``.

        Fast path returns the input unchanged when the app has no age
        restriction or no ``cp_repo`` is configured.  Preserves order and
        de-duplicates while keeping first occurrence.
        """
        # De-dupe preserving order.
        seen: set[str] = set()
        ordered: list[str] = []
        for uid in user_ids:
            if uid not in seen:
                seen.add(uid)
                ordered.append(uid)

        if app is None:
            app = await self._app_repo.get(app_id)
        if app is None or app.min_age <= 0 or self._cp_repo is None:
            return ordered

        allowed: list[str] = []
        for uid in ordered:
            p = await self._cp_repo.get_user_protection(uid)
            if p is None or not p.get("child_protection_enabled"):
                allowed.append(uid)
                continue
            declared = int(p.get("declared_age") or 0)
            if declared >= app.min_age:
                allowed.append(uid)
        return allowed

    async def _deliver(
        self,
        app_id: str,
        session_id: str,
        *,
        from_instance: str,
        payload: dict,
        kind: str,
        to_user: str | None = None,
        from_user: str | None = None,
        notify_open: bool = False,
        continued: bool = False,
        withdraw: bool = False,
        ended: set[tuple[str, str, str]] | None = None,
        turn: bool = False,
    ) -> None:
        """Route an inbound app message to local users via WebSocket.

        ``continued`` (with ``notify_open``): a "continue game" — the sender
        picked an old game up again. It waits in Needs you like an invite,
        but only for a session id the recipient has **no** row for: a game
        they still know, or an invite already waiting, is never re-pointed
        at another sender (no hijack of a session id).

        Silently drops (debug log) if the app is not installed or disabled —
        the peer doesn't need to know we don't have this app — except an
        invite for one person, which waits for them (D10,
        :meth:`_keep_waiting`), and a withdrawal of such an invite.

        Parameters
        ----------
        kind:
            ``"session"`` for ``APP_SESSION`` control events,
            ``"message"`` for ``APP_MESSAGE`` and binary ``fed-app-v1``
            data frames.  Forwarded to the SPA so the bridge can relay
            it into the iframe, letting apps distinguish invites from
            in-game moves and route by session.
        to_user:
            Per-user routing hint (v_18+).  When a non-empty string that
            resolves to a local user (by username — the recipient's local
            username equals their ``remote_username`` on their home instance),
            deliver **only** to that user.  When ``None``, empty, or
            unresolvable, fall back to the legacy/best-effort household
            fan-out to every (age-eligible) local user.
        from_user:
            The remote initiator's username. Put on the WS frame as
            ``from_user`` once it resolves to a known user of
            ``from_instance`` (always on the per-user path, which refuses an
            unknown initiator); also resolves the ``from_display`` label on
            the published :class:`AppChallengeReceived`.
        notify_open:
            ``True`` only for an inbound ``APP_SESSION`` *open* — when the
            event also resolves to a single local recipient, raise a bell row +
            push for that recipient. Never set for messages or the legacy
            household fan-out (no specific recipient to notify).
        withdraw:
            ``True`` only for an inbound ``APP_SESSION`` *close* addressed to
            one person: the invite it ends stops waiting — only when the same
            household and person opened it.
        ended:
            Set for one session of an inbound ``reason: "ended"`` close
            (v_72): when the close is held for a game the person plays with
            exactly that sender, they are told the game ended
            (:class:`AppGameEnded`) — once per opponent per message: the
            set holds who was told already.
        turn:
            The sending app flagged the message ``turn: true``: once it
            is kept for a game the person plays with that very sender
            (and the app is on here), they hear it's their turn
            (:meth:`_your_turn`).

        The mailbox (D11): a move or a close for one person is also kept
        as the latest message of that session for them (:meth:`_hold`) —
        only for a session this household knows for them with that very
        sender — and that holds while the app is turned off too (no live
        frame then). An invite to a turned-off app, and anything for the
        whole household, is dropped as before.
        """
        app = await self._app_repo.get(app_id)
        if to_user is not None and (app is None or not app.enabled):
            # D10: an invite for an app this household doesn't have, or
            # turned off, waits for the person (Needs you, "Ask an admin")
            # — and the sender is never told anything.
            if notify_open and not continued:
                await self._in_order(
                    self._keep_waiting,
                    app_id,
                    app,
                    session_id,
                    from_instance=from_instance,
                    to_user=to_user,
                    from_user=from_user,
                )
                return
            if withdraw:
                await self._in_order(
                    self._withdraw_waiting,
                    app_id,
                    session_id,
                    from_instance=from_instance,
                    to_user=to_user,
                    from_user=from_user,
                )
        if app is None or (not app.enabled and (notify_open or to_user is None)):
            log.debug(
                "app_federation: dropping inbound for app %r (not installed/enabled)",
                app_id,
            )
            return
        if PERMISSION_FEDERATION not in app.permissions:
            # The household installed this app without "sends to other
            # households": another household's copy can't reach it either.
            log.debug(
                "app_federation: dropping inbound for app %r (no federation permission)",
                app_id,
            )
            return

        frame: dict[str, Any] = {
            "type": "app.message",
            "app_id": app_id,
            "session_id": session_id,
            "from_instance": from_instance,
            "kind": kind,
            "payload": payload,
        }

        # Per-user routing: deliver only to the addressed local user when it
        # resolves.  An empty/absent to_user is already mapped to None by the
        # caller (legacy household fan-out) and is never looked up.
        if to_user is not None:
            recipient = await self._user_repo.get(to_user)
            if recipient is None:
                log.warning(
                    "app_federation: %s from %s refused — addressee %r is not "
                    "a user here (session=%s)",
                    kind,
                    from_instance,
                    to_user,
                    session_id,
                )
                return
            if recipient is not None:
                # Recipient block enforcement (symmetric with DMs,
                # ``dm_service._guard_block_pair``): if the recipient has
                # blocked the remote initiator, drop both the WS delivery and
                # the challenge notification. Fail-soft — when the initiator
                # can't be resolved (no ``from_user`` / unknown remote) we
                # deliver as before (legacy/unknown sender).
                initiator = await self._resolve_remote_initiator(
                    from_instance, from_user
                )
                # A per-user delivery names its initiator (v_18+ senders
                # always ship ``to_user`` with ``from_user``): it must be a
                # user of the sending household, so the recipient's block
                # list can always be applied.
                if initiator is None:
                    log.warning(
                        "app_federation: per-user %s from %s refused — "
                        "initiator %r is not a user of that household "
                        "(session=%s)",
                        kind,
                        from_instance,
                        from_user,
                        session_id,
                    )
                    return
                # Name the sender on the frame, like the same-household path:
                # ``from_instance`` is the signature-proven household and the
                # initiator just resolved as one of its users. The username
                # itself is that household's claim (it vouches for its own
                # people); it is the ``user_ref`` the app answers them by.
                frame["from_user"] = from_user
                if withdraw:
                    await self._withdrawn(
                        app_id,
                        recipient.user_id,
                        session_id,
                        from_instance=from_instance,
                        from_user=from_user,
                    )
                if await self._user_repo.is_blocked(
                    recipient.user_id, initiator.user_id
                ) or await self._guardian_blocked(recipient.user_id, initiator.user_id):
                    log.info(
                        "app_federation: dropping inbound %s for blocked initiator "
                        "(recipient=%s initiator=%s session=%s)",
                        kind,
                        recipient.user_id,
                        initiator.user_id,
                        session_id,
                    )
                    return
                if not await self._may_message_household(
                    recipient.user_id, from_instance
                ):
                    # §CP.F3 (D5): a protected account plays only with
                    # people it could already message.
                    log.info(
                        "app_federation: dropping inbound %s for a protected "
                        "account from a household it can't message "
                        "(recipient=%s session=%s)",
                        kind,
                        recipient.user_id,
                        session_id,
                    )
                    return
                allowed = await self.age_filter_recipients(
                    app_id, [recipient.user_id], app=app
                )
                log.info(
                    "app_federation deliver app=%s session=%s kind=%s routed=%s recipients=%d",
                    app_id,
                    session_id,
                    kind,
                    "user",
                    len(allowed),
                )
                # "Continue game": only for a session id this person has no
                # row for — never a game they know with someone (or anyone)
                # else, never a waiting invite re-pointed.
                if (
                    allowed
                    and continued
                    and await self._app_repo.get_pending_session(
                        app_id, recipient.user_id, session_id
                    )
                ):
                    log.info(
                        "app_federation: continue for a known session %s "
                        "from %s dropped",
                        session_id,
                        from_instance,
                    )
                    return
                if allowed:
                    if notify_open:
                        # Keep the invite until the recipient plays it ("Play"
                        # in Needs you hands it to the app as the launch
                        # context). Stored UNCONDITIONALLY (not gated on
                        # whether broadcast_to_user reached any sockets): a live
                        # SPA socket does not imply the app is mounted. The
                        # 14-day TTL + hourly prune bound growth.
                        await self._app_repo.add_pending_session(
                            AppPendingSession(
                                app_id=app_id,
                                user_id=recipient.user_id,
                                session_id=session_id,
                                from_instance=from_instance,
                                from_user=from_user,
                                # Never one of our own marks (sent / answered /
                                # withdrawn): another household can't pose
                                # an invite as one this person sent.
                                payload={
                                    k: v
                                    for k, v in payload.items()
                                    if k not in SESSION_MARKS
                                },
                                created_at=datetime.now(UTC).isoformat(),
                            )
                        )
                    else:
                        held = await self._hold(
                            app_id,
                            recipient.user_id,
                            session_id,
                            from_instance=from_instance,
                            from_user=from_user,
                            kind=kind,
                            payload=payload,
                        )
                        if held and ended is not None and from_user:
                            await self._game_ended(
                                app_id,
                                session_id,
                                recipient.user_id,
                                from_instance=from_instance,
                                from_user=from_user,
                                initiator=initiator,
                                told=ended,
                            )
                        if held and turn and app.enabled:
                            await self._your_turn(
                                app_id,
                                session_id,
                                recipient.user_id,
                                from_instance=from_instance,
                                from_user=from_user,
                                from_user_id=initiator.user_id,
                                from_display=await self._remote_open_display(
                                    from_instance, from_user, remote=initiator
                                ),
                            )
                        if held and _is_decline(kind, payload):
                            await self._emit(
                                AppChallengeDeclined(
                                    app_id=app_id,
                                    session_id=session_id,
                                    to_user_id=recipient.user_id,
                                    from_display=await self._remote_open_display(
                                        from_instance, from_user, remote=initiator
                                    ),
                                    from_user_id=initiator.user_id,
                                )
                            )
                    # Live into the app only in a game the person plays with
                    # that very sender (D3): never an invite before Play
                    # (it waits in Needs you), never a made-up session.
                    if app.enabled and await self._plays_with(
                        app_id, recipient.user_id, session_id, from_instance, from_user
                    ):
                        await self._ws.broadcast_to_user(recipient.user_id, frame)
                    if notify_open:
                        from_display = await self._remote_open_display(
                            from_instance, from_user, remote=initiator
                        )
                        await self._emit(
                            AppChallengeReceived(
                                app_id=app_id,
                                session_id=session_id,
                                to_user_id=recipient.user_id,
                                from_display=from_display,
                                from_user_id=initiator.user_id,
                                continued=continued,
                            )
                        )
                return

        if continued:
            # A "continue game" always names one person (never a fan-out).
            return

        # Legacy / best-effort fan-out to every active, age-eligible local user
        # (soft-deleted/inactive accounts are excluded — same as /friends).
        users = await self._user_repo.list_active()
        user_ids = [u.user_id for u in users]
        if not user_ids:
            return

        # Recipients who blocked the named initiator never get the frame
        # (same rule as the per-user path). An unnamed initiator (an older
        # sender) can't be checked.
        initiator = await self._resolve_remote_initiator(from_instance, from_user)
        if initiator is not None:
            # A named, known sender rides the frame (see the per-user path);
            # an unknown name is not passed on.
            frame["from_user"] = from_user
            user_ids = [
                uid
                for uid in user_ids
                if not await self._user_repo.is_blocked(uid, initiator.user_id)
                and not await self._guardian_blocked(uid, initiator.user_id)
            ]
        else:
            # §CP.F2: an unnamed sender could be anyone there — skip a
            # protected account whose guardian blocked someone on it.
            user_ids = [
                uid
                for uid in user_ids
                if not await self._guardian_blocks_household(uid, from_instance)
            ]

        # §CP.F3 (D5): never a protected account a household it can't
        # message reaches.
        user_ids = [
            uid
            for uid in user_ids
            if await self._may_message_household(uid, from_instance)
        ]

        # Age-gate filter: skip recipients who are protected minors below
        # the app's minimum age.  Fast path when the app has no restriction.
        user_ids = await self.age_filter_recipients(app_id, user_ids, app=app)

        if not user_ids:
            return

        log.info(
            "app_federation deliver app=%s session=%s kind=%s routed=%s recipients=%d",
            app_id,
            session_id,
            kind,
            "broadcast",
            len(user_ids),
        )
        await self._ws.broadcast_to_users(user_ids, frame)

    async def _game_ended(
        self,
        app_id: str,
        session_id: str,
        user_id: str,
        *,
        from_instance: str,
        from_user: str,
        initiator: RemoteUser,
        told: set[tuple[str, str, str]],
    ) -> None:
        """Tell *user_id* their game with this opponent ended (D9) — once
        per opponent per inbound ``ended`` message (*told*)."""
        key = (user_id, from_instance, from_user)
        if key in told:
            return
        told.add(key)
        await self._emit(
            AppGameEnded(
                app_id=app_id,
                session_id=session_id,
                to_user_id=user_id,
                from_display=await self._remote_open_display(
                    from_instance, from_user, remote=initiator
                ),
                from_user_id=initiator.user_id,
            )
        )

    async def _on_ended(self, event: FederationEvent) -> None:
        """An inbound ``APP_SESSION {verb: "close", reason: "ended",
        sessions: [...]}`` (v_72): the other household uninstalled the app.

        Each entry ``{session_id, to_user, from_user}`` goes through the
        same per-person path as a single close (:meth:`_deliver`): it ends
        a waiting invite only when that household and person opened it,
        and is held (live into an open app) only for a game the addressed
        person plays with exactly that sender — so a household can end
        only its own people's games, never anyone else's. At most
        :data:`ENDED_SESSIONS_MAX` entries; a re-delivered entry is
        skipped."""
        app_id = event.payload.get("app_id")
        sessions = event.payload.get("sessions")
        if not isinstance(app_id, str) or not app_id or not isinstance(sessions, list):
            log.debug("app_federation: malformed ended close — dropping")
            return
        if len(sessions) > ENDED_SESSIONS_MAX:
            log.warning(
                "app_federation: ended close from %s with %d sessions — "
                "keeping the first %d",
                event.from_instance,
                len(sessions),
                ENDED_SESSIONS_MAX,
            )
        told: set[tuple[str, str, str]] = set()
        for entry in sessions[:ENDED_SESSIONS_MAX]:
            if not isinstance(entry, dict):
                continue
            session_id = entry.get("session_id")
            to_user = entry.get("to_user")
            from_user = entry.get("from_user")
            if not (
                isinstance(session_id, str)
                and session_id
                and isinstance(to_user, str)
                and to_user
                and isinstance(from_user, str)
                and from_user
            ):
                continue
            # Keyed on the whole entry (per sending household): an entry
            # naming the wrong person must not suppress the real one.
            if self._seen_open_session(
                event.from_instance, f"ended:{from_user}:{to_user}:{session_id}"
            ):
                continue
            await self._deliver(
                app_id,
                session_id,
                from_instance=event.from_instance,
                payload={
                    "app_id": app_id,
                    "session_id": session_id,
                    "verb": "close",
                    "reason": CLOSE_ENDED,
                },
                kind="session",
                to_user=to_user,
                from_user=from_user,
                withdraw=True,
                ended=told,
            )

    # ─── Uninstall: what is lost, and telling opponents (D9) ─────────────

    async def sessions_ending(
        self, app_id: str
    ) -> list[tuple[AppPendingSession, str, bool, bool]]:
        """Every session of *app_id* an uninstall ends with someone in
        another household: ``(row, kind, ended, tell)`` — *kind* ``"game"``
        or ``"invite"`` (:func:`_ending_kind`), *ended* whether that
        household hears ``reason: "ended"`` (v_72), *tell* whether anything
        may reach the other side at all (never across a guardian block,
        like a decline). Read before the uninstall: the rows go with the
        app."""
        own = self._federation.own_instance_id
        recent_cut = (
            datetime.now(UTC) - timedelta(seconds=UNINSTALL_GAME_RECENT_SECONDS)
        ).isoformat()
        modern: dict[str, bool] = {}
        out: list[tuple[AppPendingSession, str, bool, bool]] = []
        for row in await self._app_repo.list_app_sessions(app_id):
            kind = _ending_kind(row, own, recent_cut)
            if kind is None:
                continue
            if row.from_instance not in modern:
                modern[row.from_instance] = await self._federation.peer_supports(
                    row.from_instance,
                    min_version=FederationCapability.MIN_FOR_APP_SESSION_ENDED,
                )
            other = await self._invite_sender_id(row)
            tell = other is None or not await self._guardian_blocked(row.user_id, other)
            out.append((row, kind, modern[row.from_instance], tell))
        return out

    async def preview_uninstall(self, app_id: str) -> AppUninstallPreview:
        """The uninstall dialog's counts (D9): people here with saved
        data, games and open invites with other households, group games
        with them (``tables``, v_76), and who will be told — names the
        admin already sees for those households' people, never what the
        games hold nor who here plays whom."""
        games = invites = untold = 0
        names: list[str] = []
        seen: set[tuple[str, str]] = set()
        for row, kind, modern, tell in await self.sessions_ending(app_id):
            if kind == "game":
                games += 1
                if not modern or not tell:
                    untold += 1
                    continue
            else:
                invites += 1
                if not tell:
                    continue
                if not modern and not await self._federation.peer_supports(
                    row.from_instance,
                    min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
                ):
                    continue
            key = (row.from_instance, str(row.from_user))
            if key in seen:
                continue
            seen.add(key)
            name = await self._remote_open_display(row.from_instance, row.from_user)
            if name not in names:
                names.append(name)
        return AppUninstallPreview(
            people=await self._app_repo.kv_people(app_id),
            games=games,
            invites=invites,
            told=tuple(names[:UNINSTALL_NAMES_MAX]),
            told_more=max(0, len(names) - UNINSTALL_NAMES_MAX),
            untold=untold,
            tables=(
                await self._groups.tables_ending(app_id)
                if self._groups is not None
                else 0
            ),
        )

    async def tell_opponents(
        self,
        app_id: str,
        ending: list[tuple[AppPendingSession, str, bool, bool]],
    ) -> None:
        """The app was uninstalled: tell the other side of every session in
        *ending* (:meth:`sessions_ending`, read before the uninstall).

        A v_72 household gets ONE ``APP_SESSION {verb: "close", reason:
        "ended", sessions: [{session_id, to_user, from_user}, ...]}`` for
        all of them (chunks of :data:`ENDED_SESSIONS_MAX`, one opponent's
        entries kept together); an older one the per-session close it
        already knows for each pending invite — ``declined`` for an invite
        it sent us, ``withdrawn`` for one we sent — and nothing for a game.
        Nothing crosses a guardian block (like a decline).
        Households are told in parallel, each best effort: the uninstall
        stands if a send fails."""
        per_household: dict[str, list[dict[str, str]]] = {}
        legacy: list[tuple[AppPendingSession, str]] = []
        usernames: dict[str, str | None] = {}
        for row, kind, modern, tell in ending:
            if not tell:
                continue
            if row.user_id not in usernames:
                me = await self._user_repo.get_by_user_id(row.user_id)
                usernames[row.user_id] = me.username if me is not None else None
            username = usernames[row.user_id]
            if not username or not row.from_user:
                continue
            if modern:
                per_household.setdefault(row.from_instance, []).append(
                    {
                        "session_id": row.session_id,
                        "to_user": row.from_user,
                        "from_user": username,
                    }
                )
            elif kind == "invite":
                legacy.append((row, username))
        # One opponent's entries stay in one chunk where they fit: the
        # receiver tells each player once per opponent per event.
        for entries in per_household.values():
            entries.sort(key=lambda e: (e["to_user"], e["from_user"]))
        sends = [
            self._send_ended(app_id, instance, entries[i : i + ENDED_SESSIONS_MAX])
            for instance, entries in per_household.items()
            for i in range(0, len(entries), ENDED_SESSIONS_MAX)
        ]
        sends += [
            self._send_close(
                app_id=app_id,
                session_id=row.session_id,
                actor_user_id=row.user_id,
                to_instance=row.from_instance,
                to_user=str(row.from_user),
                # An invite they sent us: our side said no. One we sent:
                # taken back.
                reason=CLOSE_DECLINED
                if row.state == SESSION_INVITED
                else CLOSE_WITHDRAWN,
            )
            for row, _username in legacy
        ]
        if sends:
            await asyncio.gather(*sends)

    async def _send_ended(
        self, app_id: str, to_instance: str, entries: list[dict[str, str]]
    ) -> None:
        """One bundled ``ended`` close to *to_instance* (best effort)."""
        try:
            await self._federation.send_event(
                to_instance_id=to_instance,
                event_type=FederationEventType.APP_SESSION,
                payload={
                    "app_id": app_id,
                    "verb": "close",
                    "reason": CLOSE_ENDED,
                    "sessions": entries,
                },
            )
        except Exception as exc:
            log.warning(
                "app_federation: ended close (%d sessions) to %s not sent: %s",
                len(entries),
                to_instance,
                exc,
                exc_info=True,
            )

    # ─── Invites that wait for their app (D10) ───────────────────────────

    async def _keep_waiting(
        self,
        app_id: str,
        app: InstalledApp | None,
        session_id: str,
        *,
        from_instance: str,
        to_user: str,
        from_user: str | None,
        others: int | None = None,
        keep: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """An invite for an app this household doesn't have (*app* ``None``)
        or turned off reaches its person anyway (D10): after every rule a
        playable invite passes — the addressee is a user here, the sender
        a known user of ``from_instance``, no block or guardian block
        between them, a protected invitee only from a household it may
        message, the app's age gate (the catalog's rating for an app not
        installed) — it waits as their "Needs you" row with "Ask an admin"
        (:class:`AppChallengeWaiting`). The app's name comes from this
        household's catalog / install, never from the invite. An app the
        catalog doesn't list (or hides from the invitee for their age)
        leaves no row, as before.

        Nothing is sent back — no reply, no "unavailable", no read
        receipt: whether this household has the app, or who here is
        age-limited, is never revealed. The sender's invite just waits (it
        runs out after :data:`PENDING_SESSION_TTL_SECONDS`).

        A group game's invite (v_76, :meth:`keep_group_waiting`) carries
        *others* (how many more were asked) and *keep*: the table's own
        rules and its copy of the table, run last — ``False`` drops it."""
        if self._notifs is None or not _WAITING_SESSION_RE.fullmatch(session_id):
            return
        recipient = await self._user_repo.get(to_user)
        initiator = await self._resolve_remote_initiator(from_instance, from_user)
        if recipient is None or initiator is None or not recipient.is_active():
            log.info(
                "app_federation: invite for app %r without it dropped — unknown "
                "addressee or sender (session=%s)",
                app_id,
                session_id,
            )
            return
        if not await self._may_wait(recipient, initiator, from_instance):
            return
        if app is not None:
            # Turned off: the install's own name, permissions and age gate.
            if PERMISSION_FEDERATION not in app.permissions or not (
                await self.age_filter_recipients(app_id, [recipient.user_id], app=app)
            ):
                return
            if await self._app_repo.get_pending_session(
                app_id, recipient.user_id, session_id
            ):
                return  # never re-point a session this person already has
            name, reason = app.name or app_id, "app_off"
        else:
            entry = await self._catalog_entry(app_id, recipient.user_id)
            if entry is None or (
                entry.permissions and PERMISSION_FEDERATION not in entry.permissions
            ):
                return
            name, reason = entry.name or app_id, "app_missing"
        if await self._waiting_row(app_id, recipient.user_id, session_id):
            return  # the same invite again (after a restart): one row
        senders = {
            r.params.actors[-1].id
            for r in await self._waiting_rows(app_id, user_id=recipient.user_id)
            if r.params is not None
        }
        if initiator.user_id not in senders and len(senders) >= WAITING_SENDERS_MAX:
            # The receiver's bound (a repeat from a sender joins their row).
            log.debug(
                "app_federation: invite for app %r dropped — %d senders already "
                "wait for this person (session=%s)",
                app_id,
                len(senders),
                session_id,
            )
            return
        if keep is not None and not await keep():
            return
        log.info(
            "app_federation: invite for %s app %r waits for its person (session=%s)",
            reason,
            app_id,
            session_id,
        )
        await self._emit(
            AppChallengeWaiting(
                app_id=app_id,
                session_id=session_id,
                to_user_id=recipient.user_id,
                from_display=await self._remote_open_display(
                    from_instance, from_user, remote=initiator
                ),
                from_user_id=initiator.user_id,
                app_name=name,
                reason=reason,
                others=others,
            )
        )
        now = await self._app_repo.get(app_id)
        if now is not None and now.enabled:
            # The app arrived while this invite was on its way here — its
            # own pass may have run already: the invite becomes playable
            # now rather than wait for an app the household has.
            await self._make_waiting_playable(app_id)

    async def keep_group_waiting(
        self,
        app_id: str,
        app: InstalledApp | None,
        session_id: str,
        *,
        from_instance: str,
        to_user: str,
        from_user: str,
        others: int,
        keep: Callable[[], Awaitable[bool]],
    ) -> None:
        """A group game's invite for an app this household doesn't have or
        turned off (D10, v_76): it waits like a 1:1 invite
        (:meth:`_keep_waiting` — the same receiver rules, the catalog's
        name and age gate, nothing back to the hub), in order with the
        delivery jobs; *keep* is the table's service's last word (its
        rules, its copy of the table)."""
        await self._in_order(
            self._keep_waiting,
            app_id,
            app,
            session_id,
            from_instance=from_instance,
            to_user=to_user,
            from_user=from_user,
            others=others,
            keep=keep,
        )

    async def end_waiting(
        self,
        app_id: str,
        session_id: str,
        *,
        outcome: str,
        user_id: str | None = None,
    ) -> None:
        """A group game's invite that waits for its app here ends (D10,
        v_76): its table ended, or the hub withdrew it — for *user_id*, or
        everyone here it waits for. Its row reads as *outcome*. Nothing is
        sent."""
        await self._in_order(
            self._end_waiting, app_id, session_id, outcome=outcome, user_id=user_id
        )

    async def _end_waiting(
        self, app_id: str, session_id: str, *, outcome: str, user_id: str | None
    ) -> None:
        for row in await self._waiting_rows(
            app_id, user_id=user_id, session_id=session_id
        ):
            await self._emit(
                AppChallengeEnded(
                    app_id=app_id,
                    user_id=row.user_id,
                    session_ids=(session_id,),
                    outcome=outcome,
                )
            )

    async def _may_wait(
        self, recipient: User, initiator: RemoteUser, from_instance: str
    ) -> bool:
        """The rules a playable invite passes (:meth:`_deliver`): no block
        either way the recipient set, no guardian block, and (§CP.F3) a
        protected recipient only from a household it may message."""
        if await self._user_repo.is_blocked(
            recipient.user_id, initiator.user_id
        ) or await self._guardian_blocked(recipient.user_id, initiator.user_id):
            return False
        return await self._may_message_household(recipient.user_id, from_instance)

    async def _catalog_entry(self, app_id: str, user_id: str) -> AppCatalogEntry | None:
        """*app_id*'s entry in this household's catalog as *user_id* may
        see it (D5: an app rated above a protected account's age isn't
        there) — ``None`` when it isn't, or the catalog can't be read."""
        if self._catalog is None:
            return None
        try:
            entries = await self._catalog.browse_catalog_for(
                user_id=user_id, is_admin=False
            )
        except Exception as exc:  # noqa: BLE001 — the invite just isn't kept
            log.warning("app_federation: catalog unreadable for an invite: %s", exc)
            return None
        return next((e for e in entries if e.app_id == app_id), None)

    async def _waiting_rows(
        self,
        app_id: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> list[Notification]:
        """The invites that wait for *app_id* (D10) — every person's,
        *user_id*'s, *user_id*'s for *session_id*, or everyone's for
        *session_id*: open ``app_challenge_waiting`` rows."""
        if self._notifs is None:
            return []
        key = object_key(NotificationObject.APP_SESSION, f"{app_id}/{session_id}")
        if user_id is None and session_id is not None:
            where: list[tuple[str, str, Any]] = [("object_key", "=", key)]
        elif user_id is not None and session_id is None:
            where = [
                ("type", "=", WAITING_INVITE_TYPE),
                ("user_id", "=", user_id),
            ]
        elif user_id is not None:
            where = [
                ("object_key", "=", key),
                ("user_id", "=", user_id),
            ]
        else:
            where = [("type", "=", WAITING_INVITE_TYPE)]
        prefix = object_key(NotificationObject.APP_SESSION, f"{app_id}/")
        return [
            n
            for n in await self._notifs.find(Spec(where=where))
            if n.type == WAITING_INVITE_TYPE
            and n.is_pending_action
            and (n.object_key or "").startswith(prefix)
            and n.params is not None
            and n.params.actors
        ]

    async def _waiting_row(
        self, app_id: str, user_id: str, session_id: str
    ) -> Notification | None:
        """*user_id*'s invite *session_id* that waits for *app_id*."""
        rows = await self._waiting_rows(app_id, user_id=user_id, session_id=session_id)
        return rows[0] if rows else None

    async def _withdraw_waiting(
        self,
        app_id: str,
        session_id: str,
        *,
        from_instance: str,
        to_user: str,
        from_user: str | None,
    ) -> None:
        """The challenger withdrew an invite that waits for its app here:
        it stops waiting — only when they are who sent it."""
        recipient = await self._user_repo.get(to_user)
        initiator = await self._resolve_remote_initiator(from_instance, from_user)
        if recipient is None or initiator is None:
            return
        row = await self._waiting_row(app_id, recipient.user_id, session_id)
        if row is None or row.params is None:
            return
        if row.params.actors[-1].id != initiator.user_id:
            return
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=recipient.user_id,
                session_ids=(session_id,),
                outcome="withdrawn",
            )
        )

    async def decline_waiting(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> bool:
        """The invitee declines an invite that waits for its app (D10):
        its "Needs you" row reads as declined. Nothing is sent — the app
        isn't usable here, so (like any decline for a turned-off app) the
        sender's invite just runs out. ``False`` when no such invite
        waits for them (the caller tries a playable one)."""
        if await self._waiting_row(app_id, actor_user_id, session_id) is None:
            return False
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome="declined",
            )
        )
        return True

    async def on_app_usable(self, event: AppInstalled | AppEnabledChanged) -> None:
        """The app was added or turned on: its waiting invites become
        playable (:meth:`_make_waiting_playable`) — once every invite,
        decline and withdrawal queued before it has landed."""
        if isinstance(event, AppEnabledChanged) and not event.enabled:
            return
        await self._in_order(self._make_waiting_playable, event.app_id)

    async def _make_waiting_playable(self, app_id: str) -> None:
        """The app was added or turned on: every invite that waited for it
        becomes a normal invite (Play / Decline, a push per the person's
        settings) — a session row kept for the invitee from when the
        invite arrived, so its TTL counts from then. An invite past its
        TTL ends ``expired``; one that no longer passes the receiver rules
        (a block since, the age gate of the installed app, the sender
        gone, an app without ``federation``) ends ``done``. Still nothing
        goes to the sender.

        The receiver's bounds hold here too: only the newest invite per
        (invitee, sender person) becomes playable, and only the newest
        :data:`WAITING_SENDERS_MAX` senders' per invitee; any other
        waiting row ends ``done``. A group game's invite (flag ``group``,
        v_76) is the table's service's to turn into a Join
        (:meth:`AppGroupService.waiting_playable`) — outside those bounds:
        its table was bounded when it arrived."""
        app = await self._app_repo.get(app_id)
        if app is None or not app.enabled:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)
        rows = sorted(
            await self._waiting_rows(app.app_id),
            key=lambda n: n.created_at,
            reverse=True,
        )
        seen: set[tuple[str, str]] = set()
        senders: dict[str, int] = {}
        outcome: str | None
        for row in rows:
            session_id = (row.object_key or "").partition("/")[2]
            assert row.params is not None  # _waiting_rows keeps rows with actors
            pair = (row.user_id, row.params.actors[-1].id)
            if "group" in row.params.flags:
                outcome = (
                    await self._groups.waiting_playable(app, row, session_id, cutoff)
                    if self._groups is not None
                    else "done"
                )
            elif pair in seen or senders.get(row.user_id, 0) >= WAITING_SENDERS_MAX:
                outcome = "done"
            else:
                seen.add(pair)
                senders[row.user_id] = senders.get(row.user_id, 0) + 1
                outcome = await self._make_playable(app, row, session_id, cutoff)
            if outcome is not None:
                await self._emit(
                    AppChallengeEnded(
                        app_id=app.app_id,
                        user_id=row.user_id,
                        session_ids=(session_id,),
                        outcome=outcome,
                    )
                )

    async def _make_playable(
        self,
        app: InstalledApp,
        row: Notification,
        session_id: str,
        cutoff: datetime,
    ) -> str | None:
        """Turn one waiting invite into a playable one; ``None`` when it
        did, else how it ended instead."""
        if row.created_at < cutoff.isoformat():
            return "expired"
        assert row.params is not None  # _waiting_rows keeps rows with actors
        remote = await self._user_repo.get_remote(row.params.actors[-1].id)
        recipient = await self._user_repo.get_by_user_id(row.user_id)
        if (
            remote is None
            or remote.deprovisioned_at is not None
            or recipient is None
            or not recipient.is_active()
            or PERMISSION_FEDERATION not in app.permissions
            or not await self._may_wait(recipient, remote, remote.instance_id)
            or not await self.age_filter_recipients(
                app.app_id, [recipient.user_id], app=app
            )
            or await self._app_repo.get_pending_session(
                app.app_id, recipient.user_id, session_id
            )
        ):
            return "done"
        await self._app_repo.add_pending_session(
            AppPendingSession(
                app_id=app.app_id,
                user_id=recipient.user_id,
                session_id=session_id,
                from_instance=remote.instance_id,
                from_user=remote.remote_username,
                payload={
                    "app_id": app.app_id,
                    "session_id": session_id,
                    "verb": "open",
                },
                created_at=row.created_at,
            )
        )
        await self._emit(
            AppChallengeReceived(
                app_id=app.app_id,
                session_id=session_id,
                to_user_id=recipient.user_id,
                from_display=await self._remote_open_display(
                    remote.instance_id, remote.remote_username, remote=remote
                ),
                from_user_id=remote.user_id,
            )
        )
        return None

    async def accept_session(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> AppPendingSession:
        """The invitee plays one game invite ("Play" in Needs you).

        Takes that one invite in — and only it: every other invite keeps
        waiting in "Needs you" (opening the app accepts nothing). Its row
        reads as accepted and the invite is returned, so the host can hand
        the game to the app as its launch context. The same one atomic
        step as a decline: a second device, a decline or a withdrawal
        racing it gets :class:`AppChallengeHandledError` — as does an
        invite past its TTL (row ``expired``) or one from someone a
        guardian block separates from the invitee since it arrived (row
        ``done``). A played invite stays known as an ``active`` game: the
        other side's moves wait in its mailbox from now on."""
        app = await self.require_enabled(app_id)
        await self.assert_age_allowed(app_id, actor_user_id, app=app)
        await self._refuse_group_seat(app_id, actor_user_id, session_id)
        invite = await self._app_repo.activate_pending_session(
            app_id, actor_user_id, session_id, now=datetime.now(UTC).isoformat()
        )
        if invite is None:
            raise AppChallengeHandledError()
        outcome = "accepted"
        cutoff = datetime.now(UTC) - timedelta(seconds=PENDING_SESSION_TTL_SECONDS)
        if invite.created_at < cutoff.isoformat():
            outcome = "expired"
        else:
            # §CP.F2: an invite from someone a guardian block separates
            # from this user since it arrived is not handed to the app.
            sender = await self._invite_sender_id(invite)
            if (
                sender is not None
                and await self._guardian_blocked(actor_user_id, sender)
            ) or not await self._may_message_household(
                actor_user_id, invite.from_instance
            ):
                # …nor one from a household a protected person can't
                # message (§CP.F3, D5 — an invite from before protection).
                outcome = "done"
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome=outcome,
            )
        )
        if outcome != "accepted":
            await self._app_repo.delete_pending_session(
                app_id, actor_user_id, session_id
            )
            raise AppChallengeHandledError()
        return invite

    async def take_held(
        self, *, app_id: str, actor_user_id: str
    ) -> list[AppPendingSession]:
        """The person's app opened: the messages their mailbox kept (D11),
        the latest per session, oldest first — cleared as they are handed
        over. Each row's ``held`` is ``{kind, payload}`` and its
        ``from_instance`` / ``from_user`` name the sender. Only games the
        person plays (``active``): what came for an invite still waiting in
        Needs you stays held until Play (D3 — the app doesn't learn the
        inviter before). A message from someone the person blocked, or a
        guardian block now separates them from, or (§CP.F3) from a
        household a protected person can't message, is dropped instead."""
        app = await self.require_enabled(app_id)
        await self.assert_age_allowed(app_id, actor_user_id, app=app)
        taken = await self._app_repo.take_held(app_id, actor_user_id)
        if taken:
            # Those games are in front of the person now: their "Your
            # turn" rows read as read.
            await self._emit(
                AppTurnsSeen(
                    app_id=app_id,
                    user_id=actor_user_id,
                    session_ids=tuple(h.session_id for h in taken),
                )
            )
        out: list[AppPendingSession] = []
        for held in taken:
            sender = await self._invite_sender_id(held)
            if sender is not None and (
                await self._user_repo.is_blocked(actor_user_id, sender)
                or await self._guardian_blocked(actor_user_id, sender)
            ):
                continue
            if not await self._may_message_household(actor_user_id, held.from_instance):
                continue
            out.append(held)
        return out

    async def _refuse_group_seat(
        self, app_id: str, user_id: str, session_id: str
    ) -> None:
        """Raise :class:`AppGroupSessionError` when *session_id* is a seat
        at a group table: Play / Decline answer it through the table."""
        row = await self._app_repo.get_pending_session(app_id, user_id, session_id)
        if row is not None and row.is_group:
            raise AppGroupSessionError()

    async def _invite_sender_id(self, invite: AppPendingSession) -> str | None:
        """The user id of who opened *invite*: a local user id as stored, or
        the ``remote_users`` id of a remote challenger (``None`` unknown)."""
        if not invite.from_user:
            return None
        if invite.from_instance == self._federation.own_instance_id:
            return invite.from_user
        remote = await self._user_repo.get_remote_by_member(
            invite.from_instance, invite.from_user
        )
        return remote.user_id if remote is not None else None

    # ─── Game invites: decline / withdraw ────────────────────────────────

    async def decline_session(
        self, *, app_id: str, session_id: str, actor_user_id: str
    ) -> None:
        """The invitee declines a game invite that waits for them.

        The invite is removed in one step (a second device, the app taking
        it in or a withdrawal racing it: the loser gets
        :class:`AppChallengeHandledError`), its "Needs you" item reads as
        declined, and the challenger's app hears ``{verb: "close", reason:
        "declined"}`` — over WebSocket when they are on this household,
        else as an ``APP_SESSION`` to their household (best effort: the
        decline stands if that send fails). Nothing is sent for an app that
        is no longer enabled here."""
        await self._refuse_group_seat(app_id, actor_user_id, session_id)
        invite = await self._app_repo.take_pending_session(
            app_id, actor_user_id, session_id
        )
        if invite is None:
            raise AppChallengeHandledError()
        await self._emit(
            AppChallengeEnded(
                app_id=app_id,
                user_id=actor_user_id,
                session_ids=(session_id,),
                outcome="declined",
            )
        )
        app = await self._app_repo.get(app_id)
        if app is None or not app.enabled or not invite.from_user:
            return
        if (
            invite.from_instance != self._federation.own_instance_id
            and PERMISSION_FEDERATION not in app.permissions
        ):
            # The invite is gone here; nothing leaves the household for an
            # app that may no longer send to other households.
            return
        # §CP.F2: an invite from before a guardian block is still declined
        # here, but nothing reaches the person on the other side of it.
        sender = await self._invite_sender_id(invite)
        if sender is not None and await self._guardian_blocked(actor_user_id, sender):
            return
        await self._send_close(
            app_id=app_id,
            session_id=session_id,
            actor_user_id=actor_user_id,
            to_instance=invite.from_instance,
            to_user=invite.from_user,
            reason="declined",
        )

    async def cancel_session(
        self, *, app_id: str, session_id: str, target: dict, actor_user_id: str
    ) -> None:
        """The challenger withdraws a game invite they sent to *target*.

        ``target`` is the same ``{instance_id, user_ref, is_local}`` the
        session was opened with. A local invitee's invite stops waiting
        here (only one this challenger opened); a remote invitee's
        household gets ``APP_SESSION {verb: "close", reason: "withdrawn"}``
        and does the same on its side. Either way the invitee's app hears
        the close. Only for a game this household knows the actor plays
        with *target* (:class:`AppSessionUnknownError` otherwise); an
        invite they sent is marked withdrawn — it stops counting as open,
        and nothing more goes out in it."""
        await self.require_enabled(app_id, target)
        known = await self._known_session(app_id, actor_user_id, session_id, target)
        await self._assert_person_allowed(actor_user_id, target)
        if known.sent:
            await self._app_repo.withdraw_session(app_id, actor_user_id, session_id)
        own = self._federation.own_instance_id
        if target.get("is_local"):
            await self._withdrawn(
                app_id,
                target["user_ref"],
                session_id,
                from_instance=own,
                from_user=actor_user_id,
            )
        await self._send_close(
            app_id=app_id,
            session_id=session_id,
            actor_user_id=actor_user_id,
            to_instance=own if target.get("is_local") else target["instance_id"],
            to_user=target["user_ref"],
            reason="withdrawn",
        )

    async def _withdrawn(
        self,
        app_id: str,
        user_id: str,
        session_id: str,
        *,
        from_instance: str,
        from_user: str | None,
    ) -> None:
        """The challenger (``from_instance`` / ``from_user``) ended an
        invite that waited for ``user_id``: it stops waiting — only when
        they are who opened it."""
        if not from_user:
            return
        row = await self._app_repo.get_pending_session(app_id, user_id, session_id)
        if row is None or row.is_group:
            # A group seat ends only through its table (v_76).
            return
        taken = await self._app_repo.take_pending_session(
            app_id,
            user_id,
            session_id,
            from_instance=from_instance,
            from_user=from_user,
        )
        if taken is not None:
            await self._emit(
                AppChallengeEnded(
                    app_id=app_id,
                    user_id=user_id,
                    session_ids=(session_id,),
                    outcome="withdrawn",
                )
            )

    async def _send_close(
        self,
        *,
        app_id: str,
        session_id: str,
        actor_user_id: str,
        to_instance: str,
        to_user: str,
        reason: str,
    ) -> None:
        """Tell the other side's app the session ended: ``{verb: "close",
        reason}``. On this household ``to_user`` is a user id and the frame
        goes over WebSocket; on another household it is their username and
        rides ``APP_SESSION`` (v_18+ households only — older ones can't
        address a person, and a close is not worth a household fan-out)."""
        payload: dict[str, Any] = {
            "app_id": app_id,
            "session_id": session_id,
            "verb": "close",
            "reason": reason,
        }
        own = self._federation.own_instance_id
        if to_instance == own:
            recipients = await self.age_filter_recipients(app_id, [to_user])
            # Live only into a game they play with the actor (D3).
            await self._emit_frame(
                [
                    uid
                    for uid in recipients
                    if await self._plays_with(
                        app_id, uid, session_id, own, actor_user_id
                    )
                ],
                app_id=app_id,
                session_id=session_id,
                from_instance=own,
                from_user=actor_user_id,
                kind="session",
                payload=payload,
            )
            # The mailbox: the close waits for their app like a remote one.
            if (
                to_user in recipients
                and await self._hold(
                    app_id,
                    to_user,
                    session_id,
                    from_instance=own,
                    from_user=actor_user_id,
                    kind="session",
                    payload=payload,
                )
                and _is_decline("session", payload)
            ):
                me = await self._user_repo.get_by_user_id(actor_user_id)
                await self._emit(
                    AppChallengeDeclined(
                        app_id=app_id,
                        session_id=session_id,
                        to_user_id=to_user,
                        from_display=(
                            me.display_name if me is not None else actor_user_id
                        ),
                        from_user_id=actor_user_id,
                    )
                )
            return
        try:
            if not await self._federation.peer_supports(
                to_instance,
                min_version=FederationCapability.MIN_FOR_APP_USER_ROUTING,
            ):
                return
            me = await self._user_repo.get_by_user_id(actor_user_id)
            payload["to_user"] = to_user
            payload["from_user"] = me.username if me is not None else actor_user_id
            await self._federation.send_event(
                to_instance_id=to_instance,
                event_type=FederationEventType.APP_SESSION,
                payload=payload,
            )
        except Exception as exc:
            # The answer stands on this side; the other app just isn't told.
            log.warning(
                "app_federation: close (%s) for session %s to %s not sent: %s",
                reason,
                session_id,
                to_instance,
                exc,
                exc_info=True,
            )
