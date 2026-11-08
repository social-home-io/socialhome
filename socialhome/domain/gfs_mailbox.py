"""GFS mailboxes — anonymous per-space delivery addresses on a GFS.

A household reached through a GFS gets one mailbox per (household, space,
generation) instead of being addressed by its instance id, so the GFS sees
traffic within one space but cannot link the household's spaces to each
other or to its identity. The household derives the mailbox's keys
(:mod:`socialhome.federation.gfs_mailbox`), registers the signing half at
the GFS with proof of possession (``POST /gfs/mailbox/register``) and
claims queued and live frames on an anonymous WebSocket of its own
(``GET /gfs/mailbox/ws``, one mailbox per socket).

The wire shapes both halves must agree on live here, so this module is pure
and imports nothing from the GFS or the household:

* :func:`derive_mailbox_id` — ``base32(SHA-256("sh-mbx-v1" ‖ sign_pk))[:32]``,
  the exact shape of an instance id (32 lowercase base32 characters), so a
  JSON envelope's ``to_instance`` and a binary ``\\x00SHR`` frame's recipient
  field carry a mailbox id unchanged. The ``sh-mbx-v1`` prefix keeps it
  apart from :func:`socialhome.crypto.derive_instance_id` over the same key.
* :func:`register_signed_bytes` / :func:`claim_signed_bytes` — the
  domain-separated bytes the mailbox key signs. Both bind the GFS's public
  instance id, so a registration or a claim made for one GFS proves nothing
  at another.
* the signature suite tag (``sig_suite``), validated with no fallback.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass

from ..privacy_pass import token_challenge

#: Hash prefix of the mailbox id (separates it from an instance id).
MAILBOX_ID_PREFIX: bytes = b"sh-mbx-v1"
#: The id's length — the same as an instance id's.
MAILBOX_ID_CHARS: int = 32

#: Domain separation for the proof of possession in a registration.
MAILBOX_REGISTER_CONTEXT: str = "sh-mbx-register:v1"
#: Domain separation for the answer to a claim challenge.
MAILBOX_CLAIM_CONTEXT: str = "sh-mbx-claim:v1"

#: The mailbox signature suite. Phase 2 adds ``"ed25519+mldsa65"`` beside it.
MAILBOX_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_MAILBOX_SIG_SUITES: frozenset[str] = frozenset({MAILBOX_SIG_SUITE_ED25519})

#: The signed ``/gfs/info`` capability a GFS with mailboxes advertises.
ENVELOPE_MAILBOX_CAPABILITY: str = "envelope_mailbox"

#: Routes.
MAILBOX_REGISTER_ROUTE: str = "/gfs/mailbox/register"
MAILBOX_WS_ROUTE: str = "/gfs/mailbox/ws"
MAILBOX_TICKETS_ROUTE: str = "/gfs/mailbox/tickets"

#: The signed ``/gfs/info`` capability carrying the ticket issuer key
#: (``{suite, issuer_key_id, public_key, per_day}``).
MAILBOX_TICKETS_CAPABILITY: str = "mailbox_tickets"
#: The RFC 9577 ``TokenChallenge`` every mailbox ticket is issued for: no
#: redemption context (tickets are fetched ahead of use, in batches) and the
#: registration as the origin, so a ticket for this purpose is never
#: accepted for another one. The issuer itself is bound by the token's
#: ``token_key_id``.
MAILBOX_TICKET_CHALLENGE: bytes = token_challenge(
    b"gfs.social-home.io", origin_info=b"mailbox-register"
)
#: The signed ``action`` of a ticket request (domain separation from every
#: other ``{instance_id, ..., ts}``-signed GFS request).
TICKETS_ACTION: str = "mailbox_tickets"
#: Most token requests one ``POST /gfs/mailbox/tickets`` may carry.
MAX_TICKETS_PER_REQUEST: int = 64

#: WebSocket frame types of the claim handshake.
FRAME_MAILBOX_CHALLENGE: str = "mailbox_challenge"
FRAME_MAILBOX_CLAIM: str = "mailbox_claim"
FRAME_MAILBOX_CLAIMED: str = "mailbox_claimed"

#: A nonce / challenge: 22-64 url-safe base64 characters (≥ 128 bits).
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22,64}")
#: A field inside the signed bytes: printable, no whitespace (so ``\n``
#: framing can never shift), bounded.
_FIELD_RE = re.compile(r"[\x21-\x7e]{1,128}")


class UnsupportedMailboxSigSuite(ValueError):
    """A mailbox signature names a suite this build does not verify."""


class InvalidMailboxField(ValueError):
    """A field of the signed bytes is not a bounded printable token."""


@dataclass(slots=True, frozen=True)
class GfsMailbox:
    """One registered mailbox (GFS table ``gfs_mailboxes``, migration 0029).

    Deliberately nothing about who owns it: no instance id, no address."""

    mailbox_id: str
    sign_pk: bytes
    created_at: int
    last_claim_at: int | None


@dataclass(slots=True, frozen=True)
class TicketIssuerKey:
    """One mailbox-ticket issuer key (GFS table ``gfs_ticket_issuer_keys``,
    migration 0030): the RSA private key as PKCS#8 DER, its RFC 9578 key id
    and when it was made (it is accepted for redemption for two epochs)."""

    epoch: int
    key_id: bytes
    private_key_der: bytes
    created_at: int

    def __repr__(self) -> str:  # never print the private key
        return f"TicketIssuerKey(epoch={self.epoch}, created_at={self.created_at})"


@dataclass(slots=True, frozen=True)
class TicketKeyPin:
    """A household's pin of one GFS's ticket issuer key (key consistency,
    :class:`socialhome.federation.gfs_mailbox.TicketKeyGuard`): the key id
    it admitted, when it first saw it (Unix seconds), the epoch length then
    advertised, and the key before it (still redeemable for one epoch).
    Persisted with the household's GFS state once mailboxes are wired."""

    key_id: bytes
    first_seen: int
    epoch_days: int
    previous_key_id: bytes | None


def derive_mailbox_id(sign_pk: bytes) -> str:
    """The mailbox id of the Ed25519 public key *sign_pk*."""
    if len(sign_pk) != 32:
        raise ValueError("mailbox signing key must be 32 bytes")
    digest = hashlib.sha256(MAILBOX_ID_PREFIX + sign_pk).digest()
    return base64.b32encode(digest).decode("ascii").lower()[:MAILBOX_ID_CHARS]


def validate_mailbox_sig_suite(suite: object) -> None:
    """Raise :class:`UnsupportedMailboxSigSuite` unless *suite* is one this
    build verifies — never a default."""
    if not isinstance(suite, str) or suite not in SUPPORTED_MAILBOX_SIG_SUITES:
        raise UnsupportedMailboxSigSuite(
            f"unsupported mailbox sig_suite {suite!r}; "
            f"this build supports {sorted(SUPPORTED_MAILBOX_SIG_SUITES)!r}"
        )


def is_mailbox_nonce(value: object) -> bool:
    """Whether *value* is shaped like a register nonce / claim challenge."""
    return isinstance(value, str) and _NONCE_RE.fullmatch(value) is not None


def _signed(context: str, *fields: str) -> bytes:
    for field in fields:
        if not isinstance(field, str) or not _FIELD_RE.fullmatch(field):
            raise InvalidMailboxField("mailbox signed field is not a printable token")
    return "\n".join((context, *fields)).encode("ascii")


def register_signed_bytes(
    gfs_instance_id: str, mailbox_id: str, ts: int, nonce: str
) -> bytes:
    """What the mailbox key signs to register *mailbox_id* at the GFS
    *gfs_instance_id* (proof of possession)."""
    return _signed(
        MAILBOX_REGISTER_CONTEXT, gfs_instance_id, mailbox_id, str(int(ts)), nonce
    )


def claim_signed_bytes(gfs_instance_id: str, mailbox_id: str, challenge: str) -> bytes:
    """What the mailbox key signs to claim *mailbox_id* on a socket that was
    sent *challenge*."""
    return _signed(MAILBOX_CLAIM_CONTEXT, gfs_instance_id, mailbox_id, challenge)


__all__ = [
    "ENVELOPE_MAILBOX_CAPABILITY",
    "FRAME_MAILBOX_CHALLENGE",
    "FRAME_MAILBOX_CLAIM",
    "FRAME_MAILBOX_CLAIMED",
    "MAILBOX_CLAIM_CONTEXT",
    "MAILBOX_ID_CHARS",
    "MAILBOX_ID_PREFIX",
    "MAILBOX_REGISTER_CONTEXT",
    "MAILBOX_REGISTER_ROUTE",
    "MAILBOX_SIG_SUITE_ED25519",
    "MAILBOX_WS_ROUTE",
    "SUPPORTED_MAILBOX_SIG_SUITES",
    "GfsMailbox",
    "InvalidMailboxField",
    "UnsupportedMailboxSigSuite",
    "claim_signed_bytes",
    "derive_mailbox_id",
    "is_mailbox_nonce",
    "register_signed_bytes",
    "validate_mailbox_sig_suite",
]
