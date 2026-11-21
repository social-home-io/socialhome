"""Failures-only throttle for ``/api/auth/token`` and password reset (§25.7).

Only a wrong password (or a wrong reset link) counts. A household behind one
reverse proxy, tunnel or HA shares one client address, so a throttle that
counted every login locked the family's sixth phone out for 15 minutes.

Login: :data:`LOGIN_IP_MAX_FAILURES` failures per client address and
:data:`LOGIN_USER_MAX_FAILURES` per username in a rolling
:data:`FAILURE_WINDOW_S` — the shape of the household dashboard's
``DashboardAuth`` throttle. The username bucket keys on the normalized name
(:func:`normalize_username`) whether or not such a user exists: the throttle
never learns which names are real, so its answers can't tell them apart.

Each attempt is counted *before* the password check and handed back on
success (:meth:`LoginThrottle.login_succeeded`), so a burst of concurrent
guesses can't all slip past the limit while the first is still being
checked. A success hands back one attempt on the address (never the
address's other failures — knowing one password must not reset the guesses
at the others) and clears the username's failures.

Successes have a generous cap of their own, never handed back:
:data:`LOGIN_USER_MAX_SUCCESSES` per username per window. A person signs in
once per device; without it, one known password could loop the endpoint for
unlimited scrypt work and token rows.

Password reset: the link is a single-use 256-bit token, so the token itself
is not something to throttle per target, and the request names no user.
Wrong links count per client address (:data:`RESET_IP_MAX_FAILURES`), in a
bucket apart from the login one. A redeemed link is handed back and clears
the login failures of the user it reset, so someone who locked themselves
out signs in with the new password at once.

The table is bounded (:data:`MAX_KEYS`). When full it drops a key whose
stamps all aged out, else one still under its limit (a guesser gains nothing
from that a fresh address wouldn't give) — never a locked-out bucket. With
nothing it may drop, a new key is refused for :data:`FULL_RETRY_S` (fails
closed), like the dashboard's ``_FailureTable``.

The client address is the socket peer, bucketed by
:func:`socialhome.rate_limiter.client_bucket` (IPv6 by /64). The household
app has no trusted-proxy setting, so ``X-Forwarded-For`` is never read here:
behind a proxy every client shares the proxy's bucket — which is why only
failures count. In memory, per process: a restart forgets it.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
import unicodedata
from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

__all__ = [
    "FAILURE_WINDOW_S",
    "FULL_RETRY_S",
    "LOGIN_IP_MAX_FAILURES",
    "LOGIN_USER_MAX_FAILURES",
    "LOGIN_USER_MAX_SUCCESSES",
    "MAX_KEYS",
    "RESET_IP_MAX_FAILURES",
    "LoginThrottle",
    "normalize_username",
]

#: Wrong passwords one client address may send per window.
LOGIN_IP_MAX_FAILURES = 10
#: Wrong passwords for one username (from any address) per window.
LOGIN_USER_MAX_FAILURES = 5
#: Right passwords for one username per window — never handed back.
LOGIN_USER_MAX_SUCCESSES = 10
#: Wrong password-reset links one client address may send per window.
RESET_IP_MAX_FAILURES = 10
FAILURE_WINDOW_S = 15 * 60
#: Most keys held at once (a key with a few stamps is ~200 bytes).
MAX_KEYS = 50_000
#: How long a newcomer waits when the table is full of locked-out keys.
FULL_RETRY_S = 10

_LOGIN_IP = "login-ip:"
_LOGIN_USER = "login-user:"
_LOGIN_OK = "login-ok:"
_RESET_IP = "reset-ip:"

_LIMITS = {
    _LOGIN_IP: LOGIN_IP_MAX_FAILURES,
    _LOGIN_USER: LOGIN_USER_MAX_FAILURES,
    _LOGIN_OK: LOGIN_USER_MAX_SUCCESSES,
    _RESET_IP: RESET_IP_MAX_FAILURES,
}


def normalize_username(username: str) -> str:
    """The form a username is throttled under: NFKC, trimmed, case-folded.

    Logins match the stored name exactly, so folding only ever merges
    spellings that can't both be right — it makes the limit stricter,
    never looser.
    """
    return unicodedata.normalize("NFKC", username).strip().casefold()


def _name_digest(username: str) -> str:
    # A digest: fixed size whatever the body sent, and no raw names in memory.
    return hashlib.sha256(normalize_username(username).encode("utf-8")).hexdigest()


def _limit(key: str) -> int:
    return _LIMITS[key[: key.index(":") + 1]]


class LoginThrottle:
    """Stamp counts per key in a rolling :data:`FAILURE_WINDOW_S`."""

    __slots__ = ("_buckets", "_clock", "_full_until", "_max_keys", "_pruned_at")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        # Least recently stamped first: eviction walks from the front.
        self._buckets: dict[str, list[float]] = {}
        self._pruned_at = clock()
        self._full_until = 0.0

    # ── Login ──────────────────────────────────────────────────────────

    def begin_login(self, client: str, username: str) -> int:
        """Start a login attempt: ``0`` and the attempt counts as a failure
        until :meth:`login_succeeded`, else the seconds to wait (nothing
        counted)."""
        name = _name_digest(username)
        return self._begin(
            charge=(_LOGIN_IP + client, _LOGIN_USER + name),
            check=(_LOGIN_OK + name,),
        )

    def login_succeeded(self, client: str, username: str) -> None:
        """The password was right: hand back one attempt on the address,
        clear the username's failures and count the success."""
        name = _name_digest(username)
        self._refund(_LOGIN_IP + client)
        self._buckets.pop(_LOGIN_USER + name, None)
        self._stamp(_LOGIN_OK + name, self._clock())

    # ── Password reset ─────────────────────────────────────────────────

    def begin_reset(self, client: str) -> int:
        """Start a reset-link redeem; same contract as :meth:`begin_login`."""
        return self._begin(charge=(_RESET_IP + client,), check=())

    def reset_succeeded(self, client: str, username: str) -> None:
        """The link was good: hand back this attempt and lift the login
        lockout of the user whose password it just set."""
        self._refund(_RESET_IP + client)
        self._buckets.pop(_LOGIN_USER + _name_digest(username), None)

    # ── Introspection (tests, diagnostics) ─────────────────────────────

    @property
    def key_count(self) -> int:
        return len(self._buckets)

    def bucket_keys(self) -> Iterator[str]:
        return iter(self._buckets)

    # ── Internals ──────────────────────────────────────────────────────

    def _begin(self, *, charge: tuple[str, ...], check: tuple[str, ...]) -> int:
        now = self._clock()
        if now - self._pruned_at >= FAILURE_WINDOW_S:
            self._prune(now)
        wait = max(self._wait(key, now) for key in charge + check)
        if wait:
            return wait
        new = sum(1 for key in charge if key not in self._buckets)
        if new and not self._room(new, now, keep=charge):
            return max(1, math.ceil(self._full_until - now))
        for key in charge:
            self._stamp(key, now)
        return 0

    def _live(self, key: str, now: float) -> list[float] | None:
        """``key``'s stamps still inside the window (pruned in place)."""
        stamps = self._buckets.get(key)
        if stamps is None:
            return None
        cut = 0
        while cut < len(stamps) and now - stamps[cut] >= FAILURE_WINDOW_S:
            cut += 1
        if cut:
            del stamps[:cut]
        if not stamps:
            del self._buckets[key]
            return None
        return stamps

    def _wait(self, key: str, now: float) -> int:
        stamps = self._live(key, now)
        limit = _limit(key)
        if stamps is None or len(stamps) < limit:
            return 0
        # The ``limit``-th newest stamp leaving the window frees a try.
        return max(1, math.ceil(stamps[-limit] + FAILURE_WINDOW_S - now))

    def _stamp(self, key: str, now: float) -> None:
        stamps = self._buckets.pop(key, None) or []
        stamps.append(now)
        del stamps[: -_limit(key)]  # at most ``limit`` kept
        self._buckets[key] = stamps  # to the back: most recently stamped

    def _refund(self, key: str) -> None:
        stamps = self._buckets.get(key)
        if stamps:
            stamps.pop()
            if not stamps:
                del self._buckets[key]

    def _room(self, needed: int, now: float, *, keep: tuple[str, ...]) -> bool:
        """Make room for ``needed`` new keys, or fail closed."""
        if len(self._buckets) + needed <= self._max_keys:
            return True
        if now < self._full_until:
            return False
        self._prune(now)
        for key in list(self._buckets):
            if len(self._buckets) + needed <= self._max_keys:
                return True
            if key in keep:
                continue
            stamps = self._live(key, now)
            if stamps is not None and len(stamps) < _limit(key):
                del self._buckets[key]
        if len(self._buckets) + needed <= self._max_keys:
            return True
        self._full_until = now + FULL_RETRY_S
        log.warning("login throttle full of locked-out keys — refusing new ones")
        return False

    def _prune(self, now: float) -> None:
        self._pruned_at = now
        for key in [
            k
            for k, stamps in self._buckets.items()
            if not stamps or now - stamps[-1] >= FAILURE_WINDOW_S
        ]:
            del self._buckets[key]
