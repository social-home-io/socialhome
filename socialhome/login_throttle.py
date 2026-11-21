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
checked. A success hands back its own attempt on the address (never the
address's other failures — knowing one password must not reset the guesses
at the others) and clears the username's failures.

Password reset: the link is a single-use 256-bit token, so the token itself
is not something to throttle per target, and the request names no user.
Wrong links count per client address (:data:`RESET_IP_MAX_FAILURES`), in a
bucket apart from the login one. A redeemed link is handed back and clears
the login failures of the user it reset, so someone who locked themselves
out signs in with the new password at once.

The client address is the socket peer, bucketed by
:func:`socialhome.rate_limiter.client_bucket` (IPv6 by /64). The household
app has no trusted-proxy setting, so ``X-Forwarded-For`` is never read here:
behind a proxy every client shares the proxy's bucket — which is why only
failures count. In memory, per process: a restart forgets it.
"""

from __future__ import annotations

import hashlib
import math
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterator

__all__ = [
    "FAILURE_WINDOW_S",
    "LOGIN_IP_MAX_FAILURES",
    "LOGIN_USER_MAX_FAILURES",
    "MAX_KEYS",
    "RESET_IP_MAX_FAILURES",
    "LoginThrottle",
    "normalize_username",
]

#: Wrong passwords one client address may send per window.
LOGIN_IP_MAX_FAILURES = 10
#: Wrong passwords for one username (from any address) per window.
LOGIN_USER_MAX_FAILURES = 5
#: Wrong password-reset links one client address may send per window.
RESET_IP_MAX_FAILURES = 10
FAILURE_WINDOW_S = 15 * 60
#: Most keys held at once; past it the oldest-inserted key goes. Filling it
#: takes ~10 000 client addresses failing ten times each inside a window.
MAX_KEYS = 100_000

_LOGIN_IP = "login-ip:"
_LOGIN_USER = "login-user:"
_RESET_IP = "reset-ip:"


def normalize_username(username: str) -> str:
    """The form a username is throttled under: NFKC, trimmed, case-folded.

    Logins match the stored name exactly, so folding only ever merges
    spellings that can't both be right — it makes the limit stricter,
    never looser.
    """
    return unicodedata.normalize("NFKC", username).strip().casefold()


def _user_key(username: str) -> str:
    # A digest: fixed size whatever the body sent, and no raw names in memory.
    digest = hashlib.sha256(normalize_username(username).encode("utf-8"))
    return _LOGIN_USER + digest.hexdigest()


class LoginThrottle:
    """Failure counts per key in a rolling :data:`FAILURE_WINDOW_S`."""

    __slots__ = ("_buckets", "_clock", "_max_keys", "_pruned_at")

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = MAX_KEYS,
    ) -> None:
        self._clock = clock
        self._max_keys = max_keys
        # Insertion-ordered: the oldest key is evicted first when full.
        self._buckets: dict[str, deque[float]] = {}
        self._pruned_at = clock()

    # ── Login ──────────────────────────────────────────────────────────

    def begin_login(self, client: str, username: str) -> int:
        """Start a login attempt: ``0`` and the attempt counts as a failure
        until :meth:`login_succeeded`, else the seconds to wait (nothing
        counted)."""
        keys = (
            (_LOGIN_IP + client, LOGIN_IP_MAX_FAILURES),
            (_user_key(username), LOGIN_USER_MAX_FAILURES),
        )
        return self._begin(keys)

    def login_succeeded(self, client: str, username: str) -> None:
        """The password was right: hand back this attempt on the address
        and clear the username's failures."""
        self._refund(_LOGIN_IP + client)
        self._buckets.pop(_user_key(username), None)

    # ── Password reset ─────────────────────────────────────────────────

    def begin_reset(self, client: str) -> int:
        """Start a reset-link redeem; same contract as :meth:`begin_login`."""
        return self._begin(((_RESET_IP + client, RESET_IP_MAX_FAILURES),))

    def reset_succeeded(self, client: str, username: str) -> None:
        """The link was good: hand back this attempt and lift the login
        lockout of the user whose password it just set."""
        self._refund(_RESET_IP + client)
        self._buckets.pop(_user_key(username), None)

    # ── Introspection (tests, diagnostics) ─────────────────────────────

    @property
    def key_count(self) -> int:
        return len(self._buckets)

    def bucket_keys(self) -> Iterator[str]:
        return iter(self._buckets)

    # ── Internals ──────────────────────────────────────────────────────

    def _begin(self, keys: tuple[tuple[str, int], ...]) -> int:
        now = self._clock()
        if now - self._pruned_at >= FAILURE_WINDOW_S:
            self._prune(now)
        wait = max(self._wait(key, limit, now) for key, limit in keys)
        if wait:
            return wait
        for key, limit in keys:
            stamps = self._buckets.get(key)
            if stamps is None:
                while len(self._buckets) >= self._max_keys:
                    del self._buckets[next(iter(self._buckets))]
                stamps = self._buckets[key] = deque(maxlen=limit)
            stamps.append(now)
        return 0

    def _wait(self, key: str, limit: int, now: float) -> int:
        stamps = self._buckets.get(key)
        if stamps is None:
            return 0
        while stamps and now - stamps[0] >= FAILURE_WINDOW_S:
            stamps.popleft()
        if not stamps:
            del self._buckets[key]
            return 0
        if len(stamps) < limit:
            return 0
        # The deque holds at most ``limit`` stamps: the oldest one leaving
        # the window frees the next try.
        return max(1, math.ceil(stamps[0] + FAILURE_WINDOW_S - now))

    def _refund(self, key: str) -> None:
        stamps = self._buckets.get(key)
        if stamps:
            stamps.pop()
            if not stamps:
                del self._buckets[key]

    def _prune(self, now: float) -> None:
        self._pruned_at = now
        for key in [
            k
            for k, stamps in self._buckets.items()
            if not stamps or now - stamps[-1] >= FAILURE_WINDOW_S
        ]:
            del self._buckets[key]
