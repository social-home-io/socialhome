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
  tokens. Fetch ahead of use (:meth:`MailboxTicketCache.refill` on connect):
  a fetch right before a register would let the GFS line the two up by time.
* :func:`register_mailbox` — the anonymous ``POST /gfs/mailbox/register``
  with proof of possession, bound to the GFS id the household pinned (a
  cluster node answers to its siblings' and its former ids too), carrying
  one ticket (spent only when the mailbox is new).
* :class:`MailboxClaim` — the anonymous claim socket (``/gfs/mailbox/ws``,
  one mailbox per socket): answers the challenge, then yields the queued
  and live frames — ``dict`` for a JSON envelope frame, ``bytes`` for a
  binary ``\\x00SHR`` media frame, unchanged.

Not wired into any space flow yet (that is redesign PR 2, v_88).
"""

from __future__ import annotations

import asyncio
import logging
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
from ..crypto import b64url_decode, b64url_encode, ed25519_public_key, sign_ed25519
from ..domain.gfs_mailbox import (
    FRAME_MAILBOX_CHALLENGE,
    FRAME_MAILBOX_CLAIM,
    FRAME_MAILBOX_CLAIMED,
    MAILBOX_REGISTER_ROUTE,
    MAILBOX_SIG_SUITE_ED25519,
    MAILBOX_TICKET_CHALLENGE,
    MAILBOX_TICKETS_ROUTE,
    MAILBOX_WS_ROUTE,
    MAX_TICKETS_PER_REQUEST,
    TICKETS_ACTION,
    claim_signed_bytes,
    derive_mailbox_id,
    is_mailbox_nonce,
    register_signed_bytes,
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

    @classmethod
    def from_capability(cls, cap: object) -> TicketIssuer:
        """Parse and check the capability: a supported ``suite`` (no
        fallback), an RSASSA-PSS key whose RFC 9578 key id is the advertised
        ``issuer_key_id``. Raises :class:`MailboxTicketError`."""
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
        if key_id != pp.token_key_id(key) or not isinstance(per_day, int):
            raise MailboxTicketError(0, "invalid_ticket_capability")
        return cls(key_id=key_id, public_key=key, per_day=per_day)


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
) -> list[bytes]:
    """Fetch up to *count* tickets (the GFS may grant fewer — what is left
    of today's batch). Returns encoded RFC 9577 tokens, each checked
    against the issuer key. Raises :class:`MailboxTicketError`."""
    body, pending = await asyncio.to_thread(
        build_ticket_request,
        issuer,
        count,
        instance_id=instance_id,
        signing_key=signing_key,
    )
    async with session.post(
        gfs_url.rstrip("/") + MAILBOX_TICKETS_ROUTE,
        json=body,
        timeout=aiohttp.ClientTimeout(total=MAILBOX_REGISTER_TIMEOUT_S),
    ) as resp:
        data = await _json_body(resp)
        if resp.status != 200:
            raise MailboxTicketError(
                resp.status, str(data.get("error") or "unexpected")
            )
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
    GFS and the household's key). In memory only: a ticket is worth one
    mailbox and the batch is refilled daily, so losing it on restart costs
    nothing that matters.

    Call :meth:`refill` ahead of use (on connect, once a day): :meth:`take`
    fetches only when the cache is empty, and a fetch right before a
    register lets the GFS pair the two by time."""

    __slots__ = ("_batch", "_fetch", "_lock", "_tickets")

    def __init__(
        self, fetch: Callable[[int], Awaitable[list[bytes]]], *, batch: int
    ) -> None:
        self._fetch = fetch
        self._batch = max(1, min(batch, MAX_TICKETS_PER_REQUEST))
        self._tickets: list[bytes] = []
        self._lock = asyncio.Lock()

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

    async def take(self) -> bytes:
        """One unspent ticket (it leaves the cache: a ticket is never
        offered twice)."""
        if not self._tickets:
            await self.refill()
        if not self._tickets:  # pragma: no cover — fetch grants ≥ 1 or raises
            raise MailboxTicketError(0, "no_ticket")
        return self._tickets.pop()

    def clear(self) -> None:
        """Drop every ticket (the GFS no longer accepts their key)."""
        self._tickets.clear()


#: Register refusals after which the ticket — or every cached one — is
#: useless, and one retry with a fresh ticket makes sense.
_TICKET_SPENT: str = "ticket_spent"
_TICKET_STALE: frozenset[str] = frozenset({"stale_ticket_key", "invalid_ticket"})


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


async def register_mailbox(
    session: aiohttp.ClientSession,
    gfs_url: str,
    keys: MailboxKeys,
    gfs_instance_id: str,
    *,
    tickets: MailboxTicketCache | None = None,
) -> str:
    """Register *keys*' mailbox at *gfs_url*, spending one ticket from
    *tickets* (a GFS that requires them refuses a new mailbox without).
    A spent or stale ticket is replaced once. Returns the mailbox id the GFS
    confirmed; raises :class:`MailboxRegisterError` on a refusal (and
    :class:`MailboxTicketError` when no ticket can be had)."""
    url = gfs_url.rstrip("/") + MAILBOX_REGISTER_ROUTE
    for attempt in range(2):
        ticket = await tickets.take() if tickets is not None else None
        async with session.post(
            url,
            json=build_register_body(keys, gfs_instance_id, ticket=ticket),
            timeout=aiohttp.ClientTimeout(total=MAILBOX_REGISTER_TIMEOUT_S),
        ) as resp:
            body = await _json_body(resp)
            status = resp.status
        if status == 200:
            break
        error = str(body.get("error") or "unexpected")
        if tickets is not None and attempt == 0:
            if error in _TICKET_STALE:
                tickets.clear()  # the GFS moved on from their key
                continue
            if error == _TICKET_SPENT:
                continue
        raise MailboxRegisterError(status, error)
    if body.get("mailbox_id") != keys.mailbox_id:
        raise MailboxRegisterError(status, "mailbox_id_mismatch")
    return keys.mailbox_id


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
