"""Household side of GFS mailboxes (GFS redesign PR 1 — library only).

A household reached through a GFS gets one anonymous mailbox per (space,
generation) there. This module holds what the household needs to own one:

* :func:`new_mailbox_root` / :func:`derive_mailbox_keys` — the mailbox's
  X25519 sealing key and Ed25519 claim key, both derived with HKDF-SHA256
  from the household's **mailbox root**: a dedicated 32-byte secret
  generated once (:func:`new_mailbox_root`), the GFS id, the space id and
  the generation. The root is stored under the household
  KEK beside its other secrets (wired in redesign PR 2). It is NOT the
  key-wrap seed: that key has its own job (sealing to the household) and
  its own rotation, and a mailbox key must never be able to reveal or
  depend on it. Besides the root, only ``gen`` needs storing: the keys come
  back from the root, the id from the claim key
  (:func:`socialhome.domain.gfs_mailbox.derive_mailbox_id`). Different
  spaces / generations give unrelated keys and ids, so the GFS cannot link
  them.
* :class:`TicketIssuer` / :func:`fetch_tickets` / :class:`MailboxTicketCache`
  — anonymous registration tickets (standard Privacy Pass, RFC 9578 token
  type 0x0002, :mod:`socialhome.privacy_pass`). The household asks for a
  batch over its AUTHENTICATED channel (a request signed with its identity
  key), blinded under the issuer key from the GFS's signed ``/gfs/info``
  capability ``mailbox_tickets``; the GFS blind-signs and never sees the
  tokens. :class:`TicketKeyGuard` keeps the key consistent (one key per
  epoch, the same one an anonymous read of ``/gfs/info`` shows) before
  blinding and before redeeming. The cache refills on its own jittered
  daily schedule (:meth:`MailboxTicketCache.start`), never right after a
  register: a fetch next to a register would let the GFS line them up.
* :func:`register_mailbox` — the anonymous ``POST /gfs/mailbox/register``
  with proof of possession, bound to the GFS id the household pinned (a
  cluster node answers to its siblings' and its former ids too). It tries
  without a ticket first and shows one only when the GFS answers
  ``ticket_required`` (a NEW mailbox), so re-registering never shows one.
* :func:`unregister_mailbox` — the anonymous ``POST /gfs/mailbox/unregister``
  signed by the mailbox key when the household leaves the space. The GFS
  answers 200 whatever happened (no oracle); ``False`` means the request did
  not get that answer (an older GFS without the route, a network fault) and
  the mailbox simply ages out in the idle sweep.
* :class:`MailboxClaim` — the anonymous claim socket (``/gfs/mailbox/ws``,
  one mailbox per socket): answers the challenge, then yields the queued
  and live frames — ``dict`` for a JSON envelope frame, ``bytes`` for a
  binary ``\\x00SHR`` media frame, unchanged.

Not wired into any space flow yet (that is redesign PR 2, v_88).
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self

import aiohttp
import orjson
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .. import privacy_pass as pp
from ..canonical_json import canonical_bytes
from ..capabilities_sig import verify_capabilities
from ..crypto import b64url_decode, b64url_encode, ed25519_public_key, sign_ed25519
from ..domain.gfs_mailbox import (
    FRAME_MAILBOX_CHALLENGE,
    FRAME_MAILBOX_CLAIM,
    FRAME_MAILBOX_CLAIMED,
    MAILBOX_REGISTER_ROUTE,
    MAILBOX_SIG_SUITE_ED25519,
    MAILBOX_TICKET_CHALLENGE,
    MAILBOX_TICKETS_ROUTE,
    MAILBOX_UNREGISTER_ROUTE,
    MAILBOX_WS_ROUTE,
    MAX_TICKETS_PER_REQUEST,
    TICKETS_ACTION,
    TicketKeyPin,
    claim_signed_bytes,
    derive_mailbox_id,
    is_mailbox_nonce,
    register_signed_bytes,
    unregister_signed_bytes,
)
from ..domain.subscriber_accepts import ACCEPTS_ENVELOPE_MEDIA

log = logging.getLogger(__name__)

#: Length of the mailbox root secret.
MAILBOX_ROOT_BYTES: int = 32
#: HKDF salt of every mailbox key (versions the derivation).
MAILBOX_KDF_SALT: bytes = b"sh-mbx-v1"
#: HKDF info labels: one per key, so the two never share output.
_KEM_INFO: bytes = b"sh-mbx-kem:v1"
_SIGN_INFO: bytes = b"sh-mbx-sign:v1"

#: The claim socket's ping interval — inside Cloudflare's 100 s idle timeout.
MAILBOX_CLIENT_HEARTBEAT_S: float = 30.0
#: How long the client waits for the challenge, then for ``mailbox_claimed``.
MAILBOX_CLAIM_TIMEOUT_S: float = 10.0
#: Largest frame the claim socket takes (a binary media frame is ≤ 528 KiB).
MAILBOX_CLIENT_MAX_MSG_BYTES: int = 4 * 1024 * 1024
#: Register request timeout.
MAILBOX_REGISTER_TIMEOUT_S: float = 15.0
#: The shortest issuer-key epoch a household accepts. A short epoch lets a
#: GFS rotate keys to tag households, and shrinks the set of households one
#: key's tickets hide among.
MIN_TICKET_EPOCH_DAYS: int = 7
#: Clock-skew allowance when a new key appears at an epoch boundary.
TICKET_EPOCH_SKEW_S: int = 3600
#: Mean time between two ticket refills; each one is jittered ±50 %.
TICKET_REFILL_PERIOD_S: float = 86_400.0
#: A refill slot this close after a register (a ticket taken) is skipped:
#: an identified fetch next to an anonymous register would tie them.
TICKET_REFILL_QUIET_S: float = 3600.0
#: Mean time between two anonymous ``/gfs/info`` reads; jittered ±50 %.
TICKET_ANON_VIEW_PERIOD_S: float = 6 * 3600.0
_DAY_S: int = 86_400


async def _jittered_loop(
    stop: asyncio.Event,
    period: float,
    rng: random.Random,
    step: Callable[[], Awaitable[None]],
    what: str,
) -> None:
    """Run *step* about once per *period* (each wait jittered ±50 %) until
    *stop* is set. A failing step logs a WARNING (the error type only — no
    id, no key) and the loop goes on: one GFS outage never ends it."""
    while not stop.is_set():
        wait = rng.uniform(0.5 * period, 1.5 * period)
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
            return
        except TimeoutError:
            pass
        try:
            await step()
        except MailboxTicketError as exc:
            log.warning("gfs.mailbox.tickets: %s failed (%s)", what, exc.error)
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            # A network fault the fetch did not wrap: keep going.
            log.warning("gfs.mailbox.tickets: %s failed (%s)", what, type(exc).__name__)


async def _stop_task(stop: asyncio.Event, task: asyncio.Task | None) -> None:
    stop.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except TimeoutError, asyncio.CancelledError:
            task.cancel()


class MailboxRegisterError(Exception):
    """The GFS refused a registration (``status`` + its ``error`` code)."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"mailbox registration refused: {status} {error}")
        self.status = status
        self.error = error


class MailboxTicketError(Exception):
    """No ticket could be had: the GFS refused the request (``status`` +
    ``error``; 429 ``ticket_quota`` = today's batch is used up), offers no
    tickets, or answered with something that does not unblind."""

    def __init__(self, status: int, error: str) -> None:
        super().__init__(f"mailbox tickets refused: {status} {error}")
        self.status = status
        self.error = error


class MailboxClaimError(Exception):
    """The claim socket was refused or broke before the claim completed."""


@dataclass(slots=True, frozen=True)
class MailboxKeys:
    """One mailbox's keys (secret — never logged, never stored: derive)."""

    space_id: str
    gen: int
    kem_private: bytes
    kem_public: bytes
    sign_seed: bytes
    sign_public: bytes
    mailbox_id: str

    def __repr__(self) -> str:  # never print the secrets
        return f"MailboxKeys(space_id={self.space_id!r}, gen={self.gen})"


def _hkdf(seed: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=32, salt=MAILBOX_KDF_SALT, info=info
    ).derive(seed)


def new_mailbox_root() -> bytes:
    """A fresh mailbox root secret (generated once per household)."""
    return secrets.token_bytes(MAILBOX_ROOT_BYTES)


def derive_mailbox_keys(
    mailbox_root: bytes, gfs_instance_id: str, space_id: str, gen: int
) -> MailboxKeys:
    """The mailbox keys of (*space_id*, *gen*) at the GFS
    *gfs_instance_id* under the household's dedicated *mailbox_root*
    secret (:func:`new_mailbox_root`). The GFS id is in the derivation, so
    two GFSs serving the same (global) space get unrelated mailboxes — they
    can't match ids to link a household across servers. Pass the id the
    household PINNED when it minted the mailbox (and keep using it: a GFS
    that changes its id keeps answering to the old one as an alias)."""
    if not gfs_instance_id or "|" in gfs_instance_id:
        raise ValueError("invalid GFS instance id")
    if len(mailbox_root) != MAILBOX_ROOT_BYTES:
        raise ValueError("mailbox root must be 32 bytes")
    if not space_id or "|" in space_id:
        raise ValueError("invalid space id")
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 0:
        raise ValueError("gen must be a non-negative integer")
    scope = (
        b"|"
        + gfs_instance_id.encode()
        + b"|"
        + space_id.encode()
        + b"|"
        + str(gen).encode()
    )
    kem_private = _hkdf(mailbox_root, _KEM_INFO + scope)
    sign_seed = _hkdf(mailbox_root, _SIGN_INFO + scope)
    kem_public = (
        X25519PrivateKey.from_private_bytes(kem_private)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    )
    sign_public = ed25519_public_key(sign_seed)
    return MailboxKeys(
        space_id=space_id,
        gen=gen,
        kem_private=kem_private,
        kem_public=kem_public,
        sign_seed=sign_seed,
        sign_public=sign_public,
        mailbox_id=derive_mailbox_id(sign_public),
    )


# ── Registration tickets (Privacy Pass) ─────────────────────────────────


@dataclass(slots=True, frozen=True)
class TicketIssuer:
    """A GFS's ticket issuer, from its signed ``/gfs/info`` capability
    ``mailbox_tickets`` (:meth:`from_capability`)."""

    key_id: bytes
    public_key: rsa.RSAPublicKey
    per_day: int
    epoch_days: int

    @classmethod
    def from_capability(cls, cap: object) -> TicketIssuer:
        """Parse and check the capability: a supported ``suite`` (no
        fallback), an RSASSA-PSS key whose RFC 9578 key id is the advertised
        ``issuer_key_id``, and an epoch of at least
        :data:`MIN_TICKET_EPOCH_DAYS`. Raises :class:`MailboxTicketError`."""
        if not isinstance(cap, dict):
            raise MailboxTicketError(0, "no_ticket_capability")
        try:
            pp.validate_ticket_suite(cap.get("suite"))
            spki = b64url_decode(str(cap.get("public_key") or ""))
            key = pp.decode_public_key(spki)
            key_id = b64url_decode(str(cap.get("issuer_key_id") or ""))
        except (ValueError, TypeError) as exc:
            raise MailboxTicketError(0, "invalid_ticket_capability") from exc
        per_day = cap.get("per_day")
        epoch_days = cap.get("epoch_days")
        if (
            key_id != pp.token_key_id(key)
            or not isinstance(per_day, int)
            or isinstance(per_day, bool)
            or not isinstance(epoch_days, int)
            or isinstance(epoch_days, bool)
        ):
            raise MailboxTicketError(0, "invalid_ticket_capability")
        if epoch_days < MIN_TICKET_EPOCH_DAYS:
            raise MailboxTicketError(0, "ticket_epoch_too_short")
        return cls(
            key_id=key_id, public_key=key, per_day=per_day, epoch_days=epoch_days
        )


async def read_ticket_issuer(
    session: aiohttp.ClientSession, gfs_url: str, *, gfs_public_key: str
) -> TicketIssuer:
    """Read the ticket issuer from *gfs_url*'s ``/gfs/info``: the GFS key
    must be the one the household pinned (*gfs_public_key*) and the
    capability block must verify under it. Call it once over the household's
    usual session and once over the anonymous one (:class:`TicketKeyGuard`).
    Raises :class:`MailboxTicketError`."""
    try:
        async with session.get(
            gfs_url.rstrip("/") + "/gfs/info",
            timeout=aiohttp.ClientTimeout(total=MAILBOX_REGISTER_TIMEOUT_S),
        ) as resp:
            info = await _json_body(resp)
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise MailboxTicketError(0, "gfs_info_unreachable") from exc
    caps = info.get("capabilities")
    gfs_id = info.get("gfs_instance_id")
    sig = info.get("capabilities_sig")
    sig_suite = info.get("capabilities_sig_suite")
    if (
        status != 200
        or info.get("public_key") != gfs_public_key
        or not isinstance(caps, dict)
        or not isinstance(gfs_id, str)
        or not isinstance(sig, str)
        or not isinstance(sig_suite, str)
        or not verify_capabilities(gfs_public_key, gfs_id, caps, sig, sig_suite)
    ):
        raise MailboxTicketError(status, "invalid_ticket_capability")
    return TicketIssuer.from_capability(caps.get("mailbox_tickets"))


class TicketKeyGuard:
    """Issuer-key consistency for ONE GFS (the Privacy Pass consistency
    problem): a GFS that showed each household its own key would read the
    household off every ticket's ``token_key_id``. Two checks:

    * the key must be the one an ANONYMOUS read of ``/gfs/info`` shows —
      *anon_view*, over the cookie-less session mailbox registration uses
      (:func:`read_ticket_issuer`) — so the GFS can't single the household
      out through its identified session. That read runs on its own
      jittered schedule (:meth:`start`, :meth:`refresh`) and is cached:
      :meth:`confirm_issuer` (before blinding) compares against the cache,
      so no anonymous read sits milliseconds before the identified fetch.
      :meth:`confirm_ticket` (before redeeming, itself anonymous) reads
      afresh.
    * one new key per epoch at most (:class:`TicketKeyPin`): a key seen
      first in epoch *e* is replaced only once the clock is in a later
      epoch (``epoch_days`` from the capability, at least
      :data:`MIN_TICKET_EPOCH_DAYS`); the replaced key stays usable for its
      one overlap epoch. Epochs are aligned to the Unix epoch, as the GFS
      aligns them.

    A GFS that recognises the household's ADDRESS can still show it its own
    key in both views from first contact (the stated IP limit). Failures
    raise :class:`MailboxTicketError` — ``ticket_key_unconfirmed`` (no
    anonymous read yet), ``ticket_key_inconsistent`` (the views differ, or a
    second new key inside one epoch) or ``ticket_key_not_admitted`` (a
    ticket under a key no longer admitted) — and log a WARNING with no key
    and no id. The pin is in memory; :attr:`pin` / ``pin=`` let the caller
    keep it across restarts."""

    __slots__ = (
        "_anon",
        "_anon_view",
        "_clock",
        "_period",
        "_pin",
        "_rng",
        "_stop",
        "_task",
    )

    def __init__(
        self,
        anon_view: Callable[[], Awaitable[TicketIssuer]],
        *,
        pin: TicketKeyPin | None = None,
        clock: Callable[[], float] = time.time,
        period: float = TICKET_ANON_VIEW_PERIOD_S,
        rng: random.Random | None = None,
    ) -> None:
        self._anon_view = anon_view
        self._anon: TicketIssuer | None = None
        self._pin = pin
        self._clock = clock
        self._period = period
        self._rng = rng if rng is not None else random.SystemRandom()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def pin(self) -> TicketKeyPin | None:
        return self._pin

    def _refuse(self, why: str, error: str = "ticket_key_inconsistent"):
        log.warning("gfs.mailbox.tickets: issuer key not consistent (%s)", why)
        return MailboxTicketError(0, error)

    def _admit(self, issuer: TicketIssuer) -> None:
        now = int(self._clock())
        pin = self._pin
        if pin is None:
            self._pin = TicketKeyPin(issuer.key_id, now, issuer.epoch_days, None)
            return
        if issuer.key_id in (pin.key_id, pin.previous_key_id):
            return
        epoch_s = max(pin.epoch_days, issuer.epoch_days) * _DAY_S
        if (now + TICKET_EPOCH_SKEW_S) // epoch_s <= pin.first_seen // epoch_s:
            raise self._refuse("a second new key inside one epoch")
        self._pin = TicketKeyPin(issuer.key_id, now, issuer.epoch_days, pin.key_id)

    async def refresh(self) -> None:
        """Read the anonymous view now, admit it and cache it. Raises
        :class:`MailboxTicketError` (the cache keeps the last good view)."""
        issuer = await self._anon_view()
        self._admit(issuer)
        self._anon = issuer

    async def start(self) -> None:
        """Start the jittered anonymous-read loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            _jittered_loop(
                self._stop, self._period, self._rng, self.refresh, "anonymous read"
            )
        )

    async def stop(self) -> None:
        """Stop the anonymous-read loop and wait for it to exit."""
        await _stop_task(self._stop, self._task)
        self._task = None

    async def confirm_issuer(self, issuer: TicketIssuer) -> None:
        """Check *issuer* (the household's own read) before blinding,
        against the CACHED anonymous view (no read here)."""
        anon = self._anon
        if anon is None:
            raise self._refuse("no anonymous view yet", "ticket_key_unconfirmed")
        if anon.key_id != issuer.key_id:
            raise self._refuse("the anonymous view shows another key")
        self._admit(issuer)

    async def confirm_ticket(self, ticket: bytes) -> None:
        """Check, before redeeming *ticket*, that the anonymous view (read
        now, over the anonymous session) shows an admitted key and that the
        ticket is under one."""
        await self.refresh()
        pin = self._pin
        if pin is None or _ticket_key_id(ticket) not in (
            pin.key_id,
            pin.previous_key_id,
        ):
            raise self._refuse(
                "the ticket's key is not an admitted one", "ticket_key_not_admitted"
            )


def build_ticket_request(
    issuer: TicketIssuer,
    count: int,
    *,
    instance_id: str,
    signing_key: bytes,
    ts: str | None = None,
) -> tuple[dict, list[pp.PendingToken]]:
    """The signed ``POST /gfs/mailbox/tickets`` body for *count* tokens
    and the blinding state :func:`fetch_tickets` finishes them with. The
    household signs ``{action, instance_id, suite, issuer_key_id, requests,
    ts}`` with its identity key (*signing_key*, the Ed25519 seed)."""
    if not 0 < count <= MAX_TICKETS_PER_REQUEST:
        raise ValueError("ticket count out of range")
    requests: list[str] = []
    pending: list[pp.PendingToken] = []
    for _ in range(count):
        raw, state = pp.token_request(issuer.public_key, MAILBOX_TICKET_CHALLENGE)
        requests.append(b64url_encode(raw))
        pending.append(state)
    payload: dict[str, object] = {
        "action": TICKETS_ACTION,
        "instance_id": instance_id,
        "suite": pp.TICKET_SUITE_PP_BLINDRSA,
        "issuer_key_id": b64url_encode(issuer.key_id),
        "requests": requests,
        "ts": ts if ts is not None else datetime.now(UTC).isoformat(),
    }
    signature = b64url_encode(sign_ed25519(signing_key, canonical_bytes(payload)))
    return {**payload, "signature": signature}, pending


async def fetch_tickets(
    session: aiohttp.ClientSession,
    gfs_url: str,
    issuer: TicketIssuer,
    count: int,
    *,
    instance_id: str,
    signing_key: bytes,
    guard: TicketKeyGuard,
) -> list[bytes]:
    """Fetch up to *count* tickets (the GFS may grant fewer — what is left
    of today's batch). *guard* checks the issuer key first (nothing is
    blinded under a key it refuses). Returns encoded RFC 9577 tokens, each
    checked against the issuer key. Raises :class:`MailboxTicketError`."""
    await guard.confirm_issuer(issuer)
    body, pending = await asyncio.to_thread(
        build_ticket_request,
        issuer,
        count,
        instance_id=instance_id,
        signing_key=signing_key,
    )
    try:
        async with session.post(
            gfs_url.rstrip("/") + MAILBOX_TICKETS_ROUTE,
            json=body,
            timeout=aiohttp.ClientTimeout(total=MAILBOX_REGISTER_TIMEOUT_S),
        ) as resp:
            data = await _json_body(resp)
            status = resp.status
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise MailboxTicketError(0, "gfs_unreachable") from exc
    if status != 200:
        raise MailboxTicketError(status, str(data.get("error") or "unexpected"))
    responses = data.get("responses")
    if (
        data.get("suite") != pp.TICKET_SUITE_PP_BLINDRSA
        or data.get("issuer_key_id") != body["issuer_key_id"]
        or not isinstance(responses, list)
        or not 0 < len(responses) <= len(pending)
    ):
        raise MailboxTicketError(200, "invalid_ticket_response")
    try:
        return await asyncio.to_thread(_finalize_all, issuer, pending, responses)
    except (pp.InvalidToken, ValueError, TypeError) as exc:
        raise MailboxTicketError(200, "invalid_ticket_response") from exc


def _finalize_all(
    issuer: TicketIssuer, pending: list[pp.PendingToken], responses: list
) -> list[bytes]:
    return [
        pp.finalize_token(issuer.public_key, state, b64url_decode(str(r))).encode()
        for state, r in zip(pending, responses, strict=False)
    ]


class MailboxTicketCache:
    """The household's unspent tickets for ONE GFS, refilled through
    *fetch* (``count → tickets``, e.g. :func:`fetch_tickets` bound to the
    GFS, the household's key and *guard*). In memory only: a ticket is
    worth one mailbox and the batch is refilled daily, so losing it on
    restart costs nothing that matters.

    Refills run on their own schedule (:meth:`start`): about once per
    *period*, each wait jittered ±50 %, and a slot within *quiet* seconds
    after a register (a ticket taken) is skipped. :meth:`take` never
    fetches: an identified fetch next to an anonymous register would let
    the GFS pair the two by time — and a GFS that refused a register on
    purpose could force exactly that."""

    __slots__ = (
        "_batch",
        "_fetch",
        "_guard",
        "_last_take",
        "_lock",
        "_period",
        "_quiet",
        "_rng",
        "_stop",
        "_task",
        "_tickets",
    )

    def __init__(
        self,
        fetch: Callable[[int], Awaitable[list[bytes]]],
        *,
        batch: int,
        guard: TicketKeyGuard,
        period: float = TICKET_REFILL_PERIOD_S,
        quiet: float = TICKET_REFILL_QUIET_S,
        rng: random.Random | None = None,
    ) -> None:
        self._fetch = fetch
        self._guard = guard
        self._batch = max(1, min(batch, MAX_TICKETS_PER_REQUEST))
        self._tickets: list[bytes] = []
        self._lock = asyncio.Lock()
        self._period = period
        self._quiet = quiet
        self._rng = rng if rng is not None else random.SystemRandom()
        self._last_take = -float("inf")
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def __len__(self) -> int:
        return len(self._tickets)

    def __repr__(self) -> str:  # tokens are bearer credentials
        return f"MailboxTicketCache(<{len(self._tickets)} tickets>)"

    async def refill(self) -> int:
        """Top the cache up to the batch size; returns how many it holds.
        Raises :class:`MailboxTicketError` (429 once today's batch is
        used up)."""
        async with self._lock:
            want = self._batch - len(self._tickets)
            if want > 0:
                self._tickets.extend(await self._fetch(want))
            return len(self._tickets)

    async def start(self) -> None:
        """Start the jittered refill loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            _jittered_loop(self._stop, self._period, self._rng, self._slot, "refill")
        )

    async def stop(self) -> None:
        """Stop the refill loop and wait for it to exit."""
        await _stop_task(self._stop, self._task)
        self._task = None

    async def _slot(self) -> None:
        await self.refill_if_quiet()

    async def refill_if_quiet(self) -> bool:
        """:meth:`refill`, unless a ticket was taken within the quiet window
        (an identified fetch right after an anonymous register would tie
        them) — e.g. on a (re)connect, so a fresh household is not without
        tickets until the first daily slot. Returns whether it refilled."""
        if time.monotonic() - self._last_take < self._quiet:
            return False  # right after a register: wait for another slot
        await self.refill()
        return True

    def take(self) -> bytes:
        """One unspent ticket (it leaves the cache: a ticket is never
        offered twice). Raises :class:`MailboxTicketError` ``no_ticket``
        when empty — never fetches (see the class docstring)."""
        if not self._tickets:
            raise MailboxTicketError(0, "no_ticket")
        self._last_take = time.monotonic()
        return self._tickets.pop()

    def put_back(self, ticket: bytes) -> None:
        """Return a ticket the GFS never saw (refused by the guard, or a
        connection that never reached the GFS). A ticket the GFS saw is
        never put back: showing it again for another mailbox would link
        the two."""
        self._tickets.append(ticket)

    async def confirm(self, ticket: bytes) -> None:
        """:meth:`TicketKeyGuard.confirm_ticket` before redeeming."""
        await self._guard.confirm_ticket(ticket)

    def drop_key(self, key_id: bytes) -> None:
        """Drop the tickets issued under *key_id* (the GFS no longer
        accepts it); tickets under other keys stay."""
        self._tickets = [t for t in self._tickets if _ticket_key_id(t) != key_id]

    def clear(self) -> None:
        """Drop every ticket."""
        self._tickets.clear()


def _ticket_key_id(ticket: bytes) -> bytes:
    """The ``token_key_id`` of an encoded RFC 9577 token."""
    return ticket[2 + 32 + 32 : 2 + 32 + 32 + 32]


#: A register refused for this ticket: it is gone (the cached ones are
#: still good), so one retry with the next cached ticket makes sense.
_TICKET_SPENT: str = "ticket_spent"
#: The GFS no longer accepts the ticket's key: drop every ticket under it.
_TICKET_STALE_KEY: str = "stale_ticket_key"
#: The guard no longer admits the ticket's key: drop every ticket under it.
_TICKET_KEY_NOT_ADMITTED: str = "ticket_key_not_admitted"
#: A NEW mailbox: the GFS wants a ticket.
_TICKET_REQUIRED: str = "ticket_required"


def build_register_body(
    keys: MailboxKeys,
    gfs_instance_id: str,
    *,
    ticket: bytes | None = None,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    """The ``POST /gfs/mailbox/register`` body for *keys* at the GFS whose
    public instance id is *gfs_instance_id* (from its signed ``/gfs/info``),
    carrying *ticket* (an encoded token) when given."""
    ts = int(time.time()) if now is None else now
    nonce = nonce if nonce is not None else b64url_encode(secrets.token_bytes(16))
    signed = register_signed_bytes(gfs_instance_id, keys.mailbox_id, ts, nonce)
    body: dict[str, object] = {
        "sig_suite": MAILBOX_SIG_SUITE_ED25519,
        "sign_pk": b64url_encode(keys.sign_public),
        "mailbox_id": keys.mailbox_id,
        "gfs_instance_id": gfs_instance_id,
        "ts": ts,
        "nonce": nonce,
        "sig": b64url_encode(sign_ed25519(keys.sign_seed, signed)),
    }
    if ticket is not None:
        body["ticket_suite"] = pp.TICKET_SUITE_PP_BLINDRSA
        body["ticket"] = b64url_encode(ticket)
    return body


async def _json_body(resp: aiohttp.ClientResponse) -> dict:
    raw = await resp.read()
    try:
        body = orjson.loads(raw) if raw else {}
    except orjson.JSONDecodeError:
        body = {}
    return body if isinstance(body, dict) else {}


async def _post_register(
    session: aiohttp.ClientSession, url: str, body: dict
) -> tuple[int, dict]:
    async with session.post(
        url,
        json=body,
        timeout=aiohttp.ClientTimeout(total=MAILBOX_REGISTER_TIMEOUT_S),
    ) as resp:
        return resp.status, await _json_body(resp)


async def register_mailbox(
    session: aiohttp.ClientSession,
    gfs_url: str,
    keys: MailboxKeys,
    gfs_instance_id: str,
    *,
    tickets: MailboxTicketCache | None = None,
) -> str:
    """Register *keys*' mailbox at *gfs_url*.

    The first try carries no ticket: an existing mailbox (or a GFS that
    takes no tickets) answers 200 and no ticket is ever shown. Only on
    ``403 ticket_required`` — a NEW mailbox — is one taken from *tickets*,
    checked against the key guard (:meth:`MailboxTicketCache.confirm`; one
    the guard refuses for an inconsistent view goes back untouched, one
    under a key no longer admitted is dropped with its key's tickets) and
    sent. A spent ticket is replaced
    once from the cache; a ticket under a retired key drops that key's
    tickets and fails — the caller registers again after the cache's own
    scheduled refill, never right after a fetch. A ticket whose request
    never reached the GFS (no connection) goes back to the cache; one the
    GFS saw does not (reusing it would link two mailboxes).

    Returns the mailbox id the GFS confirmed; raises
    :class:`MailboxRegisterError` on a refusal and
    :class:`MailboxTicketError` (``no_ticket`` when the cache is empty,
    ``ticket_key_inconsistent`` when the guard refuses)."""
    url = gfs_url.rstrip("/") + MAILBOX_REGISTER_ROUTE
    status, body = await _post_register(
        session, url, build_register_body(keys, gfs_instance_id)
    )
    error = str(body.get("error") or "unexpected")
    if status != 200 and error == _TICKET_REQUIRED and tickets is not None:
        for attempt in range(2):
            ticket = tickets.take()
            try:
                await tickets.confirm(ticket)
            except MailboxTicketError as exc:
                if exc.error == _TICKET_KEY_NOT_ADMITTED:
                    # Its key aged out: put back, it would be taken again
                    # next time and block every register.
                    tickets.drop_key(_ticket_key_id(ticket))
                else:
                    tickets.put_back(ticket)  # never shown to the GFS
                raise
            try:
                status, body = await _post_register(
                    session,
                    url,
                    build_register_body(keys, gfs_instance_id, ticket=ticket),
                )
            except aiohttp.ClientConnectorError:
                tickets.put_back(ticket)  # never reached the GFS
                raise
            if status == 200:
                break
            error = str(body.get("error") or "unexpected")
            if error == _TICKET_STALE_KEY:
                tickets.drop_key(_ticket_key_id(ticket))
            elif error == _TICKET_SPENT and attempt == 0 and len(tickets):
                continue
            break
    if status != 200:
        raise MailboxRegisterError(status, error)
    if body.get("mailbox_id") != keys.mailbox_id:
        raise MailboxRegisterError(status, "mailbox_id_mismatch")
    return keys.mailbox_id


def build_unregister_body(
    keys: MailboxKeys,
    gfs_instance_id: str,
    *,
    now: int | None = None,
    nonce: str | None = None,
) -> dict:
    """The ``POST /gfs/mailbox/unregister`` body dropping *keys*' mailbox at
    the GFS whose public instance id is *gfs_instance_id*."""
    ts = int(time.time()) if now is None else now
    nonce = nonce if nonce is not None else b64url_encode(secrets.token_bytes(16))
    signed = unregister_signed_bytes(gfs_instance_id, keys.mailbox_id, ts, nonce)
    return {
        "sig_suite": MAILBOX_SIG_SUITE_ED25519,
        "mailbox_id": keys.mailbox_id,
        "gfs_instance_id": gfs_instance_id,
        "ts": ts,
        "nonce": nonce,
        "sig": b64url_encode(sign_ed25519(keys.sign_seed, signed)),
    }


async def unregister_mailbox(
    session: aiohttp.ClientSession,
    gfs_url: str,
    keys: MailboxKeys,
    gfs_instance_id: str,
) -> bool:
    """Drop *keys*' mailbox at *gfs_url*. ``True`` on the GFS's 200;
    ``False`` on any other status (an older GFS answers 404) or a network
    fault — never raises for those (the idle sweep drops it eventually)."""
    url = gfs_url.rstrip("/") + MAILBOX_UNREGISTER_ROUTE
    try:
        status, _body = await _post_register(
            session, url, build_unregister_body(keys, gfs_instance_id)
        )
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("gfs.mailbox: unregister did not reach the GFS: %r", exc)
        return False
    if status != 200:
        log.warning("gfs.mailbox: unregister answered HTTP %d", status)
        return False
    return True


def build_claim(
    keys: MailboxKeys,
    gfs_instance_id: str,
    challenge: str,
    *,
    accepts: tuple[str, ...] = (ACCEPTS_ENVELOPE_MEDIA,),
) -> dict:
    """The ``mailbox_claim`` answer to *challenge*."""
    signed = claim_signed_bytes(gfs_instance_id, keys.mailbox_id, challenge)
    return {
        "type": FRAME_MAILBOX_CLAIM,
        "mailbox_id": keys.mailbox_id,
        "gfs_instance_id": gfs_instance_id,
        "sig_suite": MAILBOX_SIG_SUITE_ED25519,
        "sig": b64url_encode(sign_ed25519(keys.sign_seed, signed)),
        "accepts": list(accepts),
    }


class MailboxClaim:
    """One anonymous claim socket for ONE mailbox.

    ``async with MailboxClaim(...) as claim: async for frame in claim: ...``
    — entering connects and completes the claim (or raises
    :class:`MailboxClaimError`); iterating yields ``dict`` JSON frames and
    ``bytes`` media frames until the socket closes. Reconnects are the
    caller's job (PR 2)."""

    __slots__ = ("_accepts", "_gfs_instance_id", "_gfs_url", "_keys", "_session", "_ws")

    def __init__(
        self,
        session: aiohttp.ClientSession,
        gfs_url: str,
        keys: MailboxKeys,
        gfs_instance_id: str,
        *,
        accepts: tuple[str, ...] = (ACCEPTS_ENVELOPE_MEDIA,),
    ) -> None:
        self._session = session
        self._gfs_url = gfs_url.rstrip("/")
        self._keys = keys
        self._gfs_instance_id = gfs_instance_id
        self._accepts = accepts
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def __aenter__(self) -> Self:
        ws = await self._session.ws_connect(
            self._gfs_url + MAILBOX_WS_ROUTE,
            heartbeat=MAILBOX_CLIENT_HEARTBEAT_S,
            max_msg_size=MAILBOX_CLIENT_MAX_MSG_BYTES,
        )
        self._ws = ws
        try:
            await self._handshake(ws)
        except BaseException:
            await ws.close()
            self._ws = None
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def aclose(self) -> None:
        """:meth:`close` under the name ``contextlib.aclosing`` calls — for a
        claim entered by hand (e.g. retried until the GFS accepts it)."""
        await self.close()

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        challenge = await self._json(ws)
        if challenge.get("type") != FRAME_MAILBOX_CHALLENGE:
            raise MailboxClaimError("no challenge")
        # The claim is signed over the GFS id WE pinned, never the one the
        # challenge names: a node of the same cluster may still serve another
        # (per-node or former) id and accepts ours; a server that does not
        # answer to our pinned id refuses the claim (4401).
        suites = challenge.get("sig_suites")
        if not isinstance(suites, list) or MAILBOX_SIG_SUITE_ED25519 not in suites:
            raise MailboxClaimError("no common mailbox sig suite")
        nonce = challenge.get("challenge")
        if not isinstance(nonce, str) or not is_mailbox_nonce(nonce):
            raise MailboxClaimError("malformed challenge")
        await ws.send_json(
            build_claim(self._keys, self._gfs_instance_id, nonce, accepts=self._accepts)
        )
        claimed = await self._json(ws)
        if claimed.get("type") != FRAME_MAILBOX_CLAIMED:
            raise MailboxClaimError("claim not acknowledged")

    @staticmethod
    async def _json(ws: aiohttp.ClientWebSocketResponse) -> dict:
        try:
            msg = await ws.receive(timeout=MAILBOX_CLAIM_TIMEOUT_S)
        except TimeoutError as exc:
            raise MailboxClaimError("claim timed out") from exc
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise MailboxClaimError(
                f"claim refused (close {ws.close_code}: {msg.extra or ''})"
            )
        try:
            body = orjson.loads(msg.data)
        except orjson.JSONDecodeError as exc:
            raise MailboxClaimError("claim frame is not JSON") from exc
        if not isinstance(body, dict):
            raise MailboxClaimError("claim frame is not an object")
        return body

    async def receive(self, timeout: float | None = None) -> dict | bytes | None:
        """The next frame: ``dict`` (JSON) or ``bytes`` (binary media),
        ``None`` once the socket closed. Malformed text frames are skipped."""
        ws = self._ws
        if ws is None:
            return None
        while True:
            msg = await ws.receive(timeout=timeout)
            if msg.type == aiohttp.WSMsgType.BINARY:
                return bytes(msg.data)
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    body = orjson.loads(msg.data)
                except orjson.JSONDecodeError:
                    log.debug("gfs.mailbox.client: skipped a non-JSON frame")
                    continue
                if isinstance(body, dict):
                    return body
                continue
            return None

    async def __aiter__(self) -> AsyncIterator[dict | bytes]:
        while True:
            frame = await self.receive()
            if frame is None:
                return
            yield frame
