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
  (:meth:`remember`) or the redeem failed (:meth:`abandon`).
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
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import aiohttp
import orjson

from ..crypto import b64url_decode, b64url_encode
from ..domain.federation import GfsConnection
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
from ..federation.space_mailbox_entry import build_entry
from ..infrastructure.key_manager import KeyManager
from ..repositories.gfs_connection_repo import AbstractGfsConnectionRepo
from ..repositories.instance_config_repo import AbstractInstanceConfigRepo
from ..repositories.space_repo import AbstractSpaceRepo
from .gfs_connection_service import GfsConnectionService
from .gfs_envelope_sender import normalize_gfs_base

log = logging.getLogger(__name__)

#: ``instance_config`` key of the KEK-wrapped mailbox root.
MAILBOX_ROOT_CONFIG_KEY: str = "gfs_mailbox_root"
#: ``instance_config`` key prefix of a GFS's persisted ticket-key pin
#: (+ that GFS's pinned instance id).
TICKET_PIN_CONFIG_PREFIX: str = "gfs_mailbox_ticket_pin:"
#: Tickets one GFS's cache holds (a ticket buys one new mailbox).
TICKET_BATCH: int = 8
#: Claim-socket reconnect backoff.
CLAIM_BACKOFF_MIN_S: float = 2.0
CLAIM_BACKOFF_MAX_S: float = 300.0
#: The largest generation a fresh mailbox picks (random, so a re-joined
#: space gets an unrelated mailbox and the GFS cannot tie the two).
_GEN_SPACE: int = 2**31

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


def _claim_key(space_id: str, own: OwnMailbox) -> tuple[str, str, int]:
    return (space_id, own.gfs_instance_id, own.gen)


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
        "_claims",
        "_config",
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
        "_rng",
        "_root",
        "_spaces",
        "_tickets",
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
        rng: random.Random | None = None,
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
        self._root: bytes | None = None
        self._inbound: InboundHandler | None = None
        #: GFS connection id → its ticket machinery.
        self._tickets: dict[str, _Tickets] = {}
        #: (space, gfs id, gen) → our open claim.
        self._claims: dict[tuple[str, str, int], _Claim] = {}
        #: Claims minted for a seat not written yet (never released).
        self._pending: set[tuple[str, str, int]] = set()
        #: (space, instance) → the counterpart's entry (mirror of the rows).
        self._peers: dict[tuple[str, str], SpaceMailboxEntry] = {}
        self._lock = asyncio.Lock()

    def attach_inbound(self, handler: InboundHandler) -> None:
        """Where frames on our mailboxes go (``GfsRelayInbound
        .handle_mailbox_frame``)."""
        self._inbound = handler

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load (or make) the root, mirror the rows, reopen our claims."""
        await self._load_root()
        rows = await self._load_rows()
        for space_id, _instance_id, own in rows:
            if own is not None:
                await self._open_claim(space_id, own)

    async def stop(self) -> None:
        """Close every claim socket and stop the ticket loops."""
        claims = list(self._claims.values())
        self._claims.clear()
        for claim in claims:
            await self._close_claim(claim)
        tickets = list(self._tickets.values())
        self._tickets.clear()
        for t in tickets:
            await t.cache.stop()
            await t.guard.stop()

    async def _load_root(self) -> bytes:
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
        existing = self._tickets.get(conn.id)
        if existing is not None:
            return existing
        pin = pin_from_json(
            await self._config.get(TICKET_PIN_CONFIG_PREFIX + conn.gfs_instance_id)
        )
        gfs = self._gfs

        async def _anon_view():
            async with gfs.anon_session(conn) as session:
                if session is None:
                    raise MailboxTicketError(0, "no_anonymous_session")
                return await read_ticket_issuer(
                    session, conn.inbox_url, gfs_public_key=conn.public_key
                )

        guard = TicketKeyGuard(_anon_view, pin=pin, rng=self._rng)

        async def _fetch(count: int) -> list[bytes]:
            issuer = await read_ticket_issuer(
                gfs.client(), conn.inbox_url, gfs_public_key=conn.public_key
            )
            got = await fetch_tickets(
                gfs.client(),
                conn.inbox_url,
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

    # ── Mint / remember / abandon ────────────────────────────────────────

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

    async def mint(
        self, space_id: str, gfs_url: str
    ) -> tuple[SpaceMailboxEntry, OwnMailbox] | None:
        """Our mailbox for *space_id* at the GFS *gfs_url*, registered and
        claimed, and its signed entry — or ``None`` when that GFS takes no
        mailboxes or no ticket could be had (the pair then keeps today's
        instance-addressed relay). Reuses the generation we already hold for
        the space there (a host with several GFS-only members has ONE
        mailbox per space). The result stays pending until :meth:`remember`
        or :meth:`abandon`."""
        conn = await self._connection_at(gfs_url)
        if conn is None:
            return None
        root = await self._load_root()
        async with self._lock:
            own = await self._own_for(space_id, conn.gfs_instance_id)
            if own is None:
                own = OwnMailbox(
                    gen=self._rng.randrange(_GEN_SPACE),
                    gfs_instance_id=conn.gfs_instance_id,
                    gfs_url=normalize_gfs_base(conn.inbox_url),
                )
            key = _claim_key(space_id, own)
            try:
                keys = derive_mailbox_keys(root, own.gfs_instance_id, space_id, own.gen)
            except ValueError:
                log.info("gfs.mailbox: this space id cannot carry a mailbox")
                return None
            if key not in self._claims:
                if not await self._register(conn, keys):
                    return None
                await self._open_claim(space_id, own, keys=keys)
            self._pending.add(key)
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

    async def _own_for(self, space_id: str, gfs_id: str) -> OwnMailbox | None:
        """The generation we already hold for *space_id* at *gfs_id* (an open
        claim, or a row), or ``None``."""
        for (space, gid, _gen), claim in self._claims.items():
            if space == space_id and gid == gfs_id:
                return claim.own
        for space, _instance, own in await self._load_rows():
            if space == space_id and own is not None and own.gfs_instance_id == gfs_id:
                return own
        return None

    async def _register(self, conn: GfsConnection, keys: MailboxKeys) -> bool:
        try:
            tickets = await self._tickets_for(conn)
            async with self._gfs.anon_session(conn) as session:
                if session is None:
                    log.warning("gfs.mailbox: no anonymous session — not registering")
                    return False
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
            return False
        except MailboxTicketError as exc:
            log.warning(
                "gfs.mailbox: no ticket for a new mailbox at %r (%s) — the space "
                "keeps the instance-addressed relay",
                conn.display_name,
                exc.error,
            )
            return False
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.warning(
                "gfs.mailbox: could not reach %r to register a mailbox (%s)",
                conn.display_name,
                type(exc).__name__,
            )
            return False
        finally:
            await self._save_pin(conn)
        return True

    async def remember(
        self,
        space_id: str,
        instance_id: str,
        *,
        peer: SpaceMailboxEntry | None,
        own: OwnMailbox | None,
    ) -> None:
        """Write the pair's mailbox state onto its ``space_instances`` row
        (which must exist) and end the pending state of *own*."""
        if peer is None and own is None:
            return
        value = orjson.dumps(
            {
                "peer": peer.to_wire() if peer is not None else None,
                "own": own_to_wire(own) if own is not None else None,
            }
        ).decode()
        await self._spaces.set_space_instance_mailbox(space_id, instance_id, value)
        if peer is not None:
            self._peers[(space_id, instance_id)] = peer
        if own is not None:
            self._pending.discard(_claim_key(space_id, own))

    async def abandon(self, space_id: str, own: OwnMailbox | None) -> None:
        """A mint whose seat never happened (the redeem failed): drop it
        unless a row refers to it after all."""
        if own is None:
            return
        self._pending.discard(_claim_key(space_id, own))
        await self.release_unused()

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
        """Close and unregister every mailbox of ours no row refers to any
        more (not pending). Returns how many went. Fail-soft."""
        async with self._lock:
            rows = await self._load_rows()
            used = {
                _claim_key(space, own) for space, _i, own in rows if own is not None
            }
            gone = [
                (key, claim)
                for key, claim in self._claims.items()
                if key not in used and key not in self._pending
            ]
            for key, _claim in gone:
                del self._claims[key]
        for _key, claim in gone:
            await self._close_claim(claim)
            await self._unregister(claim)
        if gone:
            log.info("gfs.mailbox: dropped %d mailbox(es) no seat uses", len(gone))
        return len(gone)

    async def _unregister(self, claim: _Claim) -> None:
        conn = await self._conn_for(claim.own)
        if conn is None:
            return  # the GFS's idle sweep removes it
        try:
            async with self._gfs.anon_session(conn) as session:
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
        self, space_id: str, own: OwnMailbox, *, keys: MailboxKeys | None = None
    ) -> None:
        key = _claim_key(space_id, own)
        if key in self._claims:
            return
        if keys is None:
            root = await self._load_root()
            keys = derive_mailbox_keys(root, own.gfs_instance_id, space_id, own.gen)
        claim = _Claim(keys=keys, own=own)
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
                await asyncio.wait_for(claim.task, timeout=5.0)
            except TimeoutError, asyncio.CancelledError:
                claim.task.cancel()

    async def _claim_loop(self, claim: _Claim) -> None:
        backoff = CLAIM_BACKOFF_MIN_S
        reregistered = False
        while not claim.stop.is_set():
            conn = await self._conn_for(claim.own)
            try:
                if conn is not None:
                    await self._claim_once(conn, claim)
                    backoff = CLAIM_BACKOFF_MIN_S
                    reregistered = False
            except MailboxClaimError as exc:
                log.info("gfs.mailbox: claim refused (%s)", exc)
                # Swept while we were away (idle), or never registered:
                # register again once (a new mailbox spends a ticket).
                if conn is not None and not reregistered:
                    reregistered = True
                    await self._register(conn, claim.keys)
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

    async def _claim_once(self, conn: GfsConnection, claim: _Claim) -> None:
        async with self._gfs.anon_session(conn) as session:
            if session is None:
                raise MailboxClaimError("no anonymous session")
            socket = MailboxClaim(
                session, conn.inbox_url, claim.keys, claim.own.gfs_instance_id
            )
            async with socket:
                claim.socket = socket
                claim.claimed.set()
                if claim.stop.is_set():
                    return
                async for frame in socket:
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
