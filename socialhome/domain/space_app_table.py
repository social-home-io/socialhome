"""Space tables — a group game opened *in a space* (group play §3, v_77).

A member opens a table in a space and any member with a writing seat may
take a seat (up to :data:`SPACE_TABLE_MAX_PLAYERS`). The table is an
ordinary group session (:mod:`socialhome.domain.app_group`) whose
``space_id`` is set: its host household (the *hub*) keeps the roster and
talks to each player's household over the space's own transport
(``SPACE_APP_SESSION``). Every other member household hears of it only
through its **card** — ``SPACE_APP_TABLE``, an upsert by ``rev`` carrying
what the "Open games" strip shows: the app, who opened it, the note, the
players range, how many sit, open / full / playing / closed and the
expiry. Never the roster.

:func:`card_wire` is what the hub sends, :func:`parse_card` the receiver's
bound check (anything out of shape raises
:class:`~socialhome.domain.app_group.GroupWireError` and is dropped), and
:func:`card_table` how a household keeps a card it has nobody at.

Pure data, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .app_group import (
    END_FINISHED,
    GROUP_END_REASONS,
    GROUP_ENDED,
    GROUP_EXPIRES_MAX,
    GROUP_LOBBY,
    GROUP_MAX_PLAYERS,
    GROUP_MIN_PLAYERS,
    GROUP_NAME_MAX,
    GROUP_NOTE_MAX,
    GROUP_PLAYING,
    GROUP_REF_MAX,
    SEAT_SEATED,
    GroupSeat,
    GroupSession,
    GroupWireError,
    TableCard,
)
from .errors import CodedError

# ─── Bounds (design §3.8 / §3.7) ──────────────────────────────────────────

#: The most players a space table seats.
SPACE_TABLE_MAX_PLAYERS: int = GROUP_MAX_PLAYERS
#: Tables one person opens in one space in any 24 hours (the hub refuses
#: a fourth; a receiver drops one from a hub that doesn't).
SPACE_TABLES_PER_PERSON_DAY: int = 3
#: Tables one person opens with "Let everyone know" in one space in any 24
#: hours (one in-app row per member — D5).
SPACE_TABLE_ANNOUNCES_PER_DAY: int = 1
#: Open tables a space shows (the newest; older ones are hidden).
SPACE_TABLES_SHOWN: int = 10
#: The hub re-sends a card for a changed seat count at most this often per
#: table; open / full / started / closed go out at once.
CARD_COUNT_EVERY_SECONDS: float = 600.0

# ─── Card states ──────────────────────────────────────────────────────────

CARD_OPEN: str = "open"  # takes people (waiting, or a late-join game)
CARD_FULL: str = "full"  # every seat taken
CARD_PLAYING: str = "playing"  # started, takes nobody more
CARD_CLOSED: str = "closed"  # ended
CARD_STATES: frozenset[str] = frozenset(
    {CARD_OPEN, CARD_FULL, CARD_PLAYING, CARD_CLOSED}
)

# ─── Errors ───────────────────────────────────────────────────────────────


class SpaceAppNotAllowedError(CodedError):
    """The app isn't on the space's games list ("Games allowed here"), or
    the space can't have games at all (a global space, or one archived)."""

    status = 409
    code = "SPACE_APP_NOT_ALLOWED"
    detail = "This game isn't allowed in this space."


class SpaceTableLimitError(CodedError):
    """Too many tables from one person in one space today
    (:data:`SPACE_TABLES_PER_PERSON_DAY`, ``SPACE_TABLE_DAILY_LIMIT``) — or,
    with "Let everyone know", more than
    :data:`SPACE_TABLE_ANNOUNCES_PER_DAY` (``SPACE_TABLE_ANNOUNCE_LIMIT``:
    open it without letting everyone know)."""

    status = 429
    code = "SPACE_TABLE_DAILY_LIMIT"
    detail = "Too many new games in this space today."

    def __init__(self, *, announce: bool = False) -> None:
        if announce:
            super().__init__(
                "You already let everyone know about a game here today.",
                code="SPACE_TABLE_ANNOUNCE_LIMIT",
                params={"limit": SPACE_TABLE_ANNOUNCES_PER_DAY},
            )
        else:
            super().__init__(params={"limit": SPACE_TABLES_PER_PERSON_DAY})


class SpaceHostTooOldError(CodedError):
    """The table's host household runs a Social Home below v_77."""

    status = 409
    code = "SPACE_TABLE_HOUSEHOLD_TOO_OLD"
    detail = "The household that started this game needs an update."


# ─── The card ─────────────────────────────────────────────────────────────


def card_state(table: GroupSession) -> str:
    """What the card says of *table*."""
    if table.state == GROUP_ENDED:
        return CARD_CLOSED
    if not table.is_open:
        return CARD_PLAYING
    return CARD_FULL if table.is_full else CARD_OPEN


@dataclass(slots=True, frozen=True)
class SpaceTableCard:
    """A space table's card as a receiver read it (:func:`parse_card`)."""

    table_id: str
    rev: int
    app_id: str
    host_user: str
    host_user_id: str
    host_name: str
    min_players: int
    max_players: int
    seated: int
    state: str
    late_join: bool
    created_at: str
    expires_at: str
    started_at: str | None = None
    end_reason: str | None = None
    note: str | None = None
    announce: bool = False


def card_wire(
    table: GroupSession, space_id: str, *, announce: bool = False
) -> dict[str, Any]:
    """The card a hub sends for its *table* (sealed; ``space_id`` is the
    routing field too). The host is the table's host person now."""
    host = table.entry(table.host_instance, table.host_user)
    return {
        "space_id": space_id,
        "table_id": table.session_id,
        "rev": table.roster_rev,
        "app_id": table.app_id,
        "host_user": table.host_user,
        "host_user_id": host.user_id if host is not None else None,
        "host_name": host.name if host is not None else table.host_user,
        "note": table.note,
        "players": {"min": table.min_players, "max": table.max_players},
        "seated": len(table.seated),
        "state": card_state(table),
        "end_reason": table.end_reason,
        "late_join": table.late_join,
        "created_at": table.created_at,
        "started_at": table.started_at,
        "expires_at": table.expires_at,
        "announce": announce,
    }


def _text(raw: object, limit: int, what: str, *, optional: bool = False) -> str | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str) or not raw.strip() or len(raw) > limit:
        raise GroupWireError(f"{what} malformed")
    if any(ch in raw for ch in "\r\n\x00"):
        raise GroupWireError(f"{what} not one line")
    return raw


def _int(raw: object, lo: int, hi: int, what: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool) or not lo <= raw <= hi:
        raise GroupWireError(f"{what} out of range")
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
    return parsed.astimezone(UTC)


def parse_card(raw: object, *, now: datetime) -> SpaceTableCard:
    """A card as this household reads it — or :class:`GroupWireError` when
    anything is out of shape or past a bound."""
    if not isinstance(raw, dict):
        raise GroupWireError("card missing")
    table_id = _text(raw.get("table_id"), GROUP_REF_MAX, "table_id")
    app_id = _text(raw.get("app_id"), GROUP_REF_MAX, "app_id")
    host_user = _text(raw.get("host_user"), GROUP_REF_MAX, "host_user")
    host_user_id = _text(raw.get("host_user_id"), GROUP_REF_MAX, "host_user_id")
    host_name = _text(raw.get("host_name"), GROUP_NAME_MAX, "host_name")
    note = _text(raw.get("note"), GROUP_NOTE_MAX, "note", optional=True)
    rev = _int(raw.get("rev"), 1, 2**31, "rev")
    players = raw.get("players")
    if not isinstance(players, dict):
        raise GroupWireError("players missing")
    lo = _int(players.get("min"), GROUP_MIN_PLAYERS, SPACE_TABLE_MAX_PLAYERS, "min")
    hi = _int(players.get("max"), lo, SPACE_TABLE_MAX_PLAYERS, "max")
    seated = _int(raw.get("seated"), 0, hi, "seated")
    state = raw.get("state")
    if state not in CARD_STATES:
        raise GroupWireError("state unknown")
    end_reason = raw.get("end_reason")
    if state == CARD_CLOSED:
        if end_reason is not None and end_reason not in GROUP_END_REASONS:
            raise GroupWireError("end_reason unknown")
    elif end_reason is not None:
        raise GroupWireError("end_reason on an open table")
    late_join = raw.get("late_join", False)
    announce = raw.get("announce", False)
    if not isinstance(late_join, bool) or not isinstance(announce, bool):
        raise GroupWireError("flag malformed")
    created = _when(raw.get("created_at"), "created_at")
    expires = _when(raw.get("expires_at"), "expires_at")
    started = _when(raw.get("started_at"), "started_at", optional=True)
    assert created is not None and expires is not None
    if expires > now + GROUP_EXPIRES_MAX or created > now + timedelta(hours=1):
        raise GroupWireError("times out of range")
    assert table_id and app_id and host_user and host_user_id and host_name
    return SpaceTableCard(
        table_id=table_id,
        rev=rev,
        app_id=app_id,
        host_user=host_user,
        host_user_id=host_user_id,
        host_name=host_name,
        min_players=lo,
        max_players=hi,
        seated=seated,
        state=str(state),
        late_join=late_join,
        created_at=created.isoformat(),
        expires_at=expires.isoformat(),
        started_at=started.isoformat() if started is not None else None,
        end_reason=(end_reason or END_FINISHED) if state == CARD_CLOSED else None,
        note=note,
        announce=announce,
    )


def card_table(
    card: SpaceTableCard,
    *,
    space_id: str,
    hub_instance: str,
    seen_at: str,
) -> GroupSession:
    """*card* as a household keeps a space table it has nobody at: the
    host as the one roster entry, the seat count on :class:`TableCard`."""
    if card.state == CARD_CLOSED:
        state = GROUP_ENDED
    elif card.started_at is not None or card.state == CARD_PLAYING:
        state = GROUP_PLAYING
    else:
        state = GROUP_LOBBY
    return GroupSession(
        session_id=card.table_id,
        app_id=card.app_id,
        host_instance=hub_instance,
        host_user=card.host_user,
        hub_instance=hub_instance,
        state=state,
        min_players=card.min_players,
        max_players=card.max_players,
        roster=(
            GroupSeat(
                instance=hub_instance,
                user=card.host_user,
                name=card.host_name,
                state=SEAT_SEATED,
                seat=1,
                user_id=card.host_user_id,
            ),
        ),
        roster_rev=card.rev,
        created_at=card.created_at,
        expires_at=card.expires_at,
        updated_at=seen_at,
        space_id=space_id,
        end_reason=card.end_reason,
        late_join=card.late_join,
        note=card.note,
        started_at=card.started_at,
        opener_user=card.host_user,
        card=TableCard(seated=card.seated, announce=card.announce, seen_at=seen_at),
    )
