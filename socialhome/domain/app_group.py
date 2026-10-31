"""Group sessions ("tables") for Social Home Apps — pure domain (v_76).

A *table* is an app session with 2–30 seats. Its **host household** (the
*hub*: the household of the person who opened it) keeps the roster and is
the only household the others talk to about it; the *host person* may
change (hand-over), the hub never does. Every other household with a
player keeps a copy of the table (a *mirror*) as the hub last sent it.

The wire shape is one *table snapshot* (:meth:`GroupSession.to_wire`),
carried sealed inside ``APP_SESSION`` by the hub's ``group_invite`` /
``group_seat`` / ``group_roster`` / ``group_start`` / ``group_close``; a
receiver keeps a snapshot only when its ``rev`` is newer than what it
holds. :func:`parse_table` is the receiver's bound check — anything out
of shape raises :class:`GroupWireError` and the event is dropped.

People are named by ``(instance, user)`` — their household and their
username there — on the wire and in storage, never by a local user id:
the roster is shared between households.

Pure data, no I/O.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .errors import CodedError

# ─── Bounds (design 2.1 / 2.12) ───────────────────────────────────────────

#: The most players any table seats (every path; an app's manifest
#: ``players.max`` above it is cut to it at install).
GROUP_MAX_PLAYERS: int = 30
#: The fewest players a table takes.
GROUP_MIN_PLAYERS: int = 2
#: A table opened by picking people (the invite sheet) seats at most this
#: many — the host and up to nine people picked.
GROUP_PICK_MAX_PLAYERS: int = 10
#: Roster entries a table may carry at all (seated, left, removed — and,
#: on the hub only, invited / declined). Bounds a hub's snapshot.
GROUP_ROSTER_MAX: int = 2 * GROUP_MAX_PLAYERS
#: Tables waiting for players (``lobby``) one person hosts in one app.
GROUP_OPEN_TABLES_MAX: int = 5
#: New tables one person opens in one app in any 24 hours. The people
#: invited also count against the 1:1 invite caps (one per person).
GROUP_TABLES_PER_DAY_MAX: int = 10
#: The host's own words on a table ("Bring snacks").
GROUP_NOTE_MAX: int = 80
#: A display name or household name on the wire.
GROUP_NAME_MAX: int = 80
#: A username / household id on the wire.
GROUP_REF_MAX: int = 128
#: A seat number on the wire.
GROUP_SEAT_MAX: int = 999
#: The hub sends the seated households at most one roster update per this
#: many seconds per table (coalesced); a join answer goes out at once.
GROUP_ROSTER_EVERY_SECONDS: float = 5.0
#: How long a table may wait for players, in days (the host picks).
GROUP_LOBBY_DAYS: tuple[int, ...] = (1, 3, 7, 14)
GROUP_LOBBY_DEFAULT_DAYS: int = 3
#: A snapshot's ``expires_at`` may lie at most this far ahead (the longest
#: lobby plus clock slack): never past the 14-day invite TTL.
GROUP_EXPIRES_MAX: timedelta = timedelta(days=14, hours=1)
#: A table that ended is kept this long (the lobby / app can still say
#: why), then pruned — it holds names of people from other households.
GROUP_ENDED_KEEP_SECONDS: int = 7 * 24 * 60 * 60
#: A mirror whose lobby ran out is ended locally after this grace (the hub
#: may be offline and never send its close).
GROUP_MIRROR_GRACE_SECONDS: int = 60 * 60
#: A receiver keeps at most this many open tables from one hub household
#: (its own bound — the hub's caps don't hold for a hub that ignores them).
GROUP_MIRRORS_PER_HUB_MAX: int = 50

# ─── States ───────────────────────────────────────────────────────────────

GROUP_LOBBY: str = "lobby"
GROUP_PLAYING: str = "playing"
GROUP_ENDED: str = "ended"
GROUP_STATES: frozenset[str] = frozenset({GROUP_LOBBY, GROUP_PLAYING, GROUP_ENDED})

#: Why a table ended.
END_FINISHED: str = "finished"
END_CANCELLED: str = "cancelled"
END_EXPIRED: str = "expired"
END_HOST_GONE: str = "host_gone"
END_APP_REMOVED: str = "app_removed"
END_CLOSED_BY_MODERATOR: str = "closed_by_moderator"
GROUP_END_REASONS: frozenset[str] = frozenset(
    {
        END_FINISHED,
        END_CANCELLED,
        END_EXPIRED,
        END_HOST_GONE,
        END_APP_REMOVED,
        END_CLOSED_BY_MODERATOR,
    }
)

#: A person's place at a table. ``invited`` / ``declined`` live on the
#: hub only (who else was asked is never sent to the players).
SEAT_INVITED: str = "invited"
SEAT_DECLINED: str = "declined"
SEAT_SEATED: str = "seated"
SEAT_LEFT: str = "left"
SEAT_REMOVED: str = "removed"
SEAT_STATES: frozenset[str] = frozenset(
    {SEAT_INVITED, SEAT_DECLINED, SEAT_SEATED, SEAT_LEFT, SEAT_REMOVED}
)
#: The entries a snapshot carries to the players.
WIRE_SEAT_STATES: frozenset[str] = frozenset({SEAT_SEATED, SEAT_LEFT, SEAT_REMOVED})

#: Why one person's place at a table is over (``group_close`` with
#: ``to_user``): removed by the host, the invite withdrawn (the game
#: started, or the table closed), or no seat left when they joined.
CLOSE_REMOVED: str = "removed"
CLOSE_WITHDRAWN: str = "withdrawn"
CLOSE_FULL: str = "full"
PERSON_CLOSE_REASONS: frozenset[str] = frozenset(
    {CLOSE_REMOVED, CLOSE_WITHDRAWN, CLOSE_FULL}
)

# ─── Verbs (``APP_SESSION``, sealed, v_76 only) ────────────────────────────

VERB_INVITE: str = "group_invite"  # hub → invitee household
VERB_JOIN: str = "group_join"  # player household → hub
VERB_SEAT: str = "group_seat"  # hub → joiner household
VERB_ROSTER: str = "group_roster"  # hub → seated households (coalesced)
VERB_START: str = "group_start"  # hub → seated; host person → hub
VERB_LEAVE: str = "group_leave"  # player household → hub (also a decline)
VERB_REMOVE: str = "group_remove"  # host person → hub
VERB_CLOSE: str = "group_close"  # hub → households; host person → hub
GROUP_VERBS: frozenset[str] = frozenset(
    {
        VERB_INVITE,
        VERB_JOIN,
        VERB_SEAT,
        VERB_ROSTER,
        VERB_START,
        VERB_LEAVE,
        VERB_REMOVE,
        VERB_CLOSE,
    }
)


# ─── Errors ───────────────────────────────────────────────────────────────


class GroupWireError(ValueError):
    """An inbound table snapshot out of shape or past a bound — dropped."""


class AppGroupNotFoundError(CodedError):
    """No such table — or none the caller has a place at (the same answer:
    a table you aren't at doesn't exist for you)."""

    status = 404
    code = "APP_GROUP_NOT_FOUND"
    detail = "No such game."


class AppGroupNotHostError(CodedError):
    """Only the host person may start, remove a player or close a table."""

    status = 403
    code = "APP_GROUP_NOT_HOST"
    detail = "Only the person who started this game can do that."


class AppGroupClosedError(CodedError):
    """The table no longer takes this (it started, ended, or the caller's
    place is over)."""

    status = 409
    code = "APP_GROUP_CLOSED"
    detail = "This game no longer takes that."


class AppGroupFullError(CodedError):
    """Every seat is taken."""

    status = 409
    code = "APP_GROUP_FULL"
    detail = "This game is full."


class AppGroupTooFewError(CodedError):
    """Start asked for before the app's fewest players are seated."""

    status = 409
    code = "APP_GROUP_TOO_FEW"
    detail = "Not enough players yet."

    def __init__(self, min_players: int) -> None:
        super().__init__(params={"min": min_players})


class AppGroupCantJoinError(CodedError):
    """The caller's own household keeps them out (a block either way, a
    guardian block, or a protected account and a player it may not
    message). No reason is given: the block stays unseen (design 2.11)."""

    status = 403
    code = "APP_GROUP_CANT_JOIN"
    detail = "You can't join this game."


class AppGroupsNotSupportedError(CodedError):
    """The app can't be opened as a table (its manifest has no
    ``tables: true``)."""

    status = 409
    code = "APP_GROUPS_NOT_SUPPORTED"
    detail = "This app can't be played in a group."


class AppGroupSizeError(CodedError):
    """The players asked for don't fit the app (its ``players`` range) or
    the way in (at most :data:`GROUP_PICK_MAX_PLAYERS` by picking)."""

    status = 422
    code = "APP_GROUP_SIZE"
    detail = "That many players don't fit this game."

    def __init__(self, min_players: int, max_players: int) -> None:
        super().__init__(params={"min": min_players, "max": max_players})


class AppGroupHouseholdTooOldError(CodedError):
    """A picked person's household runs a Social Home without group play
    (below v_76): it would never get the invite."""

    status = 409
    code = "APP_GROUP_HOUSEHOLD_TOO_OLD"
    detail = "Someone's household needs an update for group games."


class AppGroupLimitError(CodedError):
    """Too many tables from one person in one app:
    :data:`GROUP_OPEN_TABLES_MAX` waiting for players
    (``APP_GROUP_OPEN_LIMIT``) or :data:`GROUP_TABLES_PER_DAY_MAX` in 24
    hours (``APP_GROUP_DAILY_LIMIT``); ``params.limit`` is the number."""

    status = 429
    code = "APP_GROUP_OPEN_LIMIT"
    detail = "Too many games are waiting for players."

    def __init__(self, *, daily: bool) -> None:
        if daily:
            super().__init__(
                "Too many new games today.",
                code="APP_GROUP_DAILY_LIMIT",
                params={"limit": GROUP_TABLES_PER_DAY_MAX},
            )
        else:
            super().__init__(params={"limit": GROUP_OPEN_TABLES_MAX})


class AppGroupSessionError(CodedError):
    """A 1:1 session route (accept / decline / a move) used on a group
    seat: a table is answered and played through its group routes."""

    status = 409
    code = "APP_GROUP_SESSION"
    detail = "This is a group game: answer it from the game."


# ─── Data ─────────────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class GroupSeat:
    """One person's place at a table.

    ``instance`` / ``user`` — their household and their username there;
    ``name`` / ``household_name`` — labels as the hub knows them (no
    ``household_name`` for the hub's own people); ``seat`` — their seat
    number (``0`` before they ever sat: invited / declined); ``joined_at``
    — when they took it (UTC ISO 8601), which decides the host hand-over
    ("the player seated longest")."""

    instance: str
    user: str
    name: str
    state: str
    household_name: str | None = None
    seat: int = 0
    joined_at: str | None = None

    def is_person(self, instance: str, user: str) -> bool:
        """Whether this entry is *user* of *instance*."""
        return self.instance == instance and self.user == user

    def to_wire(self) -> dict[str, Any]:
        """The entry as the players see it — names, never more."""
        return {
            "seat": self.seat,
            "instance": self.instance,
            "user": self.user,
            "name": self.name,
            "household": self.household_name,
            "state": self.state,
        }


@dataclass(slots=True, frozen=True)
class GroupSession:
    """One table, as a household keeps it (``app_group_sessions``).

    ``hub_instance`` is the host household (fixed); ``host_instance`` /
    ``host_user`` the host person now. ``roster`` holds every place, in
    seat order, ``roster_rev`` the hub's numbering of its changes (a mirror
    keeps the newest it got). ``seq`` numbers the table's messages (v_76
    part II). ``space_id`` is set for a table opened in a space (v_77);
    ``None`` for one opened by picking people."""

    session_id: str
    app_id: str
    host_instance: str
    host_user: str
    hub_instance: str
    state: str
    min_players: int
    max_players: int
    roster: tuple[GroupSeat, ...]
    roster_rev: int
    created_at: str
    expires_at: str
    updated_at: str
    space_id: str | None = None
    end_reason: str | None = None
    late_join: bool = False
    note: str | None = None
    seq: int = 0
    started_at: str | None = None

    # ─── Reading ──────────────────────────────────────────────────────

    @property
    def seated(self) -> tuple[GroupSeat, ...]:
        """The people in a seat now, in seat order."""
        return tuple(e for e in self.roster if e.state == SEAT_SEATED)

    @property
    def pending_invites(self) -> int:
        """People asked who haven't answered (hub only)."""
        return sum(1 for e in self.roster if e.state == SEAT_INVITED)

    @property
    def invited_total(self) -> int:
        """Everyone the host asked — what the table cost of the invite
        caps (hub only; the host themself isn't counted)."""
        return sum(
            1
            for e in self.roster
            if not e.is_person(self.host_instance, self.host_user)
        )

    @property
    def host_seat(self) -> int:
        """The host person's seat (``0`` if they hold none)."""
        host = self.entry(self.host_instance, self.host_user)
        return host.seat if host is not None else 0

    @property
    def is_full(self) -> bool:
        return len(self.seated) >= self.max_players

    @property
    def is_open(self) -> bool:
        """Still taking people: waiting for players, or played by an app
        that takes late joiners."""
        return self.state == GROUP_LOBBY or (
            self.state == GROUP_PLAYING and self.late_join
        )

    @property
    def can_start(self) -> bool:
        return self.state == GROUP_LOBBY and len(self.seated) >= self.min_players

    def entry(self, instance: str, user: str) -> GroupSeat | None:
        """*user* of *instance*'s place, or ``None``."""
        return next((e for e in self.roster if e.is_person(instance, user)), None)

    def at_seat(self, seat: int) -> GroupSeat | None:
        """Who holds (or held) *seat*."""
        if seat <= 0:
            return None
        return next((e for e in self.roster if e.seat == seat), None)

    def is_host(self, instance: str, user: str) -> bool:
        return self.host_instance == instance and self.host_user == user

    def households(self, *states: str) -> frozenset[str]:
        """The households with someone in *states* (default: seated)."""
        wanted = frozenset(states) or frozenset({SEAT_SEATED})
        return frozenset(e.instance for e in self.roster if e.state in wanted)

    @property
    def next_seat(self) -> int:
        """A seat number never used at this table."""
        return max((e.seat for e in self.roster), default=0) + 1

    def successor(self) -> GroupSeat | None:
        """Who becomes host when the host person leaves a game being
        played: the player seated longest (earliest ``joined_at``, then the
        lowest seat), never the host themself."""
        others = [
            e
            for e in self.seated
            if not e.is_person(self.host_instance, self.host_user)
        ]
        if not others:
            return None
        return min(others, key=lambda e: (e.joined_at or "", e.seat))

    # ─── Changing (a new value each time; the caller saves it) ────────

    def with_entry(self, entry: GroupSeat) -> GroupSession:
        """This table with *entry* replacing the same person's (or added),
        the roster kept in seat order (unseated last)."""
        others = [e for e in self.roster if not e.is_person(entry.instance, entry.user)]
        roster = sorted([*others, entry], key=lambda e: (e.seat <= 0, e.seat))
        return dataclasses.replace(self, roster=tuple(roster))

    def bumped(self, now: str, **changes: Any) -> GroupSession:
        """*changes* applied, ``roster_rev`` + 1 and ``updated_at`` *now*."""
        return dataclasses.replace(
            self, roster_rev=self.roster_rev + 1, updated_at=now, **changes
        )

    # ─── Wire ─────────────────────────────────────────────────────────

    def to_wire(self) -> dict[str, Any]:
        """The table snapshot a hub sends (sealed). Who else was invited
        never travels: only seated, left and removed places."""
        return {
            "rev": self.roster_rev,
            "state": self.state,
            "end_reason": self.end_reason,
            "host_seat": self.host_seat,
            "min": self.min_players,
            "max": self.max_players,
            "late_join": self.late_join,
            "note": self.note,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "started_at": self.started_at,
            "roster": [e.to_wire() for e in self.roster if e.state in WIRE_SEAT_STATES],
        }


# ─── Parsing a snapshot (receiver) ────────────────────────────────────────


def _int(raw: object, lo: int, hi: int, what: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not lo <= raw <= hi:
        raise GroupWireError(f"{what} out of range")
    return raw


def _text(raw: object, limit: int, what: str, *, optional: bool = False) -> str | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > limit:
        raise GroupWireError(f"{what} malformed")
    if any(ch in raw for ch in "\r\n\x00"):
        raise GroupWireError(f"{what} not one line")
    return raw


def _when(raw: object, what: str, *, optional: bool = False) -> datetime | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str) or len(raw) > 64:
        raise GroupWireError(f"{what} malformed")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise GroupWireError(f"{what} malformed") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Stored as UTC ISO 8601 so the expiry / prune compare as strings.
    return parsed.astimezone(UTC)


def note_ok(note: str | None) -> str | None:
    """The host's note, trimmed — ``None`` when empty; raises
    :class:`ValueError` past :data:`GROUP_NOTE_MAX` or across lines."""
    if note is None:
        return None
    note = note.strip()
    if not note:
        return None
    if len(note) > GROUP_NOTE_MAX or any(ch in note for ch in "\r\n\x00"):
        raise ValueError("note too long or not one line")
    return note


def parse_table(
    raw: object,
    *,
    session_id: str,
    app_id: str,
    hub_instance: str,
    now: datetime,
) -> GroupSession:
    """A hub's table snapshot as this household keeps it — or
    :class:`GroupWireError` when anything is out of shape or past a bound.

    ``hub_instance`` is the household that sent it (the signature-proven
    sender), so a snapshot can only ever speak for the hub that sent it.
    The host person must hold an entry; at most ``max`` people seated;
    seats and people unique. A space table (``space_id``) is not taken
    (v_77)."""
    if not isinstance(raw, dict):
        raise GroupWireError("table missing")
    if raw.get("space_id") is not None:
        raise GroupWireError("space tables need v_77")
    rev = _int(raw.get("rev"), 1, 2**31, "rev")
    state = raw.get("state")
    if state not in GROUP_STATES:
        raise GroupWireError("state unknown")
    end_reason = raw.get("end_reason")
    if state == GROUP_ENDED:
        if end_reason not in GROUP_END_REASONS:
            raise GroupWireError("end_reason unknown")
    elif end_reason is not None:
        raise GroupWireError("end_reason on an open table")
    min_players = _int(raw.get("min"), GROUP_MIN_PLAYERS, GROUP_MAX_PLAYERS, "min")
    max_players = _int(raw.get("max"), min_players, GROUP_MAX_PLAYERS, "max")
    late_join = raw.get("late_join", False)
    if not isinstance(late_join, bool):
        raise GroupWireError("late_join malformed")
    note = _text(raw.get("note"), GROUP_NOTE_MAX, "note", optional=True)
    created = _when(raw.get("created_at"), "created_at")
    expires = _when(raw.get("expires_at"), "expires_at")
    started = _when(raw.get("started_at"), "started_at", optional=True)
    assert created is not None and expires is not None
    if expires > now + GROUP_EXPIRES_MAX or created > now + timedelta(hours=1):
        raise GroupWireError("times out of range")
    entries = raw.get("roster")
    if not isinstance(entries, list) or not 1 <= len(entries) <= GROUP_ROSTER_MAX:
        raise GroupWireError("roster size")
    roster: list[GroupSeat] = []
    seats: set[int] = set()
    people: set[tuple[str, str]] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise GroupWireError("roster entry malformed")
        seat = _int(item.get("seat"), 1, GROUP_SEAT_MAX, "seat")
        instance = _text(item.get("instance"), GROUP_REF_MAX, "instance")
        user = _text(item.get("user"), GROUP_REF_MAX, "user")
        name = _text(item.get("name"), GROUP_NAME_MAX, "name")
        household = _text(
            item.get("household"), GROUP_NAME_MAX, "household", optional=True
        )
        entry_state = item.get("state")
        if entry_state not in WIRE_SEAT_STATES:
            raise GroupWireError("seat state unknown")
        assert instance is not None and user is not None and name is not None
        if seat in seats or (instance, user) in people:
            raise GroupWireError("seat or person twice")
        seats.add(seat)
        people.add((instance, user))
        roster.append(
            GroupSeat(
                instance=instance,
                user=user,
                name=name,
                household_name=household,
                state=entry_state,
                seat=seat,
            )
        )
    host_seat = _int(raw.get("host_seat"), 1, GROUP_SEAT_MAX, "host_seat")
    host = next((e for e in roster if e.seat == host_seat), None)
    if host is None:
        raise GroupWireError("host holds no seat")
    if sum(1 for e in roster if e.state == SEAT_SEATED) > max_players:
        raise GroupWireError("more seated than seats")
    stamp = now.isoformat()
    return GroupSession(
        session_id=session_id,
        app_id=app_id,
        host_instance=host.instance,
        host_user=host.user,
        hub_instance=hub_instance,
        state=state,
        min_players=min_players,
        max_players=max_players,
        roster=tuple(sorted(roster, key=lambda e: e.seat)),
        roster_rev=rev,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        updated_at=stamp,
        end_reason=end_reason if state == GROUP_ENDED else None,
        late_join=late_join,
        note=note,
        started_at=started.isoformat() if started is not None else None,
    )
