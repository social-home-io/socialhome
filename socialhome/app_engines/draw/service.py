"""AppDrawService — the gift draw engine (``engine: "draw"``, v_78).

Group-play design §5, hardened after the first independent review. An app
at a table that declares ``engines: ["draw"]`` asks Social Home to draw
names; every participant's **own household** then does its part by itself,
and the result is shown in Social Home's own sheet: the app never sees who
draws whom, the organiser learns only their own recipient, and the
table's **hub** (the host household, which passes every message on) learns
nothing more than any participant.

One round (every step rides the sealed ``APP_SESSION`` verb ``engine``
with ``engine: "draw"``, ``engine_suite``, ``step``, ``gen``, ``round``;
the cryptography is :mod:`socialhome.app_engines.draw.crypto`)::

    hub → all     start {people, identities, settings, because}
    each → hub    key {pub, sig}                        (a fresh mixer key)
    hub → all     mixers {keys}                         (in mixing order)
    each → hub    onions {onions, commits, r_commit, sig}  (from person seeds)
    hub → mixer k mix {k, batches | list + prev}        (one after the other)
    mixer → hub   mixed {k, list, in, sig} | open {seeds, stuck: k}
    hub → all     list {S, batches, chain}              (per-entry hashes, signed)
    each → hub    reveal {r}  | fail {missing, seed} | open {every seed}
    hub → all     reveals {r of every household}        → the circle
    each → hub    commit {card hashes, sig}             (before any card)
    hub → all     commits
    each → hub    cards {sealed cards}
    hub → all     cards_all                             (= the commitments)
    each → hub    done {count, sig} | fail {cards} | redo {partner}  (own seed)
    (a mixer stuck) hub → all void {stuck} → each: open {seeds}
    hub → all     complete {every signed done} | end {reason, proof?}
    after the reveal: handover / wish (each → hub → all)

Security invariants (see ``docs/protocol/app-engines.md``)
------------------------------------------------------------
* One transcript per round (the context, the mixer keys, every batch, the
  chain, the reveals and the circle, in order): every signature made after
  a value appeared signs the transcript up to it, and every household checks
  it against its own. Any equivocation stalls the round — nobody named,
  nothing opened.
* A couple within one household is told before any card (both seeds); a
  partner complaint about one's own household's card proves nothing.
  Redraws for the pairs and for the cards are counted by every household.
* After the list nothing is ever opened, except a complainer's own seed:
  a void comes only before the list (never after a household's ``done``).
* A round broken by someone's proven doing before the list names them to
  every household: a seed rebuilds its person's entry at each step of the
  mix and the chain's signed per-entry hashes show which mixer's output
  lacks it; a void's validly signed openings show who broke it (an
  unsigned one proves nothing). Each household checks on its own copy,
  names the same households and counts them: none takes a round after the
  same household broke two (the draw waits for the organiser).
* A card complaint is a visible redraw naming nobody, at most
  ``DRAW_CARD_REDRAWS_MAX`` per draw (every household counts), then the
  draw stops (``end {broken}``). Card commitments and ``done`` carry the
  circle each household drew: another ``R`` stalls, it blames nobody.
* An ``end`` only when this household's own copy shows it, never after the
  reveal; a fresh draw after one counts as a Draw again.
* A new round only for the first draw, the organiser's visible Draw again,
  fewer people (before ``S`` is out, or a whole stuck household removed —
  and only once this household's own roster shows them gone), a complaint
  it verifies itself, or a public void (a key in ``S`` nobody can seal to).
* The people of a first round / Draw again are exactly the seated people
  of this household's own copy of the table — no invisible extras.
* Onions derive from person seeds: a complaint reveals the seed and anyone
  rebuilds the onion — a household can't commit to one box and onion
  another. A missing key is told only before any card exists.
* The circle is ``S`` ordered by a commit-reveal of every household — no
  mixer chooses neighbours.
* Every household commits to its card hashes before any card is sent; a
  card list is taken only when it is exactly the commitments.
* A round completes only with every household's signed ``done``.
* A hand-over carries the leaver's box keys (their own, then every one
  handed to them); the giver checks them against the circle and opens the
  new card from its own stored card list.
* Every secret is KEK-wrapped; the app gets the state only, never a result.
"""

from __future__ import annotations

import asyncio
import binascii
import itertools
import logging
import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

import orjson

from ...crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from ...domain.app_group import (
    GROUP_ENDED,
    GROUP_LOBBY,
    SEAT_SEATED,
    GroupSession,
)
from ...domain.events import AppDrawChanged
from ...domain.federation import FederationEvent
from ...domain.federation_capabilities import FederationCapability
from ..domain import ENGINE_SUITE_FIELD, VERB_ENGINE, AppEngineActionError, EngineCall
from . import crypto as dc
from .domain import (
    BECAUSE_AGAIN,
    BECAUSE_CHANGED,
    BECAUSE_COMPLAINT,
    BECAUSE_FIRST,
    BECAUSE_VOID,
    COMPLAINT_CARDS,
    COMPLAINT_COUPLE,
    COMPLAINT_KINDS,
    COMPLAINT_MISSING,
    COMPLAINT_PARTNER,
    DRAW_AGAIN_MAX,
    DRAW_CARD_REDRAWS_MAX,
    DRAW_END_REASONS,
    DRAW_MAX_PEOPLE,
    DRAW_MIN_PEOPLE,
    DRAW_PARTNER_REDRAWS_MAX,
    DRAW_PENDING_SECONDS,
    DRAW_ROUNDS_MAX,
    DRAW_STEPS,
    DRAW_TO_HUB_STEPS,
    DRAW_WAIT_SECONDS,
    DRAW_WISH_UPDATES_MAX,
    END_BROKEN,
    END_RULES,
    END_TOO_FEW,
    STATE_DRAWING,
    STATE_FAILED,
    STATE_GATHERING,
    STATE_READY,
    STEP_CARDS,
    STEP_CARDS_ALL,
    STEP_COMMIT,
    STEP_COMMITS,
    STEP_COMPLETE,
    STEP_DONE,
    STEP_END,
    STEP_FAIL,
    STEP_HANDOVER,
    STEP_KEY,
    STEP_LIST,
    STEP_MIX,
    STEP_MIXED,
    STEP_MIXERS,
    STEP_ONIONS,
    STEP_OPEN,
    STEP_REDO,
    STEP_REVEAL,
    STEP_REVEALS,
    STEP_START,
    STEP_VOID,
    STEP_WISH,
    VOID_STUCK,
    VOID_UNUSABLE,
    AppDrawAgainLimitError,
    AppDrawHostElsewhereError,
    AppDrawHouseholdTooOldError,
    AppDrawNoResultError,
    AppDrawNotHostError,
    AppDrawPairsError,
    AppDrawPastDayError,
    AppDrawSettingsError,
    AppDrawStateError,
    AppDrawTooFewError,
    AppDrawWishTooLongError,
    AppDrawWishUpdatesError,
    DrawSettings,
    DrawWireError,
    pairs_fit,
    wish_ok,
)

if TYPE_CHECKING:
    from ...domain.events import DomainEvent
    from ...domain.user import User
    from ...infrastructure.event_bus import EventBus
    from ...infrastructure.key_manager import KeyManager
    from ...infrastructure.ws_manager import WebSocketManager
    from ...repositories.app_engine_state_repo import AbstractAppEngineStateRepo
    from ...repositories.app_group_repo import AbstractAppGroupRepo
    from ...services.app_federation_service import AppFederationService
    from ...services.app_group_service import AppGroupService

log = logging.getLogger(__name__)

#: The engine's id (manifest ``engines``, the wire, the routes).
DRAW_ENGINE_ID: str = dc.ENGINE_ID

#: The WebSocket frame each local person at a table gets when an engine's
#: state changes (the app hears it as ``engine:event``; never a result).
WS_FRAME_TYPE = "app.engine"

_PHASE_KEYS = "keys"
_PHASE_ONIONS = "onions"
_PHASE_MIXING = "mixing"
_PHASE_REVEAL = "reveal"
_PHASE_COMMIT = "commit"
_PHASE_CARDS = "cards"
_PHASE_OPENING = "opening"
_PHASE_VOID = "void"
#: A household broke a second round of this draw: no more rounds until the
#: organiser draws without it.
_PHASE_HELD = "held"
_PHASE_COMPLETE = "complete"
_PHASE_ENDED = "ended"
_RUNNING = frozenset(
    {
        _PHASE_KEYS,
        _PHASE_ONIONS,
        _PHASE_MIXING,
        _PHASE_REVEAL,
        _PHASE_COMMIT,
        _PHASE_CARDS,
        _PHASE_OPENING,
        _PHASE_VOID,
        _PHASE_HELD,
    }
)
#: Phases once the circle is known (cards being sealed, held, opened): a
#: removal then starts a new draw — a Draw again.
_AFTER_CIRCLE = frozenset({"commit", "cards", "opening"})
#: Complaint kinds that are chance (the pairs), told with a ``redo``.
_CHANCE = frozenset({COMPLAINT_PARTNER, COMPLAINT_COUPLE})
#: Phases after ``S`` is out: a leave no longer starts a new round.
_AFTER_S = frozenset(
    {_PHASE_REVEAL, _PHASE_COMMIT, _PHASE_CARDS, _PHASE_OPENING, _PHASE_VOID}
)

#: The largest value a draw message carries (an onion with 30 layers).
_MAX_BLOB = dc.onion_size(DRAW_MAX_PEOPLE)


def _now() -> datetime:
    return datetime.now(UTC)


def _b64(raw: bytes) -> str:
    return b64url_encode(raw)


def _raw(value: object, size: int | None = None, what: str = "value") -> bytes:
    """*value* (b64url on the wire) as bytes of *size* — else
    :class:`DrawWireError`."""
    limit = 4 * (size or _MAX_BLOB) // 3 + 4
    if not isinstance(value, str) or not value or len(value) > limit:
        raise DrawWireError(f"{what} malformed")
    try:
        out = b64url_decode(value)
    except (binascii.Error, ValueError) as exc:
        raise DrawWireError(f"{what} malformed") from exc
    if size is not None and len(out) != size:
        raise DrawWireError(f"{what} wrong size")
    return out


def _raw_list(value: object, count: int, size: int, what: str) -> list[bytes]:
    """Exactly *count* distinct entries of *size* bytes."""
    if not isinstance(value, list) or len(value) != count:
        raise DrawWireError(f"{what} wrong count")
    out = [_raw(v, size, what) for v in value]
    if len(set(out)) != len(out):
        raise DrawWireError(f"{what} repeats an entry")
    return out


def _int(value: object, lo: int, hi: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise DrawWireError(f"{what} malformed")
    return value


def _person_key(p: dc.Person) -> tuple[str, str]:
    return (p.instance, p.user)


def _people_wire(people: list[dc.Person], names: dict[tuple[str, str], str]) -> list:
    return [
        {
            "seat": p.seat,
            "instance": p.instance,
            "user": p.user,
            "name": names.get(_person_key(p), p.user),
        }
        for p in people
    ]


def _parse_people(raw: object) -> tuple[list[dc.Person], dict[tuple[str, str], str]]:
    """A round's people — 3 to 30, seats and persons unique."""
    if not isinstance(raw, list) or not DRAW_MIN_PEOPLE <= len(raw) <= DRAW_MAX_PEOPLE:
        raise DrawWireError("people wrong count")
    people: list[dc.Person] = []
    names: dict[tuple[str, str], str] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise DrawWireError("person malformed")
        seat = _int(item.get("seat"), 1, 999, "seat")
        inst, user, name = item.get("instance"), item.get("user"), item.get("name")
        if not isinstance(inst, str) or not isinstance(user, str):
            raise DrawWireError("person malformed")
        for v in (inst, user):
            if not v or len(v.encode()) > dc.CARD_REF_MAX_BYTES:
                raise DrawWireError("person malformed")
        p = dc.Person(seat=seat, instance=inst, user=user)
        people.append(p)
        names[_person_key(p)] = name[:80] if isinstance(name, str) and name else user
    if len({p.seat for p in people}) != len(people) or len(
        {_person_key(p) for p in people}
    ) != len(people):
        raise DrawWireError("people repeat")
    return sorted(people, key=lambda p: p.seat), names


def _people_from(raw: list) -> list[dc.Person]:
    return [
        dc.Person(seat=p["seat"], instance=p["instance"], user=p["user"]) for p in raw
    ]


def _seated(table: GroupSession) -> set[tuple[str, str]]:
    return {(e.instance, e.user) for e in table.roster if e.state == SEAT_SEATED}


def _redraw_kind(because: object) -> str | None:
    """The complaint kind a round's reason names (``None``: none)."""
    if (
        isinstance(because, dict)
        and because.get("kind") == BECAUSE_COMPLAINT
        and isinstance(because.get("complaint"), dict)
    ):
        kind = because["complaint"].get("kind")
        return kind if isinstance(kind, str) else None
    return None


def _card_redraw(because: object) -> bool:
    """Whether a round's reason is a card complaint (a redraw naming
    nobody, counted per draw)."""
    return _redraw_kind(because) == COMPLAINT_CARDS


def _partner_redraw(because: object) -> bool:
    """Whether a round's reason is the pairs (chance, counted per draw)."""
    return _redraw_kind(because) in _CHANCE


def _before_the_day(frame: dict) -> bool:
    """Whether a draw of *frame*'s settings is still before (or on) its
    exchange day — a day's grace for the clocks of different homes."""
    day = DrawSettings.from_wire(frame["settings"]).day
    return not day or date.fromisoformat(day) >= _now().date() - timedelta(days=1)


#: The transcript's stages, in order.
_STAGES = ("keys", "batches", "chain", "reveals")


class _Round:
    """A round's public frame, rebuilt from stored JSON."""

    __slots__ = (
        "ctx",
        "gen",
        "identities",
        "names",
        "order",
        "people",
        "round",
        "settings",
        "transcript",
    )

    def __init__(self, data: dict) -> None:
        self.gen: int = data["gen"]
        self.round: int = data["round"]
        self.people: list[dc.Person] = _people_from(data["people"])
        self.names: dict[tuple[str, str], str] = {
            (p["instance"], p["user"]): p.get("name") or p["user"]
            for p in data["people"]
        }
        self.identities: dict[str, str] = data["identities"]
        self.settings = DrawSettings.from_wire(data["settings"])
        self.ctx: bytes = b64url_decode(data["ctx"])
        self.order: list[str] = data["order"]
        #: The round's transcript as THIS household holds it, per stage
        #: (``keys``, ``batches``, ``chain``, ``reveals``).
        self.transcript: dict[str, bytes] = {
            stage: b64url_decode(data[f"t_{stage}"])
            for stage in _STAGES
            if data.get(f"t_{stage}")
        }

    @property
    def n(self) -> int:
        return len(self.people)

    @property
    def h(self) -> int:
        return len(self.order)

    def of(self, instance: str) -> list[dc.Person]:
        return [p for p in self.people if p.instance == instance]

    def seat_of(self, instance: str, user: str) -> int | None:
        return next(
            (p.seat for p in self.people if p.instance == instance and p.user == user),
            None,
        )

    def identity(self, instance: str) -> bytes:
        return bytes.fromhex(self.identities[instance])

    def t(self, stage: str) -> bytes:
        """Our transcript up to *stage* — every signature made after it
        signs it; one checked against it fails when anyone was handed
        another value (:class:`DrawWireError`: the round stalls)."""
        got = self.transcript.get(stage)
        if got is None:
            raise DrawWireError(f"no {stage} here yet")
        return got


def _frame(
    *,
    session_id: str,
    gen: int,
    round_no: int,
    people: list[dc.Person],
    names: dict[tuple[str, str], str],
    identities: dict[str, str],
    settings: DrawSettings,
) -> dict:
    ctx = dc.round_context(
        suite=dc.DRAW_SUITE_X25519_AESGCM,
        session_id=session_id,
        gen=gen,
        round_no=round_no,
        people=people,
        identities=identities,
        settings_digest=dc.sha256(settings.digest_bytes()),
    )
    return {
        "gen": gen,
        "round": round_no,
        "people": _people_wire(people, names),
        "identities": identities,
        "settings": settings.to_wire(),
        "ctx": _b64(ctx),
        "order": dc.mix_order(session_id, gen, round_no, [p.instance for p in people]),
    }


# ─── Checks every household runs on the round's public data ──────────────


def _check_batches(
    rnd: _Round, raw: object
) -> tuple[dict[str, dict], list[bytes], bytes]:
    """Every household's signed batch (``{hashes, commits, r_commit}``,
    signed over our transcript of the mixer keys), the first mixer's input —
    every onion hash, households in id order — and the transcript after
    them."""
    if not isinstance(raw, list) or len(raw) != rnd.h:
        raise DrawWireError("batches wrong count")
    by_inst: dict[str, dict] = {}
    for b in raw:
        if not isinstance(b, dict) or b.get("instance") not in rnd.order:
            raise DrawWireError("batch malformed")
        by_inst[b["instance"]] = b
    if len(by_inst) != rnd.h:
        raise DrawWireError("batches repeat")
    first: list[bytes] = []
    out: dict[str, dict] = {}
    parts: list[tuple[str, list[bytes], list[tuple[str, bytes]], bytes]] = []
    for inst in sorted(by_inst):
        b = by_inst[inst]
        mine = rnd.of(inst)
        hs = _raw_list(b.get("hashes"), len(mine), dc.DIGEST_BYTES, "hashes")
        commits = _commits(b.get("commits"), mine)
        r_commit = _raw(b.get("r_commit"), dc.DIGEST_BYTES, "r_commit")
        sig = _raw(b.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.batch_message(rnd.t("keys"), inst, hs, commits, r_commit)
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            raise DrawWireError("batch signature")
        first += hs
        parts.append((inst, hs, commits, r_commit))
        out[inst] = {
            "hashes": [_b64(x) for x in hs],
            "commits": [[u, _b64(c)] for u, c in commits],
            "r_commit": _b64(r_commit),
        }
    return out, first, dc.transcript_batches(rnd.t("keys"), parts)


def _check_chain(
    rnd: _Round, raw: object, first: list[bytes], *, length: int, t_batches: bytes
) -> list[list[bytes]]:
    """The signed chain's first *length* links: mixer *k* signed the hash
    of every entry it gave, in order, and took exactly what the one before
    gave — so a person's seed shows which mixer's output lacks them."""
    if not isinstance(raw, list) or len(raw) != length:
        raise DrawWireError("chain wrong length")
    prev = dc.list_digest(first)
    links: list[list[bytes]] = []
    for k, link in enumerate(raw):
        if not isinstance(link, dict) or link.get("instance") != rnd.order[k]:
            raise DrawWireError("chain out of order")
        hashes = _raw_list(link.get("hashes"), rnd.n, dc.DIGEST_BYTES, "hashes")
        sig = _raw(link.get("sig"), dc.SIG_BYTES, "sig")
        d_out = dc.list_digest(hashes)
        msg = dc.mix_message(t_batches, k, rnd.order[k], prev, d_out)
        if not verify_ed25519(rnd.identity(rnd.order[k]), msg, sig):
            raise DrawWireError("chain signature")
        links.append(hashes)
        prev = d_out
    return links


def check_list(
    rnd: _Round, body: object
) -> tuple[list[bytes], dict[str, dict], list[list[bytes]], bytes, bytes]:
    """A ``list`` body against its round: the n box keys ``S``, every
    household's batch and every mixer's output hashes — or
    :class:`DrawWireError`. Every batch is signed by its household; every
    link by the mixer whose turn it was; the last gives ``S``."""
    if not isinstance(body, dict):
        raise DrawWireError("list malformed")
    S = _raw_list(body.get("S"), rnd.n, dc.KEY_BYTES, "S")
    batches, first, t_batches = _check_batches(rnd, body.get("batches"))
    links = _check_chain(
        rnd, body.get("chain"), first, length=rnd.h, t_batches=t_batches
    )
    if links[-1] != [dc.entry_hash(s) for s in S]:
        raise DrawWireError("chain doesn't end in S")
    t_chain = dc.transcript_chain(t_batches, list(zip(rnd.order, links, strict=True)))
    return S, batches, links, t_batches, t_chain


def _commits(raw: object, mine: list[dc.Person]) -> list[tuple[str, bytes]]:
    """A household's commitments — exactly one per person of it."""
    if not isinstance(raw, list) or len(raw) != len(mine):
        raise DrawWireError("commits wrong count")
    out: list[tuple[str, bytes]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise DrawWireError("commit malformed")
        out.append((item[0], _raw(item[1], dc.DIGEST_BYTES, "commit")))
    if sorted(u for u, _ in out) != sorted(p.user for p in mine):
        raise DrawWireError("commits name the wrong people")
    return sorted(out)


def parse_commits(rnd: _Round, raw: object) -> dict[str, list[str]]:
    """Every household's signed card commitment (hashes, b64, sorted) —
    each over the same transcript as ours (the circle included)."""
    if not isinstance(raw, dict) or set(raw) != set(rnd.order):
        raise DrawWireError("commitments malformed")
    out: dict[str, list[str]] = {}
    for inst in rnd.order:
        c = raw[inst]
        if not isinstance(c, dict):
            raise DrawWireError("commitment malformed")
        hashes = _raw_list(
            c.get("hashes"), len(rnd.of(inst)), dc.DIGEST_BYTES, "hashes"
        )
        sig = _raw(c.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(inst),
            dc.card_commit_message(rnd.t("reveals"), inst, hashes),
            sig,
        ):
            raise DrawWireError("card commitment signature")
        out[inst] = sorted(_b64(x) for x in hashes)
    return out


def parse_reveals(
    rnd: _Round, raw: object, batches: dict[str, dict]
) -> dict[str, bytes]:
    """Every household's circle randomness, each matching its signed
    commitment."""
    if not isinstance(raw, dict) or set(raw) != set(rnd.order):
        raise DrawWireError("reveals malformed")
    out: dict[str, bytes] = {}
    for inst in rnd.order:
        r = _raw(raw[inst], dc.SEED_BYTES, "r")
        if _b64(dc.r_commitment(rnd.ctx, inst, r)) != batches[inst]["r_commit"]:
            raise DrawWireError("r doesn't match its commitment")
        out[inst] = r
    return out


def check_card(rnd: _Round, card_bytes: bytes, *, giver: dc.Person) -> dc.Card | None:
    """A card a giver's box opened — ``None`` unless it is a valid card of
    another person of the round, signed by their own household."""
    try:
        card = dc.decode_card(card_bytes)
    except dc.DrawCryptoError:
        return None
    if (
        card.instance not in rnd.identities
        or rnd.seat_of(card.instance, card.user) is None
        or (card.instance, card.user) == _person_key(giver)
    ):
        return None
    if not verify_ed25519(
        rnd.identity(card.instance), dc.card_message(rnd.ctx, card), card.sig
    ):
        return None
    return card


def usable_card(rnd: _Round, card_bytes: bytes, *, giver: dc.Person) -> dc.Card | None:
    """The one test of a round's card, the same for the giver and for
    anyone checking its complaint: :func:`check_card` and first sealed
    (``rev`` 0). Anything else is unusable — a proven complaint."""
    card = check_card(rnd, card_bytes, giver=giver)
    return card if card is not None and card.rev == 0 else None


def check_card_list(
    cards: list[bytes], committed: dict[str, list[str]], n: int
) -> bool:
    """Whether *cards* are exactly the sealed cards every household
    committed to (by hash) — n of them, no other."""
    want = sorted(h for hs in committed.values() for h in hs)
    return len(cards) == n and sorted(_b64(dc.sha256(c)) for c in cards) == want


def _dropper(
    layers: list[bytes], outs: list[set[bytes]], first: set[bytes]
) -> int | None:
    """The first mixer whose input held this entry (``layers[k]``) and
    whose signed output lacks what it peels to — ``None`` when it got
    through (or never got in: its household's own doing)."""
    have = first
    for k, out in enumerate(outs):
        if dc.entry_hash(layers[k]) not in have:
            return None
        if dc.entry_hash(layers[k + 1]) not in out:
            return k
        have = out
    return None


def _proven_seed(
    rnd: _Round, complaint: object, batches: dict[str, dict]
) -> (
    tuple[str, str, str, bytes, dc.PersonKeys, tuple[str, dc.PersonKeys] | None] | None
):
    """``(kind, instance, user, seed, keys, pair)`` of a complaint signed by
    its household over our transcript (a missing key: up to the chain; any
    other: up to the reveals) whose seed derives the commitment it signed
    for that person — and, for a couple, the partner's too."""
    if not isinstance(complaint, dict):
        return None
    kind = complaint.get("kind")
    inst, user = complaint.get("instance"), complaint.get("user")
    if (
        kind not in COMPLAINT_KINDS
        or not isinstance(inst, str)
        or not isinstance(user, str)
    ):
        return None
    if inst not in rnd.identities or rnd.seat_of(inst, user) is None:
        return None
    seed = _raw(complaint.get("seed"), dc.SEED_BYTES, "seed")
    sig = _raw(complaint.get("sig"), dc.SIG_BYTES, "sig")
    pair: tuple[str, bytes] | None = None
    if kind == COMPLAINT_COUPLE:
        partner = complaint.get("partner")
        if not isinstance(partner, str) or rnd.seat_of(inst, partner) is None:
            return None
        pair = (partner, _raw(complaint.get("partner_seed"), dc.SEED_BYTES, "seed"))
    t = rnd.t("chain" if kind == COMPLAINT_MISSING else "reveals")
    msg = dc.complaint_message(t, inst, kind, user, seed, pair=pair)
    if not verify_ed25519(rnd.identity(inst), msg, sig):
        return None
    commits = batches.get(inst, {}).get("commits", [])
    keys = dc.person_keys(seed)
    if [user, _b64(dc.person_commitment(rnd.ctx, inst, user, seed))] not in commits:
        return None
    pair_keys = None
    if pair is not None:
        if [pair[0], _b64(dc.person_commitment(rnd.ctx, inst, *pair))] not in commits:
            return None
        pair_keys = (pair[0], dc.person_keys(pair[1]))
    return str(kind), inst, user, seed, keys, pair_keys


def complaint_verdict(
    rnd: _Round,
    complaint: object,
    *,
    S: list[bytes],
    batches: dict[str, dict],
    links: list[list[bytes]],
    mixer_pubs: list[bytes],
    cards: list[bytes] | None,
    C: list[bytes] | None = None,
) -> list[str] | None:
    """``None`` unless *complaint* about round *rnd* is proven; else the
    households it proves broke the round. Its proof is the person's seed —
    the complainer's own box only (the commitment it derives must be the
    one the household signed); nobody else opens anything:

    * ``missing`` — the onion the seed rebuilds is one the household sent,
      its key isn't in ``S``, and the signed chain shows the mixer whose
      output lacks it: that mixer is named. A household can't complain
      about its own mixer's doing (refused).
    * ``cards`` — the box opens no card :func:`usable_card` takes, or more
      than one: proven, **nobody named** — nothing else is ever opened to
      find out who sealed it so (``[]``); such redraws are capped per draw.
    * ``couple`` — before any card: two people of the complaining
      household the pairs exclude sit next to each other in the circle
      (*C*, the circle as we drew it); both seeds. Chance, nobody named.
    * ``partner`` — after the cards: the one card names someone the pairs
      exclude, sealed by another household (a couple within one household
      is told before any card exists); chance, nobody named."""
    try:
        got = _proven_seed(rnd, complaint, batches)
        if got is None:
            return None
        kind, inst, user, seed, keys, pair = got
        if kind == COMPLAINT_COUPLE:
            if C is None or pair is None:
                return None
            if keys.box_pub not in C or pair[1].box_pub not in C:
                return None
            j = C.index(keys.box_pub)
            if C[(j + 1) % rnd.n] != pair[1].box_pub:
                return None
            a, b = rnd.seat_of(inst, user), rnd.seat_of(inst, pair[0])
            return [] if a and b and rnd.settings.excludes(a, b) else None
        if kind == COMPLAINT_MISSING:
            if keys.box_pub in S:
                return None
            layers = dc.onion_layers(seed, mixer_pubs, ctx=rnd.ctx)
            if _b64(dc.entry_hash(layers[0])) not in batches[inst]["hashes"]:
                return None  # the household onioned another box
            first = {_raw(h) for b in batches.values() for h in b["hashes"]}
            k = _dropper(layers, [set(x) for x in links], first)
            if k is None or rnd.order[k] == inst:
                return None  # its own mixer dropped it: no proof against anyone
            return [rnd.order[k]]
        if S.count(keys.box_pub) != 1 or cards is None:
            return None
        giver = dc.Person(seat=rnd.seat_of(inst, user) or 0, instance=inst, user=user)
        opened = [
            (sealed, o)
            for sealed in cards
            if (o := dc.open_card(keys.box_priv, sealed, ctx=rnd.ctx)) is not None
        ]
        if kind == COMPLAINT_CARDS:
            ok = len(opened) == 1 and usable_card(rnd, opened[0][1], giver=giver)
            return None if ok else []
        card = usable_card(rnd, opened[0][1], giver=giver) if len(opened) == 1 else None
        if card is None or card.instance == inst:
            return None  # its own household's couple is told before the cards
        to_seat = rnd.seat_of(card.instance, card.user)
        if to_seat is not None and rnd.settings.excludes(giver.seat, to_seat):
            return []
        return None
    except DrawWireError, dc.DrawCryptoError, KeyError, ValueError:
        return None


def complaint_holds(
    rnd: _Round,
    complaint: object,
    *,
    S: list[bytes],
    batches: dict[str, dict],
    links: list[list[bytes]],
    mixer_pubs: list[bytes],
    cards: list[bytes] | None,
    C: list[bytes] | None = None,
) -> bool:
    """Whether *complaint* is proven (:func:`complaint_verdict`)."""
    return (
        complaint_verdict(
            rnd,
            complaint,
            S=S,
            batches=batches,
            links=links,
            mixer_pubs=mixer_pubs,
            cards=cards,
            C=C,
        )
        is not None
    )


def _opening_signed(rnd: _Round, instance: str, opening: object, t: bytes) -> bool:
    """Whether a seeds opening carries its household's signature over our
    transcript *t* (with its ``stuck`` word, when it has one)."""
    try:
        if not isinstance(opening, dict):
            return False
        seeds_raw = opening.get("seeds")
        if not isinstance(seeds_raw, dict):
            return False
        seeds = [(u, _raw(s, dc.SEED_BYTES, "seed")) for u, s in seeds_raw.items()]
        stuck = opening.get("stuck")
        if stuck is not None:
            stuck = _int(stuck, 0, rnd.h - 1, "stuck")
        sig = _raw(opening.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.opening_message(t, instance, seeds, stuck=stuck)
        return verify_ed25519(rnd.identity(instance), msg, sig)
    except DrawWireError, KeyError, ValueError:
        return False


def opened_seeds(
    rnd: _Round,
    instance: str,
    opening: object,
    *,
    batches: dict[str, dict],
    mixer_pubs: list[bytes],
    t: bytes,
    stuck: int | None = None,
) -> list[bytes] | None:
    """A household's signed opening of every seed of a round that broke
    before its cards: the seeds when they rebuild exactly the onions and
    commitments it signed; ``None`` when they don't — its own signed word
    shows it broke the round. An opening without its household's valid
    signature proves nothing about anyone (:class:`DrawWireError`): a relay
    can't frame a household with a junk one."""
    if not _opening_signed(rnd, instance, opening, t):
        raise DrawWireError("an opening without its household's word")
    assert isinstance(opening, dict)
    if opening.get("stuck") != stuck:
        return None  # signed, but a stuck word that isn't its turn
    try:
        seeds = [
            (u, _raw(s, dc.SEED_BYTES, "seed")) for u, s in opening["seeds"].items()
        ]
        if sorted(u for u, _ in seeds) != sorted(p.user for p in rnd.of(instance)):
            return None
        batch = batches[instance]
        rebuilt = sorted(
            _b64(dc.entry_hash(dc.onion_from_seed(s, mixer_pubs, ctx=rnd.ctx)))
            for _u, s in seeds
        )
        commits = sorted(
            [u, _b64(dc.person_commitment(rnd.ctx, instance, u, s))] for u, s in seeds
        )
        if rebuilt != sorted(batch["hashes"]) or commits != sorted(batch["commits"]):
            return None
        return [s for _u, s in seeds]
    except DrawWireError, dc.DrawCryptoError, KeyError, ValueError:
        return None


def judge_void(
    rnd: _Round,
    ev: object,
    *,
    mixer_pubs: list[bytes],
    S: list[bytes] | None = None,
    batches: dict[str, dict] | None = None,
    links: list[list[bytes]] | None = None,
) -> list[str]:
    """Who broke a round that broke publicly **before any card existed** —
    from every household's signed opening and the round's list as the
    caller holds it (*S*, *batches*, *links*; for a stuck mix the batches
    and the chain so far come with the evidence). The hub and every
    household run this same check, so every household names the same
    households (:class:`DrawWireError`: the evidence doesn't show a broken
    round, or an opening lacks its household's signature — nobody named).

    Every seed rebuilds its person's entry at each step of the mix; the
    first mixer whose signed output lacks one is named, and so is a
    household whose signed opening doesn't rebuild what it signed. A stuck
    mixer whose input was exactly every honest entry (nobody else named)
    could have peeled it: it is named."""
    if not isinstance(ev, dict):
        raise DrawWireError("void malformed")
    why = ev.get("why")
    openings = ev.get("openings")
    if not isinstance(openings, dict) or set(openings) != set(rnd.order):
        raise DrawWireError("openings malformed")
    stuck: int | None = None
    if why == VOID_UNUSABLE:
        if S is None or batches is None or links is None:
            raise DrawWireError("no list here")
        if all(dc.is_usable_key(s) for s in S):
            raise DrawWireError("the round wasn't void")
        t = rnd.t("chain")
    elif why == VOID_STUCK:
        stuck = _int(ev.get("k"), 0, rnd.h - 1, "k")
        batches, first_list, t = _check_batches(rnd, ev.get("batches"))
        links = _check_chain(
            rnd, ev.get("chain"), first_list, length=stuck, t_batches=t
        )
        claim = openings[rnd.order[stuck]]
        if not isinstance(claim, dict) or claim.get("stuck") != stuck:
            raise DrawWireError("no stuck mixer's word")
    else:
        raise DrawWireError("void reason malformed")
    culprits: set[str] = set()
    seeds: list[bytes] = []
    for inst in rnd.order:
        got = opened_seeds(
            rnd,
            inst,
            openings[inst],
            batches=batches,
            mixer_pubs=mixer_pubs,
            t=t,
            stuck=stuck if stuck is not None and inst == rnd.order[stuck] else None,
        )
        if got is None:
            culprits.add(inst)
        else:
            seeds += got
    first = {_raw(h) for b in batches.values() for h in b["hashes"]}
    outs = [set(x) for x in links]
    for seed in seeds:
        k = _dropper(dc.onion_layers(seed, mixer_pubs, ctx=rnd.ctx), outs, first)
        if k is not None:
            culprits.add(rnd.order[k])
    if stuck is not None and not culprits:
        # Its input was every honest entry, each sealed to its key.
        culprits.add(rnd.order[stuck])
    return sorted(culprits)


class AppDrawService:
    """The gift draw engine: the hub's rounds and each household's own
    steps. An :class:`~socialhome.app_engines.registry.AppEngine`."""

    engine_id: str = DRAW_ENGINE_ID
    suites: frozenset[str] = dc.SUPPORTED_DRAW_SUITES
    steps: frozenset[str] = DRAW_STEPS

    __slots__ = (
        "_app_fed",
        "_bus",
        "_federation",
        "_groups",
        "_household_label",
        "_key_manager",
        "_locks",
        "_rand",
        "_repo",
        "_state",
        "_tasks",
        "_user_repo",
        "_ws",
    )

    def __init__(
        self,
        *,
        groups: AppGroupService,
        group_repo: AbstractAppGroupRepo,
        state_repo: AbstractAppEngineStateRepo,
        app_federation: AppFederationService,
        user_repo: Any,
        federation: Any,
        key_manager: KeyManager,
        ws: WebSocketManager,
        household_label: str,
        bus: EventBus | None = None,
        rand: dc.Rand = os.urandom,
    ) -> None:
        self._groups = groups
        self._repo = group_repo
        self._state = state_repo
        self._app_fed = app_federation
        self._user_repo = user_repo
        self._federation = federation
        self._key_manager = key_manager
        self._ws = ws
        self._household_label = household_label
        self._bus = bus
        #: Randomness for keys, seeds, shuffles and seals (``os.urandom``;
        #: tests pin it to make test vectors).
        self._rand = rand
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._tasks: set[asyncio.Task] = set()

    # ─── Plumbing ─────────────────────────────────────────────────────

    @property
    def _own(self) -> str:
        return str(self._federation.own_instance_id)

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def stop(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _emit(self, event: DomainEvent) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft publish
            log.warning("app_draw: bus publish failed: %s", exc)

    def _ad(self, session_id: str) -> bytes:
        return f"socialhome/app_engine/state|{session_id}|{DRAW_ENGINE_ID}".encode()

    async def _load(self, session_id: str) -> dict:
        raw = await self._state.get(session_id, DRAW_ENGINE_ID)
        if raw is None:
            return {"v": 1}
        record = orjson.loads(raw)
        plain = self._key_manager.decrypt(
            record["enc"], associated_data=self._ad(session_id)
        )
        return orjson.loads(plain)

    async def _save(self, session_id: str, st: dict) -> None:
        enc = self._key_manager.encrypt(
            orjson.dumps(st), associated_data=self._ad(session_id)
        )
        await self._state.set(
            session_id, DRAW_ENGINE_ID, orjson.dumps({"v": 1, "enc": enc}).decode()
        )

    def _sign(self, message: bytes) -> str:
        return _b64(sign_ed25519(self._federation.own_identity_seed, message))

    async def _send(
        self, table: GroupSession, to_instance: str, step: str, body: dict
    ) -> None:
        """One draw step to *to_instance* — this household's own steps run
        in a task of their own (after the caller's lock is released)."""
        payload = {
            "app_id": table.app_id,
            "session_id": table.session_id,
            "verb": VERB_ENGINE,
            "engine": DRAW_ENGINE_ID,
            ENGINE_SUITE_FIELD: dc.DRAW_SUITE_X25519_AESGCM,
            "step": step,
            **body,
        }
        if to_instance == self._own:
            self._spawn(self._local(table.session_id, payload))
            return
        await self._groups.send_to(table, to_instance, payload)

    async def _local(self, session_id: str, payload: dict) -> None:
        table = await self._repo.get(session_id)
        if table is None:
            return
        try:
            await self._step(table, self._own, payload)
        except (DrawWireError, dc.DrawCryptoError, dc.UnsupportedDrawSuite) as exc:
            log.info("app_draw: own %s step dropped: %s", payload.get("step"), exc)
        except Exception:
            log.exception("app_draw: own step %s failed", payload.get("step"))

    async def _supports(self, instance: str) -> bool:
        if instance == self._own:
            return True
        return bool(
            await self._federation.space_member_supports(
                instance, min_version=FederationCapability.MIN_FOR_APP_ENGINES
            )
        )

    # ─── Inbound (from the registry) ──────────────────────────────────

    async def on_inbound(self, event: FederationEvent, table: GroupSession) -> None:
        """A draw step for *table* — the registry checked the engine, its
        suite, and that this household's copy of the app declares it."""
        sender = event.from_instance
        if not await self._supports(sender):
            log.info("app_draw: step from %s below v_78 dropped", sender)
            return
        try:
            await self._step(table, sender, event.payload)
        except (DrawWireError, dc.DrawCryptoError, dc.UnsupportedDrawSuite) as exc:
            log.info(
                "app_draw: %s from %s for %s dropped: %s",
                event.payload.get("step"),
                sender,
                table.session_id,
                exc,
            )

    async def _step(self, table: GroupSession, sender: str, p: dict) -> None:
        dc.check_suite(p.get(ENGINE_SUITE_FIELD))
        if p.get("engine") != DRAW_ENGINE_ID:
            raise DrawWireError("not a draw step")
        step = p.get("step")
        if step not in DRAW_STEPS:
            raise DrawWireError("unknown step")
        gen = _int(p.get("gen"), 1, DRAW_AGAIN_MAX + 1, "gen")
        round_no = _int(p.get("round"), 1, DRAW_ROUNDS_MAX, "round")
        is_hub = table.hub_instance == self._own
        # A hand-over / wish update the hub passes on keeps its step and
        # says so (``relayed``); only the hub's own copy may.
        if step in DRAW_TO_HUB_STEPS and p.get("relayed") is not True:
            if not is_hub:
                raise DrawWireError("not the hub")
            async with self._lock(table.session_id):
                await self._hub_step(table, sender, str(step), gen, round_no, p)
            return
        if sender != table.hub_instance:
            raise DrawWireError("not from the hub")
        async with self._lock(table.session_id):
            await self._participant_step(table, str(step), gen, round_no, p)

    # ─── The hub ──────────────────────────────────────────────────────

    async def _hub_step(
        self,
        table: GroupSession,
        sender: str,
        step: str,
        gen: int,
        round_no: int,
        p: dict,
    ) -> None:
        st = await self._load(table.session_id)
        hub = st.get("hub")
        if hub is None or (hub["gen"], hub["round"]) != (gen, round_no):
            raise DrawWireError("not the current round")
        rnd = _Round(hub)
        if sender not in rnd.order:
            raise DrawWireError("not a participant household")
        match step:
            case "key":
                await self._hub_key(table, st, rnd, sender, p)
            case "onions":
                await self._hub_onions(table, st, rnd, sender, p)
            case "mixed":
                await self._hub_mixed(table, st, rnd, sender, p)
            case "reveal":
                await self._hub_reveal(table, st, rnd, sender, p)
            case "open":
                await self._hub_open(table, st, rnd, sender, p)
            case "commit":
                await self._hub_commit(table, st, rnd, sender, p)
            case "cards":
                await self._hub_cards(table, st, rnd, sender, p)
            case "done":
                await self._hub_done(table, st, rnd, sender, p)
            case "fail" | "redo":
                await self._hub_complaint(table, st, rnd, sender, step, p)
            case "handover" | "wish":
                await self._hub_relay_after(table, st, rnd, sender, step, p)
            case _:  # pragma: no cover — DRAW_TO_HUB_STEPS is exhaustive
                raise DrawWireError("unknown step")

    @staticmethod
    def _hub_expect(hub: dict, phase: str) -> None:
        if hub["phase"] != phase:
            raise DrawWireError(f"not in {phase}")

    @staticmethod
    def _rid(rnd: _Round, **fields: Any) -> dict:
        return {"gen": rnd.gen, "round": rnd.round, **fields}

    async def _to_all(
        self, table: GroupSession, rnd: _Round, step: str, body: dict
    ) -> None:
        for inst in rnd.order:
            await self._send(table, inst, step, self._rid(rnd, **body))

    async def _hub_key(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_KEYS)
        pub = _raw(p.get("pub"), dc.KEY_BYTES, "pub")
        sig = _raw(p.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(sender), dc.key_message(rnd.ctx, sender, pub), sig
        ):
            raise DrawWireError("key signature")
        if not dc.is_usable_key(pub):
            raise DrawWireError("mixer key unusable")
        if sender in hub["keys"]:
            return  # first key wins; a repeat is the outbox retrying
        hub["keys"][sender] = {"pub": _b64(pub), "sig": _b64(sig)}
        if len(hub["keys"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        hub["phase"] = _PHASE_ONIONS
        hub["since"] = _now().isoformat()
        hub["mixer_pubs"] = [hub["keys"][i]["pub"] for i in rnd.order]
        hub["t_keys"] = _b64(
            dc.transcript_keys(
                rnd.ctx, [(i, _raw(hub["keys"][i]["pub"])) for i in rnd.order]
            )
        )
        await self._save(table.session_id, st)
        keys = [{"instance": i, **hub["keys"][i]} for i in rnd.order]
        await self._to_all(table, rnd, STEP_MIXERS, {"keys": keys})

    async def _hub_onions(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_ONIONS)
        if sender in hub["batches"]:
            return
        hub["batches"][sender] = self._check_batch(rnd, sender, p)
        if len(hub["batches"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        hub["phase"] = _PHASE_MIXING
        hub["k"] = 0
        hub["since"] = _now().isoformat()
        l0 = [
            o for inst in sorted(hub["batches"]) for o in hub["batches"][inst]["onions"]
        ]
        hub["in"] = _b64(dc.list_digest([dc.entry_hash(_raw(o)) for o in l0]))
        # What everyone checks later (the onions themselves go to the
        # first mixer only).
        hub["batches_pub"] = [
            {
                "instance": i,
                "hashes": [
                    _b64(dc.entry_hash(_raw(o))) for o in hub["batches"][i]["onions"]
                ],
                "commits": hub["batches"][i]["commits"],
                "r_commit": hub["batches"][i]["r_commit"],
                "sig": hub["batches"][i]["sig"],
            }
            for i in sorted(hub["batches"])
        ]
        _b, _f, t_batches = _check_batches(_Round(hub), hub["batches_pub"])
        hub["t_batches"] = _b64(t_batches)
        await self._save(table.session_id, st)
        batches = [{"instance": i, **hub["batches"][i]} for i in sorted(hub["batches"])]
        await self._send(
            table, rnd.order[0], STEP_MIX, self._rid(rnd, k=0, batches=batches)
        )

    @staticmethod
    def _check_batch(rnd: _Round, inst: str, b: dict) -> dict:
        """A household's onions: one per person of it, each exactly the
        round's onion size, signed with its commitments and r-commitment."""
        mine = rnd.of(inst)
        onions = _raw_list(b.get("onions"), len(mine), dc.onion_size(rnd.h), "onions")
        commits = _commits(b.get("commits"), mine)
        r_commit = _raw(b.get("r_commit"), dc.DIGEST_BYTES, "r_commit")
        sig = _raw(b.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.batch_message(
            rnd.t("keys"), inst, [dc.entry_hash(o) for o in onions], commits, r_commit
        )
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            raise DrawWireError("onions signature")
        return {
            "onions": [_b64(o) for o in onions],
            "commits": [[u, _b64(c)] for u, c in commits],
            "r_commit": _b64(r_commit),
            "sig": _b64(sig),
        }

    async def _hub_mixed(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_MIXING)
        k = hub["k"]
        if _int(p.get("k"), 0, rnd.h - 1, "k") != k or sender != rnd.order[k]:
            raise DrawWireError("not this mixer's turn")
        d_in = _raw(p.get("in"), dc.DIGEST_BYTES, "in")
        if _b64(d_in) != hub["in"]:
            raise DrawWireError("mixed another list")
        out = _raw_list(p.get("list"), rnd.n, dc.onion_size(rnd.h - k - 1), "list")
        hashes = [dc.entry_hash(o) for o in out]
        d_out = dc.list_digest(hashes)
        sig = _raw(p.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(sender),
            dc.mix_message(rnd.t("batches"), k, sender, d_in, d_out),
            sig,
        ):
            raise DrawWireError("mix signature")
        # Every entry's hash, signed: a seed later shows whose output
        # lacks its person (n × 32 bytes a link).
        hub["chain"].append(
            {"instance": sender, "hashes": [_b64(x) for x in hashes], "sig": _b64(sig)}
        )
        prev = {"instance": sender, "in": hub["in"], "sig": _b64(sig)}
        hub["in"] = _b64(d_out)
        hub["k"] = k + 1
        hub["since"] = _now().isoformat()
        if k + 1 < rnd.h:
            await self._save(table.session_id, st)
            await self._send(
                table,
                rnd.order[k + 1],
                STEP_MIX,
                self._rid(
                    rnd,
                    k=k + 1,
                    list=[_b64(o) for o in out],
                    prev=prev,
                    # Every batch (signed over the keys): the next mixer
                    # checks them against its own transcript.
                    batches=hub["batches_pub"],
                ),
            )
            return
        hub["S"] = [_b64(o) for o in out]
        hub["list_body"] = {
            "S": hub["S"],
            "batches": hub["batches_pub"],
            "chain": hub["chain"],
        }
        hub["batches"] = {}  # the onions are spent: keep the hashes only
        hub["t_chain"] = _b64(
            dc.transcript_chain(
                rnd.t("batches"),
                [(c["instance"], [_raw(x) for x in c["hashes"]]) for c in hub["chain"]],
            )
        )
        # A key in S nobody can seal to breaks the round publicly: every
        # household opens its seeds, which names who broke it.
        if all(dc.is_usable_key(o) for o in out):
            hub["phase"] = _PHASE_REVEAL
        else:
            hub["phase"] = _PHASE_VOID
            hub["void"] = {"why": VOID_UNUSABLE}
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, STEP_LIST, hub["list_body"])

    async def _hub_reveal(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_REVEAL)
        r = _raw(p.get("r"), dc.SEED_BYTES, "r")
        batch = next(b for b in hub["list_body"]["batches"] if b["instance"] == sender)
        if _b64(dc.r_commitment(rnd.ctx, sender, r)) != batch["r_commit"]:
            raise DrawWireError("r doesn't match its commitment")
        if sender in hub["reveals"]:
            return
        hub["reveals"][sender] = _b64(r)
        if len(hub["reveals"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        S = [_raw(x) for x in hub["S"]]
        R = dc.round_randomness(
            rnd.ctx, {i: _raw(r) for i, r in hub["reveals"].items()}
        )
        C = dc.circle(S, ctx=rnd.ctx, R=R)
        hub["C"] = [_b64(x) for x in C]
        hub["t_reveals"] = _b64(
            dc.transcript_reveals(
                rnd.t("chain"),
                {i: _raw(r) for i, r in hub["reveals"].items()},
                dc.circle_digest(C),
            )
        )
        hub["phase"] = _PHASE_COMMIT
        hub["since"] = _now().isoformat()
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, STEP_REVEALS, {"reveals": hub["reveals"]})

    async def _hub_commit(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_COMMIT)
        hashes = _raw_list(
            p.get("hashes"), len(rnd.of(sender)), dc.DIGEST_BYTES, "hashes"
        )
        sig = _raw(p.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(sender),
            dc.card_commit_message(rnd.t("reveals"), sender, hashes),
            sig,
        ):
            raise DrawWireError("card commitment signature")
        if sender in hub["commits"]:
            return
        hub["commits"][sender] = {"hashes": [_b64(x) for x in hashes], "sig": _b64(sig)}
        if len(hub["commits"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        every = [x for c in hub["commits"].values() for x in c["hashes"]]
        if len(set(every)) != len(every):
            # Two households committed the same card: refuse the last word
            # (nothing else changed) — the round waits for an honest one.
            hub["commits"].pop(sender)
            raise DrawWireError("card commitments repeat")
        hub["phase"] = _PHASE_CARDS
        hub["since"] = _now().isoformat()
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, STEP_COMMITS, {"commits": hub["commits"]})

    async def _hub_cards(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_CARDS)
        if sender in hub["cards"]:
            return
        cards = _raw_list(
            p.get("cards"), len(rnd.of(sender)), dc.SEALED_CARD_BYTES, "cards"
        )
        if sorted(_b64(dc.sha256(c)) for c in cards) != sorted(
            hub["commits"][sender]["hashes"]
        ):
            raise DrawWireError("cards aren't the ones committed to")
        hub["cards"][sender] = [_b64(c) for c in cards]
        if len(hub["cards"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        every = sorted(
            (c for cs in hub["cards"].values() for c in cs),
            key=lambda c: dc.sha256(_raw(c)),
        )
        hub["all_cards"] = every
        hub["cards"] = {i: len(cs) for i, cs in hub["cards"].items()}
        hub["phase"] = _PHASE_OPENING
        hub["since"] = _now().isoformat()
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, STEP_CARDS_ALL, {"cards": every})

    async def _hub_done(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_OPENING)
        count = _int(p.get("count"), 1, DRAW_MAX_PEOPLE, "count")
        if count != len(rnd.of(sender)):
            raise DrawWireError("done for the wrong count")
        sig = _raw(p.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(sender),
            dc.done_message(rnd.t("reveals"), sender, count),
            sig,
        ):
            raise DrawWireError("done signature")
        if sender in hub["done"]:
            return
        hub["done"][sender] = {"count": count, "sig": _b64(sig)}
        if len(hub["done"]) < rnd.h:
            await self._save(table.session_id, st)
            await self._push(table, st)
            return
        hub["phase"] = _PHASE_COMPLETE
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, STEP_COMPLETE, {"dones": hub["done"]})
        await self._push(table, st)

    def _hub_round_data(
        self, hub: dict
    ) -> tuple[
        list[bytes], dict[str, dict], list[list[bytes]], list[bytes], list[bytes] | None
    ]:
        S = [_raw(s) for s in hub["S"]]
        batches = {b["instance"]: b for b in hub["list_body"]["batches"]}
        links = [[_raw(x) for x in link["hashes"]] for link in hub["chain"]]
        mixer_pubs = [_raw(x) for x in hub["mixer_pubs"]]
        cards = [_raw(c) for c in hub.get("all_cards", [])] or None
        return S, batches, links, mixer_pubs, cards

    async def _hub_complaint(
        self,
        table: GroupSession,
        st: dict,
        rnd: _Round,
        sender: str,
        step: str,
        p: dict,
    ) -> None:
        hub = st["hub"]
        complaint = p.get("complaint")
        if not isinstance(complaint, dict):
            raise DrawWireError("no complaint")
        kind = complaint.get("kind")
        if complaint.get("instance") != sender or (
            (step == STEP_REDO) != (kind in _CHANCE)
        ):
            raise DrawWireError("complaint malformed")
        if kind == COMPLAINT_MISSING:
            # Told INSTEAD of revealing, before any card exists.
            if hub["phase"] != _PHASE_REVEAL or sender in hub["reveals"]:
                raise DrawWireError("a missing key told too late")
        elif kind == COMPLAINT_COUPLE:
            # Told INSTEAD of committing, before any card exists.
            if hub["phase"] != _PHASE_COMMIT or sender in hub["commits"]:
                raise DrawWireError("a couple told too late")
        elif hub["phase"] != _PHASE_OPENING or sender in hub["done"]:
            raise DrawWireError("a card complaint out of turn")
        S, batches, links, mixer_pubs, cards = self._hub_round_data(hub)
        verdict = await asyncio.to_thread(
            complaint_verdict,
            rnd,
            complaint,
            S=S,
            batches=batches,
            links=links,
            mixer_pubs=mixer_pubs,
            cards=cards,
            C=[_raw(x) for x in hub.get("C", [])] or None,
        )
        if verdict is None:
            # A signed complaint that doesn't hold — proven: named to the
            # organiser ("…'s home stopped the draw").
            self._blame(hub, [sender])
            log.warning(
                "app_draw: %s stopped round %s/%s of %s without proof",
                sender,
                rnd.gen,
                rnd.round,
                table.session_id,
            )
            await self._save(table.session_id, st)
            await self._push(table, st)
            return
        because = {"kind": BECAUSE_COMPLAINT, "complaint": complaint}
        if kind == COMPLAINT_CARDS:
            # A visible redraw naming nobody — counted; past the cap the
            # draw stops and the organiser decides.
            count = hub.get("card_redraws", 0) + 1
            if count > DRAW_CARD_REDRAWS_MAX:
                await self._hub_end(
                    table,
                    st,
                    END_BROKEN,
                    because=because,
                    people=_people_wire(rnd.people, rnd.names),
                )
                return
            hub["card_redraws"] = count
        elif kind in _CHANCE:
            # Chance (the pairs): counted too, with room for a real small
            # table of couples; past it the pairs can't be drawn.
            count = hub.get("partner_redraws", 0) + 1
            if count > DRAW_PARTNER_REDRAWS_MAX:
                await self._hub_end(
                    table,
                    st,
                    END_RULES,
                    because=because,
                    people=_people_wire(rnd.people, rnd.names),
                )
                return
            hub["partner_redraws"] = count
        # Every household checks the complaint on its own copy of the
        # round — the evidence is the complaint alone.
        await self._redraw(
            table,
            st,
            rnd,
            {"kind": BECAUSE_COMPLAINT, "complaint": complaint},
            verdict,
        )

    @staticmethod
    def _blame(hub: dict, culprits: list[str]) -> None:
        for inst in culprits:
            if inst not in hub["blamed"]:
                hub["blamed"].append(inst)

    async def _redraw(
        self,
        table: GroupSession,
        st: dict,
        rnd: _Round,
        because: dict,
        culprits: list[str],
    ) -> None:
        """A round broke: name who broke it and draw a fresh round — or,
        once the same household broke a second round of this draw, hold
        the draw for the organiser ("Draw without them"), no more rounds."""
        hub = st["hub"]
        self._blame(hub, culprits)
        counts = hub.setdefault("broke", {})
        for inst in culprits:
            counts[inst] = counts.get(inst, 0) + 1
        twice = sorted(i for i in culprits if counts[i] >= 2)
        if twice:
            hub["phase"] = _PHASE_HELD
            hub["held"] = twice
            hub["since"] = _now().isoformat()
            log.warning(
                "app_draw: %s broke a second round of %s — held for the organiser",
                ",".join(twice),
                table.session_id,
            )
            await self._save(table.session_id, st)
            await self._push(table, st)
            return
        await self._next_round(
            table, st, people=rnd.people, names=rnd.names, because=because
        )

    async def _hub_open(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        """A household's signed opening of a round that broke publicly
        before any card existed — or the mixer whose turn it is saying its
        signed input doesn't peel. An opening without its household's
        signature is dropped (the household shows as waited for). Once every
        household opened, the openings name who broke it (every household
        checks the same) and a fresh round follows."""
        hub = st["hub"]
        opening = p.get("opening")
        stuck = hub["phase"] == _PHASE_MIXING or (
            hub["phase"] == _PHASE_VOID and hub["void"]["why"] == VOID_STUCK
        )
        t = rnd.t("batches" if stuck else "chain")
        if not _opening_signed(rnd, sender, opening, t):
            raise DrawWireError("an opening without its household's word")
        assert isinstance(opening, dict)
        if hub["phase"] == _PHASE_MIXING:
            k = hub["k"]
            if sender != rnd.order[k] or opening.get("stuck") != k:
                raise DrawWireError("an opening while mixing")
            hub["phase"] = _PHASE_VOID
            hub["void"] = {"why": VOID_STUCK, "k": k}
            hub["opens"] = {sender: opening}
            hub["since"] = _now().isoformat()
            await self._save(table.session_id, st)
            await self._to_all(
                table,
                rnd,
                STEP_VOID,
                {
                    "why": VOID_STUCK,
                    "k": k,
                    "claim": opening,
                    "batches": hub["batches_pub"],
                    "chain": hub["chain"],
                },
            )
            return
        self._hub_expect(hub, _PHASE_VOID)
        if sender in hub["opens"]:
            return
        hub["opens"][sender] = opening
        if len(hub["opens"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        evidence = self._void_evidence(hub)
        mixer_pubs = [_raw(x) for x in hub["mixer_pubs"]]
        if hub["void"]["why"] == VOID_STUCK:
            culprits = await asyncio.to_thread(
                judge_void, rnd, evidence, mixer_pubs=mixer_pubs
            )
        else:
            S, batches, links, _m, _c = self._hub_round_data(hub)
            culprits = await asyncio.to_thread(
                judge_void,
                rnd,
                evidence,
                mixer_pubs=mixer_pubs,
                S=S,
                batches=batches,
                links=links,
            )
        log.warning(
            "app_draw: round %s/%s of %s broke (%s) — %s",
            rnd.gen,
            rnd.round,
            table.session_id,
            hub["void"]["why"],
            ",".join(culprits) or "nobody named",
        )
        await self._redraw(table, st, rnd, evidence, culprits)

    @staticmethod
    def _void_evidence(hub: dict) -> dict:
        """What every household checks a void round's restart against: the
        openings (and, for a stuck mix, the batches and the chain so far,
        which no household holds yet). The list itself isn't sent again —
        every household checks against its own copy."""
        why = hub["void"]["why"]
        ev: dict[str, Any] = {
            "kind": BECAUSE_VOID,
            "why": why,
            "openings": hub["opens"],
        }
        if why == VOID_STUCK:
            ev.update(
                k=hub["void"]["k"], batches=hub["batches_pub"], chain=hub["chain"]
            )
        return ev

    async def _hub_relay_after(
        self,
        table: GroupSession,
        st: dict,
        rnd: _Round,
        sender: str,
        step: str,
        p: dict,
    ) -> None:
        """A hand-over or a wish-list update after the reveal: passed on to
        every participant household (key-private — the hub can't tell whom
        it is for), a bounded number per person of the sender."""
        hub = st["hub"]
        if hub["phase"] != _PHASE_COMPLETE:
            raise DrawWireError("not revealed")
        size = (
            dc.SEALED_HANDOVER_BYTES if step == STEP_HANDOVER else dc.SEALED_CARD_BYTES
        )
        sealed = _raw(p.get("sealed"), size, "sealed")
        sent = hub.setdefault("after", {})
        budget = len(rnd.of(sender)) * (DRAW_WISH_UPDATES_MAX + 1) * 2
        if sent.get(sender, 0) >= budget:
            raise DrawWireError("too many updates")
        sent[sender] = sent.get(sender, 0) + 1
        await self._save(table.session_id, st)
        await self._to_all(table, rnd, step, {"sealed": _b64(sealed), "relayed": True})

    async def _identities(
        self,
        table: GroupSession,
        instances: set[str],
        hints: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        """Each household's identity key (hex): the one this household stores
        for it, else a *hint* that derives to its id (a household id IS its
        key's fingerprint). ``None`` when one is unknown."""
        out: dict[str, str] = {}
        for inst in sorted(instances):
            if inst == self._own:
                out[inst] = bytes(self._federation.own_identity_pk).hex()
                continue
            hint = (hints or {}).get(inst) or next(
                (
                    e.identity_pk
                    for e in table.roster
                    if e.instance == inst and e.identity_pk
                ),
                None,
            )
            key = await self._groups.identity_pk_of(inst, hint)
            if key is None:
                return None
            out[inst] = key.hex()
        return out

    async def _next_round(
        self,
        table: GroupSession,
        st: dict,
        *,
        people: list[dc.Person],
        names: dict[tuple[str, str], str],
        because: dict,
        again: bool = False,
    ) -> None:
        """Start the hub's next round (or the next draw, *again*); gives up
        after :data:`DRAW_ROUNDS_MAX` rounds — the ``end`` carries the same
        proof a next round would, so every household can check it."""
        old = st.get("hub")
        gen = (
            (old["gen"] + 1)
            if (old is not None and again)
            else (old["gen"] if old else 1)
        )
        round_no = 1 if (old is None or again) else old["round"] + 1
        seats = frozenset(p.seat for p in people)
        settings = DrawSettings.from_wire(st.get("settings") or {}).only(seats)
        if len(people) < DRAW_MIN_PEOPLE:
            await self._hub_end(table, st, END_TOO_FEW)
            return
        if not pairs_fit(settings.pairs, seats):
            await self._hub_end(table, st, END_RULES)
            return
        if round_no > DRAW_ROUNDS_MAX:
            await self._hub_end(
                table,
                st,
                END_RULES,
                because=because,
                people=_people_wire(people, names),
            )
            return
        instances = {p.instance for p in people}
        if old is not None and not again and instances <= set(old["identities"]):
            # The same draw: the keys every household already checked.
            identities: dict[str, str] | None = {
                i: old["identities"][i] for i in sorted(instances)
            }
        else:
            identities = await self._identities(table, instances)
        if identities is None:
            log.warning("app_draw: %s — a household's key is unknown", table.session_id)
            raise AppDrawStateError()
        frame = _frame(
            session_id=table.session_id,
            gen=gen,
            round_no=round_no,
            people=people,
            names=names,
            identities=identities,
            settings=settings,
        )
        st["hub"] = {
            **frame,
            "phase": _PHASE_KEYS,
            "since": _now().isoformat(),
            "keys": {},
            "batches": {},
            "chain": [],
            "reveals": {},
            "commits": {},
            "cards": {},
            "done": {},
            "opens": {},
            # Who broke a round stays named for the draw, with how often.
            "blamed": [] if (old is None or again) else list(old.get("blamed", [])),
            "broke": {} if (old is None or again) else dict(old.get("broke", {})),
            "card_redraws": 0 if (old is None or again) else old.get("card_redraws", 0),
            "partner_redraws": 0
            if (old is None or again)
            else old.get("partner_redraws", 0),
        }
        await self._save(table.session_id, st)
        rnd = _Round(st["hub"])
        body = {
            "people": frame["people"],
            "identities": identities,
            "settings": frame["settings"],
            "because": because,
        }
        await self._to_all(table, rnd, STEP_START, body)
        await self._push(table, st)

    async def _hub_end(
        self,
        table: GroupSession,
        st: dict,
        reason: str,
        *,
        because: dict | None = None,
        people: list | None = None,
    ) -> None:
        """The draw gives up. Each household takes the end only when its own
        copy shows it: too few seated, pairs that no longer fit, or — at the
        last round — the proof that broke it (*because*)."""
        hub = st.get("hub")
        if hub is None:
            return
        hub["phase"] = _PHASE_ENDED
        hub["ended"] = reason
        await self._save(table.session_id, st)
        body: dict[str, Any] = {"reason": reason}
        if because is not None:
            body["because"] = because
            body["people"] = people
        await self._to_all(table, _Round(hub), STEP_END, body)
        await self._push(table, st)

    # ─── A participant household ──────────────────────────────────────

    async def _participant_step(
        self, table: GroupSession, step: str, gen: int, round_no: int, p: dict
    ) -> None:
        st = await self._load(table.session_id)
        if step == STEP_START:
            await self._on_start(table, st, gen, round_no, p)
            return
        cur = st.get("p")
        if cur is None or (cur["gen"], cur["round"]) != (gen, round_no):
            raise DrawWireError("not the current round")
        rnd = _Round(cur)
        match step:
            case "mixers":
                await self._on_mixers(table, st, rnd, p)
            case "mix":
                await self._on_mix(table, st, rnd, p)
            case "list":
                await self._on_list(table, st, rnd, p)
            case "void":
                await self._on_void(table, st, rnd, p)
            case "reveals":
                await self._on_reveals(table, st, rnd, p)
            case "commits":
                await self._on_commits(table, st, rnd, p)
            case "cards_all":
                await self._on_cards_all(table, st, rnd, p)
            case "complete":
                await self._on_complete(table, st, rnd, p)
            case "end":
                await self._on_end(table, st, p)
            case "handover":
                await self._on_handover(table, st, rnd, p)
            case "wish":
                await self._on_wish(table, st, rnd, p)
            case _:
                raise DrawWireError("not a step for a participant")

    async def _own_people(self, table: GroupSession, people: list[dc.Person]) -> bool:
        """Whether every person of this household in *people* has a place
        at the table here (a local user with a group seat)."""
        for person in people:
            if person.instance != self._own:
                continue
            user = await self._user_repo.get(person.user)
            if user is None:
                return False
            if await self._groups.seat_row(table, user.user_id) is None:
                return False
        return True

    async def _on_start(
        self, table: GroupSession, st: dict, gen: int, round_no: int, p: dict
    ) -> None:
        if table.state == GROUP_ENDED:
            raise DrawWireError("table ended")
        people, names = _parse_people(p.get("people"))
        settings = DrawSettings.from_wire(p.get("settings"))
        seats = frozenset(x.seat for x in people)
        if not pairs_fit(settings.pairs, seats):
            raise DrawWireError("pairs don't fit")
        raw_ids = p.get("identities")
        instances = {x.instance for x in people}
        if not isinstance(raw_ids, dict) or set(raw_ids) != instances:
            raise DrawWireError("identities malformed")
        identities = await self._identities(table, instances, hints=raw_ids)
        if identities is None or identities != raw_ids:
            raise DrawWireError("identities don't check out")
        if self._own not in instances or not await self._own_people(table, people):
            raise DrawWireError("our people aren't at this table")
        frame = _frame(
            session_id=table.session_id,
            gen=gen,
            round_no=round_no,
            people=people,
            names=names,
            identities=identities,
            settings=settings,
        )
        cur = st.get("p")
        if cur is not None and (cur["gen"], cur["round"]) == (gen, round_no):
            if cur["ctx"] != frame["ctx"]:
                raise DrawWireError("a second start for the same round")
            await self._send(table, table.hub_instance, STEP_KEY, cur["key_msg"])
            return
        culprits = await self._justify(
            table, st, gen, round_no, people, p.get("because")
        )
        if culprits is None:
            # Our copy of the table doesn't show it yet (the hub's roster
            # is on its way): keep the start until it does (for a while).
            kept = st.get("pending_start") or {}
            at = kept.get("at") if kept.get("p") == p else None
            st["pending_start"] = {"p": p, "at": at or _now().isoformat()}
            await self._save(table.session_id, st)
            return
        st.pop("pending_start", None)
        priv, pub = dc.new_keypair(self._rand)
        rnd = _Round(frame)
        key_msg = self._rid(
            rnd, pub=_b64(pub), sig=self._sign(dc.key_message(rnd.ctx, self._own, pub))
        )
        new_draw = cur is None or gen != cur["gen"]
        if not new_draw:
            # The next round of the same draw keeps its settings (the pairs
            # only for the seats still in): the organiser can't change them
            # while a draw runs, so neither can the hub.
            assert cur is not None
            before = DrawSettings.from_wire(cur["settings"]).only(seats)
            if settings != before:
                raise DrawWireError("the settings changed mid-draw")
        # Each household counts who broke rounds of this draw itself: no
        # round after the same household broke two (the organiser decides).
        broke = {} if new_draw else dict(st.get("broke", {}))
        for inst in culprits:
            broke[inst] = broke.get(inst, 0) + 1
        if any(broke[i] >= 2 for i in culprits):
            raise DrawWireError("a household broke a second round")
        redraws = 0 if new_draw else st.get("card_redraws", 0)
        if _card_redraw(p.get("because")):
            redraws += 1
            if redraws > DRAW_CARD_REDRAWS_MAX:
                raise DrawWireError("too many card redraws")
        partner_redraws = 0 if new_draw else st.get("partner_redraws", 0)
        if _partner_redraw(p.get("because")):
            partner_redraws += 1
            if partner_redraws > DRAW_PARTNER_REDRAWS_MAX:
                raise DrawWireError("too many redraws for the pairs")
        st["broke"] = broke
        st["card_redraws"] = redraws
        st["partner_redraws"] = partner_redraws
        if new_draw:
            st.pop("final", None)  # a new draw: everyone gets a new name
            st["blamed"] = []
        # Who broke the last round — this household checked the proof
        # itself, so it names them to its own people too.
        blamed = st.setdefault("blamed", [])
        blamed += [i for i in culprits if i not in blamed]
        st["p"] = {**frame, "mixer_priv": _b64(priv), "key_msg": key_msg}
        st["last"] = {"gen": gen, "round": round_no, "complete": False}
        st.pop("ended", None)
        st.pop("pending_end", None)
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_KEY, key_msg)
        await self._push(table, st)

    async def _justify(
        self,
        table: GroupSession,
        st: dict,
        gen: int,
        round_no: int,
        people: list[dc.Person],
        because: object,
    ) -> list[str] | None:
        """Refuse a new round without its reason (design 5.4, "No re-rolls
        on demand") — :class:`DrawWireError`. ``None``: it may hold, but
        this household's copy of the table doesn't show it yet. Else the
        households the proof shows broke the last round."""
        if not isinstance(because, dict) or not isinstance(because.get("kind"), str):
            raise DrawWireError("a round without its reason")
        kind = because["kind"]
        last = st.get("last")
        cur = st.get("p")
        now = {_person_key(x) for x in people}
        seated = _seated(table)
        if kind in (BECAUSE_FIRST, BECAUSE_AGAIN):
            if kind == BECAUSE_FIRST:
                fresh = last is None and (gen, round_no) == (1, 1)
                # Afresh only after an end this household checked itself,
                # never after the names opened (no peek-then-re-roll).
                retry = (
                    last is not None
                    and st.get("ended") is not None
                    and not last["complete"]
                    and (gen, round_no) == (last["gen"] + 1, 1)
                )
                if not (fresh or retry):
                    raise DrawWireError("not the first draw")
            elif (
                last is None
                or not last["complete"]
                or (gen, round_no) != (last["gen"] + 1, 1)
            ):
                raise DrawWireError("draw again before a reveal")
            # A new draw counts toward "Draw again" (``gen``, at most
            # DRAW_AGAIN_MAX + 1) and only until the exchange day.
            if cur is not None and not _before_the_day(cur):
                raise DrawWireError("past the exchange day")
            # Exactly the people seated at the table as we see it — no
            # invisible extras, nobody left out.
            return [] if now == seated else None
        if last is None or cur is None:
            raise DrawWireError("no draw to follow")
        next_round = (gen, round_no) == (last["gen"], last["round"] + 1)
        # Someone removed once the cards are out (people may have read
        # theirs): the fresh round counts as a Draw again — its count, its
        # exchange day.
        as_again = (gen, round_no) == (last["gen"] + 1, 1)
        if last["complete"] or not (
            next_round or (kind == BECAUSE_CHANGED and as_again)
        ):
            raise DrawWireError("not the next round")
        before = {_person_key(x) for x in _people_from(cur["people"])}
        if kind == BECAUSE_CHANGED:
            if "commit_msg" in cur and not as_again:
                raise DrawWireError("a removal after the circle is a Draw again")
            if as_again and not _before_the_day(cur):
                raise DrawWireError("past the exchange day")
            # Fewer people, never new ones — and only people our own copy
            # shows gone.
            if not now < before:
                raise DrawWireError("the people didn't change")
            if "S" in cur:
                gone_households = {i for i, _u in before - now}
                if any(i == inst for i, _u in now for inst in gone_households):
                    # After S, only whole stuck households are dropped.
                    raise DrawWireError("a leave after S is no new round")
            return None if (before - now) & seated else []
        if now != before:
            raise DrawWireError("the people changed")
        rnd = _Round(cur)
        if kind == BECAUSE_VOID:
            return await self._justify_void(rnd, cur, because)
        if kind != BECAUSE_COMPLAINT:
            raise DrawWireError("unknown reason")
        complaint = because.get("complaint")
        if not isinstance(complaint, dict):
            raise DrawWireError("a complaint without its proof")
        # Checked on this household's own copy of the round: the list, the
        # mixer keys, the committed cards — nothing taken from the hub.
        if "S" not in cur or "links" not in cur:
            raise DrawWireError("no list of ours to check it on")
        cards: list[bytes] | None = None
        C: list[bytes] | None = None
        if complaint.get("kind") == COMPLAINT_MISSING:
            if "cards_msg" in cur or "commits_cards" in cur:
                raise DrawWireError("a missing key told after the cards")
        elif complaint.get("kind") == COMPLAINT_COUPLE:
            # Told instead of committing: never once we hold every
            # household's card commitment (theirs included — a household
            # that committed its cards saw no couple, or kept it to peek).
            if "C" not in cur or "commits_cards" in cur or "cards" in cur:
                raise DrawWireError("a couple told out of turn")
            C = [_raw(x) for x in cur["C"]]
        else:
            if "cards" not in cur:
                raise DrawWireError("no cards of ours to check it on")
            cards = [_raw(c) for c in cur["cards"]]
        verdict = await asyncio.to_thread(
            complaint_verdict,
            rnd,
            complaint,
            S=[_raw(x) for x in cur["S"]],
            batches=cur["batches"],
            links=[[_raw(x) for x in link] for link in cur["links"]],
            mixer_pubs=[_raw(x) for x in cur["mixer_pubs"]],
            cards=cards,
            C=C,
        )
        if verdict is None:
            raise DrawWireError("the complaint doesn't hold")
        return verdict

    async def _justify_void(self, rnd: _Round, cur: dict, because: dict) -> list[str]:
        """A void round's restart, checked on our own copy of the round:
        our own opening must be in it exactly as we sent it, and every
        opening is checked here (:func:`judge_void`)."""
        why = because.get("why")
        answer = cur.get("void_answer")
        openings = because.get("openings")
        if (
            not answer
            or not isinstance(openings, dict)
            or openings.get(self._own) != answer[1]["opening"]
        ):
            raise DrawWireError("not our opening")
        if "mixer_pubs" not in cur:
            raise DrawWireError("no mixer keys here")
        mixer_pubs = [_raw(x) for x in cur["mixer_pubs"]]
        if why == VOID_STUCK:
            if "S" in cur:
                raise DrawWireError("a stuck mix after the list")
            return await asyncio.to_thread(
                judge_void, rnd, because, mixer_pubs=mixer_pubs
            )
        if "S" not in cur or "links" not in cur:
            raise DrawWireError("no list of ours to check it on")
        return await asyncio.to_thread(
            judge_void,
            rnd,
            because,
            mixer_pubs=mixer_pubs,
            S=[_raw(x) for x in cur["S"]],
            batches=cur["batches"],
            links=[[_raw(x) for x in link] for link in cur["links"]],
        )

    async def _on_mixers(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        keys = p.get("keys")
        if not isinstance(keys, list) or len(keys) != rnd.h:
            raise DrawWireError("keys wrong count")
        pubs: list[bytes] = []
        for inst, item in zip(rnd.order, keys, strict=True):
            if not isinstance(item, dict) or item.get("instance") != inst:
                raise DrawWireError("keys out of order")
            pub = _raw(item.get("pub"), dc.KEY_BYTES, "pub")
            sig = _raw(item.get("sig"), dc.SIG_BYTES, "sig")
            if not verify_ed25519(
                rnd.identity(inst), dc.key_message(rnd.ctx, inst, pub), sig
            ):
                raise DrawWireError("mixer key signature")
            if not dc.is_usable_key(pub):
                raise DrawWireError("mixer key unusable")
            pubs.append(pub)
        mine = dc.public_of(_raw(cur["mixer_priv"]))
        if pubs[rnd.order.index(self._own)] != mine:
            raise DrawWireError("not our mixer key")
        if "onions_msg" not in cur:
            t_keys = dc.transcript_keys(
                rnd.ctx, list(zip(rnd.order, pubs, strict=True))
            )
            cur["t_keys"] = _b64(t_keys)
            onions, seeds, commits = await asyncio.to_thread(
                self._make_onions, rnd, pubs
            )
            r = self._rand(dc.SEED_BYTES)
            r_commit = dc.r_commitment(rnd.ctx, self._own, r)
            batch_sig = self._sign(
                dc.batch_message(
                    t_keys,
                    self._own,
                    [dc.entry_hash(o) for o in onions],
                    commits,
                    r_commit,
                )
            )
            cur["seeds"] = seeds
            cur["r"] = _b64(r)
            cur["mixer_pubs"] = [_b64(x) for x in pubs]
            cur["onions_msg"] = self._rid(
                rnd,
                onions=[_b64(o) for o in onions],
                commits=[[u, _b64(c)] for u, c in commits],
                r_commit=_b64(r_commit),
                sig=batch_sig,
            )
            await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_ONIONS, cur["onions_msg"])

    def _make_onions(
        self, rnd: _Round, pubs: list[bytes]
    ) -> tuple[list[bytes], dict[str, str], list[tuple[str, bytes]]]:
        """A seed, commitment and (deterministic) onion per own person
        (CPU-bound: runs in a worker thread)."""
        onions: list[bytes] = []
        seeds: dict[str, str] = {}
        commits: list[tuple[str, bytes]] = []
        for person in rnd.of(self._own):
            seed = self._rand(dc.SEED_BYTES)
            seeds[person.user] = _b64(seed)
            commits.append(
                (
                    person.user,
                    dc.person_commitment(rnd.ctx, self._own, person.user, seed),
                )
            )
            onions.append(dc.onion_from_seed(seed, pubs, ctx=rnd.ctx))
        return dc.shuffle(onions, self._rand), seeds, sorted(commits)

    @staticmethod
    def _box_priv(cur: dict, user: str) -> bytes:
        return dc.person_keys(_raw(cur["seeds"][user])).box_priv

    @staticmethod
    def _box_pub(cur: dict, user: str) -> bytes:
        return dc.person_keys(_raw(cur["seeds"][user])).box_pub

    async def _on_mix(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        k = _int(p.get("k"), 0, rnd.h - 1, "k")
        if rnd.order[k] != self._own:
            raise DrawWireError("not our turn")
        if k == 0:
            entries, t_batches = self._first_list(rnd, p, cur)
        else:
            entries = _raw_list(p.get("list"), rnd.n, dc.onion_size(rnd.h - k), "list")
            batches, _first, t_batches = _check_batches(rnd, p.get("batches"))
            sent = cur["onions_msg"]
            if sorted(batches[self._own]["hashes"]) != sorted(
                _b64(dc.entry_hash(_raw(o))) for o in sent["onions"]
            ):
                raise DrawWireError("our onions changed")
            prev = p.get("prev")
            if not isinstance(prev, dict) or prev.get("instance") != rnd.order[k - 1]:
                raise DrawWireError("prev malformed")
            d_prev_in = _raw(prev.get("in"), dc.DIGEST_BYTES, "in")
            sig = _raw(prev.get("sig"), dc.SIG_BYTES, "sig")
            d_list = dc.list_digest([dc.entry_hash(e) for e in entries])
            msg = dc.mix_message(t_batches, k - 1, rnd.order[k - 1], d_prev_in, d_list)
            if not verify_ed25519(rnd.identity(rnd.order[k - 1]), msg, sig):
                raise DrawWireError("not the previous mixer's output")
        d_in = dc.list_digest([dc.entry_hash(e) for e in entries])
        done = cur.get("mixed")
        if done is not None:
            if done["in"] != _b64(d_in):
                log.warning(
                    "app_draw: second, different list to mix for %s", table.session_id
                )
                raise DrawWireError("already mixed another list")
            await self._send(table, table.hub_instance, done["step"], done["msg"])
            return
        try:
            out = await asyncio.to_thread(
                dc.mix,
                _raw(cur["mixer_priv"]),
                entries,
                ctx=rnd.ctx,
                k=k,
                h=rnd.h,
                rand=self._rand,
            )
        except dc.DrawCryptoError as exc:
            # Our signed input doesn't peel: say so with every seed of ours
            # (the round is void; the openings name who broke it — us, if
            # we lie).
            log.warning(
                "app_draw: mixer input for %s doesn't peel: %s", table.session_id, exc
            )
            claim = self._opening(rnd, cur, t_batches, stuck=k)
            cur["mixed"] = {"in": _b64(d_in), "step": STEP_OPEN, "msg": claim}
            cur["void_answer"] = [STEP_OPEN, claim]
            await self._save(table.session_id, st)
            await self._send(table, table.hub_instance, STEP_OPEN, claim)
            return
        d_out = dc.list_digest([dc.entry_hash(o) for o in out])
        out_sig = self._sign(dc.mix_message(t_batches, k, self._own, d_in, d_out))
        reply = self._rid(
            rnd, k=k, list=[_b64(o) for o in out], **{"in": _b64(d_in)}, sig=out_sig
        )
        cur["mixed"] = {"in": _b64(d_in), "step": STEP_MIXED, "msg": reply}
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_MIXED, reply)

    def _opening(
        self, rnd: _Round, cur: dict, t: bytes, *, stuck: int | None = None
    ) -> dict:
        """Every seed of ours this round, signed over our transcript *t*
        (with *stuck*: we are that mixer and our input doesn't peel)."""
        seeds = [(u, _raw(s)) for u, s in cur["seeds"].items()]
        sig = self._sign(dc.opening_message(t, self._own, seeds, stuck=stuck))
        opening: dict[str, Any] = {"seeds": cur["seeds"], "sig": sig}
        if stuck is not None:
            opening["stuck"] = stuck
        return self._rid(rnd, opening=opening)

    def _first_list(self, rnd: _Round, p: dict, cur: dict) -> tuple[list[bytes], bytes]:
        """The first mixer's input: exactly every household's signed onions
        (households in id order), ours exactly as we sent them — and the
        transcript after them."""
        batches = p.get("batches")
        if not isinstance(batches, list) or len(batches) != rnd.h:
            raise DrawWireError("batches wrong count")
        by_inst: dict[str, dict] = {
            b["instance"]: b
            for b in batches
            if isinstance(b, dict) and isinstance(b.get("instance"), str)
        }
        if set(by_inst) != set(rnd.order):
            raise DrawWireError("batches name the wrong households")
        entries: list[bytes] = []
        parts = []
        for inst in sorted(by_inst):
            checked = self._check_batch(rnd, inst, by_inst[inst])
            if inst == self._own and checked["onions"] != cur["onions_msg"]["onions"]:
                raise DrawWireError("our onions changed")
            onions = [_raw(o) for o in checked["onions"]]
            entries += onions
            parts.append(
                (
                    inst,
                    [dc.entry_hash(o) for o in onions],
                    [(u, _raw(c)) for u, c in checked["commits"]],
                    _raw(checked["r_commit"]),
                )
            )
        return entries, dc.transcript_batches(rnd.t("keys"), parts)

    async def _on_list(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "seeds" not in cur:
            raise DrawWireError("no onions of ours in this round")
        if cur.get("void_answer") and "S" not in cur:
            raise DrawWireError("a list after a stuck mix")
        S, batches, links, t_batches, t_chain = await asyncio.to_thread(
            check_list, rnd, p
        )
        mine = batches[self._own]
        sent = cur["onions_msg"]
        if mine["commits"] != sent["commits"] or mine["r_commit"] != sent["r_commit"]:
            raise DrawWireError("our commitments changed")
        if sorted(mine["hashes"]) != sorted(
            _b64(dc.entry_hash(_raw(o))) for o in sent["onions"]
        ):
            raise DrawWireError("our onions changed")
        if "S" in cur and cur["S"] != [_b64(s) for s in S]:
            raise DrawWireError("a second, different list")
        cur["S"] = [_b64(s) for s in S]
        cur["batches"] = batches
        cur["links"] = [[_b64(x) for x in link] for link in links]
        cur["t_batches"] = _b64(t_batches)
        cur["t_chain"] = _b64(t_chain)
        if "answer" in cur:
            await self._save(table.session_id, st)
            step, msg = cur["answer"]
            await self._send(table, table.hub_instance, step, msg)
            return
        if not all(dc.is_usable_key(s) for s in S):
            # The round broke publicly: open every seed — the openings
            # show who broke it.
            answer = (STEP_OPEN, self._opening(rnd, cur, t_chain))
            cur["void_answer"] = list(answer)
        else:
            # Decided from the list alone, before any household's r is out:
            # a missing key is told INSTEAD of our r, never after it.
            missing = next(
                (u for u in sorted(cur["seeds"]) if self._box_pub(cur, u) not in S),
                None,
            )
            if missing is not None:
                answer = (
                    STEP_FAIL,
                    self._complaint(rnd, cur, COMPLAINT_MISSING, missing),
                )
            else:
                answer = (STEP_REVEAL, self._rid(rnd, r=cur["r"]))
        cur["answer"] = list(answer)
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, answer[0], answer[1])

    async def _on_void(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        """The hub says a mixer's signed input doesn't peel: checked on our
        own copy (before the list, never once a card could exist), then we
        open our seeds. A finished round is never opened."""
        cur = st["p"]
        if "seeds" not in cur:
            raise DrawWireError("no seeds of ours in this round")
        if st["last"]["complete"] or "S" in cur or "done_msg" in cur:
            raise DrawWireError("a void after the list")
        if cur.get("void_answer"):
            step, msg = cur["void_answer"]
            await self._send(table, table.hub_instance, step, msg)
            return
        if p.get("why") != VOID_STUCK:
            raise DrawWireError("void reason malformed")
        k = _int(p.get("k"), 0, rnd.h - 1, "k")
        batches, first, t_batches = _check_batches(rnd, p.get("batches"))
        await asyncio.to_thread(
            lambda: _check_chain(
                rnd, p.get("chain"), first, length=k, t_batches=t_batches
            )
        )
        sent = cur.get("onions_msg") or {}
        if sorted(batches[self._own]["hashes"]) != sorted(
            _b64(dc.entry_hash(_raw(o))) for o in sent.get("onions", [])
        ):
            raise DrawWireError("our onions changed")
        claim = p.get("claim")
        if (
            not isinstance(claim, dict)
            or claim.get("stuck") != k
            or not _opening_signed(rnd, rnd.order[k], claim, t_batches)
        ):
            raise DrawWireError("no stuck mixer's word")
        opening = self._opening(rnd, cur, t_batches)
        cur["void_answer"] = [STEP_OPEN, opening]
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_OPEN, opening)

    def _complaint(
        self, rnd: _Round, cur: dict, kind: str, user: str, partner: str | None = None
    ) -> dict:
        """Our signed complaint over our transcript: a missing key up to the
        chain, any other up to the reveals."""
        seed = _raw(cur["seeds"][user])
        t = _raw(cur["t_chain" if kind == COMPLAINT_MISSING else "t_reveals"])
        pair = (partner, _raw(cur["seeds"][partner])) if partner else None
        sig = self._sign(
            dc.complaint_message(t, self._own, kind, user, seed, pair=pair)
        )
        complaint = {
            "kind": kind,
            "instance": self._own,
            "user": user,
            "seed": cur["seeds"][user],
            "sig": sig,
        }
        if partner:
            complaint.update(partner=partner, partner_seed=cur["seeds"][partner])
        cur["complained"] = complaint
        log.info("app_draw: round %s/%s stopped (%s)", rnd.gen, rnd.round, kind)
        return self._rid(rnd, complaint=complaint)

    async def _on_reveals(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "S" not in cur or cur.get("complained") or cur.get("void_answer"):
            raise DrawWireError("no list of ours in this round")
        reveals = parse_reveals(rnd, p.get("reveals"), cur["batches"])
        if reveals[self._own] != _raw(cur["r"]):
            raise DrawWireError("not our r")
        if "commit_msg" not in cur:
            S = [_raw(s) for s in cur["S"]]
            R = dc.round_randomness(rnd.ctx, reveals)
            C = dc.circle(S, ctx=rnd.ctx, R=R)
            t_reveals = dc.transcript_reveals(
                rnd.t("chain"), reveals, dc.circle_digest(C)
            )
            cur["t_reveals"] = _b64(t_reveals)
            cur["C"] = [_b64(x) for x in C]
            couple = self._own_couple(rnd, cur, C)
            if couple is not None:
                # Two of our people the pairs exclude sit side by side: told
                # now, before any card exists (both seeds are the proof).
                msg = self._complaint(rnd, cur, COMPLAINT_COUPLE, *couple)
                await self._save(table.session_id, st)
                await self._send(table, table.hub_instance, STEP_REDO, msg)
                return
            cards = await self._make_cards(rnd, cur, C, st.get("wishes", {}))
            hashes = [dc.sha256(c) for c in cards]
            sig = self._sign(dc.card_commit_message(t_reveals, self._own, hashes))
            cur["cards_msg"] = self._rid(rnd, cards=[_b64(c) for c in cards])
            cur["commit_msg"] = self._rid(
                rnd, hashes=[_b64(x) for x in hashes], sig=sig
            )
            await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_COMMIT, cur["commit_msg"])

    def _own_couple(
        self, rnd: _Round, cur: dict, C: list[bytes]
    ) -> tuple[str, str] | None:
        """Two of our people the pairs exclude, the first giving to the
        second in circle *C* — else ``None``."""
        mine = {self._box_pub(cur, u): u for u in cur["seeds"]}
        for user in sorted(cur["seeds"]):
            nxt = mine.get(C[(C.index(self._box_pub(cur, user)) + 1) % rnd.n])
            if nxt is None:
                continue
            a, b = rnd.seat_of(self._own, user), rnd.seat_of(self._own, nxt)
            if a and b and rnd.settings.excludes(a, b):
                return user, nxt
        return None

    async def _card_for(self, rnd: _Round, user: str, *, wish: str, rev: int) -> bytes:
        """This household's signed card for its person *user*."""
        local = await self._user_repo.get(user)
        name = dc.cut_utf8(
            (local.display_name if local is not None else None)
            or rnd.names.get((self._own, user))
            or user,
            dc.CARD_LABEL_MAX_BYTES,
        )
        household = dc.cut_utf8(self._household_label, dc.CARD_LABEL_MAX_BYTES)
        card = dc.Card(
            instance=self._own,
            user=user,
            name=name,
            household=household,
            wish=wish,
            rev=rev,
        )
        while not dc.card_fits(card):  # set_wish checked it; a renamed person
            card = dc.Card(
                instance=card.instance,
                user=card.user,
                name=card.name,
                household=card.household,
                wish=dc.cut_utf8(card.wish, len(card.wish.encode()) - 16),
                rev=rev,
            )
        sig = sign_ed25519(
            self._federation.own_identity_seed, dc.card_message(rnd.ctx, card)
        )
        return dc.encode_card(
            dc.Card(
                instance=card.instance,
                user=card.user,
                name=card.name,
                household=card.household,
                wish=card.wish,
                rev=card.rev,
                sig=sig,
            )
        )

    async def _make_cards(
        self, rnd: _Round, cur: dict, C: list[bytes], wishes: dict[str, str]
    ) -> list[bytes]:
        plain: list[tuple[bytes, bytes]] = []
        for user in sorted(cur["seeds"]):
            j = C.index(self._box_pub(cur, user))
            _gives, sealed_to = dc.circle_targets(j, rnd.n)
            card = await self._card_for(rnd, user, wish=wishes.get(user, ""), rev=0)
            plain.append((C[sealed_to], card))
        sealed = await asyncio.to_thread(
            lambda: [
                dc.seal_card(to, c, ctx=rnd.ctx, rand=self._rand) for to, c in plain
            ]
        )
        return dc.shuffle(sealed, self._rand)

    async def _on_commits(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "commit_msg" not in cur or cur.get("void_answer"):
            raise DrawWireError("no commitment of ours in this round")
        committed = parse_commits(rnd, p.get("commits"))
        if committed[self._own] != sorted(cur["commit_msg"]["hashes"]):
            raise DrawWireError("not our commitment")
        all_hashes = [x for hs in committed.values() for x in hs]
        if len(set(all_hashes)) != len(all_hashes):
            raise DrawWireError("card commitments repeat")
        cur["commits_cards"] = committed
        cur["commits_raw"] = p["commits"]
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_CARDS, cur["cards_msg"])

    async def _on_cards_all(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "commits_cards" not in cur or cur.get("void_answer"):
            raise DrawWireError("no card commitments here")
        cards = _raw_list(p.get("cards"), rnd.n, dc.SEALED_CARD_BYTES, "cards")
        if not check_card_list(cards, cur["commits_cards"], rnd.n):
            # Not the cards every household committed to: a relay's doing —
            # nothing is opened, nobody is blamed for it.
            raise DrawWireError("cards aren't the committed ones")
        cur["cards"] = [_b64(c) for c in cards]
        if cur.get("complained") is not None:
            await self._save(table.session_id, st)
            return
        if "done_msg" in cur:
            await self._save(table.session_id, st)
            await self._send(table, table.hub_instance, STEP_DONE, cur["done_msg"])
            return
        C = [_raw(x) for x in cur["C"]]
        results: dict[str, dict] = {}
        for user in sorted(cur["seeds"]):
            box_priv = self._box_priv(cur, user)
            opened = await asyncio.to_thread(dc.try_cards, box_priv, cards, ctx=rnd.ctx)
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            # The same test anyone checking our complaint runs.
            card = (
                usable_card(rnd, opened[0], giver=giver) if len(opened) == 1 else None
            )
            if card is None:
                msg = self._complaint(rnd, cur, COMPLAINT_CARDS, user)
                await self._save(table.session_id, st)
                await self._send(table, table.hub_instance, STEP_FAIL, msg)
                return
            to_seat = rnd.seat_of(card.instance, card.user) or 0
            if rnd.settings.excludes(giver.seat, to_seat):
                msg = self._complaint(rnd, cur, COMPLAINT_PARTNER, user)
                await self._save(table.session_id, st)
                await self._send(table, table.hub_instance, STEP_REDO, msg)
                return
            j = C.index(dc.public_of(box_priv))
            gives_to, _sealed_to = dc.circle_targets(j, rnd.n)
            results[user] = {
                "card": _b64(opened[0]),
                "rbox": _b64(C[gives_to]),
                "rev": 0,
                "via": None,
                "extra": [],
            }
        cur["results"] = results
        count = len(results)
        cur["done_msg"] = self._rid(
            rnd,
            count=count,
            sig=self._sign(dc.done_message(_raw(cur["t_reveals"]), self._own, count)),
        )
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_DONE, cur["done_msg"])

    async def _on_complete(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "results" not in cur or st["last"]["complete"]:
            return
        if cur.get("void_answer") or cur.get("complained"):
            raise DrawWireError("a complete for a round we opened or stopped")
        dones = p.get("dones")
        if not isinstance(dones, dict) or set(dones) != set(rnd.order):
            raise DrawWireError("complete without every household's done")
        for inst in rnd.order:
            d = dones[inst]
            if not isinstance(d, dict) or d.get("count") != len(rnd.of(inst)):
                raise DrawWireError("done malformed")
            sig = _raw(d.get("sig"), dc.SIG_BYTES, "sig")
            if not verify_ed25519(
                rnd.identity(inst),
                dc.done_message(rnd.t("reveals"), inst, d["count"]),
                sig,
            ):
                raise DrawWireError("done signature")
        st["last"]["complete"] = True
        st["final"] = {"gen": rnd.gen, "round": rnd.round, "results": cur["results"]}
        await self._save(table.session_id, st)
        for user in cur["results"]:
            await self._tell(
                table, user, "again" if rnd.gen > 1 else "ready", gen=rnd.gen
            )
        await self._push(table, st)
        # Someone of ours who left during the round hands over now.
        await self._hand_over_leavers(table, st, _seated(table))

    async def _on_end(self, table: GroupSession, st: dict, p: dict) -> None:
        """The hub says the draw gave up. Taken only when this household's
        own copy shows it (never after the names were revealed); else kept
        until the table here shows it, for a while."""
        reason = p.get("reason")
        if reason not in DRAW_END_REASONS:
            raise DrawWireError("reason malformed")
        last, cur = st.get("last"), st.get("p")
        if last is None or cur is None:
            raise DrawWireError("no draw to end")
        if last["complete"]:
            raise DrawWireError("an end after the reveal")
        if st.get("ended"):
            return
        culprits = await self._end_holds(table, st, _Round(cur), str(reason), p)
        if culprits is None:
            kept = st.get("pending_end") or {}
            at = kept.get("at") if kept.get("p") == p else None
            st["pending_end"] = {"p": p, "at": at or _now().isoformat()}
            await self._save(table.session_id, st)
            return
        blamed = st.setdefault("blamed", [])
        blamed += [i for i in culprits if i not in blamed]
        st["ended"] = reason
        st.pop("pending_end", None)
        await self._save(table.session_id, st)
        await self._push(table, st)

    # ─── After the reveal: hand-overs and wish-list updates ──────────

    async def _end_holds(
        self, table: GroupSession, st: dict, rnd: _Round, reason: str, p: dict
    ) -> list[str] | None:
        """Whether the end holds as this household sees the table: fewer
        than three of the round's people seated (``too_few``); pairs that no
        longer fit those still seated, or the last round broken with its
        proof (``rules``). ``None``: not (yet) — keep it. Else who broke
        the last round."""
        seated = _seated(table)
        still = [x for x in rnd.people if _person_key(x) in seated]
        if reason == END_TOO_FEW:
            return [] if len(still) < DRAW_MIN_PEOPLE else None
        if reason == END_BROKEN:
            # Card redraws used up, and one more card complaint that holds
            # on our own copy.
            if not _card_redraw(p.get("because")) or (
                st.get("card_redraws", 0) < DRAW_CARD_REDRAWS_MAX
            ):
                return None
            return await self._justify(
                table, st, rnd.gen, rnd.round + 1, rnd.people, p.get("because")
            )
        seats = frozenset(x.seat for x in still)
        if len(still) < len(rnd.people) and not pairs_fit(
            rnd.settings.only(seats).pairs, seats
        ):
            return []
        if _partner_redraw(p.get("because")) and (
            st.get("partner_redraws", 0) >= DRAW_PARTNER_REDRAWS_MAX
        ):
            # The pairs' redraws used up, and one more that holds here.
            return await self._justify(
                table, st, rnd.gen, rnd.round + 1, rnd.people, p.get("because")
            )
        if rnd.round < DRAW_ROUNDS_MAX or "because" not in p:
            return None
        people = (
            _parse_people(p.get("people"))[0]
            if p.get("people") is not None
            else rnd.people
        )
        return await self._justify(
            table, st, rnd.gen, rnd.round + 1, people, p.get("because")
        )

    def _final_of(self, st: dict, rnd: _Round) -> dict | None:
        final = st.get("final")
        if final is None or (final["gen"], final["round"]) != (rnd.gen, rnd.round):
            return None
        return final

    async def _on_handover(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        """A hand-over passed on by the hub: kept with the round's others
        (at most a few per person, for :data:`DRAW_PENDING_SECONDS`) and
        tried with every box key we hold — one we take may open the next."""
        final = self._final_of(st, rnd)
        if final is None:
            return
        sealed = _raw(p.get("sealed"), dc.SEALED_HANDOVER_BYTES, "sealed")
        seen = final.setdefault("handovers", [])
        if p["sealed"] not in [x["sealed"] for x in seen]:
            if len(seen) >= 2 * DRAW_MAX_PEOPLE:
                raise DrawWireError("too many hand-overs")
            seen.append({"sealed": _b64(sealed), "at": _now().isoformat()})
        await self._take_handovers(table, st, rnd)

    async def _take_handovers(self, table: GroupSession, st: dict, rnd: _Round) -> None:
        """Every hand-over kept that opens with a box key we hold now, until
        none does (a chain of leavers, in any order); each is taken once."""
        final = st["final"]
        seen = final.get("handovers", [])
        limit = _now() - timedelta(seconds=DRAW_PENDING_SECONDS)
        seen[:] = [x for x in seen if datetime.fromisoformat(x["at"]) >= limit]
        progress = True
        while progress:
            progress = False
            for item in list(seen):
                try:
                    took = await self._take_handover(
                        table, st, rnd, _raw(item["sealed"])
                    )
                except (DrawWireError, dc.DrawCryptoError, ValueError) as exc:
                    log.info("app_draw: a hand-over dropped: %s", exc)
                    took = True  # opened but wrong: never again
                if took:
                    seen.remove(item)
                    progress = True
        await self._save(table.session_id, st)

    async def _take_handover(
        self, table: GroupSession, st: dict, rnd: _Round, sealed: bytes
    ) -> bool:
        """``True`` when the hand-over opened here and was taken (or was
        refused — :class:`DrawWireError`); ``False`` when no box key of ours
        opens it, or the leaver still shows seated here."""
        final = st["final"]
        cur = st["p"]
        for user, res in final["results"].items():
            if res.get("left"):
                continue
            keys = [self._box_priv(cur, user)] + [_raw(x) for x in res.get("extra", [])]
            opened = next(
                (o for k in keys if (o := dc.open_handover(k, sealed, ctx=rnd.ctx))),
                None,
            )
            if opened is None:
                continue
            inst, leaver, chain, sig = dc.decode_handover(opened)
            pubs = [dc.public_of(k) for k in chain]
            if inst not in rnd.identities or rnd.seat_of(inst, leaver) is None:
                raise DrawWireError("hand-over from someone not in the draw")
            if not verify_ed25519(
                rnd.identity(inst),
                dc.handover_message(rnd.ctx, inst, leaver, pubs),
                sig,
            ):
                raise DrawWireError("hand-over signature")
            to_now = dc.decode_card(_raw(res["card"]))
            if (to_now.instance, to_now.user) != (inst, leaver):
                raise DrawWireError("a hand-over from someone we don't give to")
            # The leaver's own box must be exactly the one we give to; every
            # further key the one after it in the circle (handed to them).
            if pubs[0] != _raw(res["rbox"]):
                raise DrawWireError("hand-over with another box")
            C = [_raw(x) for x in cur["C"]]
            for a, b in itertools.pairwise(pubs):
                if a not in C or b != C[(C.index(a) + 1) % rnd.n]:
                    raise DrawWireError("hand-over keys aren't a chain")
            gone = table.entry(inst, leaver)
            if gone is not None and gone.state == SEAT_SEATED:
                # Our copy still seats the leaver (the hub's roster is on
                # its way): keep it until it shows them gone.
                return False
            cards = [_raw(c) for c in cur["cards"]]
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            # Every key but the last was handed on by someone who left: the
            # card sealed to it names the next one in the chain, and our
            # copy must show them gone too (no one still seated is skipped).
            for key in chain[:-1]:
                between = dc.try_cards(key, cards, ctx=rnd.ctx)
                was = (
                    usable_card(rnd, between[0], giver=giver)
                    if len(between) == 1
                    else None
                )
                if was is None:
                    raise DrawWireError("hand-over chain card")
                there = table.entry(was.instance, was.user)
                if there is not None and there.state == SEAT_SEATED:
                    return False  # kept: they show seated here (for now)
            # The new card is the one sealed to the last key of the chain in
            # the round's committed list we hold — byte for byte.
            new = dc.try_cards(chain[-1], cards, ctx=rnd.ctx)
            card = usable_card(rnd, new[0], giver=giver) if len(new) == 1 else None
            if card is None:
                raise DrawWireError("hand-over card")
            gives_to = C[(C.index(pubs[-1]) + 1) % rnd.n]
            res.update(
                {
                    "card": _b64(new[0]),
                    "rbox": _b64(gives_to),
                    "rev": 0,
                    "via": [inst, leaver],
                    "extra": [*res.get("extra", []), *(_b64(k) for k in chain)],
                }
            )
            now_at = table.entry(card.instance, card.user)
            if now_at is None or now_at.state != SEAT_SEATED:
                # They left too: their own hand-over is sealed to a key we
                # hold now — it is tried next.
                log.info("app_draw: a hand-over's new recipient left as well")
            await self._save(table.session_id, st)
            await self._tell(table, user, "handover", gen=rnd.gen)
            await self._push(table, st)
            return True
        return False

    async def _on_wish(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        final = self._final_of(st, rnd)
        if final is None:
            return
        sealed = _raw(p.get("sealed"), dc.SEALED_CARD_BYTES, "sealed")
        cur = st["p"]
        for user, res in final["results"].items():
            if res.get("left"):
                continue
            keys = [self._box_priv(cur, user)] + [_raw(x) for x in res.get("extra", [])]
            opened = next(
                (o for k in keys if (o := dc.open_card(k, sealed, ctx=rnd.ctx))), None
            )
            if opened is None:
                continue
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            card = check_card(rnd, opened, giver=giver)
            if card is None:
                raise DrawWireError("wish card")
            to_now = dc.decode_card(_raw(res["card"]))
            if (to_now.instance, to_now.user) != (card.instance, card.user) or (
                card.rev <= res["rev"]
            ):
                return
            res.update({"card": _b64(opened), "rev": card.rev})
            await self._save(table.session_id, st)
            await self._tell(table, user, "wish", gen=rnd.gen)
            await self._push(table, st)
            return

    async def _send_handover(
        self, table: GroupSession, rnd: _Round, cur: dict, user: str, res: dict
    ) -> None:
        """Our leaver's box key — and every key handed to them, so their
        giver gets whom they give to now — sealed to their giver."""
        C = [_raw(x) for x in cur["C"]]
        own = self._box_priv(cur, user)
        chain = [own] + [_raw(x) for x in res.get("extra", [])]
        j = C.index(dc.public_of(own))
        _gives, giver_pos = dc.circle_targets(j, rnd.n)
        sig = sign_ed25519(
            self._federation.own_identity_seed,
            dc.handover_message(
                rnd.ctx, self._own, user, [dc.public_of(k) for k in chain]
            ),
        )
        plain = dc.encode_handover(self._own, user, chain, sig)
        sealed = dc.seal_handover(C[giver_pos], plain, ctx=rnd.ctx, rand=self._rand)
        await self._send(
            table,
            table.hub_instance,
            STEP_HANDOVER,
            self._rid(rnd, sealed=_b64(sealed)),
        )

    # ─── Table changes (from AppGroupService via the registry) ───────

    def table_changed(self, table: GroupSession) -> None:
        """The table's roster or state changed here: re-check the draw in a
        task of its own (never under the caller's lock)."""
        self._spawn(self._table_changed(table.session_id))

    def person_left(self, table: GroupSession, username: str) -> None:
        """*username* of this household left the table (or was removed)."""
        self._spawn(self._table_changed(table.session_id, gone=username))

    async def _table_changed(self, session_id: str, *, gone: str | None = None) -> None:
        try:
            async with self._lock(session_id):
                table = await self._repo.get(session_id)
                if table is None:
                    return
                st = await self._load(session_id)
                seated = {k for k in _seated(table) if k != (self._own, gone)}
                for key in ("pending_start", "pending_end"):
                    if st.get(key) is not None:
                        await self._retry_pending(table, st, key)
                        st = await self._load(session_id)
                if not st.get("hub") and not st.get("final"):
                    return
                await self._hand_over_leavers(table, st, seated)
                if st.get("final") and st.get("p") is not None:
                    rnd_p = _Round(st["p"])
                    if self._final_of(st, rnd_p) is not None:
                        await self._take_handovers(table, st, rnd_p)
                await self._hub_roster_change(table, st)
        except Exception:
            log.exception("app_draw: table change for %s", session_id)

    async def _retry_pending(self, table: GroupSession, st: dict, key: str) -> None:
        """A start or an end kept until this household's copy showed it:
        tried again — dropped when it fails or is older than
        :data:`DRAW_PENDING_SECONDS`."""
        item = st[key]
        try:
            at = datetime.fromisoformat(item["at"])
            if (_now() - at).total_seconds() > DRAW_PENDING_SECONDS:
                raise DrawWireError("kept too long")
            p = item["p"]
            gen = _int(p.get("gen"), 1, DRAW_AGAIN_MAX + 1, "gen")
            round_no = _int(p.get("round"), 1, DRAW_ROUNDS_MAX, "round")
            if key == "pending_start":
                await self._on_start(table, st, gen, round_no, p)
            else:
                cur = st.get("p")
                if cur is None or (cur["gen"], cur["round"]) != (gen, round_no):
                    raise DrawWireError("not the current round")
                await self._on_end(table, st, p)
        except (DrawWireError, dc.DrawCryptoError, KeyError, ValueError) as exc:
            log.info("app_draw: a waiting %s dropped: %s", key, exc)
            st.pop(key, None)
            await self._save(table.session_id, st)

    async def _hub_roster_change(self, table: GroupSession, st: dict) -> None:
        """At the hub: people who left before ``S`` start a fresh round
        without them. After ``S`` a leave starts nothing (the round ends,
        then the leaver hands over) — except a whole household that holds
        the round up and the organiser took out ("Draw without them"). After
        the reveal nothing changes (the leaver hands over; Draw again needs
        three seated)."""
        hub = st.get("hub")
        if hub is None or table.hub_instance != self._own:
            return
        if table.state == GROUP_ENDED and hub["phase"] != _PHASE_ENDED:
            hub["phase"] = _PHASE_ENDED
            hub["ended"] = END_TOO_FEW
            await self._save(table.session_id, st)
            return
        if hub["phase"] not in _RUNNING:
            return
        rnd = _Round(hub)
        seated = _seated(table)
        still = [x for x in rnd.people if _person_key(x) in seated]
        if len(still) == rnd.n:
            return
        if hub["phase"] in _AFTER_S or hub["phase"] == _PHASE_HELD:
            gone_homes = {x.instance for x in rnd.people} - {x.instance for x in still}
            stuck = set(self._waiting(hub, rnd))
            if not gone_homes or not gone_homes <= stuck:
                return
            if any(x.instance in gone_homes for x in still):
                return
        again = hub["phase"] in _AFTER_CIRCLE
        if again and (hub["gen"] > DRAW_AGAIN_MAX or not _before_the_day(hub)):
            log.warning(
                "app_draw: %s — no Draw again left after a removal", table.session_id
            )
            return
        await self._next_round(
            table,
            st,
            people=still,
            names=rnd.names,
            because={"kind": BECAUSE_CHANGED},
            again=again,
        )

    async def _hand_over_leavers(
        self, table: GroupSession, st: dict, seated: set
    ) -> None:
        """Our people who left after the reveal hand their box keys to their
        giver (design 5.6) — once each."""
        final = st.get("final")
        cur = st.get("p")
        if (
            final is None
            or cur is None
            or (cur["gen"], cur["round"]) != (final["gen"], final["round"])
        ):
            return
        rnd = _Round(cur)
        for user, res in final["results"].items():
            if res.get("handed"):
                continue
            if (self._own, user) in seated:
                # Our copy may lag (the hub tells only households with
                # someone still seated): their own place here decides.
                local = await self._user_repo.get(user)
                if local is not None and await self._groups.seat_row(
                    table, local.user_id
                ):
                    continue
            res["left"] = True
            res["handed"] = True
            await self._save(table.session_id, st)
            await self._send_handover(table, rnd, cur, user, res)

    # ─── People and the app ───────────────────────────────────────────

    async def _tell(
        self, table: GroupSession, username: str, change: str, *, gen: int
    ) -> None:
        user = await self._user_repo.get(username)
        if user is None:
            return
        await self._emit(
            AppDrawChanged(
                app_id=table.app_id,
                session_id=table.session_id,
                to_user_id=user.user_id,
                change=change,
                gen=gen,
            )
        )

    async def _push(self, table: GroupSession, st: dict) -> None:
        """Each local person at the table hears the draw's state (never a
        result) on the ``app.engine`` frame."""
        for user, _row in await self._groups.local_people(table):
            await self._ws.broadcast_to_user(
                user.user_id,
                {
                    "type": WS_FRAME_TYPE,
                    "engine": DRAW_ENGINE_ID,
                    "app_id": table.app_id,
                    "session_id": table.session_id,
                    "state": self._view(table, st, user),
                },
            )

    @staticmethod
    def _waiting(hub: dict, rnd: _Round) -> list[str]:
        """The households the round waits for now."""
        phase = hub["phase"]
        have: set[str] | dict
        if phase == _PHASE_MIXING:
            return [rnd.order[hub["k"]]]
        if phase == _PHASE_HELD:
            return list(hub["held"])
        have = {
            _PHASE_KEYS: hub["keys"],
            _PHASE_ONIONS: hub["batches"],
            _PHASE_REVEAL: hub["reveals"],
            _PHASE_COMMIT: hub["commits"],
            _PHASE_CARDS: hub["cards"],
            _PHASE_OPENING: hub["done"],
            _PHASE_VOID: hub["opens"],
        }.get(phase, rnd.order)
        return [i for i in rnd.order if i not in have]

    def _view(self, table: GroupSession, st: dict, user: User) -> dict:
        """What the app may know: the state, the round, progress, the
        settings, the person's own wish list — never a result. ``blamed``
        names only proven misbehaviour: households this household's own
        checks named (shown to everyone), and for the organiser also those
        that sent the hub a signed complaint that didn't hold."""
        hub = st.get("hub") if table.hub_instance == self._own else None
        last = st.get("last")
        final = st.get("final")
        ended = (hub or {}).get("ended") or st.get("ended")
        if ended:
            state = STATE_FAILED
        elif final is not None and user.username in final["results"]:
            state = STATE_READY
        elif last is not None and not last["complete"]:
            state = STATE_DRAWING
        elif last is not None:
            state = STATE_READY
        else:
            state = STATE_GATHERING
        is_host = table.is_host(self._own, user.username)
        frame = hub or st.get("p")
        mine = (final or {}).get("results", {}).get(user.username)
        out: dict[str, Any] = {
            "state": state,
            "reason": ended,
            "gen": (frame or {}).get("gen", 0),
            "again": max(0, (frame or {}).get("gen", 1) - 1),
            "round": (frame or {}).get("round", 0),
            "of": len((frame or {}).get("people", [])),
            "done": None,
            "settings": (frame or {}).get("settings") or st.get("settings"),
            "wish": st.get("wishes", {}).get(user.username, ""),
            "has_result": mine is not None and not mine.get("left"),
            "is_host": is_host,
            "waiting_for": None,
            "blamed": None,
        }
        blamed = list(st.get("blamed", []))
        if hub is not None and is_host:
            rnd = _Round(hub)
            waiting = self._waiting(hub, rnd) if hub["phase"] in _RUNNING else []
            out["done"] = (
                sum(len(rnd.of(i)) for i in rnd.order if i not in waiting)
                if hub["phase"] != _PHASE_MIXING
                else sum(len(rnd.of(i)) for i in rnd.order[: hub["k"]])
            )
            since = datetime.fromisoformat(hub["since"])
            if waiting and (
                hub["phase"] == _PHASE_HELD
                or (_now() - since).total_seconds() >= DRAW_WAIT_SECONDS
            ):
                out["waiting_for"] = self._seats_of(rnd, waiting)
            blamed += [i for i in hub["blamed"] if i not in blamed]
        if frame is not None and blamed:
            out["blamed"] = self._seats_of(_Round(frame), blamed) or None
        return out

    @staticmethod
    def _seats_of(rnd: _Round, instances: list[str]) -> list[dict]:
        return [{"seats": [p.seat for p in rnd.of(i)]} for i in instances if rnd.of(i)]

    # ─── Route calls (through the registry) ───────────────────────────

    async def call(self, call: EngineCall) -> dict:
        """``GET …/engines/draw`` (status), ``POST …/configure``,
        ``POST …/wish``, ``POST …/start``, ``GET …/result`` (Social Home's
        own sheet only — the bridge has no call for it)."""
        ids = {
            "app_id": call.app_id,
            "session_id": call.session_id,
            "actor_user_id": call.actor_user_id,
        }
        match (call.action, call.method):
            case (None, "GET"):
                return {"state": await self.status(**ids)}
            case ("configure", "POST"):
                return {"state": await self.configure(**ids, settings=call.body)}
            case ("wish", "POST"):
                text = call.body.get("text")
                if not isinstance(text, str):
                    raise AppDrawSettingsError()
                return {"state": await self.set_wish(**ids, text=text)}
            case ("start", "POST"):
                again = call.body.get("again", False)
                if not isinstance(again, bool):
                    raise AppDrawSettingsError()
                return {"state": await self.start(**ids, again=again)}
            case ("result", "GET"):
                return {"result": await self.result(**ids)}
            case _:
                raise AppEngineActionError()

    async def status(self, *, app_id: str, session_id: str, actor_user_id: str) -> dict:
        table, user = await self._groups.seat_of_caller(
            app_id, session_id, actor_user_id
        )
        return self._view(table, await self._load(session_id), user)

    async def configure(
        self, *, app_id: str, session_id: str, actor_user_id: str, settings: dict
    ) -> dict:
        """The organiser's settings — before the first draw (or after one
        gave up). A **patch**: only the fields given change (``name``,
        ``budget``, ``day``, ``pairs``); a field given as ``null`` clears it."""
        table, user = await self._host(app_id, session_id, actor_user_id)
        if not isinstance(settings, dict):
            raise AppDrawSettingsError()
        async with self._lock(session_id):
            st = await self._load(session_id)
            hub = st.get("hub")
            if hub is not None and hub["phase"] in _RUNNING:
                raise AppDrawStateError()
            merged = dict(st.get("settings") or {})
            for field in ("name", "budget", "day", "pairs"):
                if field in settings:
                    merged[field] = settings[field]
            if merged.get("pairs") is None:
                merged["pairs"] = []
            try:
                clean = DrawSettings.from_wire(merged)
            except DrawWireError as exc:
                raise AppDrawSettingsError() from exc
            seats = frozenset(e.seat for e in table.roster if e.seat)
            if any(a not in seats or b not in seats for a, b in clean.pairs):
                raise AppDrawPairsError()
            st["settings"] = clean.to_wire()
            await self._save(session_id, st)
        await self._push(table, st)
        return self._view(table, st, user)

    async def set_wish(
        self, *, app_id: str, session_id: str, actor_user_id: str, text: object
    ) -> dict:
        """The caller's wish list. After the reveal it reaches their giver
        (at most :data:`DRAW_WISH_UPDATES_MAX` times)."""
        table, user = await self._groups.seat_of_caller(
            app_id, session_id, actor_user_id
        )
        clean = wish_ok(text)
        probe = dc.Card(
            instance=self._own,
            user=user.username,
            name=dc.cut_utf8(
                user.display_name or user.username, dc.CARD_LABEL_MAX_BYTES
            ),
            household=dc.cut_utf8(self._household_label, dc.CARD_LABEL_MAX_BYTES),
            wish=clean,
            rev=0,
            sig=b"\x00" * dc.SIG_BYTES,
        )
        if not dc.card_fits(probe):
            raise AppDrawWishTooLongError()
        async with self._lock(session_id):
            st = await self._load(session_id)
            wishes = st.setdefault("wishes", {})
            updates = st.setdefault("wish_updates", {})
            final = st.get("final")
            cur = st.get("p")
            revealed = final is not None and user.username in final["results"] and cur
            if revealed and updates.get(user.username, 0) >= DRAW_WISH_UPDATES_MAX:
                raise AppDrawWishUpdatesError()
            wishes[user.username] = clean
            if revealed:
                assert cur is not None
                rev = updates.get(user.username, 0) + 1
                updates[user.username] = rev
                rnd = _Round(cur)
                card = await self._card_for(rnd, user.username, wish=clean, rev=rev)
                C = [_raw(x) for x in cur["C"]]
                j = C.index(self._box_pub(cur, user.username))
                _g, giver_pos = dc.circle_targets(j, rnd.n)
                sealed = dc.seal_card(C[giver_pos], card, ctx=rnd.ctx, rand=self._rand)
                await self._save(session_id, st)
                await self._send(
                    table,
                    table.hub_instance,
                    STEP_WISH,
                    self._rid(rnd, sealed=_b64(sealed)),
                )
            else:
                await self._save(session_id, st)
        return self._view(table, st, user)

    async def start(
        self, *, app_id: str, session_id: str, actor_user_id: str, again: bool = False
    ) -> dict:
        """Draw names (organiser): ends late joins (an app that takes them
        takes them only until names are drawn), starts the table if it
        waits for players, then the first round — or, *again*, a new draw for
        everyone still in after a reveal (visible to all, with its count;
        only until the exchange day)."""
        table, user = await self._host(app_id, session_id, actor_user_id)
        if len(table.seated) < DRAW_MIN_PEOPLE:
            raise AppDrawTooFewError()
        if not again:
            # People can join (an app with late joiners) until names are
            # drawn: once the seats as they are could be drawn, nobody new
            # sits down — before the seats are read again below.
            await self._drawable(table)
            table = await self._groups.end_late_joins(session_id) or table
        if table.state == GROUP_LOBBY:
            await self._groups.start(
                app_id=app_id, session_id=session_id, actor_user_id=actor_user_id
            )
            table = await self._repo.get(session_id) or table
        if table.state == GROUP_ENDED:
            raise AppDrawStateError()
        seated = sorted(
            (e for e in table.roster if e.state == SEAT_SEATED), key=lambda e: e.seat
        )
        if len(seated) < DRAW_MIN_PEOPLE:
            raise AppDrawTooFewError()
        too_old = [
            e.household_name or e.instance
            for e in seated
            if not await self._supports(e.instance)
        ]
        if too_old:
            raise AppDrawHouseholdTooOldError(sorted(set(too_old)))
        people = [
            dc.Person(seat=e.seat, instance=e.instance, user=e.user) for e in seated
        ]
        names = {(e.instance, e.user): e.name for e in seated}
        async with self._lock(session_id):
            st = await self._load(session_id)
            settings = DrawSettings.from_wire(st.get("settings") or {})
            if not pairs_fit(settings.pairs, frozenset(p.seat for p in people)):
                raise AppDrawPairsError()
            hub = st.get("hub")
            if again:
                if hub is None or hub["phase"] != _PHASE_COMPLETE:
                    raise AppDrawStateError()
                if hub["gen"] > DRAW_AGAIN_MAX:
                    raise AppDrawAgainLimitError()
                if settings.day and date.fromisoformat(settings.day) < _now().date():
                    raise AppDrawPastDayError()
                because = {"kind": BECAUSE_AGAIN}
            else:
                if hub is not None and hub["phase"] != _PHASE_ENDED:
                    raise AppDrawStateError()
                # Afresh after a draw gave up counts as a Draw again (its
                # count, the exchange day) — every household checks so too.
                if hub is not None and hub["gen"] > DRAW_AGAIN_MAX:
                    raise AppDrawAgainLimitError()
                if hub is not None and not _before_the_day(hub):
                    raise AppDrawPastDayError()
                because = {"kind": BECAUSE_FIRST}
            await self._next_round(
                table,
                st,
                people=people,
                names=names,
                because=because,
                again=again or hub is not None,
            )
        return self._view(table, st, user)

    async def _drawable(self, table: GroupSession) -> None:
        """The checks :meth:`start` makes of the seats, made before late
        joins end — so a refused Draw names leaves the table open."""
        seated = [e for e in table.roster if e.state == SEAT_SEATED]
        too_old = [
            e.household_name or e.instance
            for e in seated
            if not await self._supports(e.instance)
        ]
        if too_old:
            raise AppDrawHouseholdTooOldError(sorted(set(too_old)))
        st = await self._load(table.session_id)
        settings = DrawSettings.from_wire(st.get("settings") or {})
        if not pairs_fit(settings.pairs, frozenset(e.seat for e in seated)):
            raise AppDrawPairsError()

    async def result(self, *, app_id: str, session_id: str, actor_user_id: str) -> dict:
        """Whom the caller gives to — for Social Home's own result sheet
        only (the app has no route to it: its frame can't reach the API,
        and the bridge has no call for it)."""
        _table, user = await self._groups.seat_of_caller(
            app_id, session_id, actor_user_id
        )
        st = await self._load(session_id)
        final = st.get("final")
        res = (final or {}).get("results", {}).get(user.username)
        if final is None or res is None or res.get("left"):
            raise AppDrawNoResultError()
        card = dc.decode_card(_raw(res["card"]))
        settings = DrawSettings.from_wire(st["p"]["settings"])
        household = (
            await self._app_fed.household_name(card.instance)
            if card.instance != self._own
            else self._household_label
        )
        via = res.get("via")
        return {
            "app_id": app_id,
            "session_id": session_id,
            "name": card.name,
            "household": household or card.household,
            "wish": card.wish,
            "wish_updated": card.rev > 0,
            "draw_name": settings.name,
            "budget": settings.budget,
            "day": settings.day,
            "again": max(0, final["gen"] - 1),
            "handed_over_by": _Round(st["p"]).names.get((via[0], via[1]))
            if via
            else None,
        }

    async def _host(
        self, app_id: str, session_id: str, actor_user_id: str
    ) -> tuple[GroupSession, User]:
        table, user = await self._groups.seat_of_caller(
            app_id, session_id, actor_user_id
        )
        if not table.is_host(self._own, user.username):
            raise AppDrawNotHostError()
        if table.hub_instance != self._own:
            raise AppDrawHostElsewhereError()
        return table, user


__all__ = [
    "DRAW_ENGINE_ID",
    "WS_FRAME_TYPE",
    "AppDrawService",
    "check_card",
    "check_card_list",
    "check_list",
    "complaint_holds",
    "complaint_verdict",
    "judge_void",
    "opened_seeds",
    "parse_commits",
    "parse_reveals",
    "usable_card",
]
