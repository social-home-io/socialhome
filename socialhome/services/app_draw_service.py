"""AppDrawService — the gift draw at a group table (v_78).

Group-play design §5. An app at a table (the Gift draw app) asks Social
Home to draw names; every participant's **own household** then does its
part by itself — no app needs to stay open — and the result is shown in
Social Home's own sheet: the app never sees who draws whom, the
organiser learns only their own recipient, and the table's **hub** (the
host household, which passes every message on) learns nothing more than
any participant.

The rounds (all sealed ``APP_SESSION`` verbs between the hub and each
participant household, carrying ``draw_suite``, ``gen`` and ``round``;
the cryptography is :mod:`socialhome.federation.draw_crypto`)::

    hub → all     draw_start {people, identities, settings, because}
    each → hub    draw_key {pub, sig}               (a fresh mixer key)
    hub → all     draw_mixers {keys}                (in mixing order)
    each → hub    draw_onions {onions, commits, sig}
    hub → mixer k draw_mix {k, batches | list+prev} (one after the other)
    mixer → hub   draw_mixed {k, list, in, sig}
    hub → all     draw_list {S, batches, chain}     (the signed chain)
    each → hub    draw_cards {cards}                (key-private, 1 KiB)
    hub → all     draw_cards_all {cards}            (sorted by hash)
    each → hub    draw_done {count} | draw_fail / draw_redo {complaint}
    hub → all     draw_complete | draw_end {reason}
    after the reveal: draw_handover / draw_wish (each → hub → all)

Security invariants
-------------------
* A household refuses a new round unless it is the table's first draw,
  the organiser's visible "Draw again" after a reveal, a round with fewer
  people (someone left or was removed), or a complaint about the last
  round that **it verifies itself** from the proof (the complainer's box
  key and commitment) — no re-rolls on demand.
* Every signature and seal is bound to the round's context (table, draw,
  round, people, identity keys, settings): the hub can't show two
  households two different rounds.
* A mixer mixes once per round, only exactly the previous mixer's signed
  output (the first: exactly the n signed onions).
* A card's person is named by their own household and signed by it; a
  giver checks the signature, the person and the pairs.
* Every secret (mixer key, box keys, results, wish lists) is stored
  KEK-wrapped; the app gets the state only (``status``), never a result.
* Receivers enforce the bounds: 3 ≤ n ≤ 30, exact sizes, ≤ 12 rounds per
  draw, one draw per table, only from the hub / only from a participant.
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

from ..crypto import b64url_decode, b64url_encode, sign_ed25519, verify_ed25519
from ..domain.app_draw import (
    BECAUSE_AGAIN,
    BECAUSE_CHANGED,
    BECAUSE_COMPLAINT,
    BECAUSE_FIRST,
    COMPLAINT_CARDS,
    COMPLAINT_KINDS,
    COMPLAINT_MISSING,
    COMPLAINT_PARTNER,
    DRAW_AGAIN_MAX,
    DRAW_MAX_PEOPLE,
    DRAW_MIN_PEOPLE,
    DRAW_ROUNDS_MAX,
    DRAW_TO_HUB_VERBS,
    DRAW_WAIT_SECONDS,
    DRAW_WISH_UPDATES_MAX,
    END_RULES,
    END_TOO_FEW,
    STATE_DRAWING,
    STATE_FAILED,
    STATE_GATHERING,
    STATE_READY,
    VERB_DRAW_CARDS,
    VERB_DRAW_CARDS_ALL,
    VERB_DRAW_COMPLETE,
    VERB_DRAW_DONE,
    VERB_DRAW_END,
    VERB_DRAW_FAIL,
    VERB_DRAW_HANDOVER,
    VERB_DRAW_KEY,
    VERB_DRAW_LIST,
    VERB_DRAW_MIX,
    VERB_DRAW_MIXED,
    VERB_DRAW_MIXERS,
    VERB_DRAW_ONIONS,
    VERB_DRAW_REDO,
    VERB_DRAW_START,
    VERB_DRAW_WISH,
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
from ..domain.app_group import (
    GROUP_ENDED,
    GROUP_LOBBY,
    SEAT_SEATED,
    AppGroupNotFoundError,
    GroupSession,
)
from ..domain.events import AppDrawChanged
from ..domain.federation import FederationEvent
from ..domain.federation_capabilities import FederationCapability
from ..federation import draw_crypto as dc

if TYPE_CHECKING:
    from ..domain.events import DomainEvent
    from ..domain.user import User
    from ..infrastructure.event_bus import EventBus
    from ..infrastructure.key_manager import KeyManager
    from ..infrastructure.ws_manager import WebSocketManager
    from ..repositories.app_group_repo import AbstractAppGroupRepo
    from .app_federation_service import AppFederationService
    from .app_group_service import AppGroupService

log = logging.getLogger(__name__)

#: The WebSocket frame each local person at a table gets when the draw's
#: state changes (the app hears it as ``draw:event``; never a result).
WS_FRAME_TYPE = "app.draw"

_PHASE_KEYS = "keys"
_PHASE_ONIONS = "onions"
_PHASE_MIXING = "mixing"
_PHASE_CARDS = "cards"
_PHASE_OPENING = "opening"
_PHASE_COMPLETE = "complete"
_PHASE_ENDED = "ended"
_RUNNING = frozenset(
    {_PHASE_KEYS, _PHASE_ONIONS, _PHASE_MIXING, _PHASE_CARDS, _PHASE_OPENING}
)

#: The largest value a draw message carries (an onion with 30 layers).
_MAX_BLOB = dc.onion_size(DRAW_MAX_PEOPLE)

#: What the draw's KEK-wrapped state is bound to.
_AD = b"socialhome/app_draw/state_at_rest|"


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
        for v in (inst, user):
            if (
                not isinstance(v, str)
                or not v
                or len(v.encode()) > dc.CARD_REF_MAX_BYTES
            ):
                raise DrawWireError("person malformed")
        assert isinstance(inst, str) and isinstance(user, str)
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


class _Round:
    """A round's public frame, rebuilt from stored JSON: who, in what
    order, under which context."""

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


def check_list(rnd: _Round, body: dict) -> tuple[list[bytes], dict[str, list[list]]]:
    """A ``draw_list`` body against its round: the n box keys ``S`` and
    every household's commitments — or :class:`DrawWireError` /
    :class:`~socialhome.federation.draw_crypto.DrawCryptoError`.

    Every household's onion batch is signed by that household; the chain
    starts at those onions (``list_digest`` over their hashes, households
    in id order), every link is signed by the mixer whose turn it was and
    takes exactly what the one before gave; the last link gives ``S``."""
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
    commits_all: dict[str, list[list]] = {}
    for inst in sorted(by_inst):
        b = by_inst[inst]
        mine = rnd.of(inst)
        hs = _raw_list(b.get("hashes"), len(mine), dc.DIGEST_BYTES, "hashes")
        commits = _commits(b.get("commits"), mine)
        sig = _raw(b.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.batch_message(rnd.ctx, inst, hs, commits)
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            raise DrawWireError("batch signature")
        hashes += hs
        commits_all[inst] = [[u, _b64(c)] for u, c in commits]
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
    return S, commits_all


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
    """The card a giver's box opened — ``None`` when it is not a valid
    card of another person of the round signed by their own household."""
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


def complaint_holds(
    rnd: _Round,
    complaint: dict,
    *,
    S: list[bytes],
    commits_all: dict[str, list[list]],
    cards: list[bytes] | None,
) -> bool:
    """Whether *complaint* about round *rnd* is proven: signed by its
    household, its box key committed to for that person, and what it says
    is true of the round's public data (``S``, the cards)."""
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
        box_priv = _raw(complaint.get("box_priv"), dc.KEY_BYTES, "box_priv")
        nonce = _raw(complaint.get("nonce"), dc.NONCE_BYTES, "nonce")
        sig = _raw(complaint.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.complaint_message(rnd.ctx, inst, kind, user, box_priv, nonce)
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            return False
        box_pub = dc.public_of(box_priv)
        c = _b64(dc.commitment(rnd.ctx, inst, user, box_pub, nonce))
        if [user, c] not in commits_all.get(inst, []):
            return False
        if kind == COMPLAINT_MISSING:
            return S.count(box_pub) != 1
        if S.count(box_pub) != 1 or cards is None:
            return False
        opened = dc.try_cards(box_priv, cards, ctx=rnd.ctx)
        giver = dc.Person(seat=rnd.seat_of(inst, user) or 0, instance=inst, user=user)
        card = check_card(rnd, opened[0], giver=giver) if len(opened) == 1 else None
        if kind == COMPLAINT_CARDS:
            return card is None
        # COMPLAINT_PARTNER: exactly one valid card, the giver's partner.
        if card is None:
            return False
        to_seat = rnd.seat_of(card.instance, card.user)
        return to_seat is not None and rnd.settings.excludes(giver.seat, to_seat)
    except DrawWireError, dc.DrawCryptoError, KeyError, ValueError:
        return False


class AppDrawService:
    """The gift draw: the hub's rounds and each household's own steps. See
    the module docstring."""

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
        "_tasks",
        "_user_repo",
        "_ws",
    )

    def __init__(
        self,
        *,
        groups: AppGroupService,
        group_repo: AbstractAppGroupRepo,
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
        self._app_fed = app_federation
        self._user_repo = user_repo
        self._federation = federation
        self._key_manager = key_manager
        self._ws = ws
        self._household_label = household_label
        self._bus = bus
        #: Randomness for keys, shuffles and seals (``os.urandom``; tests
        #: pin it to make test vectors).
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

    async def _load(self, session_id: str) -> dict:
        raw = await self._repo.get_draw(session_id)
        if raw is None:
            return {"v": 1}
        record = orjson.loads(raw)
        plain = self._key_manager.decrypt(
            record["enc"], associated_data=_AD + session_id.encode()
        )
        return orjson.loads(plain)

    async def _save(self, session_id: str, st: dict) -> None:
        enc = self._key_manager.encrypt(
            orjson.dumps(st), associated_data=_AD + session_id.encode()
        )
        await self._repo.set_draw(
            session_id, orjson.dumps({"v": 1, "enc": enc}).decode()
        )

    def _sign(self, message: bytes) -> str:
        return _b64(sign_ed25519(self._federation.own_identity_seed, message))

    async def _send(
        self, table: GroupSession, to_instance: str, verb: str, body: dict
    ) -> None:
        """One draw verb to *to_instance* — this household's own steps run
        in a task of their own (after the caller's lock is released)."""
        payload = {
            "app_id": table.app_id,
            "session_id": table.session_id,
            "verb": verb,
            "draw_suite": dc.DRAW_SUITE_X25519_AESGCM,
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
        except Exception:
            log.exception("app_draw: own step %s failed", payload.get("verb"))

    async def _supports(self, instance: str) -> bool:
        if instance == self._own:
            return True
        return bool(
            await self._federation.space_member_supports(
                instance, min_version=FederationCapability.MIN_FOR_APP_DRAW
            )
        )

    # ─── Inbound ──────────────────────────────────────────────────────

    async def on_inbound(self, event: FederationEvent, table: GroupSession) -> None:
        """A ``draw_*`` verb for *table* (its app and space already
        matched by :class:`AppGroupService`). Anything malformed, from a
        household below v_78 or not from who may send it is dropped."""
        sender = event.from_instance
        if not await self._supports(sender):
            log.info("app_draw: verb from %s below v_78 dropped", sender)
            return
        try:
            await self._step(table, sender, event.payload)
        except (DrawWireError, dc.DrawCryptoError, dc.UnsupportedDrawSuite) as exc:
            log.info(
                "app_draw: %s from %s for %s dropped: %s",
                event.payload.get("verb"),
                sender,
                table.session_id,
                exc,
            )

    async def _step(self, table: GroupSession, sender: str, p: dict) -> None:
        dc.check_suite(p.get("draw_suite"))
        verb = p.get("verb")
        gen = _int(p.get("gen"), 1, DRAW_AGAIN_MAX + 1, "gen")
        round_no = _int(p.get("round"), 1, DRAW_ROUNDS_MAX, "round")
        is_hub = table.hub_instance == self._own
        # A hand-over / wish update the hub passes on keeps its verb and
        # says so (``relayed``); only the hub's own copy may.
        if verb in DRAW_TO_HUB_VERBS and p.get("relayed") is not True:
            if not is_hub:
                raise DrawWireError("not the hub")
            async with self._lock(table.session_id):
                await self._hub_step(table, sender, str(verb), gen, round_no, p)
            return
        if sender != table.hub_instance:
            raise DrawWireError("not from the hub")
        async with self._lock(table.session_id):
            await self._participant_step(table, str(verb), gen, round_no, p)

    # ─── The hub ──────────────────────────────────────────────────────

    async def _hub_step(
        self,
        table: GroupSession,
        sender: str,
        verb: str,
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
        match verb:
            case "draw_key":
                await self._hub_key(table, st, rnd, sender, p)
            case "draw_onions":
                await self._hub_onions(table, st, rnd, sender, p)
            case "draw_mixed":
                await self._hub_mixed(table, st, rnd, sender, p)
            case "draw_cards":
                await self._hub_cards(table, st, rnd, sender, p)
            case "draw_done":
                await self._hub_done(table, st, rnd, sender, p)
            case "draw_fail" | "draw_redo":
                await self._hub_complaint(table, st, rnd, sender, verb, p)
            case "draw_handover" | "draw_wish":
                await self._hub_relay_after(table, st, rnd, sender, verb, p)
            case _:  # pragma: no cover — DRAW_TO_HUB_VERBS is exhaustive
                raise DrawWireError("unknown verb")

    def _hub_expect(self, hub: dict, phase: str) -> None:
        if hub["phase"] != phase:
            raise DrawWireError(f"not in {phase}")

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
        if sender in hub["keys"]:
            return  # first key wins; a repeat is the outbox retrying
        hub["keys"][sender] = {"pub": _b64(pub), "sig": _b64(sig)}
        if len(hub["keys"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        hub["phase"] = _PHASE_ONIONS
        hub["since"] = _now().isoformat()
        await self._save(table.session_id, st)
        keys = [{"instance": i, **hub["keys"][i]} for i in rnd.order]
        for inst in rnd.order:
            await self._send(table, inst, VERB_DRAW_MIXERS, self._rid(rnd, keys=keys))

    async def _hub_onions(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_ONIONS)
        if sender in hub["batches"]:
            return
        batch = self._check_batch(rnd, sender, p)
        hub["batches"][sender] = batch
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
            table, rnd.order[0], VERB_DRAW_MIX, self._rid(rnd, k=0, batches=batches)
        )

    @staticmethod
    def _check_batch(rnd: _Round, inst: str, b: dict) -> dict:
        """A household's onions: one per person of it, each exactly the
        round's onion size, signed by it with its commitments."""
        mine = rnd.of(inst)
        onions = _raw_list(b.get("onions"), len(mine), dc.onion_size(rnd.h), "onions")
        commits = _commits(b.get("commits"), mine)
        sig = _raw(b.get("sig"), dc.SIG_BYTES, "sig")
        msg = dc.batch_message(
            rnd.ctx, inst, [dc.entry_hash(o) for o in onions], commits
        )
        if not verify_ed25519(rnd.identity(inst), msg, sig):
            raise DrawWireError("onions signature")
        return {
            "onions": [_b64(o) for o in onions],
            "commits": [[u, _b64(c)] for u, c in commits],
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
                VERB_DRAW_MIX,
                self._rid(
                    rnd,
                    k=k + 1,
                    list=hub["list"],
                    prev={"instance": sender, "in": link["in"], "sig": link["sig"]},
                ),
            )
            return
        hub["phase"] = _PHASE_CARDS
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
                    "sig": hub["batches"][i]["sig"],
                }
                for i in sorted(hub["batches"])
            ],
            "chain": hub["chain"],
        }
        hub["batches"] = {}  # the onions are spent: keep the hashes only
        await self._save(table.session_id, st)
        for inst in rnd.order:
            await self._send(
                table, inst, VERB_DRAW_LIST, self._rid(rnd, **hub["list_body"])
            )

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
        hub["cards"][sender] = [_b64(c) for c in cards]
        if len(hub["cards"]) < rnd.h:
            await self._save(table.session_id, st)
            return
        every = sorted(
            (c for cs in hub["cards"].values() for c in cs),
            key=lambda c: dc.sha256(_raw(c)),
        )
        if len(set(every)) != rnd.n:
            # Two households sent the same sealed card: someone copied one.
            raise DrawWireError("cards repeat")
        hub["all_cards"] = every
        hub["cards"] = {i: len(cs) for i, cs in hub["cards"].items()}
        hub["phase"] = _PHASE_OPENING
        hub["since"] = _now().isoformat()
        await self._save(table.session_id, st)
        for inst in rnd.order:
            await self._send(
                table, inst, VERB_DRAW_CARDS_ALL, self._rid(rnd, cards=every)
            )

    async def _hub_done(
        self, table: GroupSession, st: dict, rnd: _Round, sender: str, p: dict
    ) -> None:
        hub = st["hub"]
        self._hub_expect(hub, _PHASE_OPENING)
        if _int(p.get("count"), 1, DRAW_MAX_PEOPLE, "count") != len(rnd.of(sender)):
            raise DrawWireError("done for the wrong count")
        if sender in hub["done"]:
            return
        hub["done"].append(sender)
        if len(hub["done"]) < rnd.h:
            await self._save(table.session_id, st)
            await self._push(table, st)
            return
        hub["phase"] = _PHASE_COMPLETE
        await self._save(table.session_id, st)
        for inst in rnd.order:
            await self._send(table, inst, VERB_DRAW_COMPLETE, self._rid(rnd))
        await self._push(table, st)

    async def _hub_complaint(
        self,
        table: GroupSession,
        st: dict,
        rnd: _Round,
        sender: str,
        verb: str,
        p: dict,
    ) -> None:
        hub = st["hub"]
        complaint = p.get("complaint")
        if hub["phase"] not in (_PHASE_CARDS, _PHASE_OPENING) or not isinstance(
            complaint, dict
        ):
            raise DrawWireError("no complaint expected")
        kind = complaint.get("kind")
        if complaint.get("instance") != sender or (
            (verb == VERB_DRAW_REDO) != (kind == COMPLAINT_PARTNER)
        ):
            raise DrawWireError("complaint malformed")
        if kind == COMPLAINT_MISSING and (
            hub["phase"] != _PHASE_CARDS or sender in hub["cards"]
        ):
            # A missing key is said INSTEAD of sending cards, before any
            # card is opened — never later, once the household has seen
            # whom its people drew (a household that committed to one box
            # and onioned another could otherwise re-roll at will).
            raise DrawWireError("a missing key told too late")
        S = [_raw(s) for s in hub["S"]]
        cards = [_raw(c) for c in hub.get("all_cards", [])] or None
        commits_all = {b["instance"]: b["commits"] for b in hub["list_body"]["batches"]}
        if not await asyncio.to_thread(
            complaint_holds, rnd, complaint, S=S, commits_all=commits_all, cards=cards
        ):
            # Named to the organiser: "…'s home stopped the draw without reason".
            if sender not in hub["false"]:
                hub["false"].append(sender)
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
        evidence = {
            "kind": BECAUSE_COMPLAINT,
            "complaint": complaint,
            "list": hub["list_body"],
        }
        if kind != COMPLAINT_MISSING:
            evidence["cards"] = hub.get("all_cards", [])
        await self._next_round(
            table, st, people=rnd.people, names=rnd.names, because=evidence
        )

    async def _hub_relay_after(
        self,
        table: GroupSession,
        st: dict,
        rnd: _Round,
        sender: str,
        verb: str,
        p: dict,
    ) -> None:
        """A hand-over or a wish-list update after the reveal: passed on
        to every participant household (key-private — the hub can't tell
        whom it is for), at most a few per person of the sender."""
        hub = st["hub"]
        if hub["phase"] != _PHASE_COMPLETE:
            raise DrawWireError("not revealed")
        size = (
            dc.SEALED_HANDOVER_BYTES
            if verb == VERB_DRAW_HANDOVER
            else dc.SEALED_CARD_BYTES
        )
        sealed = _raw(p.get("sealed"), size, "sealed")
        sent = hub.setdefault("after", {})
        budget = len(rnd.of(sender)) * (DRAW_WISH_UPDATES_MAX + 1) * 2
        if sent.get(sender, 0) >= budget:
            raise DrawWireError("too many updates")
        sent[sender] = sent.get(sender, 0) + 1
        await self._save(table.session_id, st)
        for inst in rnd.order:
            await self._send(
                table, inst, verb, self._rid(rnd, sealed=_b64(sealed), relayed=True)
            )

    @staticmethod
    def _rid(rnd: _Round, **fields: Any) -> dict:
        return {"gen": rnd.gen, "round": rnd.round, **fields}

    async def _identities(
        self,
        table: GroupSession,
        instances: set[str],
        hints: dict[str, object] | None = None,
    ) -> dict[str, str] | None:
        """Each household's identity key (hex): the one this household
        stores for it, else a *hint* (the roster's, or the start message's)
        that derives to its id — a household id IS its key's fingerprint,
        so a relay can't pass off another key. ``None`` when one is
        unknown."""
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
        """Start the hub's next round (or the next draw, *again*) for
        *people*; gives up after :data:`DRAW_ROUNDS_MAX` rounds."""
        old = st.get("hub")
        gen = (
            (old["gen"] + 1)
            if (old is not None and again)
            else (old["gen"] if old else 1)
        )
        round_no = 1 if (old is None or again) else old["round"] + 1
        settings = DrawSettings.from_wire(st.get("settings") or {}).only(
            frozenset(p.seat for p in people)
        )
        if len(people) < DRAW_MIN_PEOPLE:
            await self._hub_end(table, st, END_TOO_FEW)
            return
        if round_no > DRAW_ROUNDS_MAX or not pairs_fit(
            settings.pairs, frozenset(p.seat for p in people)
        ):
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
            "cards": {},
            "done": [],
            "false": [],
        }
        await self._save(table.session_id, st)
        rnd = _Round(st["hub"])
        body = {
            "people": frame["people"],
            "identities": identities,
            "settings": frame["settings"],
            "because": because,
        }
        for inst in rnd.order:
            await self._send(table, inst, VERB_DRAW_START, self._rid(rnd, **body))
        await self._push(table, st)

    async def _hub_end(self, table: GroupSession, st: dict, reason: str) -> None:
        hub = st.get("hub")
        if hub is None:
            return
        hub["phase"] = _PHASE_ENDED
        hub["ended"] = reason
        await self._save(table.session_id, st)
        rnd = _Round(hub)
        for inst in rnd.order:
            await self._send(table, inst, VERB_DRAW_END, self._rid(rnd, reason=reason))
        await self._push(table, st)

    # ─── A participant household ──────────────────────────────────────

    async def _participant_step(
        self, table: GroupSession, verb: str, gen: int, round_no: int, p: dict
    ) -> None:
        st = await self._load(table.session_id)
        if verb == VERB_DRAW_START:
            await self._on_start(table, st, gen, round_no, p)
            return
        cur = st.get("p")
        if cur is None or (cur["gen"], cur["round"]) != (gen, round_no):
            raise DrawWireError("not the current round")
        rnd = _Round(cur)
        match verb:
            case "draw_mixers":
                await self._on_mixers(table, st, rnd, p)
            case "draw_mix":
                await self._on_mix(table, st, rnd, p)
            case "draw_list":
                await self._on_list(table, st, rnd, p)
            case "draw_cards_all":
                await self._on_cards_all(table, st, rnd, p)
            case "draw_complete":
                await self._on_complete(table, st, rnd)
            case "draw_end":
                await self._on_end(table, st, p)
            case "draw_handover":
                await self._on_handover(table, st, rnd, p)
            case "draw_wish":
                await self._on_wish(table, st, rnd, p)
            case _:
                raise DrawWireError("not a verb for a participant")

    async def _own_people(
        self, table: GroupSession, rnd_people: list[dc.Person]
    ) -> bool:
        """Whether every person of this household in *rnd_people* has a
        place at the table here (a local user with a group seat)."""
        for person in rnd_people:
            if person.instance != self._own:
                continue
            user = await self._user_repo.get(person.user)
            if user is None:
                return False
            row = await self._groups.seat_row(table, user.user_id)
            if row is None:
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
            await self._send(table, table.hub_instance, VERB_DRAW_KEY, cur["key_msg"])
            return
        self._justify(table, st, gen, round_no, people, p.get("because"))
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
        await self._send(table, table.hub_instance, VERB_DRAW_KEY, key_msg)
        await self._push(table, st)

    def _justify(
        self,
        table: GroupSession,
        st: dict,
        gen: int,
        round_no: int,
        people: list[dc.Person],
        because: object,
    ) -> None:
        """Refuse a new round without its reason (design 5.4, "No re-rolls
        on demand") — :class:`DrawWireError`."""
        if not isinstance(because, dict) or not isinstance(because.get("kind"), str):
            raise DrawWireError("a round without its reason")
        kind = because["kind"]
        last = st.get("last")
        cur = st.get("p")
        if kind == BECAUSE_FIRST:
            # The table's first draw — or a fresh one after a draw gave up.
            fresh = last is None and (gen, round_no) == (1, 1)
            retry = (
                last is not None
                and st.get("ended") is not None
                and (gen, round_no) == (last["gen"] + 1, 1)
            )
            if not (fresh or retry):
                raise DrawWireError("not the first draw")
            return
        if last is None or cur is None:
            raise DrawWireError("no draw to follow")
        if kind == BECAUSE_AGAIN:
            if not last["complete"] or (gen, round_no) != (last["gen"] + 1, 1):
                raise DrawWireError("draw again before a reveal")
            return
        if last["complete"] or (gen, round_no) != (last["gen"], last["round"] + 1):
            raise DrawWireError("not the next round")
        before = {_person_key(x) for x in _people_from(cur["people"])}
        now = {_person_key(x) for x in people}
        if kind == BECAUSE_CHANGED:
            # Fewer people, never new ones (a removal is visible to all;
            # the removed can't come back to the table).
            if not now < before:
                raise DrawWireError("the people didn't change")
            return
        if kind != BECAUSE_COMPLAINT or now != before:
            raise DrawWireError("unknown reason")
        rnd = _Round(cur)
        body = because.get("list")
        complaint = because.get("complaint")
        if not isinstance(body, dict) or not isinstance(complaint, dict):
            raise DrawWireError("a complaint without its proof")
        S, commits_all = check_list(rnd, body)
        cards_raw = because.get("cards")
        cards = (
            _raw_list(cards_raw, rnd.n, dc.SEALED_CARD_BYTES, "cards")
            if cards_raw is not None
            else None
        )
        # The proof must be about the round as THIS household saw it: the
        # same S, the same cards — a relay can't drop a card to make a
        # complaint hold.
        if "S" in cur and [_b64(s) for s in S] != cur["S"]:
            raise DrawWireError("another list than ours")
        if "cards" in cur and (
            cards is None or [_b64(c) for c in cards] != cur["cards"]
        ):
            raise DrawWireError("other cards than ours")
        if complaint.get("kind") == COMPLAINT_MISSING and "cards" in cur:
            raise DrawWireError("a missing key told after the cards went out")
        if not complaint_holds(
            rnd, complaint, S=S, commits_all=commits_all, cards=cards
        ):
            raise DrawWireError("the complaint doesn't hold")

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
            pubs.append(pub)
        mine = dc.public_of(_raw(cur["mixer_priv"]))
        if pubs[rnd.order.index(self._own)] != mine:
            raise DrawWireError("not our mixer key")
        if "onions_msg" not in cur:
            onions, boxes, commits = await asyncio.to_thread(
                self._make_onions, rnd, pubs
            )
            batch_sig = self._sign(
                dc.batch_message(
                    rnd.ctx, self._own, [dc.entry_hash(o) for o in onions], commits
                )
            )
            cur["boxes"] = boxes
            cur["onions_msg"] = self._rid(
                rnd,
                onions=[_b64(o) for o in onions],
                commits=[[u, _b64(c)] for u, c in commits],
                sig=batch_sig,
            )
            await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, VERB_DRAW_ONIONS, cur["onions_msg"])

    def _make_onions(
        self, rnd: _Round, pubs: list[bytes]
    ) -> tuple[list[bytes], dict, list[tuple[str, bytes]]]:
        """A box key, commitment and onion per own person (CPU-bound: runs
        in a worker thread)."""
        onions: list[bytes] = []
        boxes: dict[str, dict] = {}
        commits: list[tuple[str, bytes]] = []
        for person in rnd.of(self._own):
            priv, pub = dc.new_keypair(self._rand)
            nonce = self._rand(dc.NONCE_BYTES)
            boxes[person.user] = {"priv": _b64(priv), "nonce": _b64(nonce)}
            commits.append(
                (
                    person.user,
                    dc.commitment(rnd.ctx, self._own, person.user, pub, nonce),
                )
            )
            onions.append(dc.build_onion(pub, pubs, ctx=rnd.ctx, rand=self._rand))
        return dc.shuffle(onions, self._rand), boxes, sorted(commits)

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
                # A mixer mixes once per round, only the one signed list.
                log.warning(
                    "app_draw: second, different list to mix for %s", table.session_id
                )
                raise DrawWireError("already mixed another list")
            await self._send(table, table.hub_instance, VERB_DRAW_MIXED, done["msg"])
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
        await self._send(table, table.hub_instance, VERB_DRAW_MIXED, reply)

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
        if "boxes" not in cur:
            raise DrawWireError("no onions of ours in this round")
        S, commits_all = await asyncio.to_thread(check_list, rnd, p)
        own_commits = [[u, c] for u, c in cur["onions_msg"]["commits"]]
        if commits_all.get(self._own) != own_commits:
            raise DrawWireError("our commitments changed")
        cur["S"] = [_b64(s) for s in S]
        cur["commits_all"] = commits_all
        if "cards_msg" in cur:
            await self._save(table.session_id, st)
            await self._send(
                table, table.hub_instance, VERB_DRAW_CARDS, cur["cards_msg"]
            )
            return
        for user, box in sorted(cur["boxes"].items()):
            if S.count(dc.public_of(_raw(box["priv"]))) != 1:
                await self._complain(table, st, rnd, COMPLAINT_MISSING, user)
                return
        cards = await self._make_cards(rnd, cur, S, st.get("wishes", {}))
        cur["cards_msg"] = self._rid(rnd, cards=[_b64(c) for c in cards])
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, VERB_DRAW_CARDS, cur["cards_msg"])

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
        self, rnd: _Round, cur: dict, S: list[bytes], wishes: dict[str, str]
    ) -> list[bytes]:
        plain: list[tuple[bytes, bytes]] = []
        for user, box in sorted(cur["boxes"].items()):
            j = S.index(dc.public_of(_raw(box["priv"])))
            _gives, sealed_to = dc.circle_targets(j, rnd.n)
            card = await self._card_for(rnd, user, wish=wishes.get(user, ""), rev=0)
            plain.append((S[sealed_to], card))
        sealed = await asyncio.to_thread(
            lambda: [
                dc.seal_card(to, c, ctx=rnd.ctx, rand=self._rand) for to, c in plain
            ]
        )
        return dc.shuffle(sealed, self._rand)

    async def _complain(
        self, table: GroupSession, st: dict, rnd: _Round, kind: str, user: str
    ) -> None:
        cur = st["p"]
        box = cur["boxes"][user]
        box_priv, nonce = _raw(box["priv"]), _raw(box["nonce"])
        sig = self._sign(
            dc.complaint_message(rnd.ctx, self._own, kind, user, box_priv, nonce)
        )
        complaint = {
            "kind": kind,
            "instance": self._own,
            "user": user,
            "box_priv": box["priv"],
            "nonce": box["nonce"],
            "sig": sig,
        }
        cur["complained"] = complaint
        await self._save(table.session_id, st)
        log.info(
            "app_draw: round %s/%s of %s stopped (%s)",
            rnd.gen,
            rnd.round,
            table.session_id,
            kind,
        )
        verb = VERB_DRAW_REDO if kind == COMPLAINT_PARTNER else VERB_DRAW_FAIL
        await self._send(
            table, table.hub_instance, verb, self._rid(rnd, complaint=complaint)
        )

    async def _on_cards_all(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        cur = st["p"]
        if "cards_msg" not in cur:
            raise DrawWireError("no cards of ours in this round")
        cards = _raw_list(p.get("cards"), rnd.n, dc.SEALED_CARD_BYTES, "cards")
        mine = {_raw(c) for c in cur["cards_msg"]["cards"]}
        if not mine <= set(cards):
            raise DrawWireError("our cards are missing")
        cur["cards"] = [_b64(c) for c in cards]
        if cur.get("complained") is not None:
            await self._save(table.session_id, st)
            return
        if "done_msg" in cur:
            await self._save(table.session_id, st)
            await self._send(table, table.hub_instance, VERB_DRAW_DONE, cur["done_msg"])
            return
        S = [_raw(s) for s in cur["S"]]
        results: dict[str, dict] = {}
        for user, box in sorted(cur["boxes"].items()):
            box_priv = _raw(box["priv"])
            opened = await asyncio.to_thread(dc.try_cards, box_priv, cards, ctx=rnd.ctx)
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            card = check_card(rnd, opened[0], giver=giver) if len(opened) == 1 else None
            if card is None or card.rev != 0:
                await self._complain(table, st, rnd, COMPLAINT_CARDS, user)
                return
            to_seat = rnd.seat_of(card.instance, card.user) or 0
            if rnd.settings.excludes(giver.seat, to_seat):
                await self._complain(table, st, rnd, COMPLAINT_PARTNER, user)
                return
            results[user] = {
                "card": _b64(opened[0]),
                "j": S.index(dc.public_of(box_priv)),
                "rev": 0,
                "via": None,
            }
        cur["results"] = results
        cur["done_msg"] = self._rid(rnd, count=len(results))
        await self._save(table.session_id, st)
        await self._send(table, table.hub_instance, VERB_DRAW_DONE, cur["done_msg"])

    async def _on_complete(self, table: GroupSession, st: dict, rnd: _Round) -> None:
        cur = st["p"]
        if "results" not in cur or st["last"]["complete"]:
            return
        st["last"]["complete"] = True
        st["final"] = {
            "gen": rnd.gen,
            "round": rnd.round,
            "results": cur["results"],
        }
        await self._save(table.session_id, st)
        for user in cur["results"]:
            await self._tell(
                table, user, "again" if rnd.gen > 1 else "ready", gen=rnd.gen
            )
        await self._push(table, st)

    async def _on_end(self, table: GroupSession, st: dict, p: dict) -> None:
        reason = p.get("reason")
        if reason not in (END_RULES, END_TOO_FEW):
            raise DrawWireError("reason malformed")
        st["ended"] = reason
        await self._save(table.session_id, st)
        await self._push(table, st)

    # ─── After the reveal: hand-overs and wish-list updates ──────────

    async def _on_handover(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        final = st.get("final")
        if final is None or (final["gen"], final["round"]) != (rnd.gen, rnd.round):
            return
        sealed = _raw(p.get("sealed"), dc.SEALED_HANDOVER_BYTES, "sealed")
        cur = st["p"]
        for user, res in final["results"].items():
            if res.get("left"):
                continue
            opened = dc.open_handover(
                _raw(cur["boxes"][user]["priv"]), sealed, ctx=rnd.ctx
            )
            if opened is None:
                continue
            inst, leaver, sig, card_bytes = dc.decode_handover(opened)
            if inst not in rnd.identities or rnd.seat_of(inst, leaver) is None:
                raise DrawWireError("hand-over from someone not in the draw")
            if not verify_ed25519(
                rnd.identity(inst),
                dc.handover_message(rnd.ctx, inst, leaver, card_bytes),
                sig,
            ):
                raise DrawWireError("hand-over signature")
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            card = check_card(rnd, card_bytes, giver=giver)
            if card is None:
                raise DrawWireError("hand-over card")
            to_now = dc.decode_card(_raw(res["card"]))
            if (to_now.instance, to_now.user) == (inst, leaver):
                gone = table.entry(inst, leaver)
                if gone is not None and gone.state == SEAT_SEATED:
                    # Our copy still seats the leaver (the hub's roster is
                    # on its way): keep it until it shows them gone — a
                    # household can't hand over someone still playing.
                    pending = st.setdefault("pending", [])
                    if p["sealed"] not in pending and len(pending) < DRAW_MAX_PEOPLE:
                        pending.append(p["sealed"])
                        await self._save(table.session_id, st)
                    return
                res.update(
                    {"card": _b64(card_bytes), "rev": card.rev, "via": [inst, leaver]}
                )
                change = "handover"
            elif (
                (to_now.instance, to_now.user) == (card.instance, card.user)
                and res.get("via") == [inst, leaver]
                and card.rev > res["rev"]
            ):
                res.update({"card": _b64(card_bytes), "rev": card.rev})
                change = "wish"
            else:
                continue
            await self._save(table.session_id, st)
            await self._tell(
                table, user, change, gen=rnd.gen, name=rnd.names.get((inst, leaver))
            )
            await self._push(table, st)
            return

    async def _on_wish(
        self, table: GroupSession, st: dict, rnd: _Round, p: dict
    ) -> None:
        final = st.get("final")
        if final is None or (final["gen"], final["round"]) != (rnd.gen, rnd.round):
            return
        sealed = _raw(p.get("sealed"), dc.SEALED_CARD_BYTES, "sealed")
        cur = st["p"]
        for user, res in final["results"].items():
            opened = dc.open_card(_raw(cur["boxes"][user]["priv"]), sealed, ctx=rnd.ctx)
            if opened is None:
                continue
            giver = dc.Person(
                seat=rnd.seat_of(self._own, user) or 0, instance=self._own, user=user
            )
            card = check_card(rnd, opened, giver=giver)
            if card is None:
                raise DrawWireError("wish card")
            if res.get("left"):
                # Our person left and handed their recipient on: pass the
                # new card on to their giver the same way.
                held = dc.decode_card(_raw(res["card"]))
                if res.get("handed") and (held.instance, held.user) == (
                    card.instance,
                    card.user,
                ):
                    res["card"] = _b64(opened)
                    await self._save(table.session_id, st)
                    await self._send_handover(table, rnd, cur, user, opened)
                return
            to_now = dc.decode_card(_raw(res["card"]))
            if (to_now.instance, to_now.user) != (
                card.instance,
                card.user,
            ) or card.rev <= res["rev"]:
                return
            res.update({"card": _b64(opened), "rev": card.rev})
            await self._save(table.session_id, st)
            await self._tell(table, user, "wish", gen=rnd.gen)
            await self._push(table, st)
            return

    async def _send_handover(
        self, table: GroupSession, rnd: _Round, cur: dict, user: str, card_bytes: bytes
    ) -> None:
        S = [_raw(s) for s in cur["S"]]
        j = S.index(dc.public_of(_raw(cur["boxes"][user]["priv"])))
        _gives, giver_pos = dc.circle_targets(j, rnd.n)
        sig = sign_ed25519(
            self._federation.own_identity_seed,
            dc.handover_message(rnd.ctx, self._own, user, card_bytes),
        )
        plain = dc.encode_handover(self._own, user, sig, card_bytes)
        sealed = dc.seal_handover(S[giver_pos], plain, ctx=rnd.ctx, rand=self._rand)
        await self._send(
            table,
            table.hub_instance,
            VERB_DRAW_HANDOVER,
            self._rid(rnd, sealed=_b64(sealed)),
        )

    # ─── Table changes (hooked from AppGroupService) ──────────────────

    def table_changed(self, table: GroupSession) -> None:
        """The table's roster or state changed here: re-check the draw in a
        task of its own (never under the caller's lock)."""
        self._spawn(self._table_changed(table.session_id))

    def person_left(self, table: GroupSession, username: str) -> None:
        """*username* of this household left the table (or was removed):
        after a reveal their recipient goes to their giver. The household's
        own copy may not show it yet — the hub tells only those still
        seated."""
        self._spawn(self._table_changed(table.session_id, gone=username))

    async def _table_changed(self, session_id: str, *, gone: str | None = None) -> None:
        try:
            async with self._lock(session_id):
                table = await self._repo.get(session_id)
                if table is None:
                    return
                st = await self._load(session_id)
                if not st.get("hub") and not st.get("final"):
                    return
                seated = {
                    (e.instance, e.user): e
                    for e in table.roster
                    if e.state == SEAT_SEATED
                    and (e.instance, e.user) != (self._own, gone)
                }
                await self._hand_over_leavers(table, st, seated)
                # Hand-overs that waited for the leaver to show as gone.
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
                hub = st.get("hub")
                if hub is None or table.hub_instance != self._own:
                    return
                rnd = _Round(hub)
                if table.state == GROUP_ENDED and hub["phase"] != _PHASE_ENDED:
                    hub["phase"] = _PHASE_ENDED
                    hub["ended"] = END_TOO_FEW
                    await self._save(session_id, st)
                    return
                still = [x for x in rnd.people if _person_key(x) in seated]
                if len(still) == rnd.n:
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
        except Exception:
            log.exception("app_draw: table change for %s", session_id)

    async def _hand_over_leavers(
        self, table: GroupSession, st: dict, seated: dict
    ) -> None:
        """Our people who left after the reveal hand their recipient on to
        their giver (design 5.6) — once each."""
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
            if (self._own, user) in seated or res.get("handed"):
                continue
            res["left"] = True
            res["handed"] = True
            await self._save(table.session_id, st)
            await self._send_handover(table, rnd, cur, user, _raw(res["card"]))

    # ─── People and the app ───────────────────────────────────────────

    async def _tell(
        self,
        table: GroupSession,
        username: str,
        change: str,
        *,
        gen: int,
        name: str | None = None,
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
                name=name,
            )
        )

    async def _push(self, table: GroupSession, st: dict) -> None:
        """Each local person at the table hears the draw's state (never a
        result) on the ``app.draw`` frame."""
        for user, _row in await self._groups.local_people(table):
            await self._ws.broadcast_to_user(
                user.user_id,
                {
                    "type": WS_FRAME_TYPE,
                    "app_id": table.app_id,
                    "session_id": table.session_id,
                    "draw": self._view(table, st, user),
                },
            )

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
            "has_result": final is not None
            and user.username in final["results"]
            and not final["results"][user.username].get("left"),
            "is_host": is_host,
            "waiting_for": None,
            "false_complaints": None,
        }
        if hub is not None and is_host:
            rnd = _Round(hub)
            done = self._done_households(hub, rnd)
            out["done"] = sum(len(rnd.of(i)) for i in done)
            since = datetime.fromisoformat(hub["since"])
            if (
                hub["phase"] in _RUNNING
                and (_now() - since).total_seconds() >= DRAW_WAIT_SECONDS
            ):
                # The mix goes one household at a time: only the one whose
                # turn it is holds it up.
                waiting = (
                    [rnd.order[hub["k"]]]
                    if hub["phase"] == _PHASE_MIXING
                    else [i for i in rnd.order if i not in done]
                )
                out["waiting_for"] = self._seats_of(rnd, waiting)
            out["false_complaints"] = self._seats_of(rnd, hub["false"]) or None
        return out

    @staticmethod
    def _done_households(hub: dict, rnd: _Round) -> set[str]:
        phase = hub["phase"]
        if phase == _PHASE_KEYS:
            return set(hub["keys"])
        if phase == _PHASE_ONIONS:
            return set(hub["batches"])
        if phase == _PHASE_MIXING:
            return set(rnd.order[: hub["k"]])
        if phase == _PHASE_CARDS:
            return set(hub["cards"])
        if phase == _PHASE_OPENING:
            return set(hub["done"])
        return set(rnd.order)

    @staticmethod
    def _seats_of(rnd: _Round, instances: list[str]) -> list[dict]:
        return [{"seats": [p.seat for p in rnd.of(i)]} for i in instances if rnd.of(i)]

    # ─── What people do (routes) ──────────────────────────────────────

    async def status(self, *, app_id: str, session_id: str, actor_user_id: str) -> dict:
        table, user = await self._groups.seat_of_caller(
            app_id, session_id, actor_user_id
        )
        return self._view(table, await self._load(session_id), user)

    async def configure(
        self, *, app_id: str, session_id: str, actor_user_id: str, settings: dict
    ) -> dict:
        """The organiser's settings — before the first draw (or after one
        gave up)."""
        table, user = await self._host(app_id, session_id, actor_user_id)
        try:
            clean = DrawSettings.from_wire(settings)
        except DrawWireError as exc:
            raise AppDrawSettingsError() from exc
        seats = frozenset(e.seat for e in table.roster if e.seat)
        if any(a not in seats or b not in seats for a, b in clean.pairs):
            raise AppDrawPairsError()
        async with self._lock(session_id):
            st = await self._load(session_id)
            hub = st.get("hub")
            if hub is not None and hub["phase"] in _RUNNING:
                raise AppDrawStateError()
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
                S = [_raw(s) for s in cur["S"]]
                j = S.index(dc.public_of(_raw(cur["boxes"][user.username]["priv"])))
                _g, giver_pos = dc.circle_targets(j, rnd.n)
                sealed = dc.seal_card(S[giver_pos], card, ctx=rnd.ctx, rand=self._rand)
                await self._save(session_id, st)
                await self._send(
                    table,
                    table.hub_instance,
                    VERB_DRAW_WISH,
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
                # A draw that gave up starts afresh as the next draw.
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
        only (the app has no route to it: its frame can't reach the API)."""
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
    "WS_FRAME_TYPE",
    "AppDrawService",
    "AppGroupNotFoundError",
    "check_card",
    "check_list",
    "complaint_holds",
]
