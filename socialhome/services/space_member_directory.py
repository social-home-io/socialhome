"""Member key cards and the host-signed member directory (v_89).

Owner decision 2026-11-09: every space member is equal however its
household is reached, so ANY writer member — not only the host — must be
able to reach a household that reaches the space only through a GFS, and a
GFS-only household must be able to check who is talking to it. This service
does both on top of the household's ONE mailbox per GFS
(:class:`~socialhome.services.gfs_mailbox_service.GfsMailboxService`):

* **Cards.** Each household signs its own card (identity + key-wrap keys,
  its mailbox at the space's GFS or none —
  :mod:`socialhome.domain.member_card`) and gives it to the space's host
  once: inside the sealed redeem / ACK for a GFS-only seat, else as
  ``SPACE_MEMBER_CARD`` once the household learns the space has a
  GFS-reached member (it received a directory). A card is self-certifying:
  the host can withhold one, never forge or alter one.
* **Directory.** The host passes the cards on as ``SPACE_MAILBOX_DIRECTORY``
  (the envelope is host-signed; receivers take it from the space's host
  only), to WRITER-seat member households only (``only_instances``), never
  to followers: one bounded snapshot (≤ :data:`SNAPSHOT_MAX` cards) to a
  newcomer, a delta on every change after that. A household with a mailbox
  gets every writer's card (it checks every sender against them); one
  without gets only the cards that carry a mailbox (all it can use).
* **Seats.** On a directory card a member seats that household lazily — a
  ``space_session`` row from the card's key-wrap key, static-static keys,
  no round-trip, never downgrading a confirmed row — so its own fan-out
  (``broadcast_to_space_members``) reaches the household's mailbox
  directly, and frames from it verify.
* **Rows.** Everything lives on the pair's ``space_instances.mailbox`` JSON
  (migration 0144), keyed by (space, household): ``card`` (that
  household's verified card); on the host also ``snap`` (which directory
  that household was given: ``"all"`` / ``"mbx"``), and on the host's own
  row ``listed`` + ``own_rev``; on a member's row of the host, ``sent``
  (the card ``rev`` the host got).

Sends that a card change causes go out from the sweep (never in the same
tick as the anonymous register that changed the card), except the host's
answer to a card it just received and a member's answer to a directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, TypeGuard

import orjson

from ..canonical_json import canonical_bytes
from ..domain.events import SpaceRemoteSeatEnded, SpaceRemoteSeatLive
from ..domain.federation import (
    DELIVERY_ERROR_QUEUED,
    FederationEvent,
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from ..domain.federation_capabilities import OURS, FederationCapability
from ..domain.member_card import InvalidMemberCard, MemberCard, parse_card
from ..domain.space import SpaceRole
from ..federation.gfs_relay_transport import MailboxTarget
from ..federation.host_equivalence import acting_hosts, serves_as_host
from ..federation.member_card import build_card, verify_card
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.space_remote_member_repo import AbstractSpaceRemoteMemberRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_envelope_sender import normalize_gfs_base
from .gfs_mailbox_service import GfsMailboxService
from .space_writer_households import SpaceWriterHouseholds, holds_member_seat

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService
    from ..infrastructure.event_bus import EventBus

log = logging.getLogger(__name__)

#: Most cards one directory snapshot carries in all (and a receiver takes).
SNAPSHOT_MAX: int = 500
#: Most cards one directory envelope carries: ~0.7 KiB a card, so a part
#: stays well inside the GFS relay's envelope cap (~191 KiB).
SNAPSHOT_PART_MAX: int = 150
#: "Seated" cards kept aside until their seat row exists: at most this many,
#: each for at most this long (a seat commits right after its ACK).
EARLY_HELLO_MAX: int = 1000
EARLY_HELLO_TTL_S: float = 600.0
#: A household's "seated" card re-sends its snapshot at most this often.
HELLO_RESNAPSHOT_S: float = 600.0
#: Most household ids one snapshot's ``members`` part names: ~35 B each in
#: the JSON, so ~100 KiB — one GFS relay envelope after encryption and
#: sealing. The scale limit of the lost-removal repair.
MEMBERS_MAX: int = 2900
#: Most households one directory delta may remove (and a receiver takes).
REMOVED_MAX: int = 500
#: A delegated admin passes a card on to the owner again this often while
#: the owner's directory still does not list that household — at most
#: :data:`FORWARD_TRIES` times per card ``rev`` in all.
FORWARD_RETRY_S: int = 6 * 3600
FORWARD_TRIES: int = 4
#: Most cards one issuer's row keeps waiting for a roster seat (newest
#: kept): a card for a household our roster does not hold yet.
WAIT_MAX: int = 100

#: Seats a household lazily from its verified card.
Seater = Callable[[MemberCard], Awaitable[None]]
#: Told ``(space_id, instance_id)`` on the host when a member household says
#: it is seated (its "seated" card, or its first sync).
SeatedHook = Callable[[str, str], Awaitable[object]]


def _loads(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count(value: object) -> int:
    return value if _is_count(value) else 0


def _ids(raw: object) -> list[str]:
    return [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []


def _event_space(event: FederationEvent) -> str | None:
    """The space a card / directory event is about: its routing
    ``space_id`` or, for a mesh-routed one (which carries none in the
    clear), the copy inside its origin-authenticated encrypted payload. Both
    present must agree."""
    payload = event.payload if isinstance(event.payload, dict) else {}
    inner = payload.get("space_id")
    inner = inner if isinstance(inner, str) and inner else None
    if event.space_id and inner and inner != event.space_id:
        return None
    return event.space_id or inner


def _content_digest(card: MemberCard) -> str:
    """What a card says, its revision and signature aside."""
    wire = {k: v for k, v in card.to_wire().items() if k not in ("rev", "sig")}
    return hashlib.sha256(canonical_bytes(wire)).hexdigest()[:32]


def _card(doc: dict[str, Any]) -> MemberCard | None:
    """The card stored on a row (verified when it was stored), or ``None``."""
    raw = doc.get("card")
    if raw is None:
        return None
    try:
        return parse_card(raw)
    except InvalidMemberCard:
        return None


class SpaceMemberDirectory:
    """See the module docstring."""

    __slots__ = (
        "_asked_at",
        "_clock",
        "_early_hello",
        "_federation",
        "_federation_repo",
        "_hello_at",
        "_identity_pk",
        "_identity_seed",
        "_instance_id",
        "_keywrap_pk",
        "_keywrap_sig",
        "_lost_mailbox",
        "_mailboxes",
        "_on_seated",
        "_reconciling",
        "_seat_ender",
        "_seater",
        "_seats",
        "_snap_in",
        "_spaces",
        "_wall_clock",
        "_writers",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        federation_repo: AbstractFederationRepo,
        writer_households: SpaceWriterHouseholds,
        mailboxes: GfsMailboxService,
        instance_id: str,
        identity_seed: bytes,
        identity_pk: bytes,
        keywrap_pk: bytes,
        keywrap_sig: str,
        remote_member_repo: AbstractSpaceRemoteMemberRepo | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._spaces = space_repo
        self._federation_repo = federation_repo
        self._writers = writer_households
        #: The roster mirror: who holds an admin seat (the delegated
        #: inviting admin acts as host). Without it only the owner does.
        self._seats = remote_member_repo
        self._mailboxes = mailboxes
        self._instance_id = instance_id
        self._identity_seed = identity_seed
        self._identity_pk = identity_pk
        self._keywrap_pk = keywrap_pk
        self._keywrap_sig = keywrap_sig
        self._federation: FederationService | None = None
        self._seater: Seater | None = None
        self._seat_ender: Callable[[str], Awaitable[object]] | None = None
        self._on_seated: SeatedHook | None = None
        self._clock = clock
        self._wall_clock = wall_clock
        #: (space, household) → when its last "seated" card re-sent its
        #: snapshot (:data:`HELLO_RESNAPSHOT_S`).
        self._hello_at: dict[tuple[str, str], float] = {}
        #: (space, household) whose "seated" card arrived before its seat
        #: row (:meth:`store_seat_card` answers it).
        self._early_hello: dict[tuple[str, str], float] = {}
        #: (space, household) whose stored card just lost its mailbox.
        self._lost_mailbox: set[tuple[str, str]] = set()
        #: (space, issuer) → (``dir_seq``, part indices taken) of the
        #: snapshot coming in: it moves our ``dir_seq`` of that issuer only
        #: once every part arrived.
        self._snap_in: dict[tuple[str, str], tuple[int, frozenset[int]]] = {}
        #: (space, host) → when we last asked that host for the directory
        #: again (:meth:`ask_host`, :data:`HELLO_RESNAPSHOT_S`).
        self._asked_at: dict[tuple[str, str], float] = {}
        #: space → held while :meth:`reconcile` versions and sends its
        #: directory, so the frames to a household go out in version order.
        self._reconciling: dict[str, asyncio.Lock] = {}
        mailboxes.attach_needed(self.needed_gfs)
        mailboxes.attach_after_sweep(self.sweep_once)
        mailboxes.attach_on_change(self.on_own_change)

    def attach_to(self, federation_service: FederationService) -> None:
        """Both events: a household acting as host takes
        ``SPACE_MEMBER_CARD``, members take ``SPACE_MAILBOX_DIRECTORY``."""
        self._federation = federation_service
        registry = federation_service._event_registry
        registry.register(FederationEventType.SPACE_MEMBER_CARD, self._on_member_card)
        registry.register(
            FederationEventType.SPACE_MAILBOX_DIRECTORY, self._on_directory
        )

    def wire(self, bus: EventBus) -> None:
        """A seat that goes live or ends: a waiting card becomes usable or
        an orphaned lazy seat ends (:meth:`_on_seat_change`), and in a space
        we act as host of the directory is brought up to date (snapshot to
        the newcomer, ``removed`` to the rest); role changes are the
        sweep's."""
        bus.subscribe(SpaceRemoteSeatLive, self._on_seat_change)
        bus.subscribe(SpaceRemoteSeatEnded, self._on_seat_change)

    async def _on_seat_change(
        self, event: SpaceRemoteSeatLive | SpaceRemoteSeatEnded
    ) -> None:
        """The ROSTER decides who is a member; the directory only supplies
        cards. A seat that went live makes a card that waited for it usable
        now; a household whose last seat ended loses its lazy seat (its row
        and card go with the roster's row)."""
        try:
            space_id, inst = event.space_id, event.instance_id
            if isinstance(event, SpaceRemoteSeatLive):
                await self._adopt(space_id, inst)
            elif self._seat_ender is not None and inst != self._instance_id:
                await self._seat_ender(inst)
            if await self.serves(space_id):
                await self.reconcile(space_id)
        except Exception:
            log.warning(
                "directory: reconcile after a seat change failed", exc_info=True
            )

    def attach_seat_ender(self, ender: Callable[[str], Awaitable[object]]) -> None:
        """How a household that left the directory loses its seat once no
        shared space justifies it (``SpaceService
        .end_space_session_if_unused``)."""
        self._seat_ender = ender

    def attach_seater(self, seater: Seater) -> None:
        """How a directory card becomes a lazy ``space_session`` seat (the
        redeem coordinator's seat path)."""
        self._seater = seater

    def attach_on_seated(self, hook: SeatedHook) -> None:
        """Who else answers a member household's "I am seated" on the host
        (``GfsChannelService.on_member_seated``). Before it, the household's
        mailbox gate refuses the host's frames for the space (its person is
        not seated yet), so anything sent when its seat went live may be
        lost — the private channel grant is sent again."""
        self._on_seated = hook

    async def _seated(self, space_id: str, instance_id: str) -> None:
        """Tell the seated hook. Fail-soft."""
        if self._on_seated is None:
            return
        try:
            await self._on_seated(space_id, instance_id)
        except Exception:
            log.warning("directory: the seated hook failed", exc_info=True)

    # ── Rows ─────────────────────────────────────────────────────────────

    async def _doc(self, space_id: str, instance_id: str) -> dict[str, Any]:
        return _loads(
            await self._spaces.get_space_instance_mailbox(space_id, instance_id)
        )

    async def _put(self, space_id: str, instance_id: str, doc: dict[str, Any]) -> None:
        """Write a row's document (``None`` when it holds nothing). The
        write is UPDATE-only: it never creates membership."""
        value = orjson.dumps(doc).decode() if doc else None
        await self._spaces.set_space_instance_mailbox(space_id, instance_id, value)

    async def _rows(self) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for space, inst, raw in await self._spaces.list_space_instance_mailboxes():
            out[(space, inst)] = _loads(raw)
        return out

    async def _space_cards(self, space_id: str) -> dict[str, MemberCard]:
        out: dict[str, MemberCard] = {}
        for (space, inst), doc in (await self._rows()).items():
            if space != space_id or inst == self._instance_id:
                continue
            card = _card(doc)
            if card is not None and card.instance_id == inst:
                out[inst] = card
        return out

    async def card_of(self, space_id: str, instance_id: str) -> MemberCard | None:
        """The verified card of *instance_id* we hold for *space_id*."""
        card = _card(await self._doc(space_id, instance_id))
        return card if card is not None and card.instance_id == instance_id else None

    async def _store(self, space_id: str, card: MemberCard) -> bool:
        """Put a VERIFIED *card* on its household's row of *space_id* unless
        we hold one as new or newer. Returns whether it changed. The row is
        the ROSTER's (a live seat of that household): a card never creates
        one — without it the card waits (:meth:`_on_directory`)."""
        inst = card.instance_id
        if inst == self._instance_id:
            return False
        if inst not in await self._spaces.list_member_instances(space_id):
            return False  # no row to hold it
        doc = await self._doc(space_id, inst)
        old = _card(doc)
        if old is not None and old.rev >= card.rev:
            return False
        doc["card"] = card.to_wire()
        await self._put(space_id, inst, doc)
        if old is not None and old.mailbox is not None and card.mailbox is None:
            # Its mailbox is gone: households given only mailbox cards must
            # hear it too, or they keep sending to a dead address.
            self._lost_mailbox.add((space_id, inst))
        if old is None or card.proto_version != old.proto_version:
            # A directory seat learns the household's version from its card
            # (it gets no capabilities fan-out).
            row = await self._federation_repo.get_instance(inst)
            if (
                row is not None
                and row.source is InstanceSource.SPACE_SESSION
                and not row.relay_via
            ):
                await self._federation_repo.set_proto_version(inst, card.proto_version)
        return True

    async def _clear(self, space_id: str, instance_id: str) -> None:
        doc = await self._doc(space_id, instance_id)
        if doc.pop("card", None) is not None:
            await self._put(space_id, instance_id, doc)

    async def _in_use(self, space_id: str) -> bool:
        """A person of ours sits in *space_id* and it still exists (not
        dissolved, not remote-terminated)."""
        space = await self._spaces.get(space_id)
        if space is None or space.dissolved:
            return False
        if space.archived and space.archived_reason in ("dissolved", "removed"):
            return False
        return bool(await self._spaces.list_local_member_user_ids(space_id))

    # ── Who is "the host" ────────────────────────────────────────────────
    #
    # One notion, :mod:`socialhome.federation.host_equivalence`: the owner
    # household and — on a household a delegated admin invited — that
    # admin while the trust holds. Nothing below compares a household to
    # ``owner_instance_id`` to decide whom to listen to.

    async def hosts_of(self, space_id: str) -> list[str]:
        """The households acting as *space_id*'s host HERE, owner first
        (:func:`acting_hosts`) — us included when we own it."""
        space = await self._spaces.get(space_id)
        if space is None or not space.owner_instance_id:
            return []
        return await acting_hosts(
            self._spaces, self._seats, space, own_instance_id=self._instance_id
        )

    async def _hosts(self, space_id: str) -> list[str]:
        """:meth:`hosts_of` without us: who we take a directory from and
        give our card to."""
        return [h for h in await self.hosts_of(space_id) if h != self._instance_id]

    async def serves(self, space_id: str) -> bool:
        """Whether WE act as *space_id*'s host toward other members: its
        owner, or a delegated admin household (:func:`serves_as_host`). A
        delegated admin keeps a directory only for the households that come
        to it — those that take it as their host (it invited them)."""
        space = await self._spaces.get(space_id)
        return await serves_as_host(
            self._spaces, space, own_instance_id=self._instance_id
        )

    async def _owns(self, space_id: str) -> bool:
        space = await self._spaces.get(space_id)
        return space is not None and space.owner_instance_id == self._instance_id

    async def hop_hosts(self, space_id: str, target: str) -> list[str]:
        """The households a host hop of *space_id* toward *target* may go
        through, in the order to try them — only ones that hold BOTH ends'
        cards and that *target* takes as its host (it drops a hop through
        anybody else):

        * the **owner** — every member takes it — when we hold a link of our
          own to it (a pairing, or the sealed-redeem seat it gave us; a seat
          made from a directory card is no hop leg) and its directory lists
          the target;
        * the **delegated admin that invited us**, for a target that admin
          told us it seated itself (``via`` on the card's row, from its
          ``via_seated`` list): such a household takes it as host too.

        Empty: no hop — the send fails and says so."""
        hosts = await self._hosts(space_id)
        doc = await self._doc(space_id, target)
        if not hosts or "card" not in doc:
            return []
        acting = await self.hosts_of(space_id)
        owner = acting[0]
        out: list[str] = []
        if owner in hosts and owner != target and owner in self._listers(doc, acting):
            row = await self._federation_repo.get_instance(owner)
            if row is not None and not (
                row.source is InstanceSource.SPACE_SESSION and not row.relay_via
            ):
                out.append(owner)
        via = doc.get("via")
        for host in hosts:
            if host != owner and host != target and via == host:
                out.append(host)
        return out

    @staticmethod
    def _listers(doc: dict[str, Any], hosts: list[str]) -> set[str]:
        """Which hosts' directories list the household of row *doc*: the
        issuers recorded with its card (``by``); a card stored before
        issuers were recorded, or handed over at seat time, counts as the
        owner's (*hosts* is owner first)."""
        by = doc.get("by")
        if isinstance(by, list):
            return {i for i in by if isinstance(i, str)}
        if doc.get("direct") is True:
            return set()  # given to us by the household itself: nobody's listing
        return set(hosts[:1])

    def _still_listed(self, doc: dict[str, Any], hosts: list[str]) -> bool:
        """A household's card still counts: it gave it to us itself while
        we act as host (``direct``), or a household acting as host here
        (*hosts*, :meth:`hosts_of`) lists it."""
        if doc.get("direct") is True:
            return True
        return bool(self._listers(doc, hosts) & set(hosts))

    # ── Our card ─────────────────────────────────────────────────────────

    async def _space_gfs(self, space_id: str) -> set[str]:
        """The GFSs WE reach *space_id* through: the relay of a sealed-redeem
        seat between us and a member household of it (a GFS-only member we
        host, or the host we joined by link) — normalized. A mailbox only
        another member's card names counts only for a GFS-reached member
        (:meth:`_member_gfs`)."""
        urls: set[str] = set()
        for inst in await self._space_cards(space_id):
            instance = await self._federation_repo.get_instance(inst)
            if (
                instance is not None
                and instance.source is InstanceSource.SPACE_SESSION
                and instance.relay_via
            ):
                urls.add(normalize_gfs_base(instance.relay_via))
        return urls

    async def _member_gfs(self, space_id: str) -> set[str]:
        """The GFSs of *space_id*'s GFS-REACHED members (the host's
        directory flags them, ``gfs_reached``) — normalized: a member
        household connected to one holds its ONE mailbox there, so such a
        member reaches it directly, with the host offline. Only a flagged
        card counts: two paired members' mailboxes never keep each other
        alive once the GFS-reached member is gone."""
        urls: set[str] = set()
        for (space, inst), doc in (await self._rows()).items():
            if space != space_id or doc.get("gfs_reached") is not True:
                continue
            card = _card(doc)
            if card is not None and card.instance_id == inst and card.mailbox:
                urls.add(normalize_gfs_base(card.mailbox.gfs))
        return urls

    async def _reached_by_gfs(self, instance_id: str) -> bool:
        """Host side: *instance_id* reaches us only through a GFS (its
        sealed-redeem seat with a relay)."""
        row = await self._federation_repo.get_instance(instance_id)
        return (
            row is not None
            and row.source is InstanceSource.SPACE_SESSION
            and bool(row.relay_via)
        )

    def _card_rev(self) -> int:
        """Our card's revision: the mailbox service's (it moves with every
        mailbox change and upgrade), at least 1."""
        return max(self._mailboxes.rev, 1)

    async def own_card(
        self, space_id: str, *, gfs_url: str = "", rev: int | None = None
    ) -> MemberCard:
        """Our signed card for *space_id*: with our mailbox at *gfs_url* (or,
        without one given, at a GFS the space is reached through, ours or a
        GFS-reached member's), else no mailbox. *rev* overrides the card
        revision (a card whose content moved under the same mailbox ``rev``)."""
        mailbox = None
        candidates = (
            [normalize_gfs_base(gfs_url)]
            if gfs_url
            else sorted(
                await self._space_gfs(space_id) | await self._member_gfs(space_id)
            )
        )
        for url in candidates:
            got = self._mailboxes.mailbox_at(url)
            if got is not None:
                mailbox = (got[0], got[1].gfs_url)
                break
        return build_card(
            instance_id=self._instance_id,
            identity_seed=self._identity_seed,
            identity_pk=self._identity_pk,
            keywrap_pk=self._keywrap_pk,
            keywrap_sig=self._keywrap_sig,
            proto_version=OURS,
            rev=self._card_rev() if rev is None else rev,
            mailbox=mailbox,
        )

    async def needed_gfs(self) -> set[str]:
        """Every GFS we need a mailbox at: one a space we are in is reached
        through by US (:meth:`_space_gfs`), or one a GFS-reached member of
        it is reached at (:meth:`_member_gfs`) — released once none is
        left."""
        urls: set[str] = set()
        for space in sorted({space for (space, _i) in await self._rows()}):
            if await self._in_use(space):
                urls |= await self._space_gfs(space)
                urls |= await self._member_gfs(space)
        return urls

    # ── Seat time (sealed redeem / ACK) ──────────────────────────────────

    @staticmethod
    def verify_seat_card(
        raw: object, *, instance_id: str, gfs_url: str
    ) -> MemberCard | None:
        """The card a counterpart handed over in a sealed redeem / ACK,
        verified: its own (``instance_id``), and a mailbox only at the GFS
        the body came through (*gfs_url*: a peer may not steer our frames to
        another GFS). ``None`` when absent or refused."""
        if raw is None:
            return None
        try:
            card = verify_card(raw, expected_instance_id=instance_id)
            if card.mailbox is not None and normalize_gfs_base(
                card.mailbox.gfs
            ) != normalize_gfs_base(gfs_url):
                raise InvalidMemberCard("card names another GFS than this leg's")
        except InvalidMemberCard as exc:
            log.info("directory: ignoring a seat card from %s: %s", instance_id, exc)
            return None
        return card

    async def store_seat_card(self, space_id: str, card: MemberCard) -> None:
        """Store a verified seat card once the seat row exists; a household
        acting as host then brings the space's directory up to date.
        Fail-soft."""
        try:
            await self._store(space_id, card)
            if await self.serves(space_id):
                await self._mark_direct(space_id, card.instance_id)
                # Its "seated" card came before our seat row (the commit
                # follows the ACK) and was kept aside: it is answered now. No
                # snapshot otherwise — one sent now could beat the joiner's
                # own seat; its card, or its first sync behind on
                # ``dir_seq``, asks for one.
                expires = self._early_hello.pop((space_id, card.instance_id), None)
                early = expires is not None and expires > self._clock()
                if early:
                    await self._mark_seated(space_id, card.instance_id)
                await self.reconcile(
                    space_id,
                    changed=[card],
                    snapshot_to={card.instance_id} if early else set(),
                )
                if early:
                    await self._seated(space_id, card.instance_id)
        except Exception:
            log.warning("directory: storing a seat card failed", exc_info=True)

    async def _mark_sent(self, space_id: str, host_id: str, card: MemberCard) -> None:
        doc = await self._doc(space_id, host_id)
        doc["sent"] = card.rev
        doc["sent_digest"] = _content_digest(card)
        await self._put(space_id, host_id, doc)

    async def seated(self, space_id: str, host_id: str, *, host_proto: int) -> None:
        """We were just seated in *space_id* by its host: give the host our
        card now — it is our "seated", which its first snapshot answers.
        *host_proto* is the host's version from its ACK, kept on its row (a
        mesh-only host has no row we could read it from)."""
        try:
            doc = await self._doc(space_id, host_id)
            doc["host_proto"] = host_proto
            await self._put(space_id, host_id, doc)
            await self._publish_to_host(space_id, host_id)
        except Exception:
            log.warning("directory: announcing our seat failed", exc_info=True)

    # ── Member: publish our card, take the directory ─────────────────────

    async def publish_cards(self) -> int:
        """Give our current card to every household acting as host of a
        space we are in (owner first, then the delegated admin that invited
        us) that is ≥ v_89 and does not have this ``rev`` yet — once per
        space, host and ``rev``: it is also how that host learns we are
        seated (its first snapshot answers it). Returns how many went out."""
        if self._federation is None:
            return 0
        sent = 0
        for space in sorted(await self._spaces.list_all(), key=lambda sp: sp.id):
            sent += await self._publish_to_hosts(space.id)
        return sent

    async def _publish_to_hosts(self, space_id: str) -> int:
        hosts = await self._hosts(space_id)
        # A writer seat of ours only: followers get no directory.
        if not hosts or not await holds_member_seat(self._spaces, space_id):
            return 0
        sent = 0
        for host in hosts:
            if await self._publish_to_host(space_id, host):
                sent += 1
        return sent

    async def _publish_to_host(
        self,
        space_id: str,
        host: str,
        *,
        host_is_v89: bool = False,
        again: bool = False,
    ) -> bool:
        """*host_is_v89*: the host just sent us its directory (a mesh-only
        host has no row we could read its version from). *again*: send our
        card even when the host has it (:meth:`ask_host`)."""
        assert self._federation is not None
        doc = await self._doc(space_id, host)
        sent = _count(doc.get("sent"))
        card = await self.own_card(space_id)
        if sent >= card.rev:
            if doc.get("sent_digest") != _content_digest(card):
                # Same mailbox ``rev``, other content (a mailbox we hold for
                # another space now applies here): a newer rev, or the host
                # keeps the old card.
                card = await self.own_card(space_id, rev=sent + 1)
            elif not again:
                return False
        v89 = FederationCapability.MIN_FOR_MAILBOX_MEMBER_MESH
        known = doc.get("host_proto")
        if host_is_v89:
            if not (isinstance(known, int) and known >= v89):
                doc["host_proto"] = v89
                await self._put(space_id, host, doc)
        elif not (
            isinstance(known, int) and known >= v89
        ) and not await self._federation.space_member_supports(host, min_version=v89):
            return False
        try:
            result = await self._federation.send_with_mesh_fallback(
                to_instance_id=host,
                event_type=FederationEventType.SPACE_MEMBER_CARD,
                # The space rides the encrypted payload too: a mesh-routed
                # envelope carries no plaintext routing ``space_id``.
                payload={"space_id": space_id, "card": card.to_wire()},
                space_id=space_id,
            )
        except Exception:
            log.warning("directory: sending our card failed", exc_info=True)
            return False
        if not result.ok and result.error != DELIVERY_ERROR_QUEUED:
            return False
        # Queued in the durable outbox counts: it redelivers on its own, and
        # a copy per sweep while the host is away would pile up.
        await self._mark_sent(space_id, host, card)
        return True

    async def _on_directory(self, event: FederationEvent) -> None:
        """A member directory for one space from a household acting as its
        host here — the owner, or the delegated admin that invited us
        (:meth:`hosts_of`): verify every card (self-certifying), store it,
        seat its household lazily, and answer with our own card if that host
        lacks it. Each issuer has its own version line (``dir_seq`` on its
        row) and a card remembers who lists it (``by``).

        The directory supplies cards — keys and addresses — and nothing
        else: who is a member is the ROSTER's to say. A card goes on its
        household's ``space_instances`` row, which only the roster creates
        and removes (a live seat); for a household without one the card
        waits on the issuer's row (``wait``) until the seat arrives
        (:meth:`_adopt`). A ``removed`` only withdraws that issuer's listing
        of a card: the card goes when no household acting as host lists it.
        Every valid frame moves the issuer's version."""
        space_id = _event_space(event)
        if not space_id:
            return
        issuer = event.from_instance
        acting = await self.hosts_of(space_id)
        hosts = [h for h in acting if h != self._instance_id]
        if not issuer or issuer not in hosts:
            log.info("directory: ignoring a directory not from the space's host")
            if issuer and "dir_seq" in await self._doc(space_id, issuer):
                # A household whose trust just ended (delegation off, its
                # admin seat gone, a rotation it did not follow): what only
                # it listed goes now, not at the next sweep.
                await self._drop_lapsed(space_id)
            return
        if not await self._in_use(space_id):
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        seq = payload.get("dir_seq")
        snapshot = payload.get("snapshot") is True
        if not _is_count(seq):
            log.info("directory: ignoring a directory without a version")
            return
        host_doc = await self._doc(space_id, issuer)
        held = _count(host_doc.get("dir_seq"))
        # One version per directory change: a delta must be newer than what
        # we hold (a late old one never re-adds a removed household); the
        # parts of one snapshot share their version.
        if seq < held or (seq == held and not snapshot):
            log.info("directory: ignoring an out-of-date directory")
            return
        if not snapshot and payload.get("base_seq") != held:
            # A delta builds on one version: taken on another (a snapshot
            # still coming in, or lost) it would move us past what we never
            # got. We stay behind and ask that host again (a snapshot still
            # coming in: the host does not answer twice in a row).
            log.info("directory: ignoring a delta on a version we do not hold")
            await self._ask(space_id, issuer)
            return
        coming = self._snap_in.get((space_id, issuer))
        if snapshot and coming is not None and seq < coming[0]:
            log.info("directory: ignoring a part of an older snapshot")
            return
        parts = payload.get("parts")
        if snapshot and _is_count(parts) and parts > SNAPSHOT_MAX + 1:
            log.info("directory: ignoring an oversized directory")
            return
        cards_raw = payload.get("cards")
        cards_raw = cards_raw if isinstance(cards_raw, list) else []
        removed = _ids(payload.get("removed"))
        reached = set(_ids(payload.get("gfs_reached")))
        has_members = snapshot and isinstance(payload.get("members"), list)
        members = _ids(payload.get("members")) if has_members else []
        if (
            len(cards_raw) > SNAPSHOT_MAX
            or len(reached) > SNAPSHOT_MAX
            or len(removed) > REMOVED_MAX
            or len(members) > MEMBERS_MAX
        ):
            log.info("directory: ignoring an oversized directory")
            return
        cards: dict[str, MemberCard] = {}
        for raw in cards_raw:
            try:
                card = verify_card(raw)
            except InvalidMemberCard as exc:
                log.info("directory: skipping a card that does not verify: %s", exc)
                continue
            if card.instance_id != self._instance_id:
                cards[card.instance_id] = card
        gone = set(removed)
        listed = set(members) | set(cards)
        relist = False
        if has_members:
            # The issuer's whole directory: a card it listed before and does
            # not list now loses that listing. A snapshot without
            # ``members`` (too big to name them all) drops nothing.
            for (space, inst), doc in (await self._rows()).items():
                if space != space_id or inst in listed or "card" not in doc:
                    continue
                if issuer in self._listers(doc, acting):
                    gone.add(inst)
                elif issuer == acting[0] and doc.pop("fwd_n", None) is not None:
                    # A card we passed on to the owner and its whole
                    # directory still lacks: passed on again (bounded: once
                    # per owner snapshot).
                    await self._put(space_id, inst, doc)
                    relist = True
        # Never us, never a household acting as host here.
        gone -= {self._instance_id, *hosts}
        for inst in sorted(gone):
            await self._unlist(space_id, inst, issuer, acting)
        via = (
            set()
            if issuer == acting[0]
            else set(_ids(payload.get("via_seated"))) & set(cards)
        )
        rows = set(await self._spaces.list_member_instances(space_id))
        news: list[MemberCard] = []
        waiting: dict[str, Any] = {}
        for card in cards.values():
            inst = card.instance_id
            if inst not in rows:
                # No seat of that household in our roster (yet).
                waiting[inst] = {
                    "card": card.to_wire(),
                    "reached": inst in reached,
                    "via": inst in via,
                }
                continue
            fresh = "card" not in await self._doc(space_id, inst)
            if await self._store(space_id, card):
                news.append(card)
            await self._listed_by(
                space_id,
                inst,
                issuer,
                inst in reached,
                acting,
                fresh=fresh,
                seated=inst in via,
            )
        boxes = self._mailboxes.mailbox_gfs_urls()
        if any(
            c.mailbox is not None
            and c.instance_id in reached
            and normalize_gfs_base(c.mailbox.gfs) not in boxes
            for c in cards.values()
        ):
            # A GFS-reached member at a GFS we hold no mailbox at: ours is
            # minted now (in the background) and its card goes out at once.
            self._mailboxes.mint_soon()
        host_doc = await self._doc(space_id, issuer)
        wait = host_doc.get("wait")
        wait = dict(wait) if isinstance(wait, dict) else {}
        for inst in list(wait):
            if inst in gone or inst in rows or (has_members and inst not in listed):
                del wait[inst]
        wait.update(waiting)
        if wait:
            host_doc["wait"] = dict(list(wait.items())[-WAIT_MAX:])
        else:
            host_doc.pop("wait", None)
        if self._complete(space_id, issuer, payload, seq):
            host_doc["dir_seq"] = seq
        await self._put(space_id, issuer, host_doc)
        for card in cards.values():
            if card.instance_id != issuer and card.instance_id in rows:
                await self._seat(card)
        await self._publish_to_host(space_id, issuer, host_is_v89=True)
        # Another host's card may just have arrived (the owner's, in the
        # directory of the admin that invited us): it gets our card too.
        await self._publish_to_hosts(space_id)
        if (news or gone or relist) and await self.serves(space_id):
            # We act as host for the households we invited: what the owner
            # just told us goes on to them.
            await self.reconcile(space_id, changed=news)

    async def _adopt(self, space_id: str, instance_id: str) -> None:
        """A seat of *instance_id* just went live in our roster: a card a
        host's directory gave us before (``wait`` on that host's row) goes
        on the household's row now — the row the roster creates for a live
        seat — and its lazy seat is made."""
        acting = await self.hosts_of(space_id)
        for issuer in acting:
            if issuer == self._instance_id:
                continue
            doc = await self._doc(space_id, issuer)
            wait = doc.get("wait")
            entry = wait.pop(instance_id, None) if isinstance(wait, dict) else None
            if not isinstance(entry, dict):
                continue
            if not wait:
                doc.pop("wait", None)
            await self._put(space_id, issuer, doc)
            try:
                card = parse_card(entry.get("card"))
            except InvalidMemberCard:
                continue
            await self._spaces.add_space_instance(space_id, instance_id)
            fresh = "card" not in await self._doc(space_id, instance_id)
            await self._store(space_id, card)
            await self._listed_by(
                space_id,
                instance_id,
                issuer,
                entry.get("reached") is True,
                acting,
                fresh=fresh,
                seated=entry.get("via") is True,
            )
            held = await self.card_of(space_id, instance_id)
            if held is not None:
                await self._seat(held)

    async def _listed_by(
        self,
        space_id: str,
        instance_id: str,
        issuer: str,
        reached: bool,
        hosts: list[str],
        *,
        fresh: bool,
        seated: bool = False,
    ) -> None:
        """Note on a card's row that *issuer* lists it, and keep that
        host's ``gfs_reached`` flag (sent with each card, changed or not).
        *fresh*: we held no card of that household before this frame, so
        nobody else lists it. *seated*: the issuer — a delegated admin —
        says it seated that household itself (``via``: a host hop toward it
        may go through that admin)."""
        if instance_id == self._instance_id:
            return
        doc = await self._doc(space_id, instance_id)
        if "card" not in doc:
            return
        by = set() if fresh else self._listers(doc, hosts)
        flagged = doc.get("gfs_reached") is True
        via_ok = doc.get("via") == issuer if seated else doc.get("via") != issuer
        if "by" in doc and issuer in by and flagged == reached and via_ok:
            return
        doc["by"] = sorted(by | {issuer})
        if seated:
            doc["via"] = issuer
        elif doc.get("via") == issuer:
            doc.pop("via", None)
        if reached:
            doc["gfs_reached"] = True
        else:
            doc.pop("gfs_reached", None)
        await self._put(space_id, instance_id, doc)

    async def _unlist(
        self, space_id: str, instance_id: str, issuer: str, hosts: list[str]
    ) -> None:
        """*issuer* no longer lists *instance_id*: it only withdraws its own
        listing of the card. The card goes (:meth:`_drop_card`) when no
        household acting as host lists it any more and the household did
        not give it to us itself. The row is the roster's — never touched."""
        doc = await self._doc(space_id, instance_id)
        if "card" not in doc:
            return
        doc["by"] = sorted(self._listers(doc, hosts) - {issuer})
        if doc.get("via") == issuer:
            doc.pop("via", None)
        if self._still_listed(doc, hosts):
            await self._put(space_id, instance_id, doc)
            return
        await self._put(space_id, instance_id, doc)
        await self._drop_card(space_id, instance_id)

    async def _drop_lapsed(self, space_id: str) -> None:
        """The trust in a host ended, or we stopped acting as one: its
        version line goes (a resumed trust starts from a fresh snapshot)
        and so does every household only it listed — the owner's directory
        is what counts again."""
        hosts = await self.hosts_of(space_id)
        if not hosts:
            return
        serving = await self.serves(space_id)
        for (space, inst), doc in sorted((await self._rows()).items()):
            if space != space_id or inst == self._instance_id:
                continue
            changed = False
            if inst not in hosts and ("dir_seq" in doc or "sent" in doc):
                for key in ("dir_seq", "sent", "sent_digest", "host_proto", "wait"):
                    doc.pop(key, None)
                self._snap_in.pop((space_id, inst), None)
                changed = True
            if not serving and doc.pop("direct", None) is not None:
                for key in ("fwd", "fwd_at", "fwd_n"):
                    doc.pop(key, None)
                changed = True
            if (
                "card" in doc
                and inst not in hosts
                and not self._still_listed(doc, hosts)
            ):
                doc.pop("card", None)
                changed = True
                await self._end_seat(inst)
            if changed:
                await self._put(space_id, inst, doc)

    def _complete(
        self, space_id: str, issuer: str, payload: dict[str, Any], seq: int
    ) -> bool:
        """Whether this frame completes a directory version: a delta does;
        a snapshot once every one of its ``parts`` (by ``part`` index)
        arrived, in any order — else our ``dir_seq`` stays behind and the
        next §25.6 sync echo gets a fresh snapshot."""
        if payload.get("snapshot") is not True:
            return True
        part, parts = payload.get("part"), payload.get("parts")
        if not (_is_count(part) and _is_count(parts) and part < parts):
            return False
        key = (space_id, issuer)
        got_seq, got = self._snap_in.get(key, (seq, frozenset()))
        have = (got if got_seq == seq else frozenset()) | {part}
        if len(have) < parts:
            self._snap_in[key] = (seq, have)
            return False
        self._snap_in.pop(key, None)
        return True

    async def _drop_card(self, space_id: str, instance_id: str) -> None:
        """No household acting as host lists *instance_id*'s card any more
        (or we left the space): the card goes — nothing is sent to or taken
        from it on its strength — and its lazy seat ends once no space we
        share justifies it. Its ``space_instances`` row stays the roster's."""
        await self._clear(space_id, instance_id)
        await self._end_seat(instance_id)

    async def _end_seat(self, instance_id: str) -> None:
        if self._seat_ender is None:
            return
        try:
            await self._seat_ender(instance_id)
        except Exception:
            log.warning("directory: ending a member's seat failed", exc_info=True)

    async def _seat(self, card: MemberCard) -> None:
        """A lazy ``space_session`` seat for *card*'s household (one with a
        mailbox, or any once we hold a mailbox ourselves) — never replacing
        a confirmed row of ours (a pairing, or a seat)."""
        if self._seater is None:
            return
        if card.mailbox is None and not self._mailboxes.mailbox_gfs_urls():
            return
        existing = await self._federation_repo.get_instance(card.instance_id)
        if existing is not None and existing.status is PairingStatus.CONFIRMED:
            return
        try:
            await self._seater(card)
        except Exception:
            log.warning("directory: seating a member household failed", exc_info=True)

    # ── Host: take cards, keep the directory ─────────────────────────────

    async def _on_member_card(self, event: FederationEvent) -> None:
        """A member household gives us — acting as the space's host — its
        card for one space; or, on the owner, a delegated admin passes on
        the card of a household it seated (:meth:`_on_forwarded_card`)."""
        space_id = _event_space(event)
        sender = event.from_instance
        if not space_id or not await self.serves(space_id):
            return
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("forwarded") is True:
            await self._on_forwarded_card(space_id, sender, payload)
            return
        if sender not in await self._spaces.list_member_instances(space_id):
            # A joiner's "seated" card can beat our seat row (the commit
            # follows the ACK): kept aside, answered when the seat's card is
            # stored (:meth:`store_seat_card`). Bounded.
            now = self._clock()
            self._early_hello = {k: t for k, t in self._early_hello.items() if t > now}
            if len(self._early_hello) < EARLY_HELLO_MAX:
                self._early_hello[(space_id, sender)] = now + EARLY_HELLO_TTL_S
            return
        try:
            card = verify_card(payload.get("card"), expected_instance_id=sender)
        except InvalidMemberCard as exc:
            log.info("directory: ignoring a card from %s: %s", sender, exc)
            return
        old = await self.card_of(space_id, sender)
        changed = await self._store(space_id, card)
        await self._mark_direct(space_id, sender)
        await self._mark_seated(space_id, sender)
        # A first card, or the same card again, is the household's "I am
        # seated": it gets a snapshot (at most once per
        # :data:`HELLO_RESNAPSHOT_S`). A NEWER card (a rotation) is only a
        # delta.
        snapshot_to = (
            {sender}
            if (old is None or not changed) and self._may_resnapshot(space_id, sender)
            else set()
        )
        await self.reconcile(
            space_id, changed=[card] if changed else [], snapshot_to=snapshot_to
        )
        if snapshot_to:
            await self._seated(space_id, sender)

    async def _mark_direct(self, space_id: str, instance_id: str) -> None:
        """The household gave us its card ITSELF while we act as host (its
        seat card, or a ``SPACE_MEMBER_CARD``): no other host's directory
        takes it away, and — on a delegated admin — it is passed on to the
        owner (:meth:`_forward_to_owner`)."""
        doc = await self._doc(space_id, instance_id)
        if "card" in doc and doc.get("direct") is not True:
            doc["direct"] = True
            await self._put(space_id, instance_id, doc)

    async def _on_forwarded_card(
        self, space_id: str, sender: str, payload: dict[str, Any]
    ) -> None:
        """Owner side: a delegated admin household passes on the card of a
        household it seated — one that reaches the space through that admin
        and cannot give us its card before we list it. Taken only on the
        space's owner, only from a household holding a live admin seat while
        delegation is on, and only for a household our OWN roster already
        names a member household. The card is self-certifying: the admin
        carries it, it cannot forge or alter it."""
        if not await self._owns(space_id) or not await self._is_delegated_admin(
            space_id, sender
        ):
            log.info("directory: ignoring a forwarded card not from our admin")
            return
        try:
            card = verify_card(payload.get("card"))
        except InvalidMemberCard as exc:
            log.info("directory: ignoring a forwarded card: %s", exc)
            return
        inst = card.instance_id
        if inst in (self._instance_id, sender):
            return
        if inst not in await self._spaces.list_member_instances(space_id):
            return
        changed = await self._store(space_id, card)
        # We hold no seat of our own for it (the admin seated it): a lazy
        # one from its card, as a member makes from a directory card — its
        # frames verify, and ours reach its mailbox.
        await self._seat(card)
        # Whether that household reaches the space only through a GFS is
        # the seating admin's to say (ours is a card-only seat of it).
        doc = await self._doc(space_id, inst)
        flag = payload.get("gfs_reached") is True
        if "card" in doc and (doc.get("gfs_reached") is True) != flag:
            if flag:
                doc["gfs_reached"] = True
            else:
                doc.pop("gfs_reached", None)
            await self._put(space_id, inst, doc)
            changed = True
        if changed:
            await self.reconcile(space_id, changed=[card])

    async def _is_delegated_admin(self, space_id: str, instance_id: str) -> bool:
        """By OUR rows: delegation is on and *instance_id* holds a live
        admin seat in *space_id*."""
        space = await self._spaces.get(space_id)
        features = getattr(space, "features", None)
        if self._seats is None or not getattr(
            features, "delegated_admin_authority", False
        ):
            return False
        seats = await self._seats.list_for_instance(
            space_id, instance_id, include_tombstoned=False
        )
        return any(seat.role == SpaceRole.ADMIN.value for seat in seats)

    async def _forward_to_owner(
        self, space_id: str, cards: dict[str, MemberCard]
    ) -> None:
        """Delegated admin side: pass each card a household gave US itself
        on to the space's owner while the owner's directory does not list
        that household — the owner then lists it for every member, and
        until then that household's own card to the owner is refused (the
        owner holds no card to check it against). One small frame to one
        household per newcomer and card ``rev`` when the card arrives or
        changes, and again every :data:`FORWARD_RETRY_S` while the owner
        still does not list it (a lost frame) — :data:`FORWARD_TRIES` times
        per ``rev`` in all, then it stops (an owner below this build drops
        the frame and never lists it). Only in a space that has a directory
        at all (a card with a mailbox): elsewhere the owner needs no
        card."""
        assert self._federation is not None
        space = await self._spaces.get(space_id)
        if space is None or space.owner_instance_id == self._instance_id:
            return
        owner = space.owner_instance_id
        now = int(self._wall_clock())
        for inst, card in sorted(cards.items()):
            doc = await self._doc(space_id, inst)
            by = doc.get("by")
            if (
                inst == owner
                or doc.get("direct") is not True
                or (isinstance(by, list) and owner in by)
            ):
                continue
            tries = _count(doc.get("fwd_n")) if doc.get("fwd") == card.rev else 0
            if tries >= FORWARD_TRIES or (
                tries and now - _count(doc.get("fwd_at")) < FORWARD_RETRY_S
            ):
                continue
            try:
                result = await self._federation.send_with_mesh_fallback(
                    to_instance_id=owner,
                    event_type=FederationEventType.SPACE_MEMBER_CARD,
                    payload={
                        "space_id": space_id,
                        "forwarded": True,
                        "card": card.to_wire(),
                        "gfs_reached": await self._reached(space_id, inst),
                    },
                    space_id=space_id,
                )
            except Exception:
                log.warning("directory: passing a card on failed", exc_info=True)
                continue
            if result.ok or result.error == DELIVERY_ERROR_QUEUED:
                doc["fwd"] = card.rev
                doc["fwd_at"] = now
                doc["fwd_n"] = tries + 1
                await self._put(space_id, inst, doc)

    async def _reached(self, space_id: str, instance_id: str) -> bool:
        """*instance_id* reaches the space only through a GFS: by our own
        sealed-redeem seat with a relay, or as the host that seated it
        told us (its flag on the card's row). We ourselves — in a directory
        we keep as a delegated admin — when that is how we reach the
        owner."""
        if instance_id == self._instance_id:
            hosts = await self._hosts(space_id)
            return bool(hosts) and await self._reached_by_gfs(hosts[0])
        if await self._reached_by_gfs(instance_id):
            return True
        return (await self._doc(space_id, instance_id)).get("gfs_reached") is True

    def _may_resnapshot(self, space_id: str, instance_id: str) -> bool:
        key = (space_id, instance_id)
        now = self._clock()
        if now - self._hello_at.get(key, float("-inf")) < HELLO_RESNAPSHOT_S:
            return False
        self._hello_at[key] = now
        return True

    async def directory_seq(self, space_id: str, host_id: str) -> int | None:
        """The ``dir_seq`` of *space_id*'s directory we hold — what our §25.6
        ``SPACE_SYNC_BEGIN`` to a household acting as its host echoes (each
        host has its own version line) — or ``None`` when that household
        does not act as its host here, or none is held."""
        if host_id not in await self._hosts(space_id):
            return None
        seq = (await self._doc(space_id, host_id)).get("dir_seq")
        if _is_count(seq):
            return seq
        # No complete directory yet: echo 0 wherever we hold a writer seat
        # (the host answers only where it keeps a directory). A paired
        # joiner holds no card of the space at all — it gets no host card —
        # so this echo is what repairs a first snapshot it never got.
        return 0 if await holds_member_seat(self._spaces, space_id) else None

    async def ask_host(self, space_id: str) -> None:
        """Our directory of *space_id* misses something a host has (a host
        hop from a member whose card we lack): ask every household acting
        as its host again (:meth:`_ask`). Never raises."""
        try:
            for host in await self._hosts(space_id):
                await self._ask(space_id, host)
        except Exception:
            log.warning("directory: asking the host again failed", exc_info=True)

    async def _ask(self, space_id: str, host: str) -> None:
        """Give *host* our card again now — the "seated" it answers with a
        snapshot — rather than wait for the next §25.6 sync. A writer seat
        only, at most once per :data:`HELLO_RESNAPSHOT_S` a space and host
        (the host answers at most that often too). Never raises."""
        if self._federation is None:
            return
        try:
            now = self._clock()
            last = self._asked_at.get((space_id, host), float("-inf"))
            if now - last < HELLO_RESNAPSHOT_S:
                return
            if not await holds_member_seat(self._spaces, space_id):
                return
            self._asked_at[(space_id, host)] = now
            await self._publish_to_host(space_id, host, again=True)
        except Exception:
            log.warning("directory: asking the host again failed", exc_info=True)

    async def on_sync_begin(self, event: FederationEvent) -> None:
        """Host side of the repair: a member household's periodic §25.6
        ``SPACE_SYNC_BEGIN`` echoes the ``dir_seq`` it holds; one behind ours
        (a lost delta, a snapshot that raced its seat, a fresh joiner at 0)
        gets one bounded snapshot. No echo → nothing (an older member)."""
        p = event.payload if isinstance(event.payload, dict) else {}
        echo = p.get("dir_seq")
        space_id = str(event.space_id or p.get("space_id") or "")
        sender = event.from_instance
        if not _is_count(echo) or not space_id or not sender:
            return
        if not await self.serves(space_id):
            return
        if sender not in await self._writers_v89(space_id):
            return
        # A household that syncs with us is seated.
        news = await self._mark_seated(space_id, sender)
        own_doc = await self._doc(space_id, self._instance_id)
        mine = await self.card_of(space_id, sender)
        key = "dir_seq" if mine is not None and mine.mailbox else "dir_seq_mbx"
        current = own_doc.get(key)
        behind = news or (_is_count(current) and echo < current)
        if behind and self._may_resnapshot(space_id, sender):
            await self.reconcile(space_id, snapshot_to={sender})
        if news:
            await self._seated(space_id, sender)

    async def _writers_v89(self, space_id: str) -> list[str]:
        """Writer-seat member households at v_89 (never a follower)."""
        assert self._federation is not None
        out = []
        for inst in sorted(await self._writers.writer_households(space_id)):
            if await self._federation.space_member_supports(
                inst, min_version=FederationCapability.MIN_FOR_MAILBOX_MEMBER_MESH
            ):
                out.append(inst)
        return out

    async def _recipients(self, space_id: str) -> list[str]:
        """The writer-seat households at v_89 that told us they are seated
        (their card, or a §25.6 BEGIN): a directory frame to one that is not
        could beat its own seat and be dropped as from a stranger."""
        out = []
        for inst in await self._writers_v89(space_id):
            if (await self._doc(space_id, inst)).get("seated") is True:
                out.append(inst)
        return out

    async def _mark_seated(self, space_id: str, instance_id: str) -> bool:
        """Note that *instance_id* is seated in *space_id*. Returns whether
        that is news."""
        doc = await self._doc(space_id, instance_id)
        if doc.get("seated") is True:
            return False
        doc["seated"] = True
        await self._put(space_id, instance_id, doc)
        return True

    async def _send(
        self, space_id: str, to: str, cards: list[MemberCard], **extra: Any
    ) -> bool:
        assert self._federation is not None
        payload: dict[str, Any] = {
            "space_id": space_id,
            "cards": [c.to_wire() for c in cards],
            **extra,
        }
        if cards:
            # Which of these households reach the space only through a GFS:
            # a member connected to that GFS mints its mailbox there.
            payload["gfs_reached"] = sorted(
                [
                    c.instance_id
                    for c in cards
                    if await self._reached(space_id, c.instance_id)
                ]
            )
            if not await self._owns(space_id):
                # A delegated admin names the households it seated itself
                # (they gave it their card): a host hop toward one of them
                # may go through us — it takes us as its host too.
                payload["via_seated"] = sorted(
                    [
                        c.instance_id
                        for c in cards
                        if (await self._doc(space_id, c.instance_id)).get("direct")
                        is True
                    ]
                )
        result = await self._federation.broadcast_to_space_members(
            space_id,
            FederationEventType.SPACE_MAILBOX_DIRECTORY,
            payload,
            min_proto_version=FederationCapability.MIN_FOR_MAILBOX_MEMBER_MESH,
            only_instances=frozenset({to}),
        )
        # Queued in the outbox counts: it redelivers on its own.
        return result.attempted > 0 and not result.terminal_failures

    async def reconcile(
        self,
        space_id: str,
        *,
        changed: list[MemberCard] | None = None,
        snapshot_to: set[str] | None = None,
    ) -> None:
        """Bring the directory of *space_id* (a space we host) up to date:
        a delta of *changed* cards (and of our own card when it moved, and
        of households that left it) to every writer-seat household, under a
        new ``dir_seq``; and one snapshot to each household in
        *snapshot_to*. Nothing at all while no card of the space carries a
        mailbox. Delivery is not tracked per household: one that missed a
        delta is behind on ``dir_seq`` at its next §25.6 sync and gets a
        snapshot then (:meth:`on_sync_begin`).

        One call per space at a time: two at once (a member's "seated"
        snapshot, a joiner's delta) would interleave their frames — a delta
        between a snapshot's parts, or ahead of the snapshot it builds on —
        and the member drops it, while the host, having just answered the
        "seated", answers no ask for a snapshot again before its next sync;
        and both would read and write the version at once."""
        if self._federation is None:
            return
        lock = self._reconciling.setdefault(space_id, asyncio.Lock())
        async with lock:
            await self._reconcile_locked(
                space_id, changed=changed, snapshot_to=snapshot_to
            )

    async def _reconcile_locked(
        self,
        space_id: str,
        *,
        changed: list[MemberCard] | None,
        snapshot_to: set[str] | None,
    ) -> None:
        assert self._federation is not None
        if not await self.serves(space_id):
            return
        cards = await self._space_cards(space_id)
        owner = await self._owns(space_id)
        own = await self.own_card(space_id)
        lost = {k for k in self._lost_mailbox if k[0] == space_id}
        if (
            own.mailbox is None
            and not any(c.mailbox for c in cards.values())
            and not lost
        ):
            return  # no member reached through a GFS: nothing to pass on
        if not owner:
            seats = set(await self._writers_v89(space_id))
            await self._forward_to_owner(
                space_id, {i: c for i, c in cards.items() if i in seats}
            )
            if not await self._recipients(space_id):
                # A delegated admin nobody takes as host (it invited no
                # household, or none said it is seated): no directory to
                # keep. The first one that comes gets a snapshot.
                return
        self._lost_mailbox -= lost
        writers = await self._writers_v89(space_id)
        recipients = await self._recipients(space_id)
        listed = {i: c for i, c in cards.items() if i in writers}
        listed[self._instance_id] = own
        own_doc = await self._doc(space_id, self._instance_id)
        before = {i for i in own_doc.get("listed") or [] if isinstance(i, str)}
        removed = sorted(before - set(listed))
        delta = {c.instance_id: c for c in changed or [] if c.instance_id in listed}
        if own_doc.get("own_rev") != own.rev:
            delta[self._instance_id] = own
        # Who we flag as GFS-reached: a household whose flag changed (its
        # seat paired, or a new relay seat) goes out again, card unchanged,
        # so no stale flag keeps a member's mailbox alive.
        reached = sorted(
            [i for i in cards if i in listed and await self._reached(space_id, i)]
        )
        before_reached = {i for i in own_doc.get("reached") or [] if isinstance(i, str)}
        for inst in before_reached.symmetric_difference(reached):
            if inst in listed and inst != self._instance_id:
                delta.setdefault(inst, listed[inst])
        # What a household given mailbox cards only must hear: a mailbox
        # card, a card that just lost its mailbox, a removal.
        for_mbx = [
            c
            for c in delta.values()
            if c.mailbox is not None or (space_id, c.instance_id) in lost
        ]
        seq = _count(own_doc.get("dir_seq"))
        # What each kind of recipient holds now: a delta builds on it.
        base_all, base_mbx = seq, _count(own_doc.get("dir_seq_mbx"))
        if delta or removed:
            seq = max(seq + 1, int(self._wall_clock() * 1000))
            own_doc["dir_seq"] = seq
            if for_mbx or removed or not _is_count(own_doc.get("dir_seq_mbx")):
                own_doc["dir_seq_mbx"] = seq
        own_doc["listed"] = sorted(listed)
        own_doc["reached"] = reached
        own_doc["own_rev"] = own.rev
        await self._put(space_id, self._instance_id, own_doc)
        for to in recipients:
            mine = cards.get(to)
            everything = mine is not None and mine.mailbox is not None
            if to in (snapshot_to or set()):
                # The version this household builds on: a household given
                # mailbox cards only follows ``dir_seq_mbx``.
                version = seq if everything else _count(own_doc.get("dir_seq_mbx"))
                await self._send_snapshot(space_id, to, listed, version, everything)
                continue
            relevant = list(delta.values()) if everything else for_mbx
            out = [c for c in relevant if c.instance_id != to]
            # Sent even when it only held the recipient's own card (then
            # cardless): its ``dir_seq`` must keep up, or its next sync
            # would cost a full snapshot.
            if relevant or removed:
                await self._send(
                    space_id,
                    to,
                    out,
                    removed=removed,
                    dir_seq=seq,
                    base_seq=base_all if everything else base_mbx,
                )

    async def _send_snapshot(
        self,
        space_id: str,
        to: str,
        listed: dict[str, MemberCard],
        seq: int,
        everything: bool,
    ) -> None:
        """The whole directory to *to*, all under one ``dir_seq``: first the
        cards it needs (all, or only those with a mailbox), mailbox cards
        first, at most :data:`SNAPSHOT_MAX`, in parts that fit the GFS
        relay; LAST the id of every listed household (``members``, ids only,
        at most :data:`MEMBERS_MAX`, one relay envelope). Every frame carries
        its ``part`` index and the number of ``parts``: only a complete
        snapshot moves the receiver's ``dir_seq``."""
        picked = [
            c
            for i, c in sorted(listed.items())
            if i != to and (everything or c.mailbox is not None)
        ]
        picked.sort(key=lambda c: c.mailbox is None)
        if len(picked) > SNAPSHOT_MAX:
            log.warning(
                "directory: a snapshot holds %d cards — sending the first %d",
                len(picked),
                SNAPSHOT_MAX,
            )
            picked = picked[:SNAPSHOT_MAX]
        chunks = [
            picked[i : i + SNAPSHOT_PART_MAX]
            for i in range(0, len(picked), SNAPSHOT_PART_MAX)
        ]
        parts = len(chunks) + 1
        for n, chunk in enumerate(chunks):
            if not await self._send(
                space_id, to, chunk, snapshot=True, dir_seq=seq, part=n, parts=parts
            ):
                return
        last: dict[str, Any] = {"part": parts - 1, "parts": parts}
        members = sorted(i for i in listed if i != to)
        if len(members) <= MEMBERS_MAX:
            last["members"] = members
        else:
            # The scale limit: past it the snapshot cannot name every
            # household and the receiver drops nobody (deltas carry the
            # removals, and a lost one is not repaired).
            log.warning(
                "directory: %d households — past the %d a snapshot can name",
                len(members),
                MEMBERS_MAX,
            )
        await self._send(space_id, to, [], snapshot=True, dir_seq=seq, **last)

    async def on_own_change(self) -> None:
        """Our mailboxes changed (minted, released, rotated): every space we
        host gets our new card as a delta, and every other space's host our
        new card, now."""
        for space_id in sorted({space for (space, _i) in await self._rows()}):
            try:
                if await self.serves(space_id):
                    await self.reconcile(space_id)
            except Exception:
                log.warning("directory: reconcile failed", exc_info=True)
        await self.publish_cards()

    async def sweep_once(self) -> None:
        """The backstop (each mailbox sweep tick): every space we host with
        cards is reconciled (newcomers, leavers, our own card); a space we
        left or that was dissolved drops its directory (cards, rows, seats);
        and our card reaches every other space's host that lacks it."""
        spaces = sorted({space for (space, _i) in await self._rows()})
        for space_id in spaces:
            try:
                if not await self._in_use(space_id):
                    await self._forget_space(space_id)
                    continue
                await self._drop_lapsed(space_id)
                await self._adopt_waiting(space_id)
                if await self.serves(space_id):
                    await self.reconcile(space_id)
            except Exception:
                log.warning("directory: reconcile failed", exc_info=True)
        await self.publish_cards()

    async def _adopt_waiting(self, space_id: str) -> None:
        """Backstop of :meth:`_adopt`: a waiting card whose household holds
        a row by now (a seat event we missed)."""
        rows = set(await self._spaces.list_member_instances(space_id))
        for host in await self._hosts(space_id):
            wait = (await self._doc(space_id, host)).get("wait")
            for inst in sorted(wait) if isinstance(wait, dict) else []:
                if inst in rows:
                    await self._adopt(space_id, inst)

    async def _forget_space(self, space_id: str) -> None:
        """A space we left, or that was dissolved (here or by its remote
        host): every household carded in it loses its card and — once no
        shared space is left — its lazy seat. The rows are the roster's."""
        for key in [k for k in self._snap_in if k[0] == space_id]:
            self._snap_in.pop(key, None)
        hosts = await self.hosts_of(space_id)
        for inst in sorted(await self._space_cards(space_id)):
            if inst in hosts:
                await self._clear(space_id, inst)
            else:
                await self._drop_card(space_id, inst)

    # ── Transport + inbound ──────────────────────────────────────────────

    async def _any_card(self, instance_id: str) -> MemberCard | None:
        """A verified card of *instance_id* from any space, one with a
        mailbox first (the household has ONE mailbox per GFS, so any space's
        card names it)."""
        best: MemberCard | None = None
        for (_space, inst), doc in sorted((await self._rows()).items()):
            if inst != instance_id:
                continue
            card = _card(doc)
            if card is None or card.instance_id != instance_id:
                continue
            if best is None or (card.mailbox is not None and best.mailbox is None):
                best = card
        return best

    async def target_for(
        self, instance_id: str, space_id: str | None
    ) -> MailboxTarget | None:
        """The mailbox an envelope goes to for *instance_id*: its card for
        *space_id*, or — space-less only — any space's (one mailbox serves
        all its spaces); ``None`` otherwise."""
        # Another space's card only for a SPACE-LESS envelope: one of space X
        # never goes to a household on the strength of its card in Y.
        card = (
            await self.card_of(space_id, instance_id)
            if space_id is not None
            else await self._any_card(instance_id)
        )
        if card is None or card.mailbox is None:
            return None
        return MailboxTarget(
            mailbox_id=card.mailbox.mailbox_id,
            kem_pk=card.mailbox.kem_pk,
            gfs_url=card.mailbox.gfs,
        )

    async def sender_allowed(self, sender: str, space_id: str) -> bool:
        """Whether a frame on our mailbox from *sender* about *space_id* may
        go on: we are in the space, *sender* is a member household of it by
        our roster, and we hold its verified card for that space (the host's
        from the ACK, a member's from the host's directory, a joiner's from
        its redeem)."""
        if not await self._in_use(space_id):
            return False
        if sender not in await self._spaces.list_member_instances(space_id):
            return False
        hosts = await self.hosts_of(space_id)
        doc = await self._doc(space_id, sender)
        card = _card(doc)
        if card is not None and card.instance_id == sender:
            # A card only a household that no longer acts as host listed
            # counts for nothing — at once, not at the next sweep.
            return sender in hosts or self._still_listed(doc, hosts)
        # A card of another space counts only where the roster is ours to
        # trust: the sender acts as the space's host here (a second space
        # joined over the existing seat carries no card), or WE do (its
        # member household's first card for it is on its way in). A member
        # needs the sender's card for THIS space, so a removal (its card
        # cleared by a host's delta) takes effect at once.
        if sender not in hosts and not await self.serves(space_id):
            return False
        return await self._any_card(sender) is not None


__all__ = [
    "REMOVED_MAX",
    "SNAPSHOT_MAX",
    "SpaceMemberDirectory",
]
