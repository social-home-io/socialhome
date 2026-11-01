"""The gift draw at a group table — pure domain (v_78).

A *draw* (Gift draw / Wichteln / Lootjes trekken / Père Noël secret /
Amigo invisible) is a Social Home feature an app at a table calls: every
person at the table is drawn one other person to give a gift to, in one
circle, so that each giver learns only their own recipient — not the
organiser, not the table's host household (the hub, which passes every
message on), not the app. Group-play design §5; the cryptography is
:mod:`socialhome.app_engines.draw.crypto`, the rounds
:mod:`socialhome.app_engines.draw.service`.

Pure data and bounds, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import orjson

from ...domain.app_group import GROUP_MAX_PLAYERS, GROUP_SEAT_MAX
from ...domain.errors import CodedError

# ─── Bounds (design 5.5, 5.7, owner decisions 2026-10-31) ─────────────────

#: The fewest and most people in a draw (``players.max`` of the app; 30
#: is the hard ceiling of every table).
DRAW_MIN_PEOPLE: int = 3
DRAW_MAX_PEOPLE: int = GROUP_MAX_PLAYERS
#: The fewest people when there is any pair (with 3 people and a pair no
#: circle exists).
DRAW_MIN_PEOPLE_WITH_PAIRS: int = 4
#: Rounds one draw may take before it gives up ("The names couldn't be
#: drawn with these rules"). The worst allowed rules (four people, one
#: pair) fit a random circle 1 time in 3: (2/3)^40 ≈ 1 in 11 million draws
#: gives up (12 rounds gave up 0.8 % of the time — the second review). Every
#: round that isn't chance names the household that broke it, to everyone.
DRAW_ROUNDS_MAX: int = 40
#: Redraws a draw takes for card complaints — a giver's box opened no card
#: it could use, or two. Nothing is opened to find out who sealed it so, so
#: such a redraw names nobody; past this many the draw stops ("Someone's
#: home kept breaking the draw") and the organiser decides.
DRAW_CARD_REDRAWS_MAX: int = 3
#: Redraws a draw takes because of the pairs (chance: a giver drew their
#: partner). Room for a real small table of couples — the worst allowed
#: rules fit 1 circle in 3, so (2/3)^30 ≈ 1 in 190 000 genuine draws reach
#: it — while a household can't re-roll without end.
DRAW_PARTNER_REDRAWS_MAX: int = 30
#: "Draw again" after the reveal, at most this many times per table.
DRAW_AGAIN_MAX: int = 10
#: A wish list, in characters (it must also fit the 1 KiB card).
DRAW_WISH_MAX_CHARS: int = 500
#: Wish-list updates per person after the reveal.
DRAW_WISH_UPDATES_MAX: int = 5
#: The draw's name ("Familie Müller 2026") and budget ("about 20 €").
DRAW_NAME_MAX: int = 80
DRAW_BUDGET_MAX: int = 40
#: After this long without a household's step, the organiser sees
#: "Waiting for …'s home" (and may draw without them).
DRAW_WAIT_SECONDS: int = 60 * 60
#: A step this household keeps until its own copy of the table shows what
#: the step says (a start, an end, a hand-over) is dropped after this long.
DRAW_PENDING_SECONDS: int = 24 * 60 * 60

# ─── Steps (the sealed ``APP_SESSION`` verb ``engine``, ``engine: "draw"``) ─

STEP_START = "start"  # hub → participant households
STEP_KEY = "key"  # participant → hub
STEP_MIXERS = "mixers"  # hub → participants
STEP_ONIONS = "onions"  # participant → hub
STEP_MIX = "mix"  # hub → one mixer
STEP_MIXED = "mixed"  # mixer → hub
STEP_LIST = "list"  # hub → participants (S + the signed chain)
STEP_REVEAL = "reveal"  # participant → hub (its circle randomness r)
STEP_REVEALS = "reveals"  # hub → participants (every r)
STEP_COMMIT = "commit"  # participant → hub (its sealed cards' hashes)
STEP_COMMITS = "commits"  # hub → participants (every household's)
STEP_CARDS = "cards"  # participant → hub
STEP_CARDS_ALL = "cards_all"  # hub → participants
STEP_OPEN = "open"  # participant → hub (its seeds / box keys of a broken round)
STEP_VOID = "void"  # hub → participants (a mixer's input doesn't peel)
STEP_FAIL = "fail"  # participant → hub (a key missing / cards off)
STEP_REDO = "redo"  # participant → hub (drew their partner)
STEP_DONE = "done"  # participant → hub (signed)
STEP_COMPLETE = "complete"  # hub → participants (every household's done)
STEP_HANDOVER = "handover"  # leaver household → hub → participants
STEP_WISH = "wish"  # recipient household → hub → participants
STEP_END = "end"  # hub → participants (gave up / too few left)

DRAW_STEPS: frozenset[str] = frozenset(
    {
        STEP_START,
        STEP_KEY,
        STEP_MIXERS,
        STEP_ONIONS,
        STEP_MIX,
        STEP_MIXED,
        STEP_LIST,
        STEP_REVEAL,
        STEP_REVEALS,
        STEP_COMMIT,
        STEP_COMMITS,
        STEP_CARDS,
        STEP_CARDS_ALL,
        STEP_OPEN,
        STEP_VOID,
        STEP_FAIL,
        STEP_REDO,
        STEP_DONE,
        STEP_COMPLETE,
        STEP_HANDOVER,
        STEP_WISH,
        STEP_END,
    }
)
#: Steps a participant household sends to the hub.
DRAW_TO_HUB_STEPS: frozenset[str] = frozenset(
    {
        STEP_KEY,
        STEP_ONIONS,
        STEP_MIXED,
        STEP_REVEAL,
        STEP_COMMIT,
        STEP_CARDS,
        STEP_OPEN,
        STEP_FAIL,
        STEP_REDO,
        STEP_DONE,
        STEP_HANDOVER,
        STEP_WISH,
    }
)

#: Why a new round starts (``start`` step, ``because.kind``) — a household
#: refuses a new round without one of these ("no re-rolls on demand").
BECAUSE_FIRST = "first"  # the table's first draw (or afresh after one gave up)
BECAUSE_AGAIN = "again"  # the organiser's visible "Draw again" after a reveal
BECAUSE_CHANGED = "changed"  # people left or were removed (before S, or a stuck home)
BECAUSE_COMPLAINT = "complaint"  # a household proved the last round wrong
BECAUSE_VOID = "void"  # the last round broke publicly; every household opened
BECAUSE_KINDS: frozenset[str] = frozenset(
    {BECAUSE_FIRST, BECAUSE_AGAIN, BECAUSE_CHANGED, BECAUSE_COMPLAINT, BECAUSE_VOID}
)

#: Why a round broke publicly (``void``) — only ever before any card exists:
#: every household opens its seeds, and the openings name who broke it — to
#: every household. Once a card could have been opened nothing is opened.
VOID_UNUSABLE = "unusable"  # S holds a key nobody can seal to (seeds open)
VOID_STUCK = "stuck"  # a mixer's signed input doesn't peel (seeds open)
VOID_WHYS: frozenset[str] = frozenset({VOID_UNUSABLE, VOID_STUCK})

#: What a complaint says (it always carries its proof: the person's seed).
COMPLAINT_MISSING = "missing"  # my box key isn't in the list exactly once
COMPLAINT_CARDS = "cards"  # my box opens no card, several, or a bad one
COMPLAINT_PARTNER = "partner"  # my card is my partner's (another household's)
COMPLAINT_COUPLE = "couple"  # two of my people the pairs exclude sit side by side
COMPLAINT_KINDS: frozenset[str] = frozenset(
    {COMPLAINT_MISSING, COMPLAINT_CARDS, COMPLAINT_PARTNER, COMPLAINT_COUPLE}
)

#: Why a draw ended without a result (``end`` step, ``reason``).
END_RULES = "rules"  # DRAW_ROUNDS_MAX rounds and no circle fit the pairs
END_TOO_FEW = "too_few"  # fewer than three people left
END_BROKEN = "broken"  # card complaints past DRAW_CARD_REDRAWS_MAX
DRAW_END_REASONS: frozenset[str] = frozenset({END_RULES, END_TOO_FEW, END_BROKEN})

# ─── What the app sees (``sh.draw.status()``) ─────────────────────────────

STATE_GATHERING = "gathering"
STATE_DRAWING = "drawing"
STATE_READY = "ready"
STATE_FAILED = "failed"

# ─── Errors ───────────────────────────────────────────────────────────────


class DrawWireError(ValueError):
    """An inbound draw message out of shape or past a bound — dropped."""


class AppDrawNotHostError(CodedError):
    status = 403
    code = "APP_DRAW_NOT_HOST"
    detail = "Only the person who started the draw can do that."


class AppDrawHostElsewhereError(CodedError):
    """The organiser now lives in another household than the table's host
    household (after a hand-over): v1 runs the draw from the host household
    only."""

    status = 409
    code = "APP_DRAW_HOST_ELSEWHERE"
    detail = "The draw can only be run from the home that started it."


class AppDrawStateError(CodedError):
    """The draw can't do that now (it is running, done, or never started)."""

    status = 409
    code = "APP_DRAW_STATE"
    detail = "The draw can't do that right now."


class AppDrawTooFewError(CodedError):
    status = 409
    code = "APP_DRAW_TOO_FEW"
    detail = "A gift draw needs at least three people."

    def __init__(self, min_people: int = DRAW_MIN_PEOPLE) -> None:
        super().__init__(params={"min": min_people})


class AppDrawPairsError(CodedError):
    """Pairs that overlap, name someone not at the table, or are too many
    for the people in: at most a third of them, and at least four people."""

    status = 422
    code = "APP_DRAW_PAIRS"
    detail = "These pairs don't work for this many people."


class AppDrawSettingsError(CodedError):
    status = 422
    code = "APP_DRAW_SETTINGS"
    detail = "Those draw settings aren't valid."


class AppDrawWishTooLongError(CodedError):
    status = 422
    code = "APP_DRAW_WISH_TOO_LONG"
    detail = "That wish list is too long."

    def __init__(self) -> None:
        super().__init__(params={"max": DRAW_WISH_MAX_CHARS})


class AppDrawWishUpdatesError(CodedError):
    status = 429
    code = "APP_DRAW_WISH_UPDATES"
    detail = "The wish list can't be changed again."

    def __init__(self) -> None:
        super().__init__(params={"limit": DRAW_WISH_UPDATES_MAX})


class AppDrawHouseholdTooOldError(CodedError):
    """A household at the table runs a Social Home without the draw (below
    v_78): it would never take part."""

    status = 409
    code = "APP_DRAW_HOUSEHOLD_TOO_OLD"
    detail = "Someone's home needs an update for the gift draw."

    def __init__(self, households: list[str]) -> None:
        super().__init__(params={"households": ", ".join(households)})


class AppDrawAgainLimitError(CodedError):
    status = 429
    code = "APP_DRAW_AGAIN_LIMIT"
    detail = "The names were drawn again too often."

    def __init__(self) -> None:
        super().__init__(params={"limit": DRAW_AGAIN_MAX})


class AppDrawPastDayError(CodedError):
    status = 409
    code = "APP_DRAW_PAST_DAY"
    detail = "The exchange day has passed."


class AppDrawNoResultError(CodedError):
    status = 404
    code = "APP_DRAW_NO_RESULT"
    detail = "There's no gift draw result for you here."


# ─── Settings ─────────────────────────────────────────────────────────────

_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _line(raw: object, limit: int, what: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str) or len(raw) > limit or "\n" in raw or "\r" in raw:
        raise DrawWireError(f"{what} malformed")
    return raw.strip()


def _seat(raw: object) -> int:
    if (
        not isinstance(raw, int)
        or isinstance(raw, bool)
        or not 0 < raw <= GROUP_SEAT_MAX
    ):
        raise DrawWireError("seat malformed")
    return raw


@dataclass(slots=True, frozen=True)
class DrawSettings:
    """The organiser's settings: a name, a budget (free text), the
    exchange day (``YYYY-MM-DD`` or none) and the pairs who must not draw
    each other (seats; non-overlapping)."""

    name: str = ""
    budget: str = ""
    day: str | None = None
    pairs: tuple[tuple[int, int], ...] = ()

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "budget": self.budget,
            "day": self.day,
            "pairs": [list(p) for p in self.pairs],
        }

    @classmethod
    def from_wire(cls, raw: object) -> DrawSettings:
        """The settings in *raw* — else :class:`DrawWireError`. Pairs are
        kept sorted (each pair low seat first) so every household digests
        the same bytes."""
        if not isinstance(raw, dict):
            raise DrawWireError("settings malformed")
        name = _line(raw.get("name"), DRAW_NAME_MAX, "name")
        budget = _line(raw.get("budget"), DRAW_BUDGET_MAX, "budget")
        day = raw.get("day")
        if day is not None:
            if not isinstance(day, str) or not _DAY_RE.fullmatch(day):
                raise DrawWireError("day malformed")
            try:
                date.fromisoformat(day)
            except ValueError as exc:
                raise DrawWireError("day malformed") from exc
        pairs_raw = raw.get("pairs", [])
        if not isinstance(pairs_raw, list) or len(pairs_raw) > DRAW_MAX_PEOPLE // 3:
            raise DrawWireError("pairs malformed")
        pairs: list[tuple[int, int]] = []
        seen: set[int] = set()
        for pair in pairs_raw:
            if not isinstance(pair, list) or len(pair) != 2:
                raise DrawWireError("pair malformed")
            a, b = _seat(pair[0]), _seat(pair[1])
            if a == b or a in seen or b in seen:
                raise DrawWireError("pairs overlap")
            seen.update((a, b))
            pairs.append((min(a, b), max(a, b)))
        return cls(name=name, budget=budget, day=day, pairs=tuple(sorted(pairs)))

    def digest_bytes(self) -> bytes:
        """The canonical bytes a round's context binds (``crypto``)."""
        return orjson.dumps(self.to_wire(), option=orjson.OPT_SORT_KEYS)

    def only(self, seats: frozenset[int]) -> DrawSettings:
        """The settings with only the pairs whose both people are in
        *seats* (someone of a pair left: the pair is gone)."""
        return DrawSettings(
            name=self.name,
            budget=self.budget,
            day=self.day,
            pairs=tuple(p for p in self.pairs if p[0] in seats and p[1] in seats),
        )

    def excludes(self, giver_seat: int, recipient_seat: int) -> bool:
        """Whether the giver may not draw the recipient (a pair)."""
        pair = (min(giver_seat, recipient_seat), max(giver_seat, recipient_seat))
        return pair in self.pairs


def pairs_fit(pairs: tuple[tuple[int, int], ...], seats: frozenset[int]) -> bool:
    """Whether *pairs* work for the people in *seats*: every seat of a pair
    is in, at most ⌊n/3⌋ pairs, and at least four people when there is any
    pair (design 5.5)."""
    n = len(seats)
    if not pairs:
        return True
    if n < DRAW_MIN_PEOPLE_WITH_PAIRS or len(pairs) > n // 3:
        return False
    return all(a in seats and b in seats for a, b in pairs)


def wish_ok(text: object) -> str:
    """A wish list as given (trimmed): text of at most
    :data:`DRAW_WISH_MAX_CHARS` characters — else
    :class:`AppDrawWishTooLongError` / :class:`AppDrawSettingsError`."""
    if not isinstance(text, str):
        raise AppDrawSettingsError()
    clean = text.strip().replace("\r\n", "\n")
    if len(clean) > DRAW_WISH_MAX_CHARS:
        raise AppDrawWishTooLongError()
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in clean):
        raise AppDrawSettingsError()
    return clean
