"""Household dashboard sessions — the signed tablet cookie + login throttle.

The dashboard has no user accounts: one household password unlocks it,
and a signed cookie keeps the tablet signed in.

Cookie (``sh_dash``) = ``b64url(payload) "." b64url(HMAC-SHA256(key,
payload))`` where ``payload`` is compact JSON::

    {"v": 1, "e": <session epoch>, "i": <issued-at unix int>, "k": <keep>}

The key is the KEK-wrapped dashboard cookie secret
(:meth:`DashboardSettingsService.cookie_secret`); bumping the session
epoch signs every tablet out. To add a claim later, add it to
:data:`_PAYLOAD_TYPES` + :class:`SessionClaims` (and bump ``v`` if old
cookies must not verify).

The login throttle is in memory and keyed per client address
(:func:`socialhome.rate_limiter.client_bucket`): five failures within a
rolling 15 minutes lock that client out until the oldest one ages out.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import math
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..services.dashboard_settings_service import DashboardSettingsService

log = logging.getLogger(__name__)

__all__ = [
    "COOKIE_NAME",
    "KEEP_TTL_S",
    "MAX_CLOCK_SKEW_S",
    "RENEW_AFTER_S",
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

_PAYLOAD_VERSION = 1
#: Exact claim set and the type of each value (``bool`` checked exactly,
#: so ``True`` is never accepted as an int nor ``1`` as a bool).
_PAYLOAD_TYPES: dict[str, type] = {"v": int, "e": int, "i": int, "k": bool}
#: Far above any real cookie; refuses absurd inputs before decoding.
_MAX_COOKIE_LEN = 1024
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


@dataclass(slots=True, frozen=True)
class SessionClaims:
    """The verified contents of a dashboard session cookie."""

    epoch: int
    issued_at: int
    keep: bool


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
    if not isinstance(data, dict) or set(data) != set(_PAYLOAD_TYPES):
        return None
    for key, kind in _PAYLOAD_TYPES.items():
        if type(data[key]) is not kind:
            return None
    if data["v"] != _PAYLOAD_VERSION:
        return None
    return SessionClaims(epoch=data["e"], issued_at=data["i"], keep=data["k"])


class DashboardAuth:
    """Issues and verifies dashboard session cookies; throttles logins."""

    __slots__ = (
        "_settings",
        "_clock",
        "_max_keys",
        "_failures",
        "last_seen_at",
    )

    def __init__(
        self,
        settings_service: DashboardSettingsService,
        *,
        clock: Callable[[], float] = time.time,
        max_throttle_keys: int = THROTTLE_MAX_KEYS,
    ) -> None:
        self._settings = settings_service
        self._clock = clock
        self._max_keys = max_throttle_keys
        self._failures: dict[str, deque[float]] = {}
        #: When a signed-in tablet last made a request (unix seconds).
        self.last_seen_at: float | None = None

    # ── Cookie ──────────────────────────────────────────────────────────

    async def issue(self, keep: bool) -> str:
        """A fresh cookie value for the current epoch."""
        payload = {
            "v": _PAYLOAD_VERSION,
            "e": await self._settings.epoch(),
            "i": int(self._clock()),
            "k": bool(keep),
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
        ttl = KEEP_TTL_S if claims.keep else SESSION_TTL_S
        if now - claims.issued_at > ttl:
            return None
        return claims

    def needs_renewal(self, claims: SessionClaims) -> bool:
        """``True`` when ``claims`` is old enough to be re-issued."""
        return self._clock() - claims.issued_at > RENEW_AFTER_S

    def touch(self) -> None:
        """Record a signed-in request (surfaced to admins as "last seen")."""
        self.last_seen_at = self._clock()

    # ── Throttle ────────────────────────────────────────────────────────

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

    def throttle_remaining(self, key: str) -> int:
        """Seconds until ``key`` may try again (``0`` = allowed now)."""
        now = self._clock()
        stamps = self._live(key, now)
        if stamps is None or len(stamps) < THROTTLE_MAX_FAILURES:
            return 0
        return max(1, math.ceil(stamps[0] + THROTTLE_WINDOW_S - now))

    def record_failure(self, key: str) -> None:
        now = self._clock()
        stamps = self._live(key, now)
        if stamps is None:
            if len(self._failures) >= self._max_keys:
                self._make_room(now)
            stamps = deque(maxlen=THROTTLE_MAX_FAILURES)
            self._failures[key] = stamps
        stamps.append(now)

    def record_success(self, key: str) -> None:
        self._failures.pop(key, None)

    @property
    def throttle_key_count(self) -> int:
        return len(self._failures)

    def _make_room(self, now: float) -> None:
        """Drop stale keys; if still full, evict the oldest-inserted key."""
        for key in list(self._failures):
            self._live(key, now)
        if len(self._failures) >= self._max_keys:
            oldest = next(iter(self._failures))
            log.warning("dashboard login throttle is full — evicting one client")
            del self._failures[oldest]
