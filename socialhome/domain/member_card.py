"""A household's member key card (GFS redesign PR 3, v_89).

Every household that is a member of a space where some member is reached
only through a GFS signs ONE small card with its identity key and gives it
to the space's host once (``SPACE_MEMBER_CARD``, or inside the sealed
redeem / ACK). The host passes the cards on in its member directory
(``SPACE_MAILBOX_DIRECTORY``) to the space's writer-seat households, never
to followers. A card says, under the household's own signature:

* who it is — ``instance_id`` and the ``identity_pk`` it derives from
  (§4.1.2: an instance id IS the fingerprint of the identity key, so the
  card is self-certifying and the host cannot forge one);
* its static key-wrap key (``keywrap_pk`` + the identity-signed binding
  ``keywrap_sig``) — what a member seats it with (static-static session
  keys, no round-trip) and seals a host-hop ``SPACE_ROUTED`` to;
* where it can be reached through the GFS — ``mailbox``: its ONE mailbox at
  that GFS (``{mailbox_id, kem_pk, kem_suite, sign_pk, gfs, gen}``), shared
  by every space it reaches there, or ``null`` (it has none there: reach it
  through the host);
* ``proto_version`` and ``rev`` — a later card (higher ``rev``) replaces an
  earlier one.

Parsing (:func:`parse_card`) checks shape and suites — never a default
suite; :func:`socialhome.federation.member_card.verify_card` checks the
signature and the bindings.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from ..canonical_json import canonical_bytes
from .gfs_mailbox import MAILBOX_ID_CHARS, derive_mailbox_id

#: Domain separation of the card signature.
CARD_SIGNED_CONTEXT: bytes = b"sh-member-card:v1\n"

#: The card signature suite (the household identity key). Phase 2 adds
#: ``"ed25519+mldsa65"`` beside it.
CARD_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_CARD_SIG_SUITES: frozenset[str] = frozenset({CARD_SIG_SUITE_ED25519})

#: The KEM a frame for the card's mailbox is sealed with (the relay's ECIES
#: seal).
CARD_KEM_SUITE_X25519: str = "x25519"
SUPPORTED_CARD_KEM_SUITES: frozenset[str] = frozenset({CARD_KEM_SUITE_X25519})

#: Upper bound on a GFS base URL inside a card.
MAX_GFS_URL_CHARS: int = 512

#: Every signed field of a card, in wire order (``sig`` excluded).
CARD_FIELDS: tuple[str, ...] = (
    "instance_id",
    "identity_pk",
    "keywrap_pk",
    "keywrap_sig",
    "proto_version",
    "rev",
    "mailbox",
    "sig_suite",
)
#: Every field of a card's ``mailbox``.
MAILBOX_FIELDS: tuple[str, ...] = (
    "mailbox_id",
    "kem_pk",
    "kem_suite",
    "sign_pk",
    "gfs",
    "gen",
)

_INSTANCE_ID_RE = re.compile(r"[a-z2-7]{32}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")


class InvalidMemberCard(ValueError):
    """A member card is malformed, or does not verify."""


class UnsupportedMemberCardSuite(InvalidMemberCard):
    """A card names a ``sig_suite`` / ``kem_suite`` this build does not
    support — refused, never defaulted."""


@dataclass(slots=True, frozen=True)
class CardMailbox:
    """A household's mailbox at one GFS, as its card names it."""

    mailbox_id: str
    kem_pk: bytes
    kem_suite: str
    sign_pk: bytes
    gfs: str
    gen: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "mailbox_id": self.mailbox_id,
            "kem_pk": _b64(self.kem_pk),
            "kem_suite": self.kem_suite,
            "sign_pk": _b64(self.sign_pk),
            "gfs": self.gfs,
            "gen": self.gen,
        }


@dataclass(slots=True, frozen=True)
class MemberCard:
    """A household's signed member key card (see the module doc)."""

    instance_id: str
    identity_pk: str
    keywrap_pk: str
    keywrap_sig: str
    proto_version: int
    rev: int
    mailbox: CardMailbox | None
    sig_suite: str
    sig: str

    def signed_fields(self) -> dict[str, Any]:
        """The wire dict minus ``sig`` (what :func:`card_signed_bytes`
        covers)."""
        return {
            "instance_id": self.instance_id,
            "identity_pk": self.identity_pk,
            "keywrap_pk": self.keywrap_pk,
            "keywrap_sig": self.keywrap_sig,
            "proto_version": self.proto_version,
            "rev": self.rev,
            "mailbox": self.mailbox.to_wire() if self.mailbox is not None else None,
            "sig_suite": self.sig_suite,
        }

    def to_wire(self) -> dict[str, Any]:
        return {**self.signed_fields(), "sig": self.sig}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: object, *, length: int, field: str) -> bytes:
    if not isinstance(value, str) or not _B64_RE.fullmatch(value):
        raise InvalidMemberCard(f"{field} is not base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise InvalidMemberCard(f"{field} is not base64url") from exc
    if len(raw) != length:
        raise InvalidMemberCard(f"{field} must be {length} bytes")
    return raw


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidMemberCard(f"{field} must be a non-negative integer")
    return value


def card_signed_bytes(fields: dict[str, Any]) -> bytes:
    """The bytes the household identity key signs: the context tag and the
    canonical JSON of every field but ``sig``."""
    return CARD_SIGNED_CONTEXT + canonical_bytes({k: fields[k] for k in CARD_FIELDS})


def _parse_mailbox(raw: object, *, instance_id: str) -> CardMailbox | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(MAILBOX_FIELDS):
        raise InvalidMemberCard("mailbox fields do not match")
    kem_suite = raw.get("kem_suite")
    if kem_suite not in SUPPORTED_CARD_KEM_SUITES:
        raise UnsupportedMemberCardSuite(f"unsupported card kem_suite {kem_suite!r}")
    sign_pk = _unb64(raw.get("sign_pk"), length=32, field="sign_pk")
    mailbox_id = raw.get("mailbox_id")
    if (
        not isinstance(mailbox_id, str)
        or len(mailbox_id) != MAILBOX_ID_CHARS
        or mailbox_id != derive_mailbox_id(sign_pk)
    ):
        raise InvalidMemberCard("mailbox id is not the claim key's")
    if mailbox_id == instance_id:
        raise InvalidMemberCard("mailbox id equals the instance id")
    gfs = raw.get("gfs")
    if (
        not isinstance(gfs, str)
        or not 0 < len(gfs) <= MAX_GFS_URL_CHARS
        or not gfs.startswith(("https://", "http://"))
    ):
        raise InvalidMemberCard("invalid gfs url")
    return CardMailbox(
        mailbox_id=mailbox_id,
        kem_pk=_unb64(raw.get("kem_pk"), length=32, field="kem_pk"),
        kem_suite=str(kem_suite),
        sign_pk=sign_pk,
        gfs=gfs,
        gen=_int(raw.get("gen"), "gen"),
    )


def parse_card(raw: object) -> MemberCard:
    """Parse and shape-check a wire card. Raises :class:`InvalidMemberCard`
    — on an unknown ``sig_suite`` / ``kem_suite`` too (no fallback). The
    signature is NOT checked here."""
    if not isinstance(raw, dict):
        raise InvalidMemberCard("card is not an object")
    if set(raw) != {*CARD_FIELDS, "sig"}:
        raise InvalidMemberCard("card fields do not match")
    sig_suite = raw.get("sig_suite")
    if sig_suite not in SUPPORTED_CARD_SIG_SUITES:
        raise UnsupportedMemberCardSuite(f"unsupported card sig_suite {sig_suite!r}")
    instance_id = raw.get("instance_id")
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise InvalidMemberCard("invalid instance id")
    identity_pk = raw.get("identity_pk")
    if not isinstance(identity_pk, str) or not _HEX64_RE.fullmatch(identity_pk):
        raise InvalidMemberCard("invalid identity_pk")
    keywrap_pk = raw.get("keywrap_pk")
    if not isinstance(keywrap_pk, str) or not _HEX64_RE.fullmatch(keywrap_pk):
        raise InvalidMemberCard("invalid keywrap_pk")
    keywrap_sig = raw.get("keywrap_sig")
    if not isinstance(keywrap_sig, str) or not _B64_RE.fullmatch(keywrap_sig):
        raise InvalidMemberCard("invalid keywrap_sig")
    sig = raw.get("sig")
    if not isinstance(sig, str) or not _B64_RE.fullmatch(sig):
        raise InvalidMemberCard("invalid sig")
    return MemberCard(
        instance_id=instance_id,
        identity_pk=identity_pk,
        keywrap_pk=keywrap_pk,
        keywrap_sig=keywrap_sig,
        proto_version=_int(raw.get("proto_version"), "proto_version"),
        rev=_int(raw.get("rev"), "rev"),
        mailbox=_parse_mailbox(raw.get("mailbox"), instance_id=instance_id),
        sig_suite=str(sig_suite),
        sig=sig,
    )


__all__ = [
    "CARD_FIELDS",
    "CARD_KEM_SUITE_X25519",
    "CARD_SIGNED_CONTEXT",
    "CARD_SIG_SUITE_ED25519",
    "MAILBOX_FIELDS",
    "MAX_GFS_URL_CHARS",
    "SUPPORTED_CARD_KEM_SUITES",
    "SUPPORTED_CARD_SIG_SUITES",
    "CardMailbox",
    "InvalidMemberCard",
    "MemberCard",
    "UnsupportedMemberCardSuite",
    "card_signed_bytes",
    "parse_card",
]
