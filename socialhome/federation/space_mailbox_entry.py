"""Sign and verify a household's per-space mailbox directory entry (v_88).

:class:`~socialhome.domain.space_mailbox.SpaceMailboxEntry` is the shape;
this module adds the crypto: :func:`build_entry` signs a fresh entry with
the household identity key, :func:`verify_entry` checks one a counterpart
sent — the signature under the identity key the receiver already trusts for
that household (the sealed redeem's own key, or the seated row's), the
instance id derived from that key, the key-wrap key bound to it, and the
space the entry is for. Every refusal raises
:class:`~socialhome.domain.space_mailbox.InvalidMailboxEntry`.
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
from ..domain.space_mailbox import (
    ENTRY_KEM_SUITE_X25519,
    ENTRY_SIG_SUITE_ED25519,
    InvalidMailboxEntry,
    SpaceMailboxEntry,
    entry_signed_bytes,
    parse_entry,
)
from .gfs_mailbox import MailboxKeys
from .keywrap_seal import verify_keywrap_binding


def build_entry(
    keys: MailboxKeys,
    *,
    instance_id: str,
    identity_seed: bytes,
    gfs_url: str,
    proto_version: int,
    keywrap_pk: bytes,
    keywrap_sig: str,
) -> SpaceMailboxEntry:
    """The signed entry for *keys* (our mailbox of ``keys.space_id`` at the
    GFS whose base URL is *gfs_url*)."""
    fields: dict[str, Any] = {
        "space": keys.space_id,
        "instance_id": instance_id,
        "mailbox_id": keys.mailbox_id,
        "kem_pk": b64url_encode(keys.kem_public),
        "kem_suite": ENTRY_KEM_SUITE_X25519,
        "sign_pk": b64url_encode(keys.sign_public),
        "gfs": gfs_url,
        "gen": keys.gen,
        "proto_version": proto_version,
        "keywrap_pk": keywrap_pk.hex(),
        "keywrap_sig": keywrap_sig,
        "sig_suite": ENTRY_SIG_SUITE_ED25519,
    }
    sig = b64url_encode(sign_ed25519(identity_seed, entry_signed_bytes(fields)))
    return parse_entry({**fields, "sig": sig})


def verify_entry(
    raw: object,
    *,
    identity_pk_hex: str,
    expected_instance_id: str,
    expected_space_id: str,
) -> SpaceMailboxEntry:
    """Parse *raw* and check it: minted by *expected_instance_id* (whose
    identity key is *identity_pk_hex*, already trusted by the caller), for
    *expected_space_id*, signed under that key, with a key-wrap key bound to
    it. Raises :class:`InvalidMailboxEntry`."""
    entry = parse_entry(raw)
    try:
        identity_pub = bytes.fromhex(identity_pk_hex)
    except ValueError as exc:
        raise InvalidMailboxEntry("invalid identity key") from exc
    if len(identity_pub) != 32 or derive_instance_id(identity_pub) != (
        expected_instance_id
    ):
        raise InvalidMailboxEntry("identity key does not match the household")
    if entry.instance_id != expected_instance_id:
        raise InvalidMailboxEntry("entry names another household")
    if entry.space != expected_space_id:
        raise InvalidMailboxEntry("entry is for another space")
    try:
        sig = b64url_decode(entry.sig)
    except ValueError as exc:
        raise InvalidMailboxEntry("invalid sig") from exc
    if not verify_ed25519(identity_pub, entry_signed_bytes(entry.signed_fields()), sig):
        raise InvalidMailboxEntry("entry signature does not verify")
    if not verify_keywrap_binding(
        instance_id=expected_instance_id,
        identity_pub=identity_pub,
        keywrap_pub=bytes.fromhex(entry.keywrap_pk),
        keywrap_sig=entry.keywrap_sig,
    ):
        raise InvalidMailboxEntry("key-wrap key is not bound to the household")
    return entry


__all__ = ["build_entry", "verify_entry"]
