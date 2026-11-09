"""This household's GFS mailboxes — ONE per GFS (GFS redesign PR 3, v_89).

A household that reaches a space through a GFS (a ``space_session`` seat
from an invite link or an approved "Ask to join", a space host with such
members, or a member that sends to one) owns ONE anonymous mailbox at that
GFS, shared by every space it reaches there. The host and every writer
member address the household's envelopes to that mailbox; the GFS sees a
mailbox id, never an instance id or a space id (owner decision 2026-11-09:
it may see that one household's traffic across spaces lands in one
mailbox). This service is the household half:

* **Mailbox root.** One 32-byte secret, generated once and stored KEK-wrapped
  in ``instance_config`` (``gfs_mailbox_root``). Every mailbox key derives
  from it (:func:`~socialhome.federation.gfs_mailbox.derive_mailbox_keys`),
  so only ``{gen, gfs}`` per GFS is stored (``gfs_mailboxes``, beside the
  card ``rev`` that changes with them).
* **Tickets.** Per GFS connection whose signed ``/gfs/info`` carries
  ``mailbox_tickets``: a :class:`~socialhome.federation.gfs_mailbox
  .TicketKeyGuard` (its pin persisted) and a small
  :class:`~socialhome.federation.gfs_mailbox.MailboxTicketCache` — a
  household needs one ticket per GFS, plus one per rotation.
* **Ensure** (:meth:`ensure`): our mailbox at a GFS — registered (a ticket
  for a new one) and claimed on its own anonymous socket. A mailbox minted
  before anything needs it (a joiner's, before its redeem leaves) is
  "pending" for :data:`PENDING_TTL_S`.
* **Sweep.** Every ~10 min: release a mailbox nothing needs any more
  (:meth:`attach_needed` says which GFSs are needed), mint one that is
  needed and missing, then the directory's hook.
* **Recovery / rotation.** A GFS that forgot the mailbox gets the SAME one
  registered again; one it keeps refusing, or a flood on it, gets a fresh
  generation (at most :data:`ROTATIONS_PER_DAY`). Every change bumps the
  card ``rev`` and calls the change hook, so the new card reaches every
  space the household is in at once (the member directory).

No random delays hide the timing of mailbox traffic: the same IP links a
household's identified socket and its mailbox anyway (``docs/principles.md``,
"No random delays for GFS unlinkability").

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
from typing import Any

import aiohttp
import orjson

from ..crypto import b64url_decode, b64url_encode
from ..domain.federation import GfsConnection
from ..domain.federation_capabilities import OURS
from ..domain.gfs_mailbox import OwnMailbox, TicketKeyPin
from ..federation.gfs_mailbox import (
    MailboxClaim,
    MailboxClaimError,
    MailboxFlooded,
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
from ..infrastructure.key_manager import KeyManager
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from .gfs_connection_service import GfsConnectionService
from .gfs_envelope_sender import normalize_gfs_base

log = logging.getLogger(__name__)

#: ``instance_config`` key of the KEK-wrapped mailbox root.
MAILBOX_ROOT_CONFIG_KEY: str = "gfs_mailbox_root"
#: ``instance_config`` key of our mailboxes: ``{"rev": int, "boxes":
#: {gfs_id: {"gen", "gfs"}}}`` — ``rev`` is our member card's revision.
MAILBOXES_CONFIG_KEY: str = "gfs_mailboxes"
#: ``instance_config`` key prefix of a GFS's persisted ticket-key pin.
TICKET_PIN_CONFIG_PREFIX: str = "gfs_mailbox_ticket_pin:"
#: Tickets one GFS's cache holds: one mailbox per GFS needs one, a rotation
#: or a lost mailbox one more.
TICKET_BATCH: int = 4
#: Claim-socket reconnect backoff.
CLAIM_BACKOFF_MIN_S: float = 2.0
CLAIM_BACKOFF_MAX_S: float = 300.0
#: A claim that stayed up this long resets the reconnect backoff.
CLAIM_STABLE_S: float = 60.0
#: How long a mailbox minted before anything needs it is kept (a joiner's,
#: minted before its redeem: the redeem is in flight, or will be retried).
PENDING_TTL_S: float = 1800.0
#: Mean time between two sweeps (jittered ±50 %).
SWEEP_PERIOD_S: float = 600.0
#: Bound on closing every claim at shutdown.
STOP_TIMEOUT_S: float = 8.0
#: A released mailbox is closed and unregistered after its own random wait.
RELEASE_DELAY_S: tuple[float, float] = (120.0, 900.0)
#: Re-registrations a lost mailbox may be REFUSED before it is rotated.
LOST_ROTATE_AFTER: int = 3
#: Per GFS and day, at most: re-registrations of a lost mailbox that used a
#: ticket, and rotations. A GFS that answers every claim with 4401 costs at
#: most 4 + 1 tickets a day.
RECOVERY_REGISTERS_PER_DAY: int = 4
ROTATIONS_PER_DAY: int = 1
#: Frames dropped by our mailbox's inbound budget in one day before the
#: mailbox is rotated (abuse, e.g. a flood by a removed member that still
#: knows the address — the new one reaches only current members).
FLOOD_ROTATE_DROPS: int = 500
#: Each claim socket pings this often (inside Cloudflare's 100 s idle
#: timeout).
CLAIM_HEARTBEAT_S: float = 30.0
_DAY_S: float = 86_400.0
#: :meth:`GfsMailboxService._register` outcomes.
REGISTER_OK: str = "ok"
REGISTER_NO_TICKET: str = "no_ticket"
REGISTER_REFUSED: str = "refused"
REGISTER_UNREACHABLE: str = "unreachable"
#: The largest generation a fresh mailbox picks (random, so a rotated
#: mailbox is unrelated to the one before).
_GEN_SPACE: int = 2**31
#: Daily counter slots (index into the per-GFS row after the day).
_RECOVERY, _ROTATION, _DROPS = 1, 2, 3

InboundHandler = Callable[[dict | bytes, MailboxKeys], Awaitable[object]]


@dataclass(slots=True)
class _Tickets:
    guard: TicketKeyGuard
    cache: MailboxTicketCache


@dataclass(slots=True)
class _Claim:
    keys: MailboxKeys
    own: OwnMailbox
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    socket: MailboxClaim | None = None
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    #: Re-registrations refused while lost (→ :data:`LOST_ROTATE_AFTER`).
    refusals: int = 0
    #: Released or rotated away (waiting for its delay): frames are dropped.
    released: bool = False


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


def _stored_proto(raw: str | None) -> int | None:
    """The protocol version the stored card ``rev`` was made under."""
    try:
        data = orjson.loads(raw) if raw else None
    except orjson.JSONDecodeError:
        return None
    proto = data.get("proto") if isinstance(data, dict) else None
    return proto if isinstance(proto, int) else None


def _parse_boxes(raw: str | None) -> tuple[int, dict[str, OwnMailbox]]:
    """``(rev, {gfs_id: OwnMailbox})`` from the stored document."""
    if not raw:
        return 0, {}
    try:
        data = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return 0, {}
    if not isinstance(data, dict):
        return 0, {}
    rev = data.get("rev")
    boxes: dict[str, OwnMailbox] = {}
    for gfs_id, box in (data.get("boxes") or {}).items():
        if not isinstance(box, dict):
            continue
        gen, url = box.get("gen"), box.get("gfs")
        if (
            isinstance(gen, int)
            and not isinstance(gen, bool)
            and gen >= 0
            and isinstance(url, str)
            and url
            and isinstance(gfs_id, str)
            and gfs_id
        ):
            boxes[gfs_id] = OwnMailbox(gen=gen, gfs_instance_id=gfs_id, gfs_url=url)
    return (rev if isinstance(rev, int) and not isinstance(rev, bool) else 0), boxes


class GfsMailboxService:
    """See the module docstring."""

    __slots__ = (
        "_after_sweep",
        "_boxes",
        "_claims",
        "_clock",
        "_config",
        "_connecting",
        "_daily",
        "_gfs",
        "_gfs_repo",
        "_identity_seed",
        "_inbound",
        "_instance_id",
        "_key_manager",
        "_lock",
        "_needed",
        "_on_change",
        "_pending",
        "_release_delay",
        "_releases",
        "_rev",
        "_rng",
        "_root",
        "_root_lock",
        "_session",
        "_stop",
        "_tasks",
        "_tickets",
        "_tickets_lock",
        "_wall_clock",
    )

    def __init__(
        self,
        *,
        instance_config: AbstractInstanceConfigRepo,
        key_manager: KeyManager,
        gfs_repo: AbstractGfsConnectionRepo,
        gfs_service: GfsConnectionService,
        instance_id: str,
        identity_seed: bytes,
        mailbox_session: aiohttp.ClientSession | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        release_delay: tuple[float, float] = RELEASE_DELAY_S,
    ) -> None:
        self._config = instance_config
        self._key_manager = key_manager
        self._gfs_repo = gfs_repo
        self._gfs = gfs_service
        self._instance_id = instance_id
        self._identity_seed = identity_seed
        self._rng = rng if rng is not None else random.SystemRandom()
        self._clock = clock
        self._wall_clock = wall_clock
        #: The session every mailbox request rides (owned: :meth:`stop`
        #: closes it): cookie-less, no keep-alive. ``None`` → the connection
        #: service's anonymous session (tests).
        self._session = mailbox_session
        self._root: bytes | None = None
        self._inbound: InboundHandler | None = None
        self._needed: Callable[[], Awaitable[set[str]]] | None = None
        self._on_change: Callable[[], Awaitable[None]] | None = None
        self._after_sweep: Callable[[], Awaitable[None]] | None = None
        #: GFS connection id → its ticket machinery.
        self._tickets: dict[str, _Tickets] = {}
        #: GFS id → our mailbox there (mirror of :data:`MAILBOXES_CONFIG_KEY`).
        self._boxes: dict[str, OwnMailbox] = {}
        #: Our member card's revision (stored with the boxes).
        self._rev = 0
        #: GFS id → its open claim.
        self._claims: dict[str, _Claim] = {}
        #: GFS id → until when a mailbox nothing needs yet is kept.
        self._pending: dict[str, float] = {}
        #: GFS id → [day, recovery registers, rotations, dropped frames].
        self._daily: dict[str, list[int]] = {}
        self._lock = asyncio.Lock()
        self._root_lock = asyncio.Lock()
        self._tickets_lock = asyncio.Lock()
        self._release_delay = release_delay
        self._releases: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
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

    # ── Wiring ───────────────────────────────────────────────────────────

    def attach_inbound(self, handler: InboundHandler) -> None:
        """Where frames on our mailboxes go (``GfsRelayInbound
        .handle_mailbox_frame``)."""
        self._inbound = handler

    def attach_needed(self, needed: Callable[[], Awaitable[set[str]]]) -> None:
        """Which GFSs (normalized base URLs) we need a mailbox at — the
        member directory's answer. Unwired → none (every mailbox is released
        once its pending time is over)."""
        self._needed = needed

    def attach_on_change(self, on_change: Callable[[], Awaitable[None]]) -> None:
        """Called right after our set of mailboxes changed (minted,
        released, rotated): the card ``rev`` moved and the new card goes to
        every space we are in now."""
        self._on_change = on_change

    def attach_after_sweep(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Run at the end of every sweep tick (the directory's backstop)."""
        self._after_sweep = hook

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load (or make) the root and our mailboxes, reopen their claims."""
        self._stop.clear()
        await self._load_root()
        raw = await self._config.get(MAILBOXES_CONFIG_KEY)
        self._rev, self._boxes = _parse_boxes(raw)
        if _stored_proto(raw) != OURS:
            # An upgrade moves the card too (it carries our version), so
            # every host gets it once more.
            async with self._lock:
                await self._store_boxes()
        for own in list(self._boxes.values()):
            await self._open_claim(own)
        self._spawn(self._sweep_loop(), "gfs-mailbox-sweep")

    async def stop(self) -> None:
        """Close every claim socket (bounded) and stop the ticket loops."""
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
                await self.sweep_once()
            except Exception:
                log.warning("gfs.mailbox: sweep failed", exc_info=True)

    async def sweep_once(self) -> None:
        """One sweep tick: the directory's backstop hook, then release and
        mint (a change publishes its card at once, :meth:`_changed`)."""
        if self._after_sweep is not None:
            try:
                await self._after_sweep()
            except Exception:
                log.warning("gfs.mailbox: directory sweep failed", exc_info=True)
        await self.release_unused()
        await self.mint_missing()

    async def _load_root(self) -> bytes:
        if self._root is not None:
            return self._root
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

    async def _store_boxes(self) -> None:
        """Persist our mailboxes and move the card ``rev`` (wall clock in
        ms, at least one past the last — it keeps rising across a restore)
        (caller holds the lock)."""
        self._rev = max(self._rev + 1, int(self._wall_clock() * 1000))
        doc = {
            "rev": self._rev,
            "proto": OURS,
            "boxes": {
                gid: {"gen": own.gen, "gfs": own.gfs_url}
                for gid, own in sorted(self._boxes.items())
            },
        }
        await self._config.set(MAILBOXES_CONFIG_KEY, orjson.dumps(doc).decode())

    async def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:
            log.warning("gfs.mailbox: publishing a changed card failed", exc_info=True)

    # ── Tickets ──────────────────────────────────────────────────────────

    async def on_connected(self, conn_id: str) -> None:
        """A GFS socket came up: make sure its ticket machinery runs and a
        household with no tickets gets a batch now. Fail-soft."""
        if conn_id in self._connecting:
            return
        self._connecting.add(conn_id)

        async def _run() -> None:
            try:
                await self._connected(conn_id)
            finally:
                self._connecting.discard(conn_id)

        self._spawn(_run(), "gfs-mailbox-connect")

    async def _connected(self, conn_id: str) -> None:
        """The anonymous issuer-key read, then the ticket fetch."""
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
            await tickets.cache.refill()
        except MailboxTicketError as exc:
            log.info("gfs.mailbox: no tickets on connect (%s)", exc.error)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.info("gfs.mailbox: no tickets on connect (%s)", type(exc).__name__)
        finally:
            await self._save_pin(conn)

    async def forget_connection(self, conn: GfsConnection) -> None:
        """The household left this GFS: stop its ticket loops and drop its
        unspent tickets (its mailbox goes with the next sweep)."""
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

    # ── Our mailboxes ────────────────────────────────────────────────────

    @property
    def rev(self) -> int:
        """Our member card's revision: moves with every mailbox change."""
        return self._rev

    def _keys(self, own: OwnMailbox) -> MailboxKeys:
        assert self._root is not None
        return derive_mailbox_keys(self._root, own.gfs_instance_id, own.gen)

    def mailbox_at(self, gfs_url: str) -> tuple[MailboxKeys, OwnMailbox] | None:
        """Our mailbox at the GFS *gfs_url* (keys + stored half), or
        ``None``."""
        if self._root is None or not gfs_url:
            return None
        want = normalize_gfs_base(gfs_url)
        for own in self._boxes.values():
            if normalize_gfs_base(own.gfs_url) == want:
                return self._keys(own), own
        return None

    def mailbox_gfs_urls(self) -> set[str]:
        """The (normalized) GFSs we hold a mailbox at."""
        return {normalize_gfs_base(own.gfs_url) for own in self._boxes.values()}

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

    async def ensure_within(self, gfs_url: str, *, timeout: float) -> OwnMailbox | None:
        """:meth:`ensure`, waiting at most *timeout* seconds. A mint that
        runs longer finishes in the background (its card change reaches the
        spaces when it is done); ``None`` on the timeout."""
        task: asyncio.Task[OwnMailbox | None] = asyncio.create_task(
            self.ensure(gfs_url), name="gfs-mailbox-mint"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            log.info("gfs.mailbox: a mint took too long — the seat goes without")
            return None

    async def ensure(self, gfs_url: str) -> OwnMailbox | None:
        """Our ONE mailbox at the GFS *gfs_url*, registered and claimed —
        minted now if we hold none there — or ``None`` when that GFS takes
        no mailboxes or no ticket could be had. A fresh mailbox is pending
        for :data:`PENDING_TTL_S` until something needs it."""
        conn = await self._connection_at(gfs_url)
        if conn is None:
            return None
        await self._load_root()
        minted = False
        async with self._lock:
            own = self._boxes.get(conn.gfs_instance_id)
            if own is None:
                own = OwnMailbox(
                    gen=self._rng.randrange(_GEN_SPACE),
                    gfs_instance_id=conn.gfs_instance_id,
                    gfs_url=normalize_gfs_base(conn.inbox_url),
                )
                keys = self._keys(own)
                if await self._register(conn, keys) != REGISTER_OK:
                    return None
                self._boxes[conn.gfs_instance_id] = own
                await self._store_boxes()
                minted = True
            await self._open_claim(own)
            self._pending[conn.gfs_instance_id] = max(
                self._pending.get(conn.gfs_instance_id, 0.0),
                self._clock() + PENDING_TTL_S,
            )
        if minted:
            await self._changed()
        return own

    async def _register(self, conn: GfsConnection, keys: MailboxKeys) -> str:
        """Register *keys*' mailbox: :data:`REGISTER_OK`, ``"no_ticket"``,
        ``"refused"`` or ``"unreachable"``. An empty ticket cache is refilled
        first."""
        try:
            tickets = await self._tickets_for(conn)
            if not len(tickets.cache):
                if not tickets.guard.has_view:
                    await tickets.guard.refresh()
                await tickets.cache.refill()
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
                "gfs.mailbox: %r refused a mailbox (%d %s)",
                conn.display_name,
                exc.status,
                exc.error,
            )
            return REGISTER_REFUSED
        except MailboxTicketError as exc:
            log.warning(
                "gfs.mailbox: no ticket for a new mailbox at %r (%s)",
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

    async def _needed_urls(self) -> set[str]:
        if self._needed is None:
            return set()
        return {normalize_gfs_base(u) for u in await self._needed() if u}

    async def release_unused(self) -> int:
        """Close and unregister every mailbox of ours nothing needs any
        more (no space we are in reaches that GFS) and that is not pending.
        Returns how many went. Fail-soft."""
        needed = await self._needed_urls()
        async with self._lock:
            now = self._clock()
            self._pending = {g: t for g, t in self._pending.items() if t > now}
            gone = [
                gid
                for gid, own in self._boxes.items()
                if normalize_gfs_base(own.gfs_url) not in needed
                and gid not in self._pending
            ]
            claims: list[_Claim] = []
            for gid in gone:
                del self._boxes[gid]
                claim = self._claims.pop(gid, None)
                if claim is not None:
                    claim.released = True
                    claims.append(claim)
            if gone:
                await self._store_boxes()
        for claim in claims:
            self._schedule_release(claim)
        if gone:
            log.info("gfs.mailbox: releasing %d mailbox(es) nothing needs", len(gone))
            await self._changed()
        return len(gone)

    def _schedule_release(self, claim: _Claim) -> None:
        task: asyncio.Task[None] = asyncio.create_task(
            self._release_claim(claim, delay=self._rng.uniform(*self._release_delay)),
            name="gfs-mailbox-release",
        )
        for bucket in (self._tasks, self._releases):
            bucket.add(task)
            task.add_done_callback(bucket.discard)

    async def mint_missing(self) -> int:
        """Mint our mailbox at ONE needed GFS where we hold none. Returns
        how many were minted (0 or 1)."""
        needed = await self._needed_urls()
        missing = sorted(needed - self.mailbox_gfs_urls())
        self._rng.shuffle(missing)
        for url in missing:
            if await self._connection_at(url) is None:
                continue
            own = await self.ensure(url)
            return 1 if own is not None else 0
        return 0

    def mint_soon(self) -> None:
        """:meth:`mint_missing` in the background (the directory just learnt
        of a GFS-reached member in an inbound handler); concurrent asks are
        safe, :meth:`ensure` mints under the lock."""
        if not self._stop.is_set():
            self._spawn(self._mint_soon(), "gfs-mailbox-mint")

    async def _mint_soon(self) -> None:
        try:
            await self.mint_missing()
        except Exception:
            log.warning("gfs.mailbox: a mint failed", exc_info=True)

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
            return
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

    async def _open_claim(self, own: OwnMailbox) -> None:
        existing = self._claims.get(own.gfs_instance_id)
        if existing is not None and existing.own == own:
            return
        claim = _Claim(keys=self._keys(own), own=own)
        self._claims[own.gfs_instance_id] = claim
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
            wait = self._rng.uniform(0.5 * backoff, 1.5 * backoff)
            try:
                await asyncio.wait_for(claim.stop.wait(), timeout=wait)
                return
            except TimeoutError:
                pass
            backoff = min(backoff * 2, CLAIM_BACKOFF_MAX_S)

    def _daily_row(self, gfs_id: str) -> list[int]:
        day = int(self._clock() // _DAY_S)
        row = self._daily.get(gfs_id)
        if row is None or row[0] != day:
            row = [day, 0, 0, 0]
            self._daily[gfs_id] = row
        return row

    def _spend(self, gfs_id: str, what: int, limit: int) -> bool:
        row = self._daily_row(gfs_id)
        if row[what] >= limit:
            return False
        row[what] += 1
        return True

    async def _recover(self, conn: GfsConnection, claim: _Claim) -> bool:
        """The GFS no longer knows our mailbox (its idle sweep while we were
        away, or data loss): register the SAME mailbox again, so frames
        resume with nothing to tell
        anyone. Frames sent to it meanwhile were dropped by the GFS (its
        uniform 202); §25.6 catch-up fills that gap. A mailbox the GFS keeps
        REFUSING is rotated. Returns whether this claim was replaced."""
        gid = claim.own.gfs_instance_id
        row = self._daily_row(gid)
        if row[_RECOVERY] >= RECOVERY_REGISTERS_PER_DAY:
            return False
        status = await self._register(conn, claim.keys)
        if status in (REGISTER_OK, REGISTER_REFUSED):  # a ticket was used
            row[_RECOVERY] += 1
        if status != REGISTER_REFUSED:
            return False
        claim.refusals += 1
        if claim.refusals < LOST_ROTATE_AFTER:
            return False
        return await self.rotate(gid)

    def note_flood(self, keys: MailboxKeys) -> None:
        """Our mailbox's inbound budget dropped a frame. Past
        :data:`FLOOD_ROTATE_DROPS` in a day the mailbox is rotated (once a
        day at most): a removed member that floods the old address does not
        get the new one."""
        for gid, claim in self._claims.items():
            if claim.keys.mailbox_id != keys.mailbox_id:
                continue
            row = self._daily_row(gid)
            row[_DROPS] += 1
            if row[_DROPS] == FLOOD_ROTATE_DROPS:
                log.warning("gfs.mailbox: a mailbox is flooded — rotating it")
                self._spawn(self._rotate_quiet(gid), "gfs-mailbox-rotate")
            return

    async def _rotate_quiet(self, gfs_id: str) -> None:
        await self.rotate(gfs_id, flooded=True)

    async def rotate(self, gfs_id: str, *, flooded: bool = False) -> bool:
        """Replace our mailbox at *gfs_id* with a fresh generation
        at most :data:`ROTATIONS_PER_DAY`. The old one keeps delivering
        (dropping, when *flooded*) until it is closed and unregistered after
        its release delay; the new card reaches every space at once.
        Returns whether it rotated."""
        async with self._lock:
            old = self._claims.get(gfs_id)
            own = self._boxes.get(gfs_id)
            if own is None:
                return False
            conn = await self._conn_for(own)
            if conn is None or not self._spend(gfs_id, _ROTATION, ROTATIONS_PER_DAY):
                return False
            fresh = OwnMailbox(
                gen=self._rng.randrange(_GEN_SPACE),
                gfs_instance_id=own.gfs_instance_id,
                gfs_url=own.gfs_url,
            )
            if await self._register(conn, self._keys(fresh)) != REGISTER_OK:
                if old is not None:
                    old.refusals = 0  # try again later
                return False
            self._boxes[gfs_id] = fresh
            await self._store_boxes()
            self._claims.pop(gfs_id, None)
            await self._open_claim(fresh)
        if old is not None:
            # The old address keeps delivering until its release delay is
            # over (members learn the new card meanwhile) — unless it is
            # being flooded, where its frames are dropped at once.
            old.released = flooded
            self._schedule_release(old)
        log.info("gfs.mailbox: rotated a mailbox")
        await self._changed()
        return True

    async def _claim_once(self, conn: GfsConnection, claim: _Claim) -> None:
        async with self._anon(conn) as session:
            if session is None:
                raise MailboxClaimError("no anonymous session")
            socket = MailboxClaim(
                session,
                conn.inbox_url,
                claim.keys,
                claim.own.gfs_instance_id,
                heartbeat=CLAIM_HEARTBEAT_S,
            )
            async with socket:
                claim.socket = socket
                claim.claimed.set()
                claim.refusals = 0
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
        except MailboxFlooded:
            self.note_flood(keys)
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
    "MAILBOXES_CONFIG_KEY",
    "MAILBOX_ROOT_CONFIG_KEY",
    "TICKET_PIN_CONFIG_PREFIX",
    "GfsMailboxService",
    "pin_from_json",
    "pin_to_json",
]
