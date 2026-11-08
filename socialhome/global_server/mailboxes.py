"""Anonymous per-space mailboxes on the GFS (GFS redesign PR 1).

A household reached through this server is addressed per (household, space,
generation) by a mailbox id instead of its instance id, so the server can
follow traffic within one space but cannot tie the household's spaces to
each other or to its identity. Wire shapes: :mod:`socialhome.domain.gfs_mailbox`.

**Register** (``POST /gfs/mailbox/register``, anonymous) — the body carries
the mailbox's Ed25519 public key and its signature over the domain-separated
``sh-mbx-register:v1`` bytes (this server's public instance id, the mailbox
id, ``ts``, a nonce). The server recomputes the id from the key, checks the
``ts`` window (±300 s) and the nonce (a replay is refused), verifies the
proof of possession, refuses an id an instance holds, and pins the key on
first use. Registering an existing mailbox again is a no-op success (the id
is a hash of the key, so only the key holder can produce one).

**Per-address limits without storing the address.** A per-minute window
limiter on the route plus :attr:`EnvelopeMailboxConfig.mailbox_max_per_ip`
NEW mailboxes per 24 h, both counted in this process's memory under the
coarsened client bucket — never written to the database, never logged next
to a mailbox id. Per node; each node allows its share over the live nodes.

**Claim** (``GET /gfs/mailbox/ws``, :mod:`.routes.mailbox`) — the server
sends a random challenge; the client answers with the mailbox id and a
signature over ``sh-mbx-claim:v1`` bytes. An unknown mailbox runs the same
verify against a fixed dummy key, so "no such mailbox" and "wrong key" are
one answer after the same work.

**Idle sweep** — a mailbox not claimed (since its first registration) for
``mailbox_idle_days`` is dropped with whatever still waits for it
(:meth:`sweep`, run by the maintenance loop).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

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
from .repositories import AbstractGfsFederationRepo, AbstractGfsMailboxRepo

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
#: Window of :attr:`EnvelopeMailboxConfig.mailbox_max_per_ip`.
MAILBOX_CAP_WINDOW_SECONDS: float = 24 * 60 * 60
#: Most client buckets the per-address cap tracks.
MAILBOX_CAP_MAX_KEYS: int = 100_000

_DUMMY_KEY = bytes.fromhex(TIMING_UNIFORM_DUMMY_KEY_HEX)


class MailboxRefused(Exception):
    """A registration this server will not take: ``status`` + a stable
    machine-readable ``error``."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status = status
        self.error = error


class GfsMailboxService:
    """Register mailboxes, verify claims, sweep idle ones."""

    __slots__ = (
        "_cfg",
        "_clock",
        "_fed_repo",
        "_gfs_instance_id",
        "_new_per_bucket",
        "_nodes",
        "_repo",
        "_spent_nonces",
    )

    def __init__(
        self,
        *,
        repo: AbstractGfsMailboxRepo,
        fed_repo: AbstractGfsFederationRepo,
        gfs_instance_id: str,
        config: EnvelopeMailboxConfig,
        nodes: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._fed_repo = fed_repo
        self._gfs_instance_id = gfs_instance_id
        self._cfg = config
        self._nodes = nodes
        self._clock = clock
        #: ``mailbox_id|nonce`` → unix time it was spent (insertion order is
        #: time order, so the oldest are dropped first).
        self._spent_nonces: dict[str, float] = {}
        #: client bucket → (window index, new mailboxes in it). A fixed
        #: 24 h window with one small tuple per bucket — a sliding window
        #: would hold up to ``mailbox_max_per_ip`` timestamps per address.
        #: Insertion order is recency order (each touch re-inserts).
        self._new_per_bucket: dict[str, tuple[int, int]] = {}

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def gfs_instance_id(self) -> str:
        """The id the signed bytes bind (this server's public instance id)."""
        return self._gfs_instance_id

    def capability(self) -> dict | None:
        """The signed ``/gfs/info`` ``envelope_mailbox`` entry, ``None`` off."""
        if not self._cfg.enabled:
            return None
        return {
            "sig_suites": sorted(SUPPORTED_MAILBOX_SIG_SUITES),
            "idle_days": self._cfg.mailbox_idle_days,
        }

    # ── Register ─────────────────────────────────────────────────────────

    async def register(self, body: object, *, client_bucket: str) -> str:
        """Validate a registration *body* and pin its key. Returns the
        mailbox id; raises :class:`MailboxRefused`. *client_bucket* only
        feeds the in-memory per-address cap."""
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
            signed = register_signed_bytes(self._gfs_instance_id, mailbox_id, ts, nonce)
        except InvalidMailboxField as exc:  # unreachable: shapes checked above
            raise MailboxRefused(400, "invalid_body") from exc
        if not crypto.verify_ed25519(sign_pk, signed, sig):
            raise MailboxRefused(403, "bad_signature")
        # Only now — a signed, in-window registration — is the nonce spent,
        # so junk can never fill the replay cache.
        if not self._spend_nonce(f"{mailbox_id}|{nonce}", now):
            raise MailboxRefused(409, "replay")
        if await self._fed_repo.get_instance(mailbox_id) is not None:
            raise MailboxRefused(409, "id_in_use")
        existing = await self._repo.get(mailbox_id)
        if existing is not None:
            if existing.sign_pk != sign_pk:  # a SHA-256 collision; never
                raise MailboxRefused(409, "id_in_use")
            return mailbox_id
        limit = self._cfg.mailbox_max_per_ip
        if self._nodes is not None:
            limit = node_share(limit, self._nodes())
        if not self._count_new(client_bucket, limit, now):
            raise MailboxRefused(429, "mailbox_cap")
        await self._repo.register(mailbox_id, sign_pk, now=int(now))
        return mailbox_id

    def _count_new(self, bucket: str, limit: int, now: float) -> bool:
        """Count one new mailbox for *bucket*; ``False`` past *limit* in the
        current 24 h window. Memory only, bounded to
        :data:`MAILBOX_CAP_MAX_KEYS` buckets (least recently seen dropped)."""
        window = int(now // MAILBOX_CAP_WINDOW_SECONDS)
        counts = self._new_per_bucket
        seen_window, used = counts.pop(bucket, (window, 0))
        if seen_window != window:
            used = 0
        if used >= limit:
            counts[bucket] = (window, used)
            return False
        counts[bucket] = (window, used + 1)
        while len(counts) > MAILBOX_CAP_MAX_KEYS:
            del counts[next(iter(counts))]
        return True

    def _spend_nonce(self, key: str, now: float) -> bool:
        """Record *key*; ``False`` when it was already spent."""
        horizon = now - MAILBOX_NONCE_TTL_SECONDS
        spent = self._spent_nonces
        while spent:
            oldest = next(iter(spent))
            if spent[oldest] >= horizon and len(spent) < MAILBOX_NONCE_MAX:
                break
            del spent[oldest]
        if key in self._spent_nonces:
            return False
        self._spent_nonces[key] = now
        return True

    # ── Claim ────────────────────────────────────────────────────────────

    async def verify_claim(self, claim: dict, challenge: str) -> str | None:
        """The mailbox id *claim* proves possession of for *challenge*, or
        ``None``. An unknown mailbox verifies against a dummy key, so it
        costs what a wrong signature costs. The caller checks the suite
        first (:func:`validate_mailbox_sig_suite`)."""
        mailbox_id = claim.get("mailbox_id")
        sig = _b64(claim.get("sig"))
        if not isinstance(mailbox_id, str) or not crypto.is_instance_id(mailbox_id):
            return None
        try:
            signed = claim_signed_bytes(self._gfs_instance_id, mailbox_id, challenge)
        except InvalidMailboxField:
            return None
        box = await self._repo.get(mailbox_id)
        key = box.sign_pk if box is not None else _DUMMY_KEY
        verified = sig is not None and crypto.verify_ed25519(key, signed, sig)
        if box is None or not verified:
            return None
        return mailbox_id

    async def note_claimed(self, mailbox_id: str) -> None:
        """A claim succeeded: it keeps the mailbox out of the idle sweep."""
        await self._repo.touch_claim(mailbox_id, now=int(self._clock()))

    # ── Sweep ────────────────────────────────────────────────────────────

    async def sweep(self, *, now: int) -> int:
        """Drop mailboxes idle for ``mailbox_idle_days``. Returns how many."""
        older_than = now - self._cfg.mailbox_idle_days * 86_400
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
