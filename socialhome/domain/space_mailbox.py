"""Per-space GFS mailboxes of member households (GFS redesign PR 2, v_88).

A household that reaches a space ONLY through a GFS (a ``space_session``
seat — joined from an invite link or a GFS directory "Ask to join") owns one
anonymous mailbox there per (space, generation)
(:mod:`socialhome.domain.gfs_mailbox`). Its counterpart addresses envelopes of
that space to the mailbox id instead of the household's instance id, so the
GFS cannot link the household's spaces to each other or to its identity.

This module holds the two shapes both halves agree on:

* :class:`SpaceMailboxEntry` — the **directory entry** a household hands its
  counterpart inside the sealed redeem / ACK: ``{space, instance_id,
  mailbox_id, kem_pk, kem_suite, sign_pk, gfs, gen, proto_version,
  keywrap_pk, keywrap_sig, sig_suite, sig}``. ``sig`` is the household's
  identity key over :func:`entry_signed_bytes` (domain-separated canonical
  JSON of every other field), so the entry can later be passed on to other
  members (v_89) and still prove who minted it. Parsing
  (:func:`parse_entry`) checks shape and suites — never a default suite;
  the signature is checked by
  :func:`socialhome.federation.space_mailbox_entry.verify_entry`.
* :class:`SpaceInstanceMailbox` — what one ``space_instances`` row keeps in
  its ``mailbox`` column (migration 0144): the counterpart's entry
  (``peer``) and the generation of OUR mailbox for talking with it in that
  space (``own``). Only ``gen`` (and the GFS it lives on) is stored for our
  side: the keys come back from the household's mailbox root.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any

from ..canonical_json import canonical_bytes
from .gfs_mailbox import MAILBOX_ID_CHARS, derive_mailbox_id

#: Domain separation of the entry signature.
ENTRY_SIGNED_CONTEXT: bytes = b"sh-mbx-entry:v1\n"

#: The entry signature suite (the household identity key). Phase 2 adds
#: ``"ed25519+mldsa65"`` beside it.
ENTRY_SIG_SUITE_ED25519: str = "ed25519"
SUPPORTED_ENTRY_SIG_SUITES: frozenset[str] = frozenset({ENTRY_SIG_SUITE_ED25519})

#: The KEM a frame for the mailbox is sealed with (the relay's ECIES seal).
ENTRY_KEM_SUITE_X25519: str = "x25519"
SUPPORTED_ENTRY_KEM_SUITES: frozenset[str] = frozenset({ENTRY_KEM_SUITE_X25519})

#: Upper bound on a GFS base URL inside an entry.
MAX_GFS_URL_CHARS: int = 512

_INSTANCE_ID_RE = re.compile(r"[a-z2-7]{32}")
_SPACE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_B64_RE = re.compile(r"[A-Za-z0-9_-]{1,200}")

#: Every signed field of an entry, in wire order (``sig`` excluded).
ENTRY_FIELDS: tuple[str, ...] = (
    "space",
    "instance_id",
    "mailbox_id",
    "kem_pk",
    "kem_suite",
    "sign_pk",
    "gfs",
    "gen",
    "proto_version",
    "keywrap_pk",
    "keywrap_sig",
    "sig_suite",
)


class InvalidMailboxEntry(ValueError):
    """A directory entry is malformed (or names an unknown suite)."""


class UnsupportedMailboxEntrySuite(InvalidMailboxEntry):
    """An entry names a ``sig_suite`` / ``kem_suite`` this build does not
    support — refused, never defaulted."""


@dataclass(slots=True, frozen=True)
class SpaceMailboxEntry:
    """A household's signed mailbox for one space (see the module doc)."""

    space: str
    instance_id: str
    mailbox_id: str
    kem_pk: bytes
    kem_suite: str
    sign_pk: bytes
    gfs: str
    gen: int
    proto_version: int
    keywrap_pk: str
    keywrap_sig: str
    sig_suite: str
    sig: str

    def signed_fields(self) -> dict[str, Any]:
        """The wire dict minus ``sig`` (what :func:`entry_signed_bytes`
        covers)."""
        return {
            "space": self.space,
            "instance_id": self.instance_id,
            "mailbox_id": self.mailbox_id,
            "kem_pk": _b64(self.kem_pk),
            "kem_suite": self.kem_suite,
            "sign_pk": _b64(self.sign_pk),
            "gfs": self.gfs,
            "gen": self.gen,
            "proto_version": self.proto_version,
            "keywrap_pk": self.keywrap_pk,
            "keywrap_sig": self.keywrap_sig,
            "sig_suite": self.sig_suite,
        }

    def to_wire(self) -> dict[str, Any]:
        """The entry as it rides the sealed redeem / ACK."""
        return {**self.signed_fields(), "sig": self.sig}


@dataclass(slots=True, frozen=True)
class OwnMailbox:
    """Our side of one (space, GFS) mailbox: its generation and the GFS id
    we pinned when we minted it (kept, even if the GFS later changes id —
    the derivation depends on it)."""

    gen: int
    gfs_instance_id: str
    gfs_url: str


@dataclass(slots=True, frozen=True)
class SpaceInstanceMailbox:
    """One ``space_instances.mailbox`` value (migration 0144)."""

    space_id: str
    instance_id: str
    peer: SpaceMailboxEntry | None
    own: OwnMailbox | None


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: object, *, length: int, field: str) -> bytes:
    if not isinstance(value, str) or not _B64_RE.fullmatch(value):
        raise InvalidMailboxEntry(f"{field} is not base64url")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as exc:
        raise InvalidMailboxEntry(f"{field} is not base64url") from exc
    if len(raw) != length:
        raise InvalidMailboxEntry(f"{field} must be {length} bytes")
    return raw


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidMailboxEntry(f"{field} must be a non-negative integer")
    return value


def entry_signed_bytes(fields: dict[str, Any]) -> bytes:
    """The bytes the household identity key signs: the context tag and the
    canonical JSON of every field but ``sig``."""
    return ENTRY_SIGNED_CONTEXT + canonical_bytes({k: fields[k] for k in ENTRY_FIELDS})


def parse_entry(raw: object) -> SpaceMailboxEntry:
    """Parse and shape-check a wire entry. Raises
    :class:`InvalidMailboxEntry` — on an unknown ``sig_suite`` /
    ``kem_suite`` too (no fallback). The signature is NOT checked here."""
    if not isinstance(raw, dict):
        raise InvalidMailboxEntry("entry is not an object")
    if set(raw) != {*ENTRY_FIELDS, "sig"}:
        raise InvalidMailboxEntry("entry fields do not match")
    sig_suite = raw.get("sig_suite")
    if sig_suite not in SUPPORTED_ENTRY_SIG_SUITES:
        raise UnsupportedMailboxEntrySuite(f"unsupported entry sig_suite {sig_suite!r}")
    kem_suite = raw.get("kem_suite")
    if kem_suite not in SUPPORTED_ENTRY_KEM_SUITES:
        raise UnsupportedMailboxEntrySuite(f"unsupported entry kem_suite {kem_suite!r}")
    space = raw.get("space")
    if not isinstance(space, str) or not _SPACE_ID_RE.fullmatch(space):
        raise InvalidMailboxEntry("invalid space id")
    instance_id = raw.get("instance_id")
    if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
        raise InvalidMailboxEntry("invalid instance id")
    sign_pk = _unb64(raw.get("sign_pk"), length=32, field="sign_pk")
    mailbox_id = raw.get("mailbox_id")
    if (
        not isinstance(mailbox_id, str)
        or len(mailbox_id) != MAILBOX_ID_CHARS
        or mailbox_id != derive_mailbox_id(sign_pk)
    ):
        raise InvalidMailboxEntry("mailbox id is not the claim key's")
    if mailbox_id == instance_id:
        raise InvalidMailboxEntry("mailbox id equals the instance id")
    kem_pk = _unb64(raw.get("kem_pk"), length=32, field="kem_pk")
    gfs = raw.get("gfs")
    if (
        not isinstance(gfs, str)
        or not 0 < len(gfs) <= MAX_GFS_URL_CHARS
        or not gfs.startswith(("https://", "http://"))
    ):
        raise InvalidMailboxEntry("invalid gfs url")
    keywrap_pk = raw.get("keywrap_pk")
    if not isinstance(keywrap_pk, str) or not _HEX64_RE.fullmatch(keywrap_pk):
        raise InvalidMailboxEntry("invalid keywrap_pk")
    keywrap_sig = raw.get("keywrap_sig")
    if not isinstance(keywrap_sig, str) or not _B64_RE.fullmatch(keywrap_sig):
        raise InvalidMailboxEntry("invalid keywrap_sig")
    sig = raw.get("sig")
    if not isinstance(sig, str) or not _B64_RE.fullmatch(sig):
        raise InvalidMailboxEntry("invalid sig")
    return SpaceMailboxEntry(
        space=space,
        instance_id=instance_id,
        mailbox_id=mailbox_id,
        kem_pk=kem_pk,
        kem_suite=str(kem_suite),
        sign_pk=sign_pk,
        gfs=gfs,
        gen=_int(raw.get("gen"), "gen"),
        proto_version=_int(raw.get("proto_version"), "proto_version"),
        keywrap_pk=keywrap_pk,
        keywrap_sig=keywrap_sig,
        sig_suite=str(sig_suite),
        sig=sig,
    )


def parse_own(raw: object) -> OwnMailbox | None:
    """Parse the stored ``own`` half (``None`` when absent or malformed —
    a row we cannot read has no mailbox of ours)."""
    if not isinstance(raw, dict):
        return None
    gen = raw.get("gen")
    gfs_id = raw.get("gfs_id")
    gfs_url = raw.get("gfs")
    if (
        isinstance(gen, bool)
        or not isinstance(gen, int)
        or gen < 0
        or not isinstance(gfs_id, str)
        or not gfs_id
        or not isinstance(gfs_url, str)
        or not gfs_url
    ):
        return None
    return OwnMailbox(gen=gen, gfs_instance_id=gfs_id, gfs_url=gfs_url)


def own_to_wire(own: OwnMailbox) -> dict[str, Any]:
    return {"gen": own.gen, "gfs_id": own.gfs_instance_id, "gfs": own.gfs_url}


__all__ = [
    "ENTRY_FIELDS",
    "ENTRY_KEM_SUITE_X25519",
    "ENTRY_SIGNED_CONTEXT",
    "ENTRY_SIG_SUITE_ED25519",
    "MAX_GFS_URL_CHARS",
    "SUPPORTED_ENTRY_KEM_SUITES",
    "SUPPORTED_ENTRY_SIG_SUITES",
    "InvalidMailboxEntry",
    "OwnMailbox",
    "SpaceInstanceMailbox",
    "SpaceMailboxEntry",
    "UnsupportedMailboxEntrySuite",
    "entry_signed_bytes",
    "own_to_wire",
    "parse_entry",
    "parse_own",
]
