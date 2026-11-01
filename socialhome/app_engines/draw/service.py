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
    mixer → hub   mixed {k, list, in, sig}
    hub → all     list {S, batches, chain}              (the signed chain)
    each → hub    reveal {r}  | fail {missing, seed} | open {every seed}
    hub → all     reveals {r of every household}        → the circle
    each → hub    commit {card hashes, sig}             (before any card)
    hub → all     commits
    each → hub    cards {sealed cards}
    hub → all     cards_all                             (= the commitments)
    each → hub    done {count, sig} | fail / redo {complaint with seed}
    hub → all     complete {every signed done} | end {reason}
    after the reveal: handover / wish (each → hub → all)

Security invariants (see ``docs/protocol/app-engines.md``)
------------------------------------------------------------
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
* A hand-over carries the leaver's box key; the giver checks it against
  ``S`` and opens the new card from its own stored card list.
* Every secret is KEK-wrapped; the app gets the state only, never a result.
"""

from __future__ import annotations

import asyncio
import binascii
import logging
import os
from datetime import UTC, date, datetime
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
    COMPLAINT_KINDS,
    COMPLAINT_MISSING,
    COMPLAINT_PARTNER,
    DRAW_AGAIN_MAX,
    DRAW_MAX_PEOPLE,
    DRAW_MIN_PEOPLE,
    DRAW_ROUNDS_MAX,
    DRAW_STEPS,
    DRAW_TO_HUB_STEPS,
    DRAW_WAIT_SECONDS,
    DRAW_WISH_UPDATES_MAX,
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
    STEP_WISH,
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
    }
)
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


def check_list(rnd: _Round, body: dict) -> tuple[list[bytes], dict[str, dict]]:
    """A ``list`` body against its round: the n box keys ``S`` and every
    household's batch (``{hashes, commits, r_commit}``) — or
    :class:`DrawWireError`. Every batch is signed by its household; the
    chain starts at those onions, every link is signed by the mixer whose
    turn it was and takes exactly what the one before gave; the last gives
    ``S``."""
    S = _raw_list(body.get("S"), rnd.n, dc.KEY_BYTES, "S")
    batches = body.get("batches")
    if not isinstance(batches, list) or len(batches) != rnd.h:
        raise DrawWireError("batches wrong count")
    by_inst: dict[str, dict] = {}
    for b in batches:
        if not isinstance(b, dict) or b.get("instance") not in rnd.order:
            raise DrawWireError("batch malformed")
        by_inst[b["instance"]] = b
    if len(by_inst) != rnd.h:
        raise DrawWireError("batches repeat")
    hashes: list[bytes] = []
    out: dict[str, dict] = {}
    for inst in sorted(by_inst):
        b = by_inst[inst]
        mine = rnd.of(inst)
        hs = _raw_list(b.get("hashes"), len(mine), dc.DIGEST_BYTES, "hashes")
        commits = _commits(b.get("commits"), mine)
        r_commit = _raw(b.get("r_commit"), dc.DIGEST_BYTES, "r_commit")
        sig = _raw(b.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.batch_message(rnd.ctx, inst, hs, commits, r_commit)
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            raise DrawWireError("batch signature")
        hashes += hs
        out[inst] = {
            "hashes": [_b64(x) for x in hs],
            "commits": [[u, _b64(c)] for u, c in commits],
            "r_commit": _b64(r_commit),
        }
    chain = body.get("chain")
    if not isinstance(chain, list) or len(chain) != rnd.h:
        raise DrawWireError("chain wrong length")
    prev_out = dc.list_digest(hashes)
    for k, link in enumerate(chain):
        if not isinstance(link, dict) or link.get("instance") != rnd.order[k]:
            raise DrawWireError("chain out of order")
        d_in = _raw(link.get("in"), dc.DIGEST_BYTES, "in")
        d_out = _raw(link.get("out"), dc.DIGEST_BYTES, "out")
        sig = _raw(link.get("sig"), dc.SIG_BYTES, "sig")
        if d_in != prev_out:
            raise DrawWireError("chain broken")
        msg = dc.mix_message(rnd.ctx, k, rnd.order[k], d_in, d_out)
        if not verify_ed25519(rnd.identity(rnd.order[k]), msg, sig):
            raise DrawWireError("chain signature")
        prev_out = d_out
    if prev_out != dc.list_digest([dc.entry_hash(s) for s in S]):
        raise DrawWireError("chain doesn't end in S")
    return S, out


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


def check_card_list(
    cards: list[bytes], committed: dict[str, list[str]], n: int
) -> bool:
    """Whether *cards* are exactly the sealed cards every household
    committed to (by hash) — n of them, no other."""
    want = sorted(h for hs in committed.values() for h in hs)
    return len(cards) == n and sorted(_b64(dc.sha256(c)) for c in cards) == want


def complaint_holds(
    rnd: _Round,
    complaint: dict,
    *,
    S: list[bytes],
    batches: dict[str, dict],
    mixer_pubs: list[bytes],
    cards: list[bytes] | None,
) -> bool:
    """Whether *complaint* about round *rnd* is proven. Its proof is the
    person's seed: the commitment it derives must be the one the
    household signed for that person, and — for a missing key — the onion
    it rebuilds must be one the household submitted (so a household can't
    commit to one box and onion another). Then what it says must be true of
    ``S`` and the committed cards."""
    try:
        kind = complaint.get("kind")
        inst, user = complaint.get("instance"), complaint.get("user")
        if (
            kind not in COMPLAINT_KINDS
            or not isinstance(inst, str)
            or not isinstance(user, str)
        ):
            return False
        if inst not in rnd.identities or rnd.seat_of(inst, user) is None:
            return False
        seed = _raw(complaint.get("seed"), dc.SEED_BYTES, "seed")
        sig = _raw(complaint.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.complaint_message(rnd.ctx, inst, kind, user, seed)
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            return False
        keys = dc.person_keys(seed)
        c = _b64(dc.commitment(rnd.ctx, inst, user, keys.box_pub, keys.nonce))
        if [user, c] not in batches.get(inst, {}).get("commits", []):
            return False
        if kind == COMPLAINT_MISSING:
            onion = dc.onion_from_seed(seed, mixer_pubs, ctx=rnd.ctx)
            if _b64(dc.entry_hash(onion)) not in batches[inst]["hashes"]:
                return False  # the household onioned another box
            return S.count(keys.box_pub) != 1
        if S.count(keys.box_pub) != 1 or cards is None:
            return False
        opened = dc.try_cards(keys.box_priv, cards, ctx=rnd.ctx)
        giver = dc.Person(seat=rnd.seat_of(inst, user) or 0, instance=inst, user=user)
        card = check_card(rnd, opened[0], giver=giver) if len(opened) == 1 else None
        if kind == COMPLAINT_CARDS:
            return card is None
        if card is None:
            return False
        to_seat = rnd.seat_of(card.instance, card.user)
        return to_seat is not None and rnd.settings.excludes(giver.seat, to_seat)
    except DrawWireError, dc.DrawCryptoError, KeyError, ValueError:
        return False


def opening_holds(
    rnd: _Round,
    instance: str,
    opening: dict,
    *,
    batches: dict[str, dict],
    mixer_pubs: list[bytes],
) -> bool:
    """Whether a household's opening of a broken round rebuilds exactly
    the onions and commitments it signed (else it is the one that broke
    it)."""
    try:
        seeds_raw = opening.get("seeds")
        sig = _raw(opening.get("sig"), dc.SIG_BYTES, "sig")
        if not isinstance(seeds_raw, dict):
            return False
        seeds = [(u, _raw(s, dc.SEED_BYTES, "seed")) for u, s in seeds_raw.items()]
        if not verify_ed25519(
            rnd.identity(instance), dc.opening_message(rnd.ctx, instance, seeds), sig
        ):
            return False
        if sorted(u for u, _ in seeds) != sorted(p.user for p in rnd.of(instance)):
            return False
        batch = batches[instance]
        rebuilt = sorted(
            _b64(dc.entry_hash(dc.onion_from_seed(s, mixer_pubs, ctx=rnd.ctx)))
            for _u, s in seeds
        )
        commits = sorted(
            [u, _b64(dc.person_commitment(rnd.ctx, instance, u, s))] for u, s in seeds
        )
        return rebuilt == sorted(batch["hashes"]) and commits == sorted(
            batch["commits"]
        )
    except DrawWireError, dc.DrawCryptoError, KeyError, ValueError:
        return False


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
        hub["list"] = l0
        hub["in"] = _b64(dc.list_digest([dc.entry_hash(_raw(o)) for o in l0]))
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
            rnd.ctx, inst, [dc.entry_hash(o) for o in onions], commits, r_commit
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
        d_out = dc.list_digest([dc.entry_hash(o) for o in out])
        sig = _raw(p.get("sig"), dc.SIG_BYTES, "sig")
        if not verify_ed25519(
            rnd.identity(sender), dc.mix_message(rnd.ctx, k, sender, d_in, d_out), sig
        ):
            raise DrawWireError("mix signature")
        link = {
            "k": k,
            "instance": sender,
            "in": hub["in"],
            "out": _b64(d_out),
            "sig": _b64(sig),
        }
        hub["chain"].append(link)
        hub["list"] = [_b64(o) for o in out]
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
                    list=hub["list"],
                    prev={"instance": sender, "in": link["in"], "sig": link["sig"]},
                ),
            )
            return
        hub["S"] = hub.pop("list")
        hub["list_body"] = {
            "S": hub["S"],
            "batches": [
                {
                    "instance": i,
                    "hashes": [
                        _b64(dc.entry_hash(_raw(o)))
                        for o in hub["batches"][i]["onions"]
                    ],
                    "commits": hub["batches"][i]["commits"],
                    "r_commit": hub["batches"][i]["r_commit"],
                    "sig": hub["batches"][i]["sig"],
                }
                for i in sorted(hub["batches"])
            ],
            "chain": hub["chain"],
        }
        hub["batches"] = {}  # the onions are spent: keep the hashes only
        # A key in S nobody can seal to breaks the round publicly: every
        # household opens its seeds, which names the one that sent it.
        usable = all(dc.is_usable_key(_raw(s)) for s in hub["S"])
        hub["phase"] = _PHASE_REVEAL if usable else _PHASE_VOID
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
            rnd.identity(sender), dc.card_commit_message(rnd.ctx, sender, hashes), sig
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
            rnd.identity(sender), dc.done_message(rnd.ctx, sender, count), sig
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
    ) -> tuple[list[bytes], dict[str, dict], list[bytes], list[bytes] | None]:
        S = [_raw(s) for s in hub["S"]]
        batches = {b["instance"]: b for b in hub["list_body"]["batches"]}
        mixer_pubs = [_raw(x) for x in hub["mixer_pubs"]]
        cards = [_raw(c) for c in hub.get("all_cards", [])] or None
        return S, batches, mixer_pubs, cards

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
            (step == STEP_REDO) != (kind == COMPLAINT_PARTNER)
        ):
            raise DrawWireError("complaint malformed")
        if kind == COMPLAINT_MISSING:
            # Told INSTEAD of revealing, before any card exists.
            if hub["phase"] != _PHASE_REVEAL or sender in hub["reveals"]:
                raise DrawWireError("a missing key told too late")
        elif hub["phase"] != _PHASE_OPENING or sender in hub["done"]:
            raise DrawWireError("a card complaint out of turn")
        S, batches, mixer_pubs, cards = self._hub_round_data(hub)
        if not await asyncio.to_thread(
            complaint_holds,
            rnd,
            complaint,
            S=S,
            batches=batches,
            mixer_pubs=mixer_pubs,
            cards=cards,
        ):
            # Named to the organiser: "…'s home stopped the draw".
            if sender not in hub["blamed"]:
                hub["blamed"].append(sender)
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
        evidence: dict[str, Any] = {
            "kind": BECAUSE_COMPLAINT,
            "complaint": complaint,
            "list": hub["list_body"],
            "mixer_pubs": hub["mixer_pubs"],
        }
        if kind != COMPLAINT_MISSING:
            evidence["cards"] = hub.get("all_cards", [])
            evidence["commits"] = hub["commits"]
        await self._next_round(
            table, st, people=rnd.people, names=rnd.names, because=evidence
        )

    async def _hub_open(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        """A household's opening of a round whose ``S`` holds a key nobody
        can seal to. Once every household opened (or is named for an
        opening that doesn't rebuild), a fresh round follows."""
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_VOID)
        opening = p.get("opening")
        if not isinstance(opening, dict) or sender in hub["opens"]:
            return
        _S, batches, mixer_pubs, _c = self._hub_round_data(hub)
        good = await asyncio.to_thread(
            opening_holds, rnd, sender, opening, batches=batches, mixer_pubs=mixer_pubs
        )
        hub["opens"][sender] = opening
        if not good and sender not in hub["blamed"]:
            hub["blamed"].append(sender)
        if len(hub["opens"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        evidence = {"kind": BECAUSE_VOID, "list": hub["list_body"]}
        await self._next_round(
            table, st, people=rnd.people, names=rnd.names, because=evidence
        )

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
        after :data:`DRAW_ROUNDS_MAX` rounds."""
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
        if round_no > DRAW_ROUNDS_MAX or not pairs_fit(settings.pairs, seats):
            await self._hub_end(table, st, END_RULES)
            return
        identities = await self._identities(table, {p.instance for p in people})
        if identities is None:
            log.warning("app_draw: %s — a household's key is unknown", table.session_id)
            await self._hub_end(table, st, END_TOO_FEW)
            return
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
            # Who broke a round stays named to the organiser for the draw.
            "blamed": [] if (old is None or again) else list(old.get("blamed", [])),
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

    async def _hub_end(self, table: GroupSession, st: dict, reason: str) -> None:
        hub = st.get("hub")
        if hub is None:
            return
        hub["phase"] = _PHASE_ENDED
        hub["ended"] = reason
        await self._save(table.session_id, st)
        await self._to_all(table, _Round(hub), STEP_END, {"reason": reason})
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
        if not self._justify(table, st, gen, round_no, people, p.get("because")):
            # Our copy of the table doesn't show it yet (the hub's roster
            # is on its way): keep the start until it does.
            st["pending_start"] = p
            await self._save(table.session_id, st)
            return
        st.pop("pending_start", None)
        priv, pub = dc.new_keypair(self._rand)
        rnd = _Round(frame)
        key_msg = self._rid(
            rnd, pub=_b64(pub), sig=self._sign(dc.key_message(rnd.ctx, self._own, pub))
        )
        if cur is not None and gen != cur["gen"]:
            st.pop("final", None)  # a new draw: everyone gets a new name
        st["p"] = {**frame, "mixer_priv": _b64(priv), "key_msg": key_msg}
        st["last"] = {"gen": gen, "round": round_no, "complete": False}
        st.pop("ended", None)
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_KEY, key_msg)
        await self._push(table, st)

    def _justify(
        self,
        table: GroupSession,
        st: dict,
        gen: int,
        round_no: int,
        people: list[dc.Person],
        because: object,
    ) -> bool:
        """Refuse a new round without its reason (design 5.4, "No re-rolls
        on demand") — :class:`DrawWireError`. ``False``: it may hold, but
        this household's copy of the table doesn't show it yet."""
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
                retry = (
                    last is not None
                    and st.get("ended") is not None
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
            # Exactly the people seated at the table as we see it — no
            # invisible extras, nobody left out.
            return now == seated
        if last is None or cur is None:
            raise DrawWireError("no draw to follow")
        if last["complete"] or (gen, round_no) != (last["gen"], last["round"] + 1):
            raise DrawWireError("not the next round")
        before = {_person_key(x) for x in _people_from(cur["people"])}
        if kind == BECAUSE_CHANGED:
            # Fewer people, never new ones — and only people our own copy
            # shows gone.
            if not now < before:
                raise DrawWireError("the people didn't change")
            if "S" in cur:
                gone_households = {i for i, _u in before - now}
                if any(i == inst for i, _u in now for inst in gone_households):
                    # After S, only whole stuck households are dropped.
                    raise DrawWireError("a leave after S is no new round")
            return not ((before - now) & seated)
        if now != before:
            raise DrawWireError("the people changed")
        rnd = _Round(cur)
        body = because.get("list")
        if not isinstance(body, dict):
            raise DrawWireError("a new round without its proof")
        S, batches = check_list(rnd, body)
        if "S" in cur and [_b64(s) for s in S] != cur["S"]:
            raise DrawWireError("another list than ours")
        if kind == BECAUSE_VOID:
            if all(dc.is_usable_key(s) for s in S):
                raise DrawWireError("the round wasn't void")
            return True
        if kind != BECAUSE_COMPLAINT:
            raise DrawWireError("unknown reason")
        complaint = because.get("complaint")
        if not isinstance(complaint, dict):
            raise DrawWireError("a complaint without its proof")
        mixer_pubs = _raw_list(
            because.get("mixer_pubs"), rnd.h, dc.KEY_BYTES, "mixer_pubs"
        )
        if "mixer_pubs" in cur and [_b64(x) for x in mixer_pubs] != cur["mixer_pubs"]:
            raise DrawWireError("other mixer keys than ours")
        cards: list[bytes] | None = None
        if complaint.get("kind") == COMPLAINT_MISSING:
            if "cards_msg" in cur or "commits_cards" in cur:
                raise DrawWireError("a missing key told after the cards")
        else:
            cards = _raw_list(
                because.get("cards"), rnd.n, dc.SEALED_CARD_BYTES, "cards"
            )
            committed = self._parse_commits(rnd, because.get("commits"))
            if "commits_cards" in cur and committed != cur["commits_cards"]:
                raise DrawWireError("other card commitments than ours")
            if not check_card_list(cards, committed, rnd.n):
                raise DrawWireError("cards aren't the committed ones")
        if not complaint_holds(
            rnd, complaint, S=S, batches=batches, mixer_pubs=mixer_pubs, cards=cards
        ):
            raise DrawWireError("the complaint doesn't hold")
        return True

    @staticmethod
    def _parse_commits(rnd: _Round, raw: object) -> dict[str, list[str]]:
        """Every household's signed card commitment."""
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
                rnd.identity(inst), dc.card_commit_message(rnd.ctx, inst, hashes), sig
            ):
                raise DrawWireError("card commitment signature")
            out[inst] = sorted(_b64(x) for x in hashes)
        return out

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
            onions, seeds, commits = await asyncio.to_thread(
                self._make_onions, rnd, pubs
            )
            r = self._rand(dc.SEED_BYTES)
            r_commit = dc.r_commitment(rnd.ctx, self._own, r)
            batch_sig = self._sign(
                dc.batch_message(
                    rnd.ctx,
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
            entries = self._first_list(rnd, p, cur)
        else:
            entries = _raw_list(p.get("list"), rnd.n, dc.onion_size(rnd.h - k), "list")
            prev = p.get("prev")
            if not isinstance(prev, dict) or prev.get("instance") != rnd.order[k - 1]:
                raise DrawWireError("prev malformed")
            d_prev_in = _raw(prev.get("in"), dc.DIGEST_BYTES, "in")
            sig = _raw(prev.get("sig"), dc.SIG_BYTES, "sig")
            d_list = dc.list_digest([dc.entry_hash(e) for e in entries])
            msg = dc.mix_message(rnd.ctx, k - 1, rnd.order[k - 1], d_prev_in, d_list)
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
            await self._send(table, table.hub_instance, STEP_MIXED, done["msg"])
            return
        out = await asyncio.to_thread(
            dc.mix,
            _raw(cur["mixer_priv"]),
            entries,
            ctx=rnd.ctx,
            k=k,
            h=rnd.h,
            rand=self._rand,
        )
        d_out = dc.list_digest([dc.entry_hash(o) for o in out])
        out_sig = self._sign(dc.mix_message(rnd.ctx, k, self._own, d_in, d_out))
        reply = self._rid(
            rnd, k=k, list=[_b64(o) for o in out], **{"in": _b64(d_in)}, sig=out_sig
        )
        cur["mixed"] = {"in": _b64(d_in), "msg": reply}
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_MIXED, reply)

    def _first_list(self, rnd: _Round, p: dict, cur: dict) -> list[bytes]:
        """The first mixer's input: exactly every household's signed onions
        (households in id order), ours exactly as we sent them."""
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
        for inst in sorted(by_inst):
            checked = self._check_batch(rnd, inst, by_inst[inst])
            if inst == self._own and checked["onions"] != cur["onions_msg"]["onions"]:
                raise DrawWireError("our onions changed")
            entries += [_raw(o) for o in checked["onions"]]
        return entries

    async def _on_list(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "seeds" not in cur:
            raise DrawWireError("no onions of ours in this round")
        S, batches = await asyncio.to_thread(check_list, rnd, p)
        mine = batches[self._own]
        sent = cur["onions_msg"]
        if mine["commits"] != sent["commits"] or mine["r_commit"] != sent["r_commit"]:
            raise DrawWireError("our commitments changed")
        if sorted(mine["hashes"]) != sorted(
            _b64(dc.entry_hash(_raw(o))) for o in sent["onions"]
        ):
            raise DrawWireError("our onions changed")
        cur["S"] = [_b64(s) for s in S]
        cur["batches"] = batches
        if "answer" in cur:
            await self._save(table.session_id, st)
            step, msg = cur["answer"]
            await self._send(table, table.hub_instance, step, msg)
            return
        if not all(dc.is_usable_key(s) for s in S):
            # The round broke publicly: open every seed, which shows ours
            # rebuild exactly what we signed (and names whoever's don't).
            seeds = [(u, _raw(s)) for u, s in cur["seeds"].items()]
            sig = self._sign(dc.opening_message(rnd.ctx, self._own, seeds))
            answer = (
                STEP_OPEN,
                self._rid(rnd, opening={"seeds": cur["seeds"], "sig": sig}),
            )
        else:
            missing = next(
                (
                    u
                    for u in sorted(cur["seeds"])
                    if S.count(self._box_pub(cur, u)) != 1
                ),
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

    def _complaint(self, rnd: _Round, cur: dict, kind: str, user: str) -> dict:
        seed = _raw(cur["seeds"][user])
        sig = self._sign(dc.complaint_message(rnd.ctx, self._own, kind, user, seed))
        complaint = {
            "kind": kind,
            "instance": self._own,
            "user": user,
            "seed": cur["seeds"][user],
            "sig": sig,
        }
        cur["complained"] = complaint
        log.info("app_draw: round %s/%s stopped (%s)", rnd.gen, rnd.round, kind)
        return self._rid(rnd, complaint=complaint)

    async def _on_reveals(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "S" not in cur or cur.get("complained"):
            raise DrawWireError("no list of ours in this round")
        raw = p.get("reveals")
        if not isinstance(raw, dict) or set(raw) != set(rnd.order):
            raise DrawWireError("reveals malformed")
        reveals: dict[str, bytes] = {}
        for inst in rnd.order:
            r = _raw(raw[inst], dc.SEED_BYTES, "r")
            if (
                _b64(dc.r_commitment(rnd.ctx, inst, r))
                != cur["batches"][inst]["r_commit"]
            ):
                raise DrawWireError("r doesn't match its commitment")
            reveals[inst] = r
        if reveals[self._own] != _raw(cur["r"]):
            raise DrawWireError("not our r")
        if "commit_msg" not in cur:
            S = [_raw(s) for s in cur["S"]]
            R = dc.round_randomness(rnd.ctx, reveals)
            C = dc.circle(S, ctx=rnd.ctx, R=R)
            cards = await self._make_cards(rnd, cur, C, st.get("wishes", {}))
            hashes = [dc.sha256(c) for c in cards]
            sig = self._sign(dc.card_commit_message(rnd.ctx, self._own, hashes))
            cur["C"] = [_b64(x) for x in C]
            cur["cards_msg"] = self._rid(rnd, cards=[_b64(c) for c in cards])
            cur["commit_msg"] = self._rid(
                rnd, hashes=[_b64(x) for x in hashes], sig=sig
            )
            await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_COMMIT, cur["commit_msg"])

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
        if "commit_msg" not in cur:
            raise DrawWireError("no commitment of ours in this round")
        committed = self._parse_commits(rnd, p.get("commits"))
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
        if "commits_cards" not in cur:
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
            card = check_card(rnd, opened[0], giver=giver) if len(opened) == 1 else None
            if card is None or card.rev != 0:
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
            rnd, count=count, sig=self._sign(dc.done_message(rnd.ctx, self._own, count))
        )
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, STEP_DONE, cur["done_msg"])

    async def _on_complete(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "results" not in cur or st["last"]["complete"]:
            return
        dones = p.get("dones")
        if not isinstance(dones, dict) or set(dones) != set(rnd.order):
            raise DrawWireError("complete without every household's done")
        for inst in rnd.order:
            d = dones[inst]
            if not isinstance(d, dict) or d.get("count") != len(rnd.of(inst)):
                raise DrawWireError("done malformed")
            sig = _raw(d.get("sig"), dc.SIG_BYTES, "sig")
            if not verify_ed25519(
                rnd.identity(inst), dc.done_message(rnd.ctx, inst, d["count"]), sig
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
        reason = p.get("reason")
        if reason not in (END_RULES, END_TOO_FEW):
            raise DrawWireError("reason malformed")
        st["ended"] = reason
        await self._save(table.session_id, st)
        await self._push(table, st)

    # ─── After the reveal: hand-overs and wish-list updates ──────────

    def _final_of(self, st: dict, rnd: _Round) -> dict | None:
        final = st.get("final")
        if final is None or (final["gen"], final["round"]) != (rnd.gen, rnd.round):
            return None
        return final

    async def _on_handover(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        final = self._final_of(st, rnd)
        if final is None:
            return
        sealed = _raw(p.get("sealed"), dc.SEALED_HANDOVER_BYTES, "sealed")
        cur = st["p"]
        cards = [_raw(c) for c in cur["cards"]]
        C = [_raw(x) for x in cur["C"]]
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
            inst, leaver, leaver_priv, sig = dc.decode_handover(opened)
            leaver_pub = dc.public_of(leaver_priv)
            if inst not in rnd.identities or rnd.seat_of(inst, leaver) is None:
                raise DrawWireError("hand-over from someone not in the draw")
            if not verify_ed25519(
                rnd.identity(inst),
                dc.handover_message(rnd.ctx, inst, leaver, leaver_pub),
                sig,
            ):
                raise DrawWireError("hand-over signature")
            to_now = dc.decode_card(_raw(res["card"]))
            if (to_now.instance, to_now.user) != (inst, leaver):
                raise DrawWireError("a hand-over from someone we don't give to")
            # The leaver's box must be exactly the one we give to.
            if leaver_pub != _raw(res["rbox"]):
                raise DrawWireError("hand-over with another box")
            gone = table.entry(inst, leaver)
            if gone is not None and gone.state == SEAT_SEATED:
                # Our copy still seats the leaver (the hub's roster is on
                # its way): keep it until it shows them gone.
                pending = st.setdefault("pending", [])
                if p["sealed"] not in pending and len(pending) < DRAW_MAX_PEOPLE:
                    pending.append(p["sealed"])
                    await self._save(table.session_id, st)
                return
            # The new card is the one sealed to the leaver's box in the
            # round's committed list we hold — byte for byte, nothing else.
            new = dc.try_cards(leaver_priv, cards, ctx=rnd.ctx)
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            card = check_card(rnd, new[0], giver=giver) if len(new) == 1 else None
            if card is None:
                raise DrawWireError("hand-over card")
            j = C.index(leaver_pub)
            gives_to, _s = dc.circle_targets(j, rnd.n)
            res.update(
                {
                    "card": _b64(new[0]),
                    "rbox": _b64(C[gives_to]),
                    "rev": 0,
                    "via": [inst, leaver],
                    "extra": [*res.get("extra", []), _b64(leaver_priv)],
                }
            )
            await self._save(table.session_id, st)
            await self._tell(table, user, "handover", gen=rnd.gen)
            await self._push(table, st)
            return

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
        self, table: GroupSession, rnd: _Round, cur: dict, user: str
    ) -> None:
        C = [_raw(x) for x in cur["C"]]
        box_priv = self._box_priv(cur, user)
        box_pub = dc.public_of(box_priv)
        j = C.index(box_pub)
        _gives, giver_pos = dc.circle_targets(j, rnd.n)
        sig = sign_ed25519(
            self._federation.own_identity_seed,
            dc.handover_message(rnd.ctx, self._own, user, box_pub),
        )
        plain = dc.encode_handover(self._own, user, box_priv, sig)
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
                pending_start = st.get("pending_start")
                if pending_start is not None:
                    await self._retry_start(table, st, pending_start)
                    st = await self._load(session_id)
                if not st.get("hub") and not st.get("final"):
                    return
                await self._hand_over_leavers(table, st, seated)
                waiting = st.pop("pending", [])
                if waiting and st.get("p") is not None:
                    rnd_p = _Round(st["p"])
                    for sealed in waiting:
                        try:
                            await self._on_handover(
                                table, st, rnd_p, {"sealed": sealed}
                            )
                        except (DrawWireError, dc.DrawCryptoError) as exc:
                            log.info("app_draw: a waiting hand-over dropped: %s", exc)
                    await self._save(session_id, st)
                await self._hub_roster_change(table, st)
        except Exception:
            log.exception("app_draw: table change for %s", session_id)

    async def _retry_start(self, table: GroupSession, st: dict, p: dict) -> None:
        try:
            gen = _int(p.get("gen"), 1, DRAW_AGAIN_MAX + 1, "gen")
            round_no = _int(p.get("round"), 1, DRAW_ROUNDS_MAX, "round")
            await self._on_start(table, st, gen, round_no, p)
        except (DrawWireError, dc.DrawCryptoError) as exc:
            log.info("app_draw: a waiting start dropped: %s", exc)
            st.pop("pending_start", None)
            await self._save(table.session_id, st)

    async def _hub_roster_change(self, table: GroupSession, st: dict) -> None:
        """At the hub: people who left before ``S`` start a fresh round
        without them. After ``S`` a leave starts nothing (the round ends,
        then the leaver hands over) — except a whole household that holds
        the round up and the organiser took out ("Draw without them")."""
        hub = st.get("hub")
        if hub is None or table.hub_instance != self._own:
            return
        if table.state == GROUP_ENDED and hub["phase"] != _PHASE_ENDED:
            hub["phase"] = _PHASE_ENDED
            hub["ended"] = END_TOO_FEW
            await self._save(table.session_id, st)
            return
        rnd = _Round(hub)
        seated = _seated(table)
        still = [x for x in rnd.people if _person_key(x) in seated]
        if len(still) == rnd.n:
            return
        if hub["phase"] in _AFTER_S:
            gone_homes = {x.instance for x in rnd.people} - {x.instance for x in still}
            stuck = set(self._waiting(hub, rnd))
            if not gone_homes or not gone_homes <= stuck:
                return
            if any(x.instance in gone_homes for x in still):
                return
        if hub["phase"] in _RUNNING:
            await self._next_round(
                table,
                st,
                people=still,
                names=rnd.names,
                because={"kind": BECAUSE_CHANGED},
            )
        elif hub["phase"] == _PHASE_COMPLETE and len(still) < DRAW_MIN_PEOPLE:
            await self._hub_end(table, st, END_TOO_FEW)

    async def _hand_over_leavers(
        self, table: GroupSession, st: dict, seated: set
    ) -> None:
        """Our people who left after the reveal hand their box key to their
        giver (design 5.6) — once each."""
        final = st.get("final")
        cur = st.get("p")
        if (
            final is None
            or cur is None
            or (cur["gen"], cur["round"])
            != (
                final["gen"],
                final["round"],
            )
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
            await self._send_handover(table, rnd, cur, user)

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
        settings, the person's own wish list — never a result."""
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
        if hub is not None and is_host:
            rnd = _Round(hub)
            waiting = self._waiting(hub, rnd) if hub["phase"] in _RUNNING else []
            out["done"] = (
                sum(len(rnd.of(i)) for i in rnd.order if i not in waiting)
                if hub["phase"] != _PHASE_MIXING
                else sum(len(rnd.of(i)) for i in rnd.order[: hub["k"]])
            )
            since = datetime.fromisoformat(hub["since"])
            if waiting and (_now() - since).total_seconds() >= DRAW_WAIT_SECONDS:
                out["waiting_for"] = self._seats_of(rnd, waiting)
            out["blamed"] = self._seats_of(rnd, hub["blamed"]) or None
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
        """Draw names (organiser): starts the table if it waits for
        players, then the first round — or, *again*, a new draw for
        everyone still in after a reveal (visible to all, with its count;
        only until the exchange day)."""
        table, user = await self._host(app_id, session_id, actor_user_id)
        if len(table.seated) < DRAW_MIN_PEOPLE:
            raise AppDrawTooFewError()
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
                if hub is not None and hub["gen"] > DRAW_AGAIN_MAX:
                    raise AppDrawAgainLimitError()
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
    "opening_holds",
]
