"""Per-space GFS mailboxes of THIS household (GFS redesign PR 2, v_88).

A household that reaches a space only through a GFS (a ``space_session``
seat, from an invite link or an approved GFS "Ask to join") owns one
anonymous mailbox per (space, generation) on that GFS, and addresses its
counterpart's envelopes of that space to the counterpart's mailbox. This
service is the household half of it:

* **Mailbox root.** One 32-byte secret, generated once and stored KEK-wrapped
  in ``instance_config`` (``gfs_mailbox_root``) — the household secrets
  store; no migration. Every mailbox key derives from it
  (:func:`~socialhome.federation.gfs_mailbox.derive_mailbox_keys`), so only
  the generation is stored per mailbox.
* **Tickets.** Per GFS connection whose signed ``/gfs/info`` carries
  ``mailbox_tickets``: a :class:`~socialhome.federation.gfs_mailbox
  .TicketKeyGuard` (its pin persisted in ``instance_config``, so a restart
  cannot reset "one new key per epoch") and a
  :class:`~socialhome.federation.gfs_mailbox.MailboxTicketCache`, refilled
  on its own jittered daily schedule and — for a fresh household — on
  connect (:meth:`on_connected`, outside the quiet window after a register).
* **Mint** (:meth:`mint`): at seat time derive the keys of (space, gen),
  register the mailbox (spending a ticket only if it is new) and open its
  claim socket; returns the signed directory entry for the sealed redeem /
  ACK. A mailbox stays "pending" until the seat is written
  (:meth:`remember`) or :data:`PENDING_TTL_S` passed (a failed redeem's
  retry reuses it meanwhile).
* **Claim sockets.** One anonymous socket per mailbox (never the identified
  ``/gfs/ws``, never shared: a shared socket links a household's spaces),
  reconnected with backoff, frames handed to the inbound handler with the
  mailbox's keys.
* **Targets** (:meth:`target_for`): the counterpart's mailbox for an
  envelope of a space — what :class:`~socialhome.federation.transport
  .FederationTransport` asks per relay send.
* **Release** (:meth:`release_unused`): a mailbox no ``space_instances`` row
  refers to any more (we left, or were removed, or the last GFS-only member
  of a space we host left) is closed and unregistered at the GFS.

No log line at INFO or above names a mailbox id or a key.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import orjson

from ..crypto import b64url_decode, b64url_encode
from ..domain.federation import (
    FederationEvent,
    FederationEventType,
    GfsConnection,
    InstanceSource,
)
from ..domain.federation_capabilities import OURS, FederationCapability
from ..domain.gfs_mailbox import TicketKeyPin
from ..domain.space_mailbox import (
    InvalidMailboxEntry,
    OwnMailbox,
    SpaceMailboxEntry,
    own_to_wire,
    parse_entry,
    parse_own,
)
from ..federation.gfs_mailbox import (
    MailboxClaim,
    MailboxClaimError,
    MailboxKeys,
    MailboxRegisterError,
    MailboxTicketCache,
    MailboxTicketError,
    TicketKeyGuard,
    derive_mailbox_keys,
    fetch_tickets,
    new_mailbox_root,
    read_ticket_issuer,
    register_mailbox,
    unregister_mailbox,
)
from ..federation.gfs_relay_transport import MailboxTarget
from ..federation.space_mailbox_entry import build_entry, verify_entry
from ..infrastructure.key_manager import KeyManager
from ..repositories.federation_repo import AbstractFederationRepo
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_connection_service import GfsConnectionService
from .gfs_envelope_sender import normalize_gfs_base

if TYPE_CHECKING:
    from ..federation.federation_service import FederationService

log = logging.getLogger(__name__)

#: ``instance_config`` key of the KEK-wrapped mailbox root.
MAILBOX_ROOT_CONFIG_KEY: str = "gfs_mailbox_root"
#: ``instance_config`` key prefix of a GFS's persisted ticket-key pin
#: (+ that GFS's pinned instance id).
TICKET_PIN_CONFIG_PREFIX: str = "gfs_mailbox_ticket_pin:"
#: Tickets one GFS's cache holds (a ticket buys one new mailbox).
TICKET_BATCH: int = 32
#: Claim-socket reconnect backoff.
CLAIM_BACKOFF_MIN_S: float = 2.0
CLAIM_BACKOFF_MAX_S: float = 300.0
#: A claim that stayed up this long resets the reconnect backoff (a GFS
#: that accepts and closes at once must not get a reconnect every 2 s).
CLAIM_STABLE_S: float = 60.0
#: How long a mailbox minted for a seat not written yet is kept (the redeem
#: is in flight, or will be retried): a retry reuses it — and its ticket —
#: and two concurrent redeems for one space never release each other's.
PENDING_TTL_S: float = 1800.0
#: Mean time between two sweeps of mailboxes no seat uses (jittered ±50 %):
#: the backstop for every path that ends a seat (leave, dissolve, a stub
#: purge…), on top of the immediate release when a ``space_session`` seat
#: is revoked.
SWEEP_PERIOD_S: float = 600.0
#: A fresh household's first ticket fetch waits this long (uniform) after
#: the anonymous key read on connect — short: a household that joins a space
#: right after connecting needs a ticket, and a GFS that wants to tell the
#: two reads apart by address can already (the stated IP limit).
CONNECT_REFILL_DELAY_S: tuple[float, float] = (1.0, 10.0)
#: Bound on closing every claim at shutdown.
STOP_TIMEOUT_S: float = 8.0
#: Each released mailbox is closed and unregistered after its OWN random
#: wait in this range: one kick / ban / dissolve that ends several seats
#: must not unregister their mailboxes back to back from one address.
RELEASE_DELAY_S: tuple[float, float] = (120.0, 900.0)
#: Re-registrations a lost mailbox may be REFUSED (not merely short of a
#: ticket) before it is replaced by a fresh generation.
LOST_ROTATE_AFTER: int = 3
#: Per member pair and day, at most: this many ``SPACE_MAILBOX_UPDATED``
#: announces, re-registrations of a lost mailbox, and rotations. A GFS that
#: answers every claim with 4401 can then cost at most 4 tickets + 1 per
#: day per pair and 6 small envelopes to one counterpart — never a storm.
ANNOUNCES_PER_DAY: int = 6
RECOVERY_REGISTERS_PER_DAY: int = 4
ROTATIONS_PER_DAY: int = 1
#: A seated GFS-only pair with no mailbox of ours (no ticket, a refused or
#: unreachable register, a mint past its bound) is re-minted by the sweep:
#: at most this many per pair and day, and :data:`RETRIES_PER_SWEEP` pairs
#: per sweep (random order) — the instance-addressed fallback is transient.
REMINTS_PER_DAY: int = 1
RETRIES_PER_SWEEP: int = 4
#: Each claim waits its own uniform delay in this range before its first
#: connect (a restart reopens them all) and after a lost socket (a GFS
#: restart drops them all), so the GFS cannot line them up by time.
CLAIM_JITTER_S: tuple[float, float] = (0.0, 60.0)
#: Each claim socket pings at its own period in this range (inside
#: Cloudflare's 100 s idle timeout), so sockets opened together do not
#: ping in step.
CLAIM_HEARTBEAT_S: tuple[float, float] = (20.0, 40.0)
_DAY_S: float = 86_400.0
#: :meth:`SpaceMailboxService._register` outcomes.
REGISTER_OK: str = "ok"
REGISTER_NO_TICKET: str = "no_ticket"
REGISTER_REFUSED: str = "refused"
REGISTER_UNREACHABLE: str = "unreachable"
#: The largest generation a fresh mailbox picks (random, so a re-joined
#: space gets an unrelated mailbox and the GFS cannot tie the two).
_GEN_SPACE: int = 2**31

InboundHandler = Callable[[dict | bytes, MailboxKeys], Awaitable[object]]
#: ``(space_id, instance_id, entry | None)`` → send ``SPACE_MAILBOX_UPDATED``.
Announcer = Callable[[str, str, SpaceMailboxEntry | None], Awaitable[object]]


@dataclass(slots=True)
class _Tickets:
    guard: TicketKeyGuard
    cache: MailboxTicketCache


@dataclass(slots=True)
class _Claim:
    keys: MailboxKeys
    own: OwnMailbox
    #: The ONE counterpart this mailbox serves (one mailbox per member pair).
    peer: str
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    socket: MailboxClaim | None = None
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    #: Wait before the first connect (:data:`CLAIM_JITTER_S`; 0 for a
    #: mailbox minted at seat time — its register is next to the seat anyway).
    start_delay: float = 0.0
    #: The GFS no longer knows the mailbox; counterparts were told to fall
    #: back to the instance-addressed relay until it is back.
    lost: bool = False
    #: Re-registrations refused while lost (→ :data:`LOST_ROTATE_AFTER`).
    refusals: int = 0
    #: Released (waiting for its own random delay): frames are dropped.
    released: bool = False


ClaimKey = tuple[str, str, str, int]


def _claim_key(space_id: str, peer: str, own: OwnMailbox) -> ClaimKey:
    """One mailbox per member PAIR (owner decision, #1333): (space, the
    counterpart, our pinned GFS id, gen)."""
    return (space_id, peer, own.gfs_instance_id, own.gen)


def _derive(root: bytes, space_id: str, peer: str, own: OwnMailbox) -> MailboxKeys:
    return derive_mailbox_keys(root, own.gfs_instance_id, space_id, own.gen, peer=peer)


def pin_to_json(pin: TicketKeyPin) -> str:
    return orjson.dumps(
        {
            "key_id": b64url_encode(pin.key_id),
            "first_seen": pin.first_seen,
            "epoch_days": pin.epoch_days,
            "previous_key_id": (
                b64url_encode(pin.previous_key_id)
                if pin.previous_key_id is not None
                else None
            ),
        }
    ).decode()


def pin_from_json(raw: str | None) -> TicketKeyPin | None:
    """A persisted pin, or ``None`` (absent or unreadable — a fresh pin is
    then taken on the next read, as on a first start)."""
    if not raw:
        return None
    try:
        data = orjson.loads(raw)
        prev = data.get("previous_key_id")
        return TicketKeyPin(
            key_id=b64url_decode(str(data["key_id"])),
            first_seen=int(data["first_seen"]),
            epoch_days=int(data["epoch_days"]),
            previous_key_id=b64url_decode(prev) if isinstance(prev, str) else None,
        )
    except orjson.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError:
        log.warning("gfs.mailbox: a stored ticket-key pin is unreadable — re-pinning")
        return None


class SpaceMailboxService:
    """See the module docstring."""

    __slots__ = (
        "_announcer",
        "_claim_jitter_range",
        "_claims",
        "_clock",
        "_config",
        "_connecting",
        "_daily",
        "_federation_repo",
        "_gfs",
        "_gfs_repo",
        "_identity_seed",
        "_inbound",
        "_instance_id",
        "_key_manager",
        "_keywrap_public_key",
        "_keywrap_sig",
        "_lock",
        "_peers",
        "_pending",
        "_release_delay",
        "_releases",
        "_rng",
        "_root",
        "_root_lock",
        "_session",
        "_spaces",
        "_stop",
        "_tasks",
        "_tickets",
        "_tickets_lock",
    )

    def __init__(
        self,
        *,
        space_repo: AbstractSpaceRepo,
        instance_config: AbstractInstanceConfigRepo,
        key_manager: KeyManager,
        gfs_repo: AbstractGfsConnectionRepo,
        gfs_service: GfsConnectionService,
        instance_id: str,
        identity_seed: bytes,
        keywrap_public_key: bytes,
        keywrap_sig: str,
        mailbox_session: aiohttp.ClientSession | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        release_delay: tuple[float, float] = RELEASE_DELAY_S,
        claim_jitter: tuple[float, float] = CLAIM_JITTER_S,
    ) -> None:
        self._spaces = space_repo
        self._config = instance_config
        self._key_manager = key_manager
        self._gfs_repo = gfs_repo
        self._gfs = gfs_service
        self._instance_id = instance_id
        self._identity_seed = identity_seed
        self._keywrap_public_key = keywrap_public_key
        self._keywrap_sig = keywrap_sig
        self._rng = rng if rng is not None else random.SystemRandom()
        self._clock = clock
        #: The session every mailbox request rides (owned: :meth:`stop`
        #: closes it, so a service is started once per process) (register, claim,
        #: unregister, the anonymous key read): cookie-less and with no
        #: keep-alive (``force_close``), so no pooled connection carries two
        #: mailboxes' traffic. ``None`` → the connection service's anonymous
        #: session (tests).
        self._session = mailbox_session
        self._root: bytes | None = None
        self._inbound: InboundHandler | None = None
        #: GFS connection id → its ticket machinery.
        self._tickets: dict[str, _Tickets] = {}
        #: (space, peer, gfs id, gen) → our open claim.
        self._claims: dict[ClaimKey, _Claim] = {}
        #: Claims minted for a seat not written yet → until when they are
        #: kept (:data:`PENDING_TTL_S`).
        self._pending: dict[ClaimKey, float] = {}
        #: (space, instance) → the counterpart's entry (mirror of the rows).
        self._peers: dict[tuple[str, str], SpaceMailboxEntry] = {}
        self._lock = asyncio.Lock()
        self._root_lock = asyncio.Lock()
        self._tickets_lock = asyncio.Lock()
        self._announcer: Announcer | None = None
        self._release_delay = release_delay
        self._claim_jitter_range = claim_jitter
        #: Releases waiting out their delay (:meth:`settle_releases`).
        self._releases: set[asyncio.Task] = set()
        #: (space, peer) → (day, announces, recovery registers, rotations).
        self._daily: dict[tuple[str, str], list[int]] = {}
        self._federation_repo: AbstractFederationRepo | None = None
        self._stop = asyncio.Event()
        #: Background tasks: the sweep, connect-time refills.
        self._tasks: set[asyncio.Task] = set()
        #: Connections with a connect-time refill running.
        self._connecting: set[str] = set()

    @contextlib.asynccontextmanager
    async def _anon(
        self, conn: GfsConnection
    ) -> AsyncIterator[aiohttp.ClientSession | None]:
        if self._session is not None:
            yield self._session
            return
        async with self._gfs.anon_session(conn) as session:
            yield session

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def attach_announcer(self, announcer: Announcer) -> None:
        """How a mailbox update reaches a counterpart (``SPACE_MAILBOX_UPDATED``
        over the ordinary transport)."""
        self._announcer = announcer

    def attach_to(
        self,
        federation_service: FederationService,
        federation_repo: AbstractFederationRepo,
    ) -> None:
        """Both directions of ``SPACE_MAILBOX_UPDATED`` (v_88): take the
        counterparts' updates, and send ours as an ordinary space envelope
        (routing ``space_id``, the entry in the encrypted payload)."""
        self._federation_repo = federation_repo
        federation_service._event_registry.register(
            FederationEventType.SPACE_MAILBOX_UPDATED, self._on_mailbox_updated
        )

        async def _announce(
            space_id: str, instance_id: str, entry: SpaceMailboxEntry | None
        ) -> None:
            await federation_service.send_event(
                to_instance_id=instance_id,
                event_type=FederationEventType.SPACE_MAILBOX_UPDATED,
                payload={"mailbox": entry.to_wire() if entry is not None else None},
                space_id=space_id,
            )

        self._announcer = _announce

    def attach_inbound(self, handler: InboundHandler) -> None:
        """Where frames on our mailboxes go (``GfsRelayInbound
        .handle_mailbox_frame``)."""
        self._inbound = handler

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load (or make) the root, mirror the rows, reopen our claims."""
        self._stop.clear()
        await self._load_root()
        rows = await self._load_rows()
        for space_id, instance_id, own in rows:
            if own is not None and await self._space_in_use(space_id):
                await self._open_claim(
                    space_id, instance_id, own, start_delay=self._claim_jitter()
                )
        self._spawn(self._sweep_loop(), "gfs-mailbox-sweep")

    def _claim_jitter(self) -> float:
        return self._rng.uniform(*self._claim_jitter_range)

    def _heartbeat(self) -> float:
        return self._rng.uniform(*CLAIM_HEARTBEAT_S)

    async def stop(self) -> None:
        """Close every claim socket (all at once, bounded) and stop the
        ticket loops and background tasks."""
        self._stop.set()
        claims = list(self._claims.values())
        self._claims.clear()
        for claim in claims:
            claim.stop.set()
        tickets = list(self._tickets.values())
        self._tickets.clear()
        closing = [self._close_claim(c) for c in claims]
        closing += [t.cache.stop() for t in tickets]
        closing += [t.guard.stop() for t in tickets]
        try:
            async with asyncio.timeout(STOP_TIMEOUT_S):
                await asyncio.gather(*closing, return_exceptions=True)
                if self._tasks:
                    await asyncio.gather(*list(self._tasks), return_exceptions=True)
        except TimeoutError:
            log.info("gfs.mailbox: shutdown cut short after %.0f s", STOP_TIMEOUT_S)
            for task in list(self._tasks):
                task.cancel()
            for claim in claims:
                if claim.task is not None:
                    claim.task.cancel()
        if self._session is not None:
            await self._session.close()

    async def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            wait = self._rng.uniform(0.5 * SWEEP_PERIOD_S, 1.5 * SWEEP_PERIOD_S)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except TimeoutError:
                pass
            try:
                await self.release_unused()
                await self.retry_missing()
            except Exception:
                log.warning("gfs.mailbox: sweep failed", exc_info=True)

    async def _space_in_use(self, space_id: str) -> bool:
        """Whether a person of this household still sits in *space_id* —
        a mailbox of a space we left (or that was dissolved) is not used,
        whatever row is left behind."""
        return bool(await self._spaces.list_local_member_user_ids(space_id))

    async def _load_root(self) -> bytes:
        if self._root is not None:
            return self._root
        # Locked: two first callers must not each mint (and store) a root.
        async with self._root_lock:
            if self._root is not None:
                return self._root
            stored = await self._config.get(MAILBOX_ROOT_CONFIG_KEY)
            if stored:
                root = self._key_manager.decrypt(stored)
            else:
                root = new_mailbox_root()
                await self._config.set(
                    MAILBOX_ROOT_CONFIG_KEY, self._key_manager.encrypt(root)
                )
                log.info("gfs.mailbox: generated this household's mailbox root")
            self._root = root
            return root

    async def _load_rows(self) -> list[tuple[str, str, OwnMailbox | None]]:
        """Re-read every ``space_instances.mailbox`` and refresh the peer
        mirror. Returns ``(space, instance, own)`` per row."""
        out: list[tuple[str, str, OwnMailbox | None]] = []
        peers: dict[tuple[str, str], SpaceMailboxEntry] = {}
        for (
            space_id,
            instance_id,
            raw,
        ) in await self._spaces.list_space_instance_mailboxes():
            try:
                data = orjson.loads(raw)
            except orjson.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            peer_raw = data.get("peer")
            if peer_raw is not None:
                try:
                    entry = parse_entry(peer_raw)
                except InvalidMailboxEntry:
                    entry = None
                if entry is not None and entry.space == space_id:
                    peers[(space_id, instance_id)] = entry
            out.append((space_id, instance_id, parse_own(data.get("own"))))
        self._peers = peers
        return out

    # ── Tickets ──────────────────────────────────────────────────────────

    async def on_connected(self, conn_id: str) -> None:
        """A GFS socket came up: make sure its ticket machinery runs and a
        household with no tickets gets a batch now (outside the quiet window
        after a register) instead of at the first daily slot. Fail-soft."""
        if conn_id in self._connecting:
            return  # a flapping socket: one refill task per connection
        self._connecting.add(conn_id)

        async def _run() -> None:
            try:
                await self._connected(conn_id)
            finally:
                self._connecting.discard(conn_id)

        self._spawn(_run(), "gfs-mailbox-connect")

    async def _connected(self, conn_id: str, *, delay: float | None = None) -> None:
        """The anonymous issuer-key read now; the identified ticket fetch a
        jittered while later (:data:`CONNECT_REFILL_DELAY_S`) — never next to
        each other, where a GFS could serve a fresh household its own key in
        both views. Runs off the connect hook (it never delays it)."""
        conn = await self._gfs_repo.get(conn_id)
        if conn is None or conn.status != "active":
            return
        try:
            if not await self._gfs.envelope_mailbox_supported(conn):
                return
            tickets = await self._tickets_for(conn)
            if len(tickets.cache):
                return
            await tickets.guard.refresh()
            wait = (
                delay
                if delay is not None
                else self._rng.uniform(*CONNECT_REFILL_DELAY_S)
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except TimeoutError:
                pass
            if not len(tickets.cache):
                await tickets.cache.refill_if_quiet()
        except MailboxTicketError as exc:
            log.info("gfs.mailbox: no tickets on connect (%s)", exc.error)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.info("gfs.mailbox: no tickets on connect (%s)", type(exc).__name__)
        finally:
            await self._save_pin(conn)

    async def forget_connection(self, conn: GfsConnection) -> None:
        """The household left this GFS: stop its ticket loops and drop its
        unspent tickets (the claims on it fail and back off until the seats
        that use them go)."""
        state = self._tickets.pop(conn.id, None)
        if state is None:
            return
        await state.cache.stop()
        await state.guard.stop()
        state.cache.clear()

    async def _tickets_for(self, conn: GfsConnection) -> _Tickets:
        async with self._tickets_lock:
            existing = self._tickets.get(conn.id)
            if existing is not None:
                return existing
            return await self._new_tickets(conn)

    async def _current(self, conn: GfsConnection) -> GfsConnection:
        """*conn* as stored now (its URL or pinned key may have moved)."""
        return await self._gfs_repo.get(conn.id) or conn

    async def _new_tickets(self, conn: GfsConnection) -> _Tickets:
        pin = pin_from_json(
            await self._config.get(TICKET_PIN_CONFIG_PREFIX + conn.gfs_instance_id)
        )
        gfs = self._gfs

        async def _anon_view():
            now = await self._current(conn)
            async with self._anon(now) as session:
                if session is None:
                    raise MailboxTicketError(0, "no_anonymous_session")
                return await read_ticket_issuer(
                    session, now.inbox_url, gfs_public_key=now.public_key
                )

        guard = TicketKeyGuard(_anon_view, pin=pin, rng=self._rng)

        async def _fetch(count: int) -> list[bytes]:
            now = await self._current(conn)
            issuer = await read_ticket_issuer(
                gfs.client(), now.inbox_url, gfs_public_key=now.public_key
            )
            got = await fetch_tickets(
                gfs.client(),
                now.inbox_url,
                issuer,
                count,
                instance_id=self._instance_id,
                signing_key=self._identity_seed,
                guard=guard,
            )
            await self._save_pin(conn)
            return got

        cache = MailboxTicketCache(
            _fetch, batch=TICKET_BATCH, guard=guard, rng=self._rng
        )
        state = _Tickets(guard=guard, cache=cache)
        self._tickets[conn.id] = state
        await guard.start()
        await cache.start()
        return state

    async def _save_pin(self, conn: GfsConnection) -> None:
        state = self._tickets.get(conn.id)
        pin = state.guard.pin if state is not None else None
        if pin is None:
            return
        key = TICKET_PIN_CONFIG_PREFIX + conn.gfs_instance_id
        value = pin_to_json(pin)
        if await self._config.get(key) != value:
            await self._config.set(key, value)

    # ── Mint / remember ────────────────────────────────────────

    async def _connection_at(self, gfs_url: str) -> GfsConnection | None:
        if not gfs_url:
            return None
        want = normalize_gfs_base(gfs_url)
        for conn in await self._gfs_repo.list_active():
            if (
                conn.status == "active"
                and normalize_gfs_base(conn.inbox_url) == want
                and await self._gfs.envelope_mailbox_supported(conn)
            ):
                return conn
        return None

    def _spend(self, space_id: str, peer: str, what: int, limit: int) -> bool:
        """Count one *what* (1 announce, 2 recovery register, 3 rotation,
        4 re-mint of a missing mailbox)
        for the pair today; ``False`` once *limit* is reached."""
        day = int(self._clock() // _DAY_S)
        row = self._daily.get((space_id, peer))
        if row is None or row[0] != day:
            row = [day, 0, 0, 0, 0]
            self._daily[(space_id, peer)] = row
        if row[what] >= limit:
            return False
        row[what] += 1
        return True

    async def mint_within(
        self,
        space_id: str,
        gfs_url: str,
        *,
        peer: str,
        timeout: float,
        on_late: Callable[[tuple[SpaceMailboxEntry, OwnMailbox]], Awaitable[None]]
        | None = None,
    ) -> tuple[SpaceMailboxEntry, OwnMailbox] | None:
        """:meth:`mint`, waiting at most *timeout* seconds. A mint that runs
        longer is NOT cancelled: it finishes in the background and stays
        pending, so a ticket the GFS saw is never lost to a timeout and a
        retry reuses the mailbox. Returns ``None`` on the timeout."""
        task: asyncio.Task[tuple[SpaceMailboxEntry, OwnMailbox] | None]
        task = asyncio.create_task(
            self.mint(space_id, gfs_url, peer=peer), name="gfs-mailbox-mint"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            log.info("gfs.mailbox: a mint took too long — the seat goes without")
            if on_late is not None:
                # Finished later: the caller can still hand the entry over
                # (a host pushes it with ``SPACE_MAILBOX_UPDATED``).
                self._spawn(self._after_late(task, on_late), "gfs-mailbox-late")
            return None

    async def _after_late(
        self,
        task: asyncio.Task[tuple[SpaceMailboxEntry, OwnMailbox] | None],
        on_late: Callable[[tuple[SpaceMailboxEntry, OwnMailbox]], Awaitable[None]],
    ) -> None:
        try:
            result = await task
        except Exception:
            log.info("gfs.mailbox: a late mint failed", exc_info=True)
            return
        if result is None:
            return
        try:
            await on_late(result)
        except Exception:
            log.warning(
                "gfs.mailbox: handing over a late mailbox failed", exc_info=True
            )

    async def mint(
        self, space_id: str, gfs_url: str, *, peer: str, fetch: bool = True
    ) -> tuple[SpaceMailboxEntry, OwnMailbox] | None:
        """Our mailbox for *space_id* toward the ONE counterpart *peer* at the
        GFS *gfs_url*, registered and claimed, and its signed entry — or
        ``None`` when that GFS takes no mailboxes or no ticket could be had
        (the pair then keeps today's instance-addressed relay). One mailbox
        per member pair (owner decision): a host seats each GFS-only member
        with its own mailbox, so a flood on one hurts only that pair. A
        retried redeem gets the same one (kept for :data:`PENDING_TTL_S` even
        if no seat refers to it yet). Mints run one at a time (the service
        lock): a host seating many members spends their tickets in turn."""
        conn = await self._connection_at(gfs_url)
        if conn is None:
            return None
        root = await self._load_root()
        async with self._lock:
            own = await self._own_for(space_id, peer, conn.gfs_instance_id)
            if own is None:
                own = OwnMailbox(
                    gen=self._rng.randrange(_GEN_SPACE),
                    gfs_instance_id=conn.gfs_instance_id,
                    gfs_url=normalize_gfs_base(conn.inbox_url),
                )
            key = _claim_key(space_id, peer, own)
            try:
                keys = _derive(root, space_id, peer, own)
            except ValueError:
                log.info("gfs.mailbox: this space id cannot carry a mailbox")
                return None
            if key not in self._claims:
                if await self._register(conn, keys, fetch=fetch) != REGISTER_OK:
                    return None
                await self._open_claim(space_id, peer, own, keys=keys)
            self._pending[key] = max(
                self._pending.get(key, 0.0), self._clock() + PENDING_TTL_S
            )
        entry = build_entry(
            keys,
            instance_id=self._instance_id,
            identity_seed=self._identity_seed,
            gfs_url=own.gfs_url,
            proto_version=OURS,
            keywrap_pk=self._keywrap_public_key,
            keywrap_sig=self._keywrap_sig,
        )
        return entry, own

    async def _own_for(
        self, space_id: str, peer: str, gfs_id: str
    ) -> OwnMailbox | None:
        """The generation we already hold for the pair (*space_id*, *peer*)
        at *gfs_id* — a pending mint (a retry) or the seat's, while a person
        of ours still sits in the space — or ``None``. A space we LEFT never
        hands its old generation to a re-join: that gets a fresh, unrelated
        mailbox (the old one goes with the next sweep)."""
        in_use = await self._space_in_use(space_id)
        for key, claim in self._claims.items():
            space, pair, gid, _gen = key
            if (
                space == space_id
                and pair == peer
                and gid == gfs_id
                and (key in self._pending or in_use)
            ):
                return claim.own
        if not in_use:
            return None
        for space, instance, own in await self._load_rows():
            if (
                space == space_id
                and instance == peer
                and own is not None
                and own.gfs_instance_id == gfs_id
            ):
                return own
        return None

    async def _register(
        self, conn: GfsConnection, keys: MailboxKeys, *, fetch: bool = True
    ) -> str:
        """Register *keys*' mailbox: :data:`REGISTER_OK`, ``"no_ticket"``,
        ``"refused"`` or ``"unreachable"``. *fetch* (seat time only) may
        fetch a ticket when the cache is empty; everywhere else the register
        waits for the scheduled refill — an identified fetch right before an
        anonymous register would tie the two."""
        try:
            tickets = await self._tickets_for(conn)
            if not len(tickets.cache) and not fetch:
                return REGISTER_NO_TICKET
            if not len(tickets.cache):
                # Seat time with no ticket (joined right after connecting):
                # fetch once now. This sits next to the identified redeem /
                # ACK anyway (the documented seat-time timing residual), so
                # it reveals nothing more; never right after a take.
                if not tickets.guard.has_view:
                    await tickets.guard.refresh()
                await tickets.cache.refill_if_quiet()
            async with self._anon(conn) as session:
                if session is None:
                    log.warning("gfs.mailbox: no anonymous session — not registering")
                    return REGISTER_UNREACHABLE
                await register_mailbox(
                    session,
                    conn.inbox_url,
                    keys,
                    conn.gfs_instance_id,
                    tickets=tickets.cache,
                )
        except MailboxRegisterError as exc:
            log.warning(
                "gfs.mailbox: %r refused a mailbox (%d %s) — the space keeps the "
                "instance-addressed relay",
                conn.display_name,
                exc.status,
                exc.error,
            )
            return REGISTER_REFUSED
        except MailboxTicketError as exc:
            log.warning(
                "gfs.mailbox: no ticket for a new mailbox at %r (%s) — the space "
                "keeps the instance-addressed relay",
                conn.display_name,
                exc.error,
            )
            return REGISTER_NO_TICKET
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.warning(
                "gfs.mailbox: could not reach %r to register a mailbox (%s)",
                conn.display_name,
                type(exc).__name__,
            )
            return REGISTER_UNREACHABLE
        finally:
            await self._save_pin(conn)
        return REGISTER_OK

    async def remember(
        self,
        space_id: str,
        instance_id: str,
        *,
        peer: SpaceMailboxEntry | None,
        own: OwnMailbox | None,
    ) -> bool:
        """Write the pair's mailbox state onto its ``space_instances`` row.
        From then on the row keeps *own* in use. Under the service lock: a
        release in flight sees the row and the pending state of the same
        moment, never one without the other. Returns whether the row exists
        (nothing is written — and *own* stays pending — otherwise).

        ``own=None`` keeps an own already on the row (a late
        :meth:`set_own` may have landed first — nulling it would release a
        mailbox the counterpart was already told about). ``peer`` and
        ``own`` both ``None`` still marks the pair as GFS-only, so the
        sweep's :meth:`retry_missing` finds it."""
        async with self._lock:
            if not await self._seated(space_id, instance_id):
                return False
            if own is None:
                own = next(
                    (
                        row_own
                        for space, inst, row_own in await self._load_rows()
                        if space == space_id and inst == instance_id
                    ),
                    None,
                )
            await self._write_row(space_id, instance_id, peer=peer, own=own)
            return True

    async def retry_missing(self) -> int:
        """Mint our mailbox for seated GFS-only pairs that have none (the
        seat-time mint found no ticket, was refused, could not reach the GFS,
        or finished after the seat without landing) and announce it to the
        counterpart with ``SPACE_MAILBOX_UPDATED``. Cached tickets only (an
        identified fetch next to an anonymous register would tie them), at
        most :data:`RETRIES_PER_SWEEP` pairs per run in random order and
        :data:`REMINTS_PER_DAY` per pair; only toward a counterpart at v_88
        or later, still seated as ``space_session``. Returns how many got a
        mailbox."""
        if self._federation_repo is None:
            return 0
        rows = [
            (space, inst) for space, inst, own in await self._load_rows() if own is None
        ]
        self._rng.shuffle(rows)
        done = 0
        tried = 0
        for space_id, instance_id in rows:
            if tried >= RETRIES_PER_SWEEP:
                break
            if not await self._space_in_use(space_id):
                continue
            instance = await self._federation_repo.get_instance(instance_id)
            if (
                instance is None
                or instance.source is not InstanceSource.SPACE_SESSION
                or not instance.relay_via
                or (instance.proto_version or 0)
                < FederationCapability.MIN_FOR_SPACE_MAILBOX
            ):
                continue
            if not self._spend(space_id, instance_id, 4, REMINTS_PER_DAY):
                continue
            tried += 1
            minted = await self.mint(
                space_id, instance.relay_via, peer=instance_id, fetch=False
            )
            if minted is None:
                continue
            if await self.set_own(space_id, instance_id, minted[1]):
                await self.announce_to(space_id, instance_id, minted[0])
                done += 1
        if done:
            log.info("gfs.mailbox: minted %d missing mailbox(es)", done)
        return done

    async def set_own(self, space_id: str, instance_id: str, own: OwnMailbox) -> bool:
        """Put *own* on the pair's row, keeping the counterpart's CURRENT
        entry (a mailbox minted after the seat — a host mint past its bound —
        must never overwrite a newer entry the counterpart sent since).
        ``False`` — nothing written, *own* stays pending — while the seat row
        does not exist yet."""
        async with self._lock:
            if not await self._seated(space_id, instance_id):
                return False
            await self._write_row(
                space_id,
                instance_id,
                peer=self._peers.get((space_id, instance_id)),
                own=own,
            )
            return True

    async def _seated(self, space_id: str, instance_id: str) -> bool:
        return instance_id in await self._spaces.list_member_instances(space_id)

    async def _write_row(
        self,
        space_id: str,
        instance_id: str,
        *,
        peer: SpaceMailboxEntry | None,
        own: OwnMailbox | None,
    ) -> None:
        """The row write behind :meth:`remember` (caller holds the lock).
        Always a JSON object — ``{"peer": null, "own": null}`` included — so
        the pair stays a mailbox pair: a counterpart's ``null`` (lost) never
        turns into "not GFS-only", and its later entry is still taken. Only
        leaving the space clears the column (:meth:`release_unused`)."""
        value = orjson.dumps(
            {
                "peer": peer.to_wire() if peer is not None else None,
                "own": own_to_wire(own) if own is not None else None,
            }
        ).decode()
        await self._spaces.set_space_instance_mailbox(space_id, instance_id, value)
        if peer is not None:
            self._peers[(space_id, instance_id)] = peer
        else:
            self._peers.pop((space_id, instance_id), None)
        if own is not None:
            # The row keeps it in use from now on.
            self._pending.pop(_claim_key(space_id, instance_id, own), None)

    # ── Targets ──────────────────────────────────────────────────────────

    async def target_for(
        self, instance_id: str, space_id: str | None
    ) -> MailboxTarget | None:
        """The mailbox an envelope of *space_id* goes to for *instance_id*
        (any shared space's for a space-less envelope), or ``None``. Only an
        entry at or above v_88 counts."""
        entry: SpaceMailboxEntry | None
        if space_id is not None:
            entry = self._peers.get((space_id, instance_id))
        else:
            entry = next(
                (e for (s, i), e in sorted(self._peers.items()) if i == instance_id),
                None,
            )
        if entry is None or entry.proto_version < (
            FederationCapability.MIN_FOR_SPACE_MAILBOX
        ):
            return None
        return MailboxTarget(
            mailbox_id=entry.mailbox_id, kem_pk=entry.kem_pk, gfs_url=entry.gfs
        )

    # ── Release ──────────────────────────────────────────────────────────

    async def release_unused(self) -> int:
        """Close and unregister every mailbox of ours no seat uses any
        more: no row refers to it, or the space has no person of ours left
        (we left, it was dissolved) — and it is not pending. A left space's
        rows lose their mailbox state too, so a later join mints a fresh,
        unrelated mailbox. Returns how many went. Fail-soft."""
        async with self._lock:
            rows = await self._load_rows()
            now = self._clock()
            self._pending = {k: t for k, t in self._pending.items() if t > now}
            in_use: dict[str, bool] = {}
            for space in {space for space, _i, _own in rows}:
                in_use[space] = await self._space_in_use(space)
            for space, instance_id, _own in rows:
                if not in_use[space]:
                    await self._spaces.set_space_instance_mailbox(
                        space, instance_id, None
                    )
                    self._peers.pop((space, instance_id), None)
            used = {
                _claim_key(space, instance_id, own)
                for space, instance_id, own in rows
                if own is not None and in_use[space]
            }
            gone = [
                (key, claim)
                for key, claim in self._claims.items()
                if key not in used and key not in self._pending
            ]
            for key, claim in gone:
                del self._claims[key]
                claim.released = True
        for _key, claim in gone:
            # Each on its own, after its own random wait (see
            # :data:`RELEASE_DELAY_S`); its frames are dropped meanwhile.
            task: asyncio.Task[None] = asyncio.create_task(
                self._release_claim(
                    claim, delay=self._rng.uniform(*self._release_delay)
                ),
                name="gfs-mailbox-release",
            )
            for bucket in (self._tasks, self._releases):
                bucket.add(task)
                task.add_done_callback(bucket.discard)
        if gone:
            log.info("gfs.mailbox: releasing %d mailbox(es) no seat uses", len(gone))
        return len(gone)

    async def _release_claim(self, claim: _Claim, *, delay: float) -> None:
        """Close and unregister one released mailbox after *delay* seconds.
        A shutdown meanwhile only closes it (the GFS's idle sweep drops it)."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
            stopping = True
        except TimeoutError:
            stopping = False
        await self._close_claim(claim)
        if not stopping:
            await self._unregister(claim)

    async def _unregister(self, claim: _Claim) -> None:
        conn = await self._conn_for(claim.own)
        if conn is None:
            return  # the GFS's idle sweep removes it
        try:
            async with self._anon(conn) as session:
                if session is None:
                    return
                ok = await unregister_mailbox(
                    session, conn.inbox_url, claim.keys, claim.own.gfs_instance_id
                )
        except Exception:
            log.info("gfs.mailbox: unregister failed", exc_info=True)
            return
        if not ok:
            log.info(
                "gfs.mailbox: %r did not take an unregister — its idle sweep "
                "removes the mailbox",
                conn.display_name,
            )

    # ── Claim sockets ────────────────────────────────────────────────────

    async def _conn_for(self, own: OwnMailbox) -> GfsConnection | None:
        """The active connection our mailbox lives on: the GFS id we pinned
        at mint time, else the URL."""
        conns = [c for c in await self._gfs_repo.list_active() if c.status == "active"]
        for conn in conns:
            if conn.gfs_instance_id == own.gfs_instance_id:
                return conn
        want = normalize_gfs_base(own.gfs_url)
        for conn in conns:
            if normalize_gfs_base(conn.inbox_url) == want:
                return conn
        return None

    async def _open_claim(
        self,
        space_id: str,
        peer: str,
        own: OwnMailbox,
        *,
        keys: MailboxKeys | None = None,
        start_delay: float = 0.0,
    ) -> None:
        key = _claim_key(space_id, peer, own)
        if key in self._claims:
            return
        if keys is None:
            keys = _derive(await self._load_root(), space_id, peer, own)
        claim = _Claim(keys=keys, own=own, peer=peer, start_delay=start_delay)
        self._claims[key] = claim
        claim.task = asyncio.create_task(
            self._claim_loop(claim), name="gfs-mailbox-claim"
        )

    async def _close_claim(self, claim: _Claim) -> None:
        claim.stop.set()
        socket = claim.socket
        if socket is not None:
            await socket.close()
        if claim.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(claim.task), timeout=5.0)
            except TimeoutError:
                claim.task.cancel()

    async def _claim_loop(self, claim: _Claim) -> None:
        backoff = CLAIM_BACKOFF_MIN_S
        if claim.start_delay > 0:
            try:
                await asyncio.wait_for(claim.stop.wait(), timeout=claim.start_delay)
                return
            except TimeoutError:
                pass
        while not claim.stop.is_set():
            conn = await self._conn_for(claim.own)
            started = self._clock()
            try:
                if conn is not None:
                    await self._claim_once(conn, claim)
                    if self._clock() - started >= CLAIM_STABLE_S:
                        backoff = CLAIM_BACKOFF_MIN_S
            except MailboxClaimError as exc:
                log.info("gfs.mailbox: claim refused (%s)", exc)
                if (
                    conn is not None
                    and exc.mailbox_unknown
                    and await self._recover(conn, claim)
                ):
                    return  # replaced by a fresh generation
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                log.info("gfs.mailbox: claim socket down (%s)", type(exc).__name__)
            except Exception:
                log.warning("gfs.mailbox: claim loop failed", exc_info=True)
            finally:
                claim.socket = None
                claim.claimed.clear()
            if claim.stop.is_set():
                return
            # The backoff, plus this claim's own jitter: a GFS restart drops
            # every socket at once, and they must not come back in step.
            wait = self._rng.uniform(0.5 * backoff, 1.5 * backoff)
            wait += self._claim_jitter()
            try:
                await asyncio.wait_for(claim.stop.wait(), timeout=wait)
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, CLAIM_BACKOFF_MAX_S)

    async def _recover(self, conn: GfsConnection, claim: _Claim) -> bool:
        """The GFS no longer knows our mailbox (its idle sweep while we were
        away, or data loss). Tell the counterparts to fall back to the
        instance-addressed relay at once, then register the SAME mailbox
        again as soon as a ticket is cached (never fetched here — the
        scheduled refill brings one); once it is claimed again, the
        counterparts are told it is back (:meth:`_claim_once`). A mailbox
        the GFS keeps REFUSING is replaced by a fresh generation. Returns
        whether this claim was replaced."""
        space_id = claim.keys.space_id
        if not claim.lost:
            claim.lost = True
            await self._announce_claim(claim)
        if not self._spend(space_id, claim.peer, 2, RECOVERY_REGISTERS_PER_DAY):
            return False  # today's re-registrations are used up
        status = await self._register(conn, claim.keys, fetch=False)
        if status != REGISTER_REFUSED:
            return False
        claim.refusals += 1
        if claim.refusals < LOST_ROTATE_AFTER:
            return False
        if not self._spend(space_id, claim.peer, 3, ROTATIONS_PER_DAY):
            return False
        return await self._rotate(conn, claim)

    async def _rotate(self, conn: GfsConnection, claim: _Claim) -> bool:
        """Replace a mailbox the GFS keeps refusing: a fresh generation for
        the same space, registered (cached ticket only), the rows moved over
        and a claim opened; it announces itself once claimed."""
        space_id = claim.keys.space_id
        own = OwnMailbox(
            gen=self._rng.randrange(_GEN_SPACE),
            gfs_instance_id=claim.own.gfs_instance_id,
            gfs_url=claim.own.gfs_url,
        )
        keys = _derive(await self._load_root(), space_id, claim.peer, own)
        if await self._register(conn, keys, fetch=False) != REGISTER_OK:
            claim.refusals = 0  # try again later
            return False
        old_key = _claim_key(space_id, claim.peer, claim.own)
        async with self._lock:
            for space, instance_id, row_own in await self._load_rows():
                if (
                    row_own is not None
                    and _claim_key(space, instance_id, row_own) == old_key
                ):
                    await self._write_row(
                        space,
                        instance_id,
                        peer=self._peers.get((space, instance_id)),
                        own=own,
                    )
            self._claims.pop(old_key, None)
            new = _Claim(keys=keys, own=own, peer=claim.peer, lost=True)
            self._claims[_claim_key(space_id, claim.peer, own)] = new
            new.task = asyncio.create_task(
                self._claim_loop(new), name="gfs-mailbox-claim"
            )
        claim.stop.set()
        log.info("gfs.mailbox: replaced a mailbox the GFS kept refusing")
        return True

    async def _announce_claim(self, claim: _Claim) -> None:
        """Tell the ONE counterpart this claim serves where it is: its
        entry, or ``None`` while it is lost — at most
        :data:`ANNOUNCES_PER_DAY` a day. Fail-soft."""
        space_id = claim.keys.space_id
        key = _claim_key(space_id, claim.peer, claim.own)
        if not any(
            own is not None and _claim_key(space, instance_id, own) == key
            for space, instance_id, own in await self._load_rows()
        ):
            return  # no seat uses it (yet)
        if not self._spend(space_id, claim.peer, 1, ANNOUNCES_PER_DAY):
            log.info("gfs.mailbox: today's mailbox updates for a pair are used up")
            return
        entry = None if claim.lost else self._entry_for(claim.keys, claim.own)
        await self.announce_to(space_id, claim.peer, entry)

    def _entry_for(self, keys: MailboxKeys, own: OwnMailbox) -> SpaceMailboxEntry:
        return build_entry(
            keys,
            instance_id=self._instance_id,
            identity_seed=self._identity_seed,
            gfs_url=own.gfs_url,
            proto_version=OURS,
            keywrap_pk=self._keywrap_public_key,
            keywrap_sig=self._keywrap_sig,
        )

    async def announce_to(
        self, space_id: str, instance_id: str, entry: SpaceMailboxEntry | None
    ) -> None:
        """Send ``SPACE_MAILBOX_UPDATED`` to one counterpart. Fail-soft."""
        if self._announcer is None:
            return
        try:
            await self._announcer(space_id, instance_id, entry)
        except Exception:
            log.warning("gfs.mailbox: a mailbox update did not go out", exc_info=True)

    async def _on_mailbox_updated(self, event: FederationEvent) -> None:
        """A counterpart moved its mailbox for one space (or lost it):
        verify the new entry under its identity key and bound to the GFS it
        reaches us through, and store it on that pair's row only — or clear
        it (``null``), so our sends fall back to the instance-addressed
        relay. Anything off is dropped (INFO)."""
        space_id = event.space_id
        sender = event.from_instance
        if not space_id or self._federation_repo is None:
            return
        instance = await self._federation_repo.get_instance(sender)
        if instance is None or instance.source is not InstanceSource.SPACE_SESSION:
            return
        raw = event.payload.get("mailbox") if isinstance(event.payload, dict) else None
        peer: SpaceMailboxEntry | None = None
        if raw is not None:
            try:
                peer = verify_entry(
                    raw,
                    identity_pk_hex=instance.remote_identity_pk,
                    expected_instance_id=sender,
                    expected_space_id=space_id,
                )
            except InvalidMailboxEntry as exc:
                log.info(
                    "gfs.mailbox: ignoring a mailbox update from %s: %s", sender, exc
                )
                return
            if not instance.relay_via or normalize_gfs_base(
                peer.gfs
            ) != normalize_gfs_base(instance.relay_via):
                log.info("gfs.mailbox: ignoring a mailbox update naming another GFS")
                return
        async with self._lock:
            rows = {(space, inst): own for space, inst, own in await self._load_rows()}
            if (space_id, sender) not in rows:
                return  # no mailbox state for this pair (not GFS-only)
            await self._write_row(
                space_id, sender, peer=peer, own=rows[(space_id, sender)]
            )

    async def _claim_once(self, conn: GfsConnection, claim: _Claim) -> None:
        async with self._anon(conn) as session:
            if session is None:
                raise MailboxClaimError("no anonymous session")
            socket = MailboxClaim(
                session,
                conn.inbox_url,
                claim.keys,
                claim.own.gfs_instance_id,
                heartbeat=self._heartbeat(),
            )
            async with socket:
                claim.socket = socket
                claim.claimed.set()
                if claim.lost:
                    # Back (re-registered, or a fresh generation): tell the
                    # counterparts to use it again.
                    claim.lost = False
                    claim.refusals = 0
                    self._spawn(self._announce_claim(claim), "gfs-mailbox-announce")
                if claim.stop.is_set():
                    return
                async for frame in socket:
                    if not claim.released:
                        await self._deliver(frame, claim.keys)
                    if claim.stop.is_set():
                        return

    async def _deliver(self, frame: dict | bytes, keys: MailboxKeys) -> None:
        handler = self._inbound
        if handler is None:
            return
        try:
            await handler(frame, keys)
        except ValueError as exc:
            # A refused frame is a normal outcome (a stale queued envelope,
            # junk): the reason only, never the frame or the mailbox.
            log.info("gfs.mailbox: dropped a frame: %s", exc)
        except Exception:
            log.warning("gfs.mailbox: a frame failed", exc_info=True)

    # ── Introspection (tests, demo) ──────────────────────────────────────

    async def settle_releases(self) -> None:
        """Wait for every release waiting out its delay (tests)."""
        while self._releases:
            await asyncio.gather(*list(self._releases), return_exceptions=True)

    def open_claims(self) -> int:
        return len(self._claims)

    async def wait_claimed(self, timeout: float = 10.0) -> bool:
        """Wait until every open claim is claimed (tests / demo)."""
        try:
            async with asyncio.timeout(timeout):
                for claim in list(self._claims.values()):
                    await claim.claimed.wait()
        except TimeoutError:
            return False
        return True


__all__ = [
    "MAILBOX_ROOT_CONFIG_KEY",
    "TICKET_PIN_CONFIG_PREFIX",
    "SpaceMailboxService",
    "pin_from_json",
    "pin_to_json",
]
