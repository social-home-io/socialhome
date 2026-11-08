"""Anonymous registration tickets for GFS mailboxes (Privacy Pass).

``POST /gfs/mailbox/register`` is anonymous, so a per-address limit alone
can't stop a botnet from minting mailboxes, and the server-wide daily cap
that used to back it let ~100 addresses block every new space join for the
rest of the day. Instead every NEW mailbox now spends one ticket: a
standard Privacy Pass token (RFC 9576 architecture, RFC 9577 token format,
RFC 9578 §6 publicly verifiable issuance — token type 0x0002, blind RSA
RFC 9474, :mod:`socialhome.privacy_pass`).

**Issuance** (``POST /gfs/mailbox/tickets``) — a household registered and
active here signs the request with its identity key
(:meth:`GfsFederationService.verify_signed_request`), carrying up to
:data:`MAX_TICKETS_PER_REQUEST` blinded ``TokenRequest``\\ s. The server
grants up to ``mailbox_tickets_per_day`` a UTC day per household (one row
in ``gfs_ticket_issuance``: today's count) and blind-signs them. It sees
only blinded messages and keeps none of them: it learns "household H took
N tickets today", nothing it could match to a redemption.

**Redemption** (inside :meth:`GfsMailboxService.register`) — the body
carries one unblinded ``Token``. The server checks the challenge digest
(the fixed :data:`MAILBOX_TICKET_CHALLENGE`), finds the issuer key by its
``token_key_id`` among the keys it still accepts, verifies the
authenticator (RSASSA-PSS, OpenSSL); the mailbox insert then records the
token nonce in ``gfs_spent_tickets`` in the SAME transaction — a primary
key, so a second spend on any node is refused, and a register that creates
nothing (an existing mailbox, a collision, a crash) spends nothing.

**Keys** — one RSA-2048 issuer key per epoch (``mailbox_ticket_epoch_days``),
made on first use by whichever node needs it first and stored in the shared
DB (``INSERT OR IGNORE`` per epoch, then re-read — every node signs with the
same key). A key is accepted for redemption for two epochs after it was
made, so a ticket fetched late in an epoch stays good through the next.
Spent nonces live as long as their key. The current key is published in the
signed ``/gfs/info`` capability ``mailbox_tickets``; a household blinds
only under that key and Finalize verifies under it.

Nothing here logs an instance id, a token or a blinded message.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_der_private_key,
)

from .. import crypto
from .. import privacy_pass as pp
from ..domain.gfs_mailbox import (
    MAILBOX_TICKET_CHALLENGE,
    MAX_TICKETS_PER_REQUEST,
    TICKETS_ACTION,
    TicketIssuerKey,
)
from .config import EnvelopeMailboxConfig
from .mailboxes import MailboxRefused
from .repositories import AbstractGfsMailboxTicketRepo

if TYPE_CHECKING:
    from .federation import GfsFederationService

log = logging.getLogger(__name__)

DAY_SECONDS: int = 86_400
#: How often the accepted keys are re-read from the shared table.
KEYS_REFRESH_S: float = 60.0
#: An unknown ``token_key_id`` re-reads the table at most this often (a
#: sibling may have just made the epoch's key; junk ids can't force reads).
KEYS_MISS_REFRESH_S: float = 1.0
#: SHA-256 of the fixed challenge — what every mailbox ticket carries.
CHALLENGE_DIGEST: bytes = hashlib.sha256(MAILBOX_TICKET_CHALLENGE).digest()


class _LiveKey:
    __slots__ = ("private", "public", "row")

    def __init__(self, row: TicketIssuerKey, private: rsa.RSAPrivateKey) -> None:
        self.row = row
        self.private = private
        self.public = private.public_key()


def _new_key_sync(epoch: int, now: int) -> TicketIssuerKey:
    key = pp.generate_issuer_key()
    return TicketIssuerKey(
        epoch=epoch,
        key_id=pp.token_key_id(key.public_key()),
        private_key_der=key.private_bytes(
            Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
        ),
        created_at=now,
    )


def _load_sync(row: TicketIssuerKey) -> _LiveKey:
    key = load_der_private_key(row.private_key_der, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("ticket issuer key is not RSA")
    return _LiveKey(row, key)


def _sign_all_sync(key: rsa.RSAPrivateKey, blinded: list[bytes]) -> list[bytes]:
    return [pp.blind_sign(key, b) for b in blinded]


class MailboxTicketService:
    """Issue and redeem mailbox registration tickets."""

    __slots__ = (
        "_cfg",
        "_clock",
        "_current",
        "_federation",
        "_keys",
        "_keys_at",
        "_lock",
        "_miss_at",
        "_repo",
    )

    def __init__(
        self,
        *,
        repo: AbstractGfsMailboxTicketRepo,
        config: EnvelopeMailboxConfig,
        federation: GfsFederationService,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._repo = repo
        self._cfg = config
        self._federation = federation
        self._clock = clock
        self._current: _LiveKey | None = None
        #: key id → live key, for every key still accepted for redemption.
        self._keys: dict[bytes, _LiveKey] = {}
        self._keys_at = -KEYS_REFRESH_S - 1.0
        self._miss_at = -KEYS_MISS_REFRESH_S - 1.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    @property
    def _epoch_seconds(self) -> int:
        return self._cfg.mailbox_ticket_epoch_days * DAY_SECONDS

    def _accept_after(self, now: float) -> int:
        """Keys made at or before this are no longer redeemable."""
        return int(now) - 2 * self._epoch_seconds

    # ── Keys ─────────────────────────────────────────────────────────────

    async def current_key(self) -> _LiveKey:
        """This epoch's issuer key, made (once, cluster-wide) if missing."""
        now = self._clock()
        epoch = int(now) // self._epoch_seconds
        cur = self._current
        if cur is not None and cur.row.epoch == epoch:
            return cur
        async with self._lock:
            cur = self._current
            if cur is not None and cur.row.epoch == epoch:
                return cur
            row = await self._repo.key_for_epoch(epoch)
            if row is None:
                made = await asyncio.to_thread(_new_key_sync, epoch, int(now))
                await self._repo.add_key(made)
                # Another node may have won the insert: use what is stored.
                row = await self._repo.key_for_epoch(epoch)
                if row is None:  # pragma: no cover — the insert just ran
                    raise RuntimeError("ticket issuer key was not stored")
                log.info("gfs.mailbox.tickets: issuer key for epoch %d in use", epoch)
            live = await asyncio.to_thread(_load_sync, row)
            self._current = live
            self._keys[row.key_id] = live
            return live

    async def _accepted(self, key_id: bytes) -> _LiveKey | None:
        now = self._clock()
        stale = now - self._keys_at >= KEYS_REFRESH_S
        miss = key_id not in self._keys and now - self._miss_at >= KEYS_MISS_REFRESH_S
        if stale or miss:
            if miss:
                self._miss_at = now
            rows = await self._repo.keys_since(self._accept_after(now))
            known = {
                k: v for k, v in self._keys.items() if k in {r.key_id for r in rows}
            }
            for row in rows:
                if row.key_id not in known:
                    known[row.key_id] = await asyncio.to_thread(_load_sync, row)
            self._keys = known
            self._keys_at = now
        live = self._keys.get(key_id)
        if live is None or live.row.created_at <= self._accept_after(now):
            return None
        return live

    async def capability(self) -> dict | None:
        """The signed ``/gfs/info`` ``mailbox_tickets`` entry, ``None`` off."""
        if not self._cfg.enabled:
            return None
        live = await self.current_key()
        return {
            "suite": pp.TICKET_SUITE_PP_BLINDRSA,
            "issuer_key_id": crypto.b64url_encode(live.row.key_id),
            "public_key": crypto.b64url_encode(pp.encode_public_key(live.public)),
            "per_day": self._cfg.mailbox_tickets_per_day,
            # How long one key signs: a household that sees the key change
            # faster than this can tell it is being handed its own key.
            "epoch_days": self._cfg.mailbox_ticket_epoch_days,
        }

    # ── Issuance ─────────────────────────────────────────────────────────

    async def issue(self, body: object) -> dict:
        """Blind-sign a household's batch. Raises :class:`MailboxRefused`."""
        if not isinstance(body, dict):
            raise MailboxRefused(400, "invalid_body")
        signature = body.get("signature")
        instance_id = body.get("instance_id")
        if not isinstance(signature, str) or not isinstance(instance_id, str):
            raise MailboxRefused(400, "invalid_body")
        # The ``action`` discriminator is inside the signed bytes, so no
        # other signed GFS request can be replayed as a ticket request.
        if body.get("action") != TICKETS_ACTION:
            raise MailboxRefused(400, "invalid_body")
        payload = {k: v for k, v in body.items() if k != "signature"}
        try:
            inst = await self._federation.verify_signed_request(
                instance_id, payload, signature=signature
            )
        except PermissionError as exc:
            raise MailboxRefused(403, "unauthorized") from exc
        if inst.status != "active":
            raise MailboxRefused(403, "unauthorized")
        try:
            pp.validate_ticket_suite(body.get("suite"))
        except pp.UnsupportedTicketSuite as exc:
            raise MailboxRefused(400, "unsupported_ticket_suite") from exc
        requests = body.get("requests")
        if (
            not isinstance(requests, list)
            or not 0 < len(requests) <= MAX_TICKETS_PER_REQUEST
        ):
            raise MailboxRefused(400, "invalid_requests")
        live = await self.current_key()
        if body.get("issuer_key_id") != crypto.b64url_encode(live.row.key_id):
            # Blinded under another (an older) key: the household re-reads
            # /gfs/info. Nothing was counted.
            raise MailboxRefused(409, "stale_issuer_key")
        n = live.public.public_numbers().n
        blinded: list[bytes] = []
        for item in requests:
            raw = _b64(item)
            try:
                truncated, msg = pp.parse_token_request(raw or b"")
            except pp.InvalidToken as exc:
                raise MailboxRefused(400, "invalid_requests") from exc
            if truncated != live.row.key_id[-1] or int.from_bytes(msg, "big") >= n:
                raise MailboxRefused(400, "invalid_requests")
            blinded.append(msg)
        today = int(self._clock()) // DAY_SECONDS * DAY_SECONDS
        granted = await self._repo.take_issuance(
            inst.instance_id,
            day=today,
            want=len(blinded),
            per_day=self._cfg.mailbox_tickets_per_day,
        )
        if granted == 0:
            raise MailboxRefused(429, "ticket_quota")
        signed = await asyncio.to_thread(
            _sign_all_sync, live.private, blinded[:granted]
        )
        return {
            "suite": pp.TICKET_SUITE_PP_BLINDRSA,
            "issuer_key_id": crypto.b64url_encode(live.row.key_id),
            "responses": [crypto.b64url_encode(s) for s in signed],
        }

    # ── Redemption ───────────────────────────────────────────────────────

    async def check(self, suite: object, ticket: object) -> tuple[bytes, int]:
        """Verify a ticket without spending it: ``(nonce, expires_at)``,
        which the mailbox insert records in its own transaction
        (:meth:`SqliteGfsMailboxRepo.register`). Raises
        :class:`MailboxRefused`."""
        if suite is None and ticket is None:
            raise MailboxRefused(403, "ticket_required")
        try:
            pp.validate_ticket_suite(suite)
        except pp.UnsupportedTicketSuite as exc:
            raise MailboxRefused(400, "unsupported_ticket_suite") from exc
        raw = _b64(ticket, limit=1024)
        try:
            token = pp.Token.decode(raw or b"")
        except pp.InvalidToken as exc:
            raise MailboxRefused(403, "invalid_ticket") from exc
        if token.challenge_digest != CHALLENGE_DIGEST:
            raise MailboxRefused(403, "invalid_ticket")
        live = await self._accepted(token.token_key_id)
        if live is None:
            # Made under a key this server no longer (or never) accepts —
            # the household drops its cached tickets and fetches new ones.
            raise MailboxRefused(403, "stale_ticket_key")
        if not token.verify(live.public):
            raise MailboxRefused(403, "invalid_ticket")
        return token.nonce, live.row.created_at + 2 * self._epoch_seconds

    async def prune(self, *, now: int) -> None:
        """Forget spent nonces of retired keys, past days' counts and keys
        no longer accepted."""
        # One day of margin: a node whose clock runs ahead must never forget
        # a nonce while a sibling still accepts its key.
        await self._repo.prune(
            now=now - DAY_SECONDS,
            day=now // DAY_SECONDS * DAY_SECONDS,
            keys_before=self._accept_after(now) - DAY_SECONDS,
        )


def _b64(value: object, *, limit: int = 512) -> bytes | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    try:
        return crypto.b64url_decode(value)
    except ValueError, TypeError:
        return None
