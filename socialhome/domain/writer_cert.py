"""Space writer certificates — the domain shape (v_49).

A :class:`WriterCert` says "household ``instance_pk`` may write (or only
comment) in space ``space_id`` during content epoch ``epoch``", signed by
the space AUTHORITY key (the seed only the owner and delegated admins
hold). It is issued once per writer household per epoch — never per item —
so a member's signed item can be authorized by any receiver that pins the
space key, without the host signing every post.

This module is pure: the dataclass, its wire codec and the canonical
signing body. Signing / verification live in :mod:`socialhome.writer_cert`
(which depends only on :mod:`socialhome.crypto`, so the content-blind GFS
process can import it later).

Wire shape (every field always present, never positional)::

    {"cert_suite": "ed25519", "space_id", "epoch": N,
     "instance_pk": <b64url Ed25519 household identity pubkey>,
     "scope": "write" | "comment", "issued_at": <unix seconds>,
     "cert_sig": <b64url>}

plus, since v_96, ``"reader": true`` on a follower's cert (scope
``comment`` only; absent — never ``false`` — on every other cert, so a
pre-v_96 cert keeps byte-identical signing bytes), and the v2 user binding
(``writer_user_ids``, ``users_sig``, ``users_sig_suite``). Any other key is
refused.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import WrongTypeError

#: Full write rights: posts, comments, reactions (owner / admin / moderator /
#: member seats).
WRITER_SCOPE_WRITE: str = "write"
#: Comment-only rights: follower (subscriber) seats, and only while the space
#: has ``allow_subscriber_comment`` on.
WRITER_SCOPE_COMMENT: str = "comment"
WRITER_SCOPES: frozenset[str] = frozenset({WRITER_SCOPE_WRITE, WRITER_SCOPE_COMMENT})

#: What each scope grants. ``write`` implies ``comment``.
_GRANTS: dict[str, frozenset[str]] = {
    WRITER_SCOPE_WRITE: frozenset({WRITER_SCOPE_WRITE, WRITER_SCOPE_COMMENT}),
    WRITER_SCOPE_COMMENT: frozenset({WRITER_SCOPE_COMMENT}),
}

#: Largest epoch a cert may carry (signed 64-bit SQLite INTEGER).
MAX_WRITER_CERT_EPOCH: int = 2**63 - 1

#: Bounds of the user binding (v2): at most this many users per household,
#: each id at most this long.
MAX_WRITER_USERS: int = 64
MAX_WRITER_USER_ID_CHARS: int = 128

#: Every key a wire cert may carry. Anything else is refused, so a field a
#: newer issuer signs can never be silently dropped by this parser.
_WIRE_KEYS: frozenset[str] = frozenset(
    {
        "cert_suite",
        "space_id",
        "epoch",
        "instance_pk",
        "scope",
        "issued_at",
        "cert_sig",
        "writer_user_ids",
        "users_sig",
        "users_sig_suite",
        "reader",
    }
)


def scope_permits(held: str, required: str) -> bool:
    """True iff a cert of scope ``held`` authorizes a ``required`` action."""
    return required in _GRANTS.get(held, frozenset())


def strongest_scope(scopes: Iterable[str]) -> str | None:
    """The strongest of ``scopes`` (``write`` beats ``comment``), or ``None``.

    A household with several seats in one space gets ONE cert covering its
    strongest seat."""
    held = set(scopes)
    if WRITER_SCOPE_WRITE in held:
        return WRITER_SCOPE_WRITE
    if WRITER_SCOPE_COMMENT in held:
        return WRITER_SCOPE_COMMENT
    return None


@dataclass(slots=True, frozen=True)
class WriterEntitlement:
    """What one household may do in a space, as its issuer derives it: the
    strongest ``scope`` and the users holding that right (the v2 user
    binding). ``reader`` (v_96) marks a ``comment`` entitlement that binds a
    follower: its cert is flag-gated at the receivers. A cert is re-issued at
    a new epoch whenever any of them narrows."""

    scope: str | None
    user_ids: frozenset[str] = frozenset()
    reader: bool = False


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(slots=True, frozen=True)
class WriterCert:
    """One authority-signed writer certificate (see the module docstring)."""

    cert_suite: str
    space_id: str
    epoch: int
    instance_pk: str
    scope: str
    issued_at: int
    cert_sig: str
    #: v2 user binding — the household's users holding ``scope``, and the
    #: space authority's SECOND signature over them (``users_sig``, suite
    #: ``users_sig_suite``). A separate signature, so a v1 verifier (which
    #: ignores these fields) still verifies ``cert_sig`` unchanged. Empty on
    #: a v1 cert.
    writer_user_ids: tuple[str, ...] = ()
    users_sig: str = ""
    users_sig_suite: str = ""
    #: v_96 — a follower's (subscriber seat's) cert: scope ``comment`` only,
    #: and receivers apply the space's follower switches to it
    #: (:func:`socialhome.domain.space_item.reader_cert_permits`). Signed by
    #: ``cert_sig``; absent from the wire and the signing body when false.
    reader: bool = False

    def signing_body(self) -> dict:
        """Every signed field — the cert minus ``cert_sig``. The suite tag is
        inside the signature so it can't be swapped in transit. ``reader``
        is in it only when set: a non-reader cert signs exactly the v_49
        bytes, and stripping ``reader`` from a reader cert breaks it."""
        body: dict = {
            "cert_suite": self.cert_suite,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "instance_pk": self.instance_pk,
            "scope": self.scope,
            "issued_at": self.issued_at,
        }
        if self.reader:
            body["reader"] = True
        return body

    def users_signing_body(self) -> dict:
        """What the v2 user-binding signature covers: the cert's own
        signature (so it binds exactly this cert), its routing fields, the
        sorted user ids and the binding's suite. ``reader`` is not repeated
        here: ``cert_sig`` already signs it, and this binding signs
        ``cert_sig``."""
        return {
            "cert_sig": self.cert_sig,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "instance_pk": self.instance_pk,
            "writer_user_ids": sorted(self.writer_user_ids),
            "users_sig_suite": self.users_sig_suite,
        }

    def to_wire(self) -> dict:
        wire = {**self.signing_body(), "cert_sig": self.cert_sig}
        if self.users_sig:
            wire["writer_user_ids"] = sorted(self.writer_user_ids)
            wire["users_sig"] = self.users_sig
            wire["users_sig_suite"] = self.users_sig_suite
        return wire

    @classmethod
    def from_wire(cls, raw: object) -> WriterCert:
        """Parse a wire dict; ``ValueError`` on any malformed / missing field.

        An unknown ``cert_suite`` STRING parses (the verifier rejects it with
        ``UnsupportedWriterCertSuite``); an unknown ``scope`` does not, nor
        an unknown key, nor a ``reader`` that is not exactly ``true`` or sits
        on a ``write`` cert."""
        if not isinstance(raw, dict):
            raise WrongTypeError("writer cert is not an object")
        unknown = set(raw) - _WIRE_KEYS
        if unknown:
            raise ValueError(f"writer cert: unknown fields {sorted(unknown)}")
        reader = "reader" in raw
        if reader and raw["reader"] is not True:
            raise ValueError("writer cert: bad reader")
        suite = raw.get("cert_suite")
        space_id = raw.get("space_id")
        epoch = raw.get("epoch")
        instance_pk = raw.get("instance_pk")
        scope = raw.get("scope")
        issued_at = raw.get("issued_at")
        sig = raw.get("cert_sig")
        if not isinstance(suite, str) or not suite:
            raise ValueError("writer cert: bad cert_suite")
        if not isinstance(space_id, str) or not space_id:
            raise ValueError("writer cert: bad space_id")
        if not _is_int(epoch) or not 0 <= epoch <= MAX_WRITER_CERT_EPOCH:  # type: ignore[operator]
            raise ValueError("writer cert: bad epoch")
        if not isinstance(instance_pk, str) or not instance_pk:
            raise ValueError("writer cert: bad instance_pk")
        if scope not in WRITER_SCOPES:
            raise ValueError("writer cert: bad scope")
        if not _is_int(issued_at):
            raise ValueError("writer cert: bad issued_at")
        if not isinstance(sig, str) or not sig:
            raise ValueError("writer cert: bad cert_sig")
        if reader and scope != WRITER_SCOPE_COMMENT:
            raise ValueError("writer cert: a reader cert is comment-only")
        users: tuple[str, ...] = ()
        users_sig = raw.get("users_sig", "")
        users_suite = raw.get("users_sig_suite", "")
        if users_sig:
            raw_users = raw.get("writer_user_ids")
            if (
                not isinstance(raw_users, list)
                or len(raw_users) > MAX_WRITER_USERS
                or not all(
                    isinstance(u, str) and 0 < len(u) <= MAX_WRITER_USER_ID_CHARS
                    for u in raw_users
                )
                or not isinstance(users_sig, str)
                or not isinstance(users_suite, str)
                or not users_suite
            ):
                raise ValueError("writer cert: bad user binding")
            users = tuple(sorted(set(raw_users)))
        return cls(
            cert_suite=suite,
            space_id=space_id,
            epoch=int(epoch),  # type: ignore[arg-type]
            instance_pk=instance_pk,
            scope=str(scope),
            issued_at=int(issued_at),  # type: ignore[arg-type]
            cert_sig=sig,
            writer_user_ids=users,
            users_sig=str(users_sig) if users else "",
            users_sig_suite=str(users_suite) if users else "",
            reader=reader,
        )
