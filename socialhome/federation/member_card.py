"""Sign and verify a household's member key card (v_89).

:class:`~socialhome.domain.member_card.MemberCard` is the shape; this module
adds the crypto. :func:`build_card` signs our card with the household
identity key; :func:`verify_card` checks one we were handed — by the
household itself (a sealed redeem / ACK, ``SPACE_MEMBER_CARD``) or by a
space host passing it on in its directory. The card is self-certifying, so
the check needs no key the receiver already holds: the instance id must
derive from the card's identity key, the signature must verify under it,
and the key-wrap key must be bound to it. A host can drop or withhold a
card, never forge or alter one. Every refusal raises
:class:`~socialhome.domain.member_card.InvalidMemberCard`.
"""

from __future__ import annotations

from typing import Any

from ..crypto import (
    b64url_decode,
    b64url_encode,
    derive_instance_id,
    sign_ed25519,
    verify_ed25519,
)
from ..domain.member_card import (
    CARD_KEM_SUITE_X25519,
    CARD_SIG_SUITE_ED25519,
    InvalidMemberCard,
    MemberCard,
    card_signed_bytes,
    parse_card,
)
from .gfs_mailbox import MailboxKeys
from .keywrap_seal import verify_keywrap_binding


def build_card(
    *,
    instance_id: str,
    identity_seed: bytes,
    identity_pk: bytes,
    keywrap_pk: bytes,
    keywrap_sig: str,
    proto_version: int,
    rev: int,
    mailbox: tuple[MailboxKeys, str] | None,
) -> MemberCard:
    """Our signed card. *mailbox* is ``(keys, gfs_url)`` of our mailbox at
    the GFS the card is for, or ``None`` (no mailbox there)."""
    mailbox_wire: dict[str, Any] | None = None
    if mailbox is not None:
        keys, gfs_url = mailbox
        mailbox_wire = {
            "mailbox_id": keys.mailbox_id,
            "kem_pk": b64url_encode(keys.kem_public),
            "kem_suite": CARD_KEM_SUITE_X25519,
            "sign_pk": b64url_encode(keys.sign_public),
            "gfs": gfs_url,
            "gen": keys.gen,
        }
    fields: dict[str, Any] = {
        "instance_id": instance_id,
        "identity_pk": identity_pk.hex(),
        "keywrap_pk": keywrap_pk.hex(),
        "keywrap_sig": keywrap_sig,
        "proto_version": proto_version,
        "rev": rev,
        "mailbox": mailbox_wire,
        "sig_suite": CARD_SIG_SUITE_ED25519,
    }
    sig = b64url_encode(sign_ed25519(identity_seed, card_signed_bytes(fields)))
    return parse_card({**fields, "sig": sig})


def verify_card(raw: object, *, expected_instance_id: str | None = None) -> MemberCard:
    """Parse *raw* and check it is a card its household signed: the
    instance id derives from the identity key, the signature verifies under
    it, and the key-wrap key is bound to it. With *expected_instance_id*,
    the card must also be that household's (a card handed over by its own
    household). Raises :class:`InvalidMemberCard`."""
    card = parse_card(raw)
    identity_pub = bytes.fromhex(card.identity_pk)
    if derive_instance_id(identity_pub) != card.instance_id:
        raise InvalidMemberCard("identity key does not match the instance id")
    if expected_instance_id is not None and card.instance_id != expected_instance_id:
        raise InvalidMemberCard("card names another household")
    try:
        sig = b64url_decode(card.sig)
    except ValueError as exc:
        raise InvalidMemberCard("invalid sig") from exc
    if not verify_ed25519(identity_pub, card_signed_bytes(card.signed_fields()), sig):
        raise InvalidMemberCard("card signature does not verify")
    if not verify_keywrap_binding(
        instance_id=card.instance_id,
        identity_pub=identity_pub,
        keywrap_pub=bytes.fromhex(card.keywrap_pk),
        keywrap_sig=card.keywrap_sig,
    ):
        raise InvalidMemberCard("key-wrap key is not bound to the household")
    return card


__all__ = ["build_card", "verify_card"]
