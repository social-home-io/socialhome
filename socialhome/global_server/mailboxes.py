"""Anonymous per-space mailboxes on the GFS (GFS redesign PR 1).

A household reached through this server is addressed per (household, space,
generation) by a mailbox id instead of its instance id, so the server can
follow traffic within one space but cannot tie the household's spaces to
each other or to its identity. Wire shapes: :mod:`socialhome.domain.gfs_mailbox`.

**Register** (``POST /gfs/mailbox/register``, anonymous) — the body carries
the mailbox's Ed25519 public key and its signature over the domain-separated
``sh-mbx-register:v1`` bytes (the GFS id the household pinned, the mailbox
id, ``ts``, a nonce). The server accepts any id it answers to
(:meth:`GfsMailboxService.accepted_gfs_ids`), recomputes the mailbox id from
the key, checks the ``ts`` window (±300 s) and the nonce (a replay is
refused), verifies the proof of possession, refuses an id an instance holds
(atomically with the insert), and pins the key on first use. Registering an
existing mailbox again is a no-op success (the id is a hash of the key, so
only the key holder can produce one).

**Limits without storing an address.** Per client address — the /64
bucket and its /56 and /48 supernets (×4, ×16) — NEW mailboxes per UTC day
and live claim sockets are counted in count-min sketches keyed by a salted
hash: no address is kept even in memory, and no eviction can reset a count
(a collision only over-counts). Each node allows its share over the live
nodes. Every NEW mailbox also spends one anonymous Privacy Pass ticket
(:mod:`.mailbox_tickets`) — households registered here fetch a daily batch —
which replaced a server-wide daily cap that anyone could burn.

**Claim** (``GET /gfs/mailbox/ws``, :mod:`.routes.mailbox`) — the server
sends a random challenge; the client answers with the mailbox id, the GFS id
it signed for and a signature over ``sh-mbx-claim:v1`` bytes. An unknown
mailbox runs the same verify against a fixed dummy key, so "no such mailbox"
and "wrong key" are one answer after the same work.

**Day granularity.** ``created_at`` and ``last_claim_at`` are stored as the
start of the UTC day: a household reclaiming all its mailboxes at once
would otherwise stamp them with the same second and link them. A claim
socket held open refreshes ``last_claim_at`` once a day.

**Idle sweep** — a mailbox whose last claim (or, never claimed, its
registration) is ``mailbox_idle_days`` days old is dropped with whatever
still waits for it (:meth:`sweep`, run by the maintenance loop).
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import secrets
import time
from array import array
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from .. import crypto
from ..domain.gfs_mailbox import (
    SUPPORTED_MAILBOX_SIG_SUITES,
    InvalidMailboxField,
    UnsupportedMailboxSigSuite,
    claim_signed_bytes,
    derive_mailbox_id,
    is_mailbox_nonce,
    register_signed_bytes,
    validate_mailbox_sig_suite,
)
from .config import EnvelopeMailboxConfig
from .federation import TIMING_UNIFORM_DUMMY_KEY_HEX
from .public import node_share
from .repositories import AbstractGfsMailboxRepo

if TYPE_CHECKING:
    from .mailbox_tickets import MailboxTicketService

log = logging.getLogger(__name__)

#: ``ts`` window of a registration (the §24.11 convention).
MAILBOX_TS_WINDOW_SECONDS: int = 300
#: How long a spent registration nonce is remembered: past the ``ts``
#: window on both sides, so a replay is refused for as long as its ``ts``
#: would still pass.
MAILBOX_NONCE_TTL_SECONDS: int = 2 * MAILBOX_TS_WINDOW_SECONDS + 60
#: Most spent nonces held at once (oldest dropped first — only signed,
#: otherwise-valid registrations are ever recorded).
MAILBOX_NONCE_MAX: int = 100_000
#: One UTC day — the stored timestamp granularity and the cap window.
DAY_SECONDS: int = 86_400
#: How much more a /56 and a /48 may hold than one /64.
#: Wide on purpose (a home gets a /56, an organisation a /48): the
#: supernet caps stop one network from minting through many /64s, without
#: letting a noisy neighbour in the same /48 exhaust everybody else's room.
#: With the defaults a /56 may register 3 200 and a /48 51 200 a day — and
#: every one of them spends a ticket (:mod:`.mailbox_tickets`).
PREFIX_MULTIPLIERS: tuple[tuple[int, int], ...] = ((64, 1), (56, 16), (48, 256))
#: How long a GFS id this cluster once served stays accepted for a
#: registration / claim bound to it (``gfs_known_gfs_ids``).
KNOWN_GFS_ID_TTL_DAYS: int = 90
#: How often the accepted ids are re-read from (and written to) the table.
KNOWN_GFS_ID_REFRESH_S: float = 60.0
#: Count-min sketch shape: 4 rows × 65 536 counters (1 MiB as uint32).
SKETCH_DEPTH: int = 4
SKETCH_WIDTH: int = 1 << 16

_DUMMY_KEY = bytes.fromhex(TIMING_UNIFORM_DUMMY_KEY_HEX)


def day_floor(ts: float) -> int:
    """*ts* rounded down to the start of its UTC day."""
    return int(ts) // DAY_SECONDS * DAY_SECONDS


def address_scopes(bucket: str) -> list[tuple[str, int]]:
    """The counting scopes of a client *bucket* with their limit
    multipliers: an IPv6 /64 bucket counts against itself, its /56 and its
    /48; anything else (IPv4, unknown) against itself only."""
    try:
        net = ipaddress.ip_network(bucket, strict=False)
    except ValueError:
        return [(bucket, 1)]
    if not isinstance(net, ipaddress.IPv6Network) or net.prefixlen < 64:
        return [(bucket, 1)]
    return [(str(net.supernet(new_prefix=p)), m) for p, m in PREFIX_MULTIPLIERS]


class CountMinSketch:
    """Fixed-size, salted counting structure: ``estimate`` never
    under-counts (collisions only over-count), nothing can be evicted, and
    no key is stored — only counters under a per-process random salt."""

    __slots__ = ("_rows", "_salt")

    def __init__(self) -> None:
        self._salt = secrets.token_bytes(16)
        self._rows = [array("I", bytes(4 * SKETCH_WIDTH)) for _ in range(SKETCH_DEPTH)]

    def _slots(self, key: str) -> list[int]:
        digest = hashlib.blake2b(
            key.encode(), key=self._salt, digest_size=4 * SKETCH_DEPTH
        ).digest()
        return [
            int.from_bytes(digest[4 * i : 4 * i + 4], "big") % SKETCH_WIDTH
            for i in range(SKETCH_DEPTH)
        ]

    def estimate(self, key: str) -> int:
        return min(row[i] for row, i in zip(self._rows, self._slots(key), strict=True))

    def add(self, key: str, n: int = 1) -> None:
        for row, i in zip(self._rows, self._slots(key), strict=True):
            row[i] = max(0, row[i] + n)

    def clear(self) -> None:
        self._rows = [array("I", bytes(4 * SKETCH_WIDTH)) for _ in range(SKETCH_DEPTH)]


class MailboxRefused(Exception):
    """A registration this server will not take: ``status`` + a stable
    machine-readable ``error``."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


class GfsMailboxService:
    """Register mailboxes, verify claims, count sockets, sweep idle ones."""

    __slots__ = (
        "_aliases",
        "_cfg",
        "_clock",
        "_gfs_instance_id",
        "_ids_at",
        "_ids_cache",
        "_ids_written",
        "_new_day",
        "_new_sketch",
        "_nodes",
        "_repo",
        "_siblings",
        "_socket_sketch",
        "_spent_nonces",
        "_tickets",
    )

    def __init__(
        self,
        *,
        repo: AbstractGfsMailboxRepo,
        gfs_instance_id: str,
        config: EnvelopeMailboxConfig,
        tickets: MailboxTicketService,
        aliases: Iterable[str] = (),
        sibling_ids: Callable[[], Iterable[str]] | None = None,
        nodes: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._tickets = tickets
        self._gfs_instance_id = gfs_instance_id
        self._aliases = frozenset(aliases)
        #: The public ids of cluster siblings that share our identity key —
        #: they ARE this GFS, so a mailbox key bound to one of their ids is
        #: bound to us (a cluster whose nodes still serve different ids).
        self._siblings = sibling_ids
        #: Accepted ids as last read from ``gfs_known_gfs_ids``, and when.
        self._ids_cache: frozenset[str] = frozenset()
        self._ids_at = -KNOWN_GFS_ID_REFRESH_S - 1.0
        #: id → the day this node last wrote it (one write per id per day).
        self._ids_written: dict[str, int] = {}
        self._cfg = config
        self._nodes = nodes
        self._clock = clock
        #: ``mailbox_id|nonce`` → unix time it was spent (insertion order is
        #: time order, so the oldest are dropped first).
        self._spent_nonces: dict[str, float] = {}
        #: New mailboxes per address scope in the current UTC day.
        self._new_sketch = CountMinSketch()
        self._new_day = day_floor(clock())
        #: Live claim sockets per address scope (added on accept, removed on
        #: close — exact inverse, so it never drifts).
        self._socket_sketch = CountMinSketch()

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def gfs_instance_id(self) -> str:
        """This server's public instance id (sent in the challenge)."""
        return self._gfs_instance_id

    async def accepted_gfs_ids(self) -> frozenset[str]:
        """Every GFS id a signed registration / claim may name: ours, our
        ``instance_id_aliases``, the ids our shared-key siblings serve now —
        and every id this cluster served in the last
        :data:`KNOWN_GFS_ID_TTL_DAYS` days (``gfs_known_gfs_ids`` in the
        shared DB), so a household pinned to a retired alloc's id can still
        claim. Each node writes its own id and its live siblings' (verified
        under our identity key) at most once a day per id."""
        live = {self._gfs_instance_id, *self._aliases}
        if self._siblings is not None:
            live.update(i for i in self._siblings() if isinstance(i, str) and i)
        now = self._clock()
        if now - self._ids_at >= KNOWN_GFS_ID_REFRESH_S or not live <= self._ids_cache:
            today = day_floor(now)
            fresh = {i for i in live if self._ids_written.get(i) != today}
            if fresh:
                await self._repo.remember_gfs_ids(fresh, day=today)
                for i in fresh:
                    self._ids_written[i] = today
            since = today - KNOWN_GFS_ID_TTL_DAYS * DAY_SECONDS
            known = await self._repo.known_gfs_ids(since_day=since)
            self._ids_cache = frozenset(known | live)
            self._ids_at = now
        return self._ids_cache | live

    def capability(self) -> dict | None:
        """The signed ``/gfs/info`` ``envelope_mailbox`` entry, ``None`` off."""
        if not self._cfg.enabled:
            return None
        return {
            "sig_suites": sorted(SUPPORTED_MAILBOX_SIG_SUITES),
            "idle_days": self._cfg.mailbox_idle_days,
        }

    def _share(self, limit: int) -> int:
        return node_share(limit, self._nodes()) if self._nodes is not None else limit

    # ── Register ─────────────────────────────────────────────────────────

    async def register(self, body: object, *, client_bucket: str) -> str:
        """Validate a registration *body* and pin its key. Returns the
        mailbox id; raises :class:`MailboxRefused`. *client_bucket* only
        feeds the in-memory per-address sketch."""
        if not isinstance(body, dict):
            raise MailboxRefused(400, "invalid_body")
        try:
            validate_mailbox_sig_suite(body.get("sig_suite"))
        except UnsupportedMailboxSigSuite as exc:
            raise MailboxRefused(400, "unsupported_sig_suite") from exc
        sign_pk = _b64(body.get("sign_pk"))
        sig = _b64(body.get("sig"))
        if sign_pk is None or len(sign_pk) != 32 or sig is None:
            raise MailboxRefused(400, "invalid_key")
        if not crypto.is_valid_ed25519_public_key(sign_pk):
            raise MailboxRefused(400, "invalid_key")
        mailbox_id = derive_mailbox_id(sign_pk)
        if body.get("mailbox_id") != mailbox_id:
            raise MailboxRefused(400, "mailbox_id_mismatch")
        gfs_id = body.get("gfs_instance_id")
        if not isinstance(gfs_id, str) or gfs_id not in await self.accepted_gfs_ids():
            raise MailboxRefused(403, "wrong_gfs")
        ts = body.get("ts")
        nonce = body.get("nonce")
        if not isinstance(ts, int) or isinstance(ts, bool):
            raise MailboxRefused(400, "invalid_ts")
        now = self._clock()
        if abs(now - ts) > MAILBOX_TS_WINDOW_SECONDS:
            raise MailboxRefused(400, "stale_ts")
        if not isinstance(nonce, str) or not is_mailbox_nonce(nonce):
            raise MailboxRefused(400, "invalid_nonce")
        try:
            signed = register_signed_bytes(gfs_id, mailbox_id, ts, nonce)
        except InvalidMailboxField as exc:
            raise MailboxRefused(400, "invalid_body") from exc
        if not crypto.verify_ed25519(sign_pk, signed, sig):
            raise MailboxRefused(403, "bad_signature")
        # Only now — a signed, in-window registration — is the nonce spent,
        # so junk can never fill the replay cache.
        if not self._spend_nonce(f"{mailbox_id}|{nonce}", now):
            raise MailboxRefused(409, "replay")
        existing = await self._repo.get(mailbox_id)
        if existing is not None:
            if existing.sign_pk != sign_pk:  # a SHA-256 collision; never
                raise MailboxRefused(409, "id_in_use")
            return mailbox_id
        # A NEW mailbox costs one anonymous ticket (:mod:`.mailbox_tickets`),
        # checked before anything is counted and spent only once the
        # per-address backstop let it through.
        nonce_t, expires_at = await self._tickets.check(
            body.get("ticket_suite"), body.get("ticket")
        )
        scopes = self._count_new(client_bucket, now)
        if scopes is None:
            raise MailboxRefused(429, "mailbox_cap")
        try:
            await self._tickets.spend(nonce_t, expires_at)
            outcome = await self._repo.register(mailbox_id, sign_pk, now=day_floor(now))
        except BaseException:
            for key in scopes:  # nothing was created: give the count back
                self._new_sketch.add(key, -1)
            raise
        if outcome == "collision":
            for key in scopes:
                self._new_sketch.add(key, -1)
            raise MailboxRefused(409, "id_in_use")
        return mailbox_id

    def _count_new(self, bucket: str, now: float) -> list[str] | None:
        """Count one new mailbox against every scope of *bucket*: the scope
        keys counted, or ``None`` (nothing counted) when any scope is at its
        share for today."""
        day = day_floor(now)
        if day != self._new_day:
            self._new_sketch.clear()
            self._new_day = day
        scopes = address_scopes(bucket)
        base = self._cfg.mailbox_max_per_ip
        for key, mult in scopes:
            if self._new_sketch.estimate(key) >= self._share(base * mult):
                return None
        keys = [key for key, _mult in scopes]
        for key in keys:
            self._new_sketch.add(key)
        return keys

    def _spend_nonce(self, key: str, now: float) -> bool:
        """Record *key*; ``False`` when it was already spent."""
        horizon = now - MAILBOX_NONCE_TTL_SECONDS
        spent = self._spent_nonces
        while spent:
            oldest = next(iter(spent))
            if spent[oldest] >= horizon and len(spent) < MAILBOX_NONCE_MAX:
                break
            del spent[oldest]
        if key in spent:
            return False
        spent[key] = now
        return True

    # ── Claim sockets ────────────────────────────────────────────────────

    def acquire_socket(self, bucket: str) -> list[str] | None:
        """Reserve a claim socket for *bucket*: the scope keys to hand back
        to :meth:`release_socket`, or ``None`` when a scope is at its share
        of ``mailbox_max_sockets_per_ip``."""
        scopes = address_scopes(bucket)
        base = self._cfg.mailbox_max_sockets_per_ip
        for key, mult in scopes:
            if self._socket_sketch.estimate(key) >= self._share(base * mult):
                return None
        keys = [key for key, _mult in scopes]
        for key in keys:
            self._socket_sketch.add(key)
        return keys

    def release_socket(self, keys: list[str]) -> None:
        for key in keys:
            self._socket_sketch.add(key, -1)

    async def verify_claim(self, claim: dict, challenge: str) -> str | None:
        """The mailbox id *claim* proves possession of for *challenge*, or
        ``None``. An unknown mailbox (or a GFS id we do not answer to)
        verifies against a dummy key, so it costs what a wrong signature
        costs. The caller checks the suite first."""
        mailbox_id = claim.get("mailbox_id")
        sig = _b64(claim.get("sig"))
        gfs_id = claim.get("gfs_instance_id")
        if not isinstance(gfs_id, str):
            gfs_id = self._gfs_instance_id
        if not isinstance(mailbox_id, str) or not crypto.is_instance_id(mailbox_id):
            return None
        try:
            signed = claim_signed_bytes(gfs_id, mailbox_id, challenge)
        except InvalidMailboxField:
            return None
        box = await self._repo.get(mailbox_id)
        ours = gfs_id in await self.accepted_gfs_ids()
        key = box.sign_pk if box is not None and ours else _DUMMY_KEY
        verified = sig is not None and crypto.verify_ed25519(key, signed, sig)
        if box is None or not ours or not verified:
            return None
        return mailbox_id

    async def note_claimed(self, mailbox_id: str) -> int:
        """A claim succeeded (or its socket is still held): stamp today's
        date, which keeps the mailbox out of the idle sweep. Returns the day
        written."""
        today = day_floor(self._clock())
        await self._repo.touch_claim(mailbox_id, now=today)
        return today

    async def refresh_claim(self, mailbox_id: str, last_day: int) -> int:
        """While a claim socket stays open: re-stamp at most once a UTC day.
        Returns the day now on record."""
        if day_floor(self._clock()) == last_day:
            return last_day
        return await self.note_claimed(mailbox_id)

    # ── Sweep ────────────────────────────────────────────────────────────

    async def sweep(self, *, now: int) -> int:
        """Drop mailboxes idle for ``mailbox_idle_days``. Returns how many."""
        older_than = day_floor(now) - self._cfg.mailbox_idle_days * DAY_SECONDS
        dropped = await self._repo.prune_idle(older_than=older_than)
        if dropped:
            log.info("gfs.mailbox: swept %d idle mailbox(es)", dropped)
        return dropped


def _b64(value: object) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > 256:
        return None
    try:
        return crypto.b64url_decode(value)
    except ValueError, TypeError:
        return None
