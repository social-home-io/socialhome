"""Household dashboard sessions — the signed tablet cookie + login throttle.

The dashboard has no user accounts: one household password unlocks it,
and a signed cookie keeps the tablet signed in.

Cookie (``sh_dash``) = ``b64url(payload) "." b64url(HMAC-SHA256(key,
payload))`` where ``payload`` is compact JSON::

    {"v": 1, "e": <session epoch>, "i": <issued-at unix int>, "k": <keep>,
     "l": <locked>, "n": <random nonce>}

``l`` is the privacy lock: a locked tablet shows only the lock screen
until someone types the PIN. It lives in the cookie so it is per tablet
and survives reloads and browser restarts. ``n`` makes every cookie
value unique (revocation and the PIN throttle key on it). Accepted
shapes are exactly ``{v,e,i,k}`` (pre-lock cookies, verified as
unlocked), ``{v,e,i,k,l}`` and ``{v,e,i,k,l,n}``; anything else (or a
wrong type) is refused.

The key is the KEK-wrapped dashboard cookie secret
(:meth:`DashboardSettingsService.cookie_secret`); bumping the session
epoch signs every tablet out — durably. On top, single cookies can be
revoked in memory (:meth:`DashboardAuth.revoke`: replaced by a locked
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
tablet — client address *and* cookie (:meth:`DashboardAuth.pin_throttle_key`)
— never the global brake; the PIN check shares the scrypt slot.
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

if TYPE_CHECKING:
    from ..services.dashboard_settings_service import DashboardSettingsService

log = logging.getLogger(__name__)

__all__ = [
    "COOKIE_NAME",
    "GLOBAL_MAX_FAILURES",
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
#: Failures across all clients per window before everyone is held back.
GLOBAL_MAX_FAILURES = 30
#: With the client table full of locked-out clients, newcomers are
#: refused (fail closed) and the table is rescanned at most this often.
PRUNE_INTERVAL_S = 10.0

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
#: (``+l`` and the per-cookie nonce ``n``). Nothing else.
_SHAPES = (
    frozenset({"v", "e", "i", "k"}),
    frozenset({"v", "e", "i", "k", "l"}),
    frozenset({"v", "e", "i", "k", "l", "n"}),
)
#: Random per cookie, so two cookies minted in the same second differ —
#: revocation and the PIN throttle key on the exact cookie value.
_NONCE_BYTES = 16
_NONCE = re.compile(r"[A-Za-z0-9_-]{16,32}")
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
    #: The cookie's HMAC — identifies this exact cookie value (revocation,
    #: per-tablet PIN throttle). Not part of equality or ``repr``.
    signature: bytes = field(default=b"", compare=False, repr=False)


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
    )


class DashboardAuth:
    """Issues and verifies dashboard session cookies; throttles logins."""

    __slots__ = (
        "_settings",
        "_clock",
        "_max_keys",
        "_failures",
        "_global",
        "_global_tripped",
        "_no_room_until",
        "_verify_slot",
        "_revoked",
        "_max_revoked",
        "last_seen_at",
    )

    def __init__(
        self,
        settings_service: DashboardSettingsService,
        *,
        clock: Callable[[], float] = time.time,
        max_throttle_keys: int = THROTTLE_MAX_KEYS,
        max_revocations: int = REVOCATION_MAX,
    ) -> None:
        self._settings = settings_service
        self._clock = clock
        self._max_keys = max_throttle_keys
        # signature → (reason, expires_at). Insertion-ordered (oldest first).
        self._revoked: dict[bytes, tuple[str, float]] = {}
        self._max_revoked = max_revocations
        # Insertion-ordered: eviction walks it from the front.
        self._failures: dict[str, deque[float]] = {}
        self._global: deque[float] = deque(maxlen=GLOBAL_MAX_FAILURES + 1)
        self._global_tripped = False
        self._no_room_until = 0.0
        self._verify_slot = asyncio.Semaphore(1)
        #: When a signed-in tablet last made a request (unix seconds).
        self.last_seen_at: float | None = None

    # ── Cookie ──────────────────────────────────────────────────────────

    async def issue(self, keep: bool, *, locked: bool = False) -> str:
        """A fresh cookie value for the current epoch."""
        payload = {
            "v": _PAYLOAD_VERSION,
            "e": await self._settings.epoch(),
            "i": int(self._clock()),
            "k": bool(keep),
            "l": bool(locked),
            "n": _b64encode(secrets.token_bytes(_NONCE_BYTES)),
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
        claims = replace(claims, signature=sig)
        reason = self.revocation(claims)
        if reason == REVOKED_SIGNED_OUT:
            return None
        if reason == REVOKED_LOCKED:
            return replace(claims, locked=True)
        return claims

    # ── Revocation ──────────────────────────────────────────────────────
    #
    # In memory only: a restart forgets it. The durable revocation is the
    # session epoch ("Sign out all tablets"). Entries expire with the
    # cookie they name, so a revoked cookie can never outlive its entry.

    def revoke(self, claims: SessionClaims, reason: str) -> None:
        """Revoke the cookie ``claims`` came from (by its signature).

        ``signed_out`` wins over ``locked`` and is never downgraded.
        """
        if reason not in _REVOCATION_REASONS:
            raise ValueError(f"unknown revocation reason: {reason}")
        sig = claims.signature
        current = self._revoked.get(sig)
        if current is not None and current[0] == REVOKED_SIGNED_OUT:
            return
        if current is None and len(self._revoked) >= self._max_revoked:
            self._make_revocation_room()
        self._revoked[sig] = (reason, claims.issued_at + _ttl(claims))

    def revocation(self, claims: SessionClaims) -> str | None:
        """Why the cookie behind ``claims`` was revoked, or ``None``."""
        entry = self._revoked.get(claims.signature)
        if entry is None:
            return None
        if self._clock() > entry[1]:
            del self._revoked[claims.signature]
            return None
        return entry[0]

    @property
    def revoked_count(self) -> int:
        return len(self._revoked)

    def _make_revocation_room(self) -> None:
        """Drop expired entries; else the oldest ``locked`` one (losing it
        only re-opens a lock race), else the oldest of all."""
        now = self._clock()
        for sig in [s for s, (_, exp) in self._revoked.items() if now > exp]:
            del self._revoked[sig]
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
    def pin_throttle_key(client_key: str, claims: SessionClaims) -> str:
        """The throttle bucket for PIN guesses made with this very cookie.

        Per client *and* per cookie value (a hash prefix of its
        signature): behind a TLS proxy every tablet shares one client
        address, and one tablet's wrong PINs mustn't sign out another.
        The signature, not ``issued_at``, because two tablets locked in
        the same second would share an ``issued_at``; and a guesser can't
        mint fresh locked cookies without an unlocked session (which needs
        no PIN). Separate from the password bucket, so a tablet signed out
        for wrong PINs can sign in with the password straight away. Use
        with ``count_global=False`` (a PIN guess needs a valid cookie, so
        it mustn't feed the global brake that holds back every login).
        """
        tag = hashlib.sha256(claims.signature).hexdigest()[:16]
        return f"pin:{client_key}:{tag}"

    # ── Throttle ────────────────────────────────────────────────────────
    #
    # An attempt is counted as a failure *when it starts*
    # (:meth:`begin_attempt`) and un-counted on success
    # (:meth:`record_success`): concurrent guesses can't slip past the
    # limit while earlier ones are still being checked.

    def _live(self, key: str, now: float) -> deque[float] | None:
        """``key``'s failures still inside the window (pruned in place)."""
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= THROTTLE_WINDOW_S:
            stamps.popleft()
        if not stamps:
            del self._failures[key]
            return None
        return stamps

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
        if wait or key in self._failures or len(self._failures) < self._max_keys:
            return wait
        return max(0, math.ceil(self._no_room_until - now))

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
        stamps = self._live(key, now)
        if stamps is None:
            if len(self._failures) >= self._max_keys and not self._room(now):
                return max(1, math.ceil(self._no_room_until - now))
            stamps = deque(maxlen=THROTTLE_MAX_FAILURES)
            self._failures[key] = stamps
        stamps.append(now)
        if count_global:
            self._global.append(now)
        return 0

    def record_success(self, key: str, *, count_global: bool = True) -> None:
        """The attempt was right: forget ``key``'s failures.

        Pass the same ``count_global`` as the matching :meth:`begin_attempt`.
        """
        self._failures.pop(key, None)
        if count_global and self._global:
            self._global.pop()

    @property
    def throttle_key_count(self) -> int:
        return len(self._failures)

    @property
    def global_failure_count(self) -> int:
        """Failures currently feeding the global brake (unpruned)."""
        return len(self._global)

    def _room(self, now: float) -> bool:
        """Free one slot in the full client table, or fail closed."""
        if now < self._no_room_until:
            return False
        if self._make_room(now):
            return True
        self._no_room_until = now + PRUNE_INTERVAL_S
        log.warning(
            "household dashboard: login throttle full of locked-out clients — "
            "refusing new ones"
        )
        return False

    def _make_room(self, now: float) -> bool:
        """Drop the first stale or not-locked client, walking from the
        oldest. Locked-out clients are never evicted — they rotate to the
        back so the next walk doesn't step over them again."""
        for _ in range(len(self._failures)):
            key = next(iter(self._failures))
            stamps = self._live(key, now)
            if stamps is None:
                return True
            if len(stamps) < THROTTLE_MAX_FAILURES:
                del self._failures[key]
                log.debug("household dashboard: login throttle evicted a client")
                return True
            self._failures[key] = self._failures.pop(key)
        return False
