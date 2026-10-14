"""Household dashboard sessions — the signed tablet cookie + login throttle.

The dashboard has no user accounts: one household password unlocks it,
and a signed cookie keeps the tablet signed in.

Cookie (``sh_dash``) = ``b64url(payload) "." b64url(HMAC-SHA256(key,
payload))`` where ``payload`` is compact JSON::

    {"v": 1, "e": <session epoch>, "i": <issued-at unix int>, "k": <keep>,
     "l": <locked>, "n": <session id>}

``l`` is the privacy lock: a locked tablet shows only the lock screen
until someone types the PIN. It lives in the cookie so it is per tablet
and survives reloads and browser restarts. ``n`` is a random session id,
minted at sign-in, lock and unlock and carried unchanged by renewal;
revocation and the PIN throttle key on it. Accepted
shapes are exactly ``{v,e,i,k}`` (pre-lock cookies, verified as
unlocked), ``{v,e,i,k,l}`` and ``{v,e,i,k,l,n}``; anything else (or a
wrong type) is refused.

The key is the KEK-wrapped dashboard cookie secret
(:meth:`DashboardSettingsService.cookie_secret`); bumping the session
epoch signs every tablet out — durably. On top, single sessions can be
revoked in memory by session (:meth:`DashboardAuth.revoke`: replaced by a locked
one, or signed out after too many wrong PINs); a restart forgets that.
To add a claim later, add it to :data:`_PAYLOAD_TYPES`, :data:`_SHAPES`
and :class:`SessionClaims` (and bump ``v`` if old cookies must not
verify).

The login throttle is in memory and keyed per client address
(:func:`socialhome.rate_limiter.client_bucket`): five failures within a
rolling 15 minutes lock that client out until the oldest one ages out.
On top, a global brake: more than thirty failures across *all* clients
in that window holds every client back (a guesser with many addresses),
and password checks (scrypt) run one at a time so a flood of logins
can't pin every CPU. A tablet with a valid cookie never logs in, so the
brakes don't touch it. Privacy-lock PIN guesses use the same limit per
lock — the locked session's id (:meth:`DashboardAuth.pin_throttle_key`),
never the client address — and never the global brake; the PIN check
shares the scrypt slot.

PIN buckets live in a table of their own (:data:`PIN_THROTTLE_MAX_KEYS`)
that never forgets a bucket with a live failure: when it is full a new
lock is asked to wait instead (fail closed). The login table makes room
by evicting clients under the limit — fine for a password guesser, who
gains nothing a fresh client address wouldn't give — but a PIN bucket
is the lock's whole budget, so evicting one would hand its guesser five
fresh tries (a flood of client addresses could force that).

"Got it" taps on a mention (the dashboard's one data write) have their
own small budget per session (:meth:`DashboardAuth.begin_ack`):
:data:`ACK_MAX` in :data:`ACK_WINDOW_S` — far above any real household,
but a stuck or scripted tablet can't flood the chat with writes. Map
tiles (Home's Map tab) get a budget per session too
(:meth:`DashboardAuth.begin_tile`): :data:`TILE_MAX` in
:data:`TILE_WINDOW_S`, the main app's ``/api/map/tiles`` limit — every
tile the wall can't serve from the shared cache is a fetch from the
upstream tile server, whose policy we keep. A wall panel asking for a
pairing code (anonymous) is held to :data:`PAIR_MAX` per
:data:`PAIR_WINDOW_S` per client address (:meth:`DashboardAuth.begin_pair`).
Wrong pairing codes an admin types (Admin › Dashboard) count against one
global bucket of their own (:meth:`DashboardAuth.begin_pair_code`):
:data:`PAIR_CODE_MAX_FAILURES` in :data:`PAIR_CODE_WINDOW_S`, fixed size,
and fully separate from the login / PIN failure table — a flood of failed
tablet logins can't hold pairing back, nor wrong codes hold back a login.
A paired panel's reads are budgeted per panel
(:meth:`DashboardAuth.begin_display_read`): :data:`DISPLAY_READ_MAX` JSON
reads and :data:`DISPLAY_IMAGE_MAX` images per :data:`DISPLAY_WINDOW_S`,
and of those images at most :data:`MAP_RENDER_MAX` freshly painted map
pictures (:meth:`DashboardAuth.begin_map_render` — painting is the
expensive part; a kept picture or a ``304`` is not counted).
A panel's PIN guesses use the tablet PIN limit, keyed on the panel id
(:meth:`DashboardAuth.display_pin_throttle_key`), and its "Got it" taps
the tablet's ack budget (:meth:`DashboardAuth.begin_display_ack`).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import asyncio
import math
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from ..rate_limiter import RateLimiter

if TYPE_CHECKING:
    from ..services.dashboard_settings_service import DashboardSettingsService

log = logging.getLogger(__name__)

__all__ = [
    "ACK_MAX",
    "ACK_WINDOW_S",
    "TILE_MAX",
    "TILE_WINDOW_S",
    "COOKIE_NAME",
    "GLOBAL_MAX_FAILURES",
    "PAIR_CODE_MAX_FAILURES",
    "PAIR_CODE_WINDOW_S",
    "PAIR_MAX",
    "DISPLAY_IMAGE_MAX",
    "DISPLAY_READ_MAX",
    "DISPLAY_WINDOW_S",
    "MAP_RENDER_MAX",
    "PAIR_WINDOW_S",
    "KEEP_TTL_S",
    "MAX_CLOCK_SKEW_S",
    "PRUNE_INTERVAL_S",
    "RENEW_AFTER_S",
    "REVOCATION_MAX",
    "REVOKED_LOCKED",
    "REVOKED_SIGNED_OUT",
    "SESSION_TTL_S",
    "THROTTLE_MAX_FAILURES",
    "THROTTLE_MAX_KEYS",
    "PIN_THROTTLE_MAX_KEYS",
    "THROTTLE_WINDOW_S",
    "DashboardAuth",
    "SessionClaims",
]

COOKIE_NAME = "sh_dash"

#: Lifetime of a "don't keep me signed in" session.
SESSION_TTL_S = 12 * 3600
#: Lifetime of a "keep this tablet signed in" session.
KEEP_TTL_S = 365 * 86400
#: A valid cookie older than this is re-issued on the next response.
RENEW_AFTER_S = 24 * 3600
#: How far in the future an ``issued_at`` may lie (clock drift).
MAX_CLOCK_SKEW_S = 300

THROTTLE_MAX_FAILURES = 5
THROTTLE_WINDOW_S = 15 * 60
THROTTLE_MAX_KEYS = 10_000
#: PIN buckets (one per locked tablet session / paired panel) kept at
#: once — a table apart from the login one, which never evicts a bucket
#: with a live failure (:class:`_FailureTable`).
PIN_THROTTLE_MAX_KEYS = 10_000
#: Throttle keys with these prefixes are PIN buckets
#: (:meth:`DashboardAuth.pin_throttle_key`,
#: :meth:`DashboardAuth.display_pin_throttle_key`).
_PIN_KEY_PREFIXES = ("pin:", "pin-display:")
#: Failures across all clients per window before everyone is held back.
GLOBAL_MAX_FAILURES = 30
#: With the client table full of locked-out clients, newcomers are
#: refused (fail closed) and the table is rescanned at most this often.
PRUNE_INTERVAL_S = 10.0

#: "Got it" writes one session may make per window (:meth:`DashboardAuth.begin_ack`).
ACK_MAX = 30
ACK_WINDOW_S = 300

#: Map tiles one session may load per window (:meth:`DashboardAuth.begin_tile`)
#: — the main app's ``/api/map/tiles`` rate limit.
TILE_MAX = 1200
TILE_WINDOW_S = 60

#: Wall-panel pairing starts (``POST /api/display/v1/pair``, anonymous)
#: one client address may make per window (:meth:`DashboardAuth.begin_pair`)
#: — a panel needs one per attempt; this only stops a flood from pushing
#: real panels' codes out of the bounded pending list.
PAIR_MAX = 10
PAIR_WINDOW_S = 600

#: Wrong wall-panel pairing codes (every admin together) per window
#: (:meth:`DashboardAuth.begin_pair_code`).
PAIR_CODE_MAX_FAILURES = 5
PAIR_CODE_WINDOW_S = 15 * 60
#: A paired wall panel's reads per window, per panel
#: (:meth:`DashboardAuth.begin_display_read`). It polls a board every 30 s
#: and opens a few more on a tap; images load when they change.
DISPLAY_READ_MAX = 120
DISPLAY_IMAGE_MAX = 600
DISPLAY_WINDOW_S = 60
#: Map pictures one panel may have painted per :data:`DISPLAY_WINDOW_S`
#: (:meth:`DashboardAuth.begin_map_render`): a new size or scheme, or what
#: the map shows changing. A 30 s poll of an unchanged map paints nothing.
MAP_RENDER_MAX = 6

#: Revocation reasons (:meth:`DashboardAuth.revoke`). ``locked``: the
#: cookie was replaced by a locked one (``POST /lock``) — it now counts
#: as locked, so a late response re-sending it can't lift the lock.
#: ``signed_out``: too many wrong PINs — it no longer counts at all.
REVOKED_LOCKED = "locked"
REVOKED_SIGNED_OUT = "signed_out"
_REVOCATION_REASONS = frozenset({REVOKED_LOCKED, REVOKED_SIGNED_OUT})
#: Most revoked cookie signatures kept in memory.
REVOCATION_MAX = 10_000

_PAYLOAD_VERSION = 1
#: Exact claim set and the type of each value (``bool`` checked exactly,
#: so ``True`` is never accepted as an int nor ``1`` as a bool).
_PAYLOAD_TYPES: dict[str, type] = {
    "v": int,
    "e": int,
    "i": int,
    "k": bool,
    "l": bool,
    "n": str,
}
#: The exact claim sets a cookie may carry: pre-lock cookies
#: (``{v,e,i,k}``), the first lock cookies (``+l``) and current ones
#: (``+l`` and the session id ``n``). Nothing else.
_SHAPES = (
    frozenset({"v", "e", "i", "k"}),
    frozenset({"v", "e", "i", "k", "l"}),
    frozenset({"v", "e", "i", "k", "l", "n"}),
)
#: ``n`` is the session id: random, minted at sign-in, ``/lock`` and
#: ``/unlock``, carried unchanged by renewal. Revocation and the PIN
#: throttle key on it, so they cover every renewed copy of a session.
_NONCE_BYTES = 16
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,32}")
#: Length of the session id derived for a cookie without ``n``.
_DERIVED_ID_LEN = 22
#: Far above any real cookie; refuses absurd inputs before decoding.
_MAX_COOKIE_LEN = 1024
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(slots=True, frozen=True)
class SessionClaims:
    """The verified contents of a dashboard session cookie."""

    epoch: int
    issued_at: int
    keep: bool
    locked: bool = False
    #: The session id (claim ``n``; for an older cookie without one,
    #: derived from its signature). Revocation and the per-lock PIN
    #: throttle key on it. Not part of equality or ``repr``.
    session_id: str = field(default="", compare=False, repr=False)


def _ttl(claims: SessionClaims) -> int:
    return KEEP_TTL_S if claims.keep else SESSION_TTL_S


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes | None:
    """Strict unpadded base64url decode; ``None`` on anything else."""
    if _B64URL.fullmatch(text) is None:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except binascii.Error, ValueError:
        return None


def _parse_payload(body: bytes) -> SessionClaims | None:
    try:
        data = json.loads(body)
    except ValueError:  # covers JSONDecodeError and UnicodeDecodeError
        return None
    if not isinstance(data, dict):
        return None
    keys = frozenset(data)
    if keys not in _SHAPES:
        return None
    for key in keys:
        if type(data[key]) is not _PAYLOAD_TYPES[key]:
            return None
    if data["v"] != _PAYLOAD_VERSION:
        return None
    if "n" in data and _NONCE.fullmatch(data["n"]) is None:
        return None
    return SessionClaims(
        epoch=data["e"],
        issued_at=data["i"],
        keep=data["k"],
        locked=data.get("l", False),
        session_id=data.get("n", ""),
    )


class _FailureTable:
    """Failure timestamps per throttle key, bounded at ``max_keys``.

    ``evict_unlocked``: whether making room may drop a key that still
    has live failures under the limit (the login table: a password
    guesser gets nothing from it a fresh client address wouldn't give).
    Without it (the PIN table) only keys whose failures all aged out go;
    a full table of live keys refuses newcomers instead.
    """

    __slots__ = ("buckets", "_max_keys", "_evict_unlocked", "_no_room_until", "_what")

    def __init__(self, max_keys: int, *, evict_unlocked: bool, what: str) -> None:
        # Insertion-ordered: eviction walks it from the front.
        self.buckets: dict[str, deque[float]] = {}
        self._max_keys = max_keys
        self._evict_unlocked = evict_unlocked
        self._no_room_until = 0.0
        self._what = what

    def live(self, key: str, now: float) -> deque[float] | None:
        """``key``'s failures still inside the window (pruned in place)."""
        stamps = self.buckets.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= THROTTLE_WINDOW_S:
            stamps.popleft()
        if not stamps:
            del self.buckets[key]
            return None
        return stamps

    def refusing(self, key: str, now: float) -> int:
        """Seconds a newcomer ``key`` would wait for room (``0``: none)."""
        if key in self.buckets or len(self.buckets) < self._max_keys:
            return 0
        return max(0, math.ceil(self._no_room_until - now))

    def admit(self, key: str, now: float) -> deque[float] | None:
        """``key``'s bucket, made if need be; ``None`` when the table is
        full and no room can be made (fail closed)."""
        stamps = self.live(key, now)
        if stamps is not None:
            return stamps
        if len(self.buckets) >= self._max_keys and not self._room(now):
            return None
        stamps = deque(maxlen=THROTTLE_MAX_FAILURES)
        self.buckets[key] = stamps
        return stamps

    def wait(self, now: float) -> int:
        return max(1, math.ceil(self._no_room_until - now))

    def _room(self, now: float) -> bool:
        """Free one slot in the full table, or fail closed."""
        if now < self._no_room_until:
            return False
        if self._make_room(now):
            return True
        self._no_room_until = now + PRUNE_INTERVAL_S
        log.warning(
            "household dashboard: %s throttle full — refusing new ones", self._what
        )
        return False

    def _make_room(self, now: float) -> bool:
        """Drop the first stale key — or, with ``evict_unlocked``, the
        first not-locked-out one — walking from the oldest. Kept keys
        rotate to the back so the next walk doesn't step over them again."""
        for _ in range(len(self.buckets)):
            key = next(iter(self.buckets))
            stamps = self.live(key, now)
            if stamps is None:
                return True
            if self._evict_unlocked and len(stamps) < THROTTLE_MAX_FAILURES:
                del self.buckets[key]
                log.debug("household dashboard: %s throttle evicted a key", self._what)
                return True
            self.buckets[key] = self.buckets.pop(key)
        return False


class DashboardAuth:
    """Issues and verifies dashboard session cookies; throttles logins."""

    __slots__ = (
        "_settings",
        "_clock",
        "_logins",
        "_pins",
        "_global",
        "_global_tripped",
        "_verify_slot",
        "_revoked",
        "_max_revoked",
        "_acks",
        "_tiles",
        "_pairs",
        "_display_reads",
        "_pairs_pruned_at",
        "_pair_codes",
        "last_seen_at",
    )

    def __init__(
        self,
        settings_service: DashboardSettingsService,
        *,
        clock: Callable[[], float] = time.time,
        max_throttle_keys: int = THROTTLE_MAX_KEYS,
        max_pin_keys: int = PIN_THROTTLE_MAX_KEYS,
        max_revocations: int = REVOCATION_MAX,
    ) -> None:
        self._settings = settings_service
        self._clock = clock
        # session id → (reason, expires_at). Insertion-ordered (oldest first).
        self._revoked: dict[str, tuple[str, float]] = {}
        self._max_revoked = max_revocations
        # Password / setup-link failures per client, and PIN failures per
        # lock — apart, so neither can crowd out or evict the other.
        self._logins = _FailureTable(
            max_throttle_keys, evict_unlocked=True, what="login"
        )
        self._pins = _FailureTable(max_pin_keys, evict_unlocked=False, what="PIN")
        self._global: deque[float] = deque(maxlen=GLOBAL_MAX_FAILURES + 1)
        self._global_tripped = False
        self._verify_slot = asyncio.Semaphore(1)
        self._acks = RateLimiter(monotonic=clock)
        self._tiles = RateLimiter(monotonic=clock)
        self._pairs = RateLimiter(monotonic=clock)
        self._display_reads = RateLimiter(monotonic=clock)
        self._pairs_pruned_at = clock()
        # Wrong pairing codes: one global bucket, never the login table.
        self._pair_codes: deque[float] = deque(maxlen=PAIR_CODE_MAX_FAILURES)
        #: When a signed-in tablet last made a request (unix seconds).
        self.last_seen_at: float | None = None

    # ── Cookie ──────────────────────────────────────────────────────────

    async def issue(
        self, keep: bool, *, locked: bool = False, session_id: str | None = None
    ) -> str:
        """A fresh cookie value for the current epoch.

        ``session_id`` continues a session (renewal); ``None`` starts a
        new one (sign-in, lock, unlock).
        """
        payload = {
            "v": _PAYLOAD_VERSION,
            "e": await self._settings.epoch(),
            "i": int(self._clock()),
            "k": bool(keep),
            "l": bool(locked),
            "n": session_id or _b64encode(secrets.token_bytes(_NONCE_BYTES)),
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        secret = await self._settings.cookie_secret()
        sig = hmac.new(secret, body, hashlib.sha256).digest()
        return f"{_b64encode(body)}.{_b64encode(sig)}"

    async def verify(self, raw: str | None) -> SessionClaims | None:
        """The claims of a valid, current, unexpired cookie, else ``None``.

        Never raises on malformed input.
        """
        if not isinstance(raw, str) or not raw or len(raw) > _MAX_COOKIE_LEN:
            return None
        parts = raw.split(".")
        if len(parts) != 2:
            return None
        body = _b64decode(parts[0])
        sig = _b64decode(parts[1])
        if body is None or sig is None:
            return None
        secret = await self._settings.cookie_secret()
        expected = hmac.new(secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, sig):
            return None
        claims = _parse_payload(body)
        if claims is None:
            return None
        if claims.epoch != await self._settings.epoch():
            return None
        now = self._clock()
        if claims.issued_at > now + MAX_CLOCK_SKEW_S:
            return None
        if now - claims.issued_at > _ttl(claims):
            return None
        if not claims.session_id:
            # An older cookie without ``n``: its signature names it; a
            # renewal then carries this id on as ``n``.
            derived = hashlib.sha256(sig).hexdigest()[:_DERIVED_ID_LEN]
            claims = replace(claims, session_id=derived)
        reason = self.revocation(claims)
        if reason == REVOKED_SIGNED_OUT:
            return None
        if reason == REVOKED_LOCKED:
            return replace(claims, locked=True)
        return claims

    # ── Revocation ──────────────────────────────────────────────────────
    #
    # In memory only: a restart forgets it. The durable revocation is the
    # session epoch ("Sign out all tablets"). Keyed by session id, so an
    # entry covers every renewed copy of the session; it lasts a full
    # cookie lifetime from the revocation, so no copy issued before it can
    # outlive it (and a ``signed_out`` session can't be renewed at all).
    # Only an unlocked session (``/lock``) or a lock's own sign-out adds an
    # entry — one per session — so a locked session can't grow the map.

    def revoke(self, claims: SessionClaims, reason: str) -> None:
        """Revoke the session ``claims`` belongs to (by its session id).

        ``signed_out`` wins over ``locked`` and is never downgraded.
        """
        if reason not in _REVOCATION_REASONS:
            raise ValueError(f"unknown revocation reason: {reason}")
        sid = claims.session_id
        current = self._revoked.get(sid)
        if current is not None and current[0] == REVOKED_SIGNED_OUT:
            return
        if current is None and len(self._revoked) >= self._max_revoked:
            self._make_revocation_room()
        self._revoked[sid] = (reason, self._clock() + _ttl(claims))

    def revocation(self, claims: SessionClaims) -> str | None:
        """Why the session behind ``claims`` was revoked, or ``None``."""
        entry = self._revoked.get(claims.session_id)
        if entry is None:
            return None
        if self._clock() > entry[1]:
            del self._revoked[claims.session_id]
            return None
        return entry[0]

    @property
    def revoked_count(self) -> int:
        return len(self._revoked)

    def _make_revocation_room(self) -> None:
        """Drop expired entries; else the oldest ``locked`` one (losing it
        only re-opens a lock race), else the oldest of all."""
        now = self._clock()
        for sid in [s for s, (_, exp) in self._revoked.items() if now > exp]:
            del self._revoked[sid]
        if len(self._revoked) < self._max_revoked:
            return
        log.warning("household dashboard: revocation list full — dropping oldest")
        victim = next(
            (s for s, (why, _) in self._revoked.items() if why == REVOKED_LOCKED),
            next(iter(self._revoked)),
        )
        del self._revoked[victim]

    def needs_renewal(self, claims: SessionClaims) -> bool:
        """``True`` when ``claims`` is old enough to be re-issued."""
        return self._clock() - claims.issued_at > RENEW_AFTER_S

    def now(self) -> float:
        """The dashboard's clock (unix seconds) — one source for cookies,
        the throttle and the wall clock on the page."""
        return self._clock()

    def touch(self) -> None:
        """Record a signed-in request (surfaced to admins as "last seen")."""
        self.last_seen_at = self._clock()

    # ── Password ────────────────────────────────────────────────────────

    async def check_password(self, password: str) -> bool:
        """Verify the household password — one check at a time."""
        async with self._verify_slot:
            return await self._settings.verify_password(password)

    async def check_pin(self, pin: str) -> bool:
        """Verify the privacy-lock PIN — scrypt too, so it queues in the
        same one-at-a-time slot as the password."""
        async with self._verify_slot:
            return await self._settings.verify_pin(pin)

    @staticmethod
    def pin_throttle_key(claims: SessionClaims) -> str:
        """The throttle bucket for PIN guesses on this lock.

        The session id alone — minted by ``/lock`` and kept by renewal —
        so the bucket is per lock, per tablet: tablets behind one TLS
        proxy don't share it, and neither rotating client addresses (an
        IPv6 /64 each) nor renewing the cookie buys a fresh one. A locked
        session can't mint a new id (``/lock`` on it changes nothing;
        ``/unlock`` needs the PIN). Separate from the password bucket, so
        a tablet signed out for wrong PINs can sign in with the password
        straight away. Use with ``count_global=False`` (a PIN guess needs
        a valid cookie, so it mustn't feed the global brake that holds
        back every login).
        """
        return f"{_PIN_KEY_PREFIXES[0]}{claims.session_id}"

    @staticmethod
    def display_pin_throttle_key(display_id: str) -> str:
        """The throttle bucket for a wall panel's PIN guesses: the panel
        id, the panel's lasting identity — a bearer token has no session
        to rotate, and a fresh id (a fresh bucket) takes an admin pairing
        it again. Same limit and ``count_global=False`` as
        :meth:`pin_throttle_key`."""
        return f"{_PIN_KEY_PREFIXES[1]}{display_id}"

    def begin_display_ack(self, display_id: str) -> int:
        """Count one "Got it" write by a wall panel — the same budget as a
        tablet session's (:meth:`begin_ack`), keyed on the panel id."""
        return self._begin_ack(f"ack-display:{display_id}")

    def _begin_ack(self, key: str) -> int:
        if self._acks.is_allowed(key, limit=ACK_MAX, window_s=ACK_WINDOW_S):
            return 0
        return self._acks.retry_after_s(key, window_s=ACK_WINDOW_S)

    def begin_ack(self, claims: SessionClaims) -> int:
        """Count one "Got it" write for this session: ``0`` when it may go
        ahead, else the seconds to wait (nothing is counted then). Keyed
        on the session id, like the PIN throttle; separate from the
        password / PIN failure table."""
        return self._begin_ack(f"ack:{claims.session_id}")

    def begin_tile(self, claims: SessionClaims) -> int:
        """Count one map tile for this session: ``0`` when it may load,
        else the seconds to wait (nothing is counted then). Keyed on the
        session id, like :meth:`begin_ack`."""
        key = f"tile:{claims.session_id}"
        if self._tiles.is_allowed(key, limit=TILE_MAX, window_s=TILE_WINDOW_S):
            return 0
        return self._tiles.retry_after_s(key, window_s=TILE_WINDOW_S)

    def begin_display_read(self, display_id: str, *, image: bool = False) -> int:
        """Count one read by a paired panel: ``0`` when it may go ahead,
        else the seconds to wait (nothing is counted then). JSON reads and
        images (avatars, emoji) have separate budgets, keyed on the panel
        id (at most :data:`~socialhome.domain.household_dashboard.MAX_DISPLAYS`
        keys)."""
        kind, limit = (
            ("img", DISPLAY_IMAGE_MAX) if image else ("json", DISPLAY_READ_MAX)
        )
        key = f"display-{kind}:{display_id}"
        if self._display_reads.is_allowed(key, limit=limit, window_s=DISPLAY_WINDOW_S):
            return 0
        return self._display_reads.retry_after_s(key, window_s=DISPLAY_WINDOW_S)

    def begin_map_render(self, display_id: str) -> int:
        """Count one map picture painted for a panel: ``0`` when it may be
        painted, else the seconds to wait (nothing is counted then)."""
        key = f"display-map:{display_id}"
        if self._display_reads.is_allowed(
            key, limit=MAP_RENDER_MAX, window_s=DISPLAY_WINDOW_S
        ):
            return 0
        return self._display_reads.retry_after_s(key, window_s=DISPLAY_WINDOW_S)

    def begin_pair(self, client_key: str, *, claim: bool = False) -> int:
        """Count one anonymous pairing start from ``client_key`` (a
        :func:`~socialhome.rate_limiter.client_bucket`): ``0`` when it may
        go ahead, else the seconds to wait (nothing is counted then).
        Separate from the password / PIN failure table and never part of
        the global login brake. ``claim``: a connect-claim spend
        (``/pair/claim``) — its own bucket, so code-pairing tries can't
        use up the budget a panel needs to spend its claim in time."""
        now = self._clock()
        if now - self._pairs_pruned_at > PAIR_WINDOW_S:
            # Keyed on client addresses, which may never come back.
            self._pairs.prune(window_s=PAIR_WINDOW_S)
            self._pairs_pruned_at = now
        key = f"{'claim' if claim else 'pair'}:{client_key}"
        if self._pairs.is_allowed(key, limit=PAIR_MAX, window_s=PAIR_WINDOW_S):
            return 0
        return self._pairs.retry_after_s(key, window_s=PAIR_WINDOW_S)

    @property
    def pair_key_count(self) -> int:
        """Client addresses the pair-start limiter currently remembers."""
        return self._pairs.key_count

    def begin_pair_code(self) -> int:
        """Start an admin's pairing-code check: ``0`` when it may go ahead
        — it then counts as a wrong code until
        :meth:`record_pair_code_success` — else the seconds to wait
        (nothing is counted).

        One global bucket for every admin, fixed in size
        (:data:`PAIR_CODE_MAX_FAILURES`), and independent of the
        password / PIN failure table and the global login brake: failed
        tablet logins never hold pairing back, and wrong codes never
        hold a login back. Callers serialise checks (the claim lock).
        """
        now = self._clock()
        stamps = self._pair_codes
        while stamps and now - stamps[0] >= PAIR_CODE_WINDOW_S:
            stamps.popleft()
        if len(stamps) >= PAIR_CODE_MAX_FAILURES:
            return max(1, math.ceil(stamps[0] + PAIR_CODE_WINDOW_S - now))
        stamps.append(now)
        return 0

    def record_pair_code_success(self) -> None:
        """The code was right: forget the wrong ones."""
        self._pair_codes.clear()

    @property
    def pair_code_failure_count(self) -> int:
        """Wrong pairing codes currently counted (unpruned)."""
        return len(self._pair_codes)

    # ── Throttle ────────────────────────────────────────────────────────
    #
    # An attempt is counted as a failure *when it starts*
    # (:meth:`begin_attempt`) and un-counted on success
    # (:meth:`record_success`): concurrent guesses can't slip past the
    # limit while earlier ones are still being checked.

    def _table(self, key: str) -> _FailureTable:
        """PIN buckets (by key prefix) or the login table."""
        return self._pins if key.startswith(_PIN_KEY_PREFIXES) else self._logins

    def _live(self, key: str, now: float) -> deque[float] | None:
        """``key``'s failures still inside the window (pruned in place)."""
        return self._table(key).live(key, now)

    def _global_remaining(self, now: float) -> int:
        stamps = self._global
        while stamps and now - stamps[0] >= THROTTLE_WINDOW_S:
            stamps.popleft()
        if len(stamps) <= GLOBAL_MAX_FAILURES:
            self._global_tripped = False
            return 0
        if not self._global_tripped:
            self._global_tripped = True
            log.warning(
                "household dashboard: global login brake engaged — over %d "
                "failed sign-ins in %d min; every client waits",
                GLOBAL_MAX_FAILURES,
                THROTTLE_WINDOW_S // 60,
            )
        return max(1, math.ceil(stamps[0] + THROTTLE_WINDOW_S - now))

    def _remaining(self, key: str, now: float, *, count_global: bool = True) -> int:
        wait = self._global_remaining(now) if count_global else 0
        stamps = self._live(key, now)
        if stamps is not None and len(stamps) >= THROTTLE_MAX_FAILURES:
            wait = max(wait, math.ceil(stamps[0] + THROTTLE_WINDOW_S - now), 1)
        return wait

    def throttle_remaining(self, key: str) -> int:
        """Seconds until ``key`` may try again (``0`` = allowed now)."""
        return self._remaining(key, self._clock())

    def failure_count(self, key: str) -> int:
        """``key``'s own failures inside the window."""
        stamps = self._live(key, self._clock())
        return 0 if stamps is None else len(stamps)

    def attempt_wait(self, key: str, *, count_global: bool = True) -> int:
        """Seconds :meth:`begin_attempt` would make ``key`` wait now —
        including a full client table refusing newcomers — counting
        nothing."""
        now = self._clock()
        wait = self._remaining(key, now, count_global=count_global)
        return wait or self._table(key).refusing(key, now)

    def begin_attempt(self, key: str, *, count_global: bool = True) -> int:
        """Start a sign-in attempt for ``key``.

        Returns ``0`` when the attempt may go ahead — it is then counted
        as a failure until :meth:`record_success` — else the seconds to
        wait (nothing is counted).

        ``count_global=False`` (the 256-bit setup link) uses only the
        per-client limit: it neither feeds nor obeys the global brake, so
        cheap token guesses can't lock everyone out of password sign-in
        and a tripped brake can't block pairing a tablet.
        """
        now = self._clock()
        wait = self._remaining(key, now, count_global=count_global)
        if wait:
            return wait
        table = self._table(key)
        stamps = table.admit(key, now)
        if stamps is None:
            return table.wait(now)
        stamps.append(now)
        if count_global:
            self._global.append(now)
        return 0

    def record_success(self, key: str, *, count_global: bool = True) -> None:
        """The attempt was right: forget ``key``'s failures.

        Pass the same ``count_global`` as the matching :meth:`begin_attempt`.
        """
        self._table(key).buckets.pop(key, None)
        if count_global and self._global:
            self._global.pop()

    @property
    def throttle_key_count(self) -> int:
        """Keys in the login and PIN tables together."""
        return len(self._logins.buckets) + len(self._pins.buckets)

    @property
    def global_failure_count(self) -> int:
        """Failures currently feeding the global brake (unpruned)."""
        return len(self._global)
