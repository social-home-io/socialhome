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
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

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

    def signing_body(self) -> dict:
        """Every signed field — the cert minus ``cert_sig``. The suite tag is
        inside the signature so it can't be swapped in transit."""
        return {
            "cert_suite": self.cert_suite,
            "space_id": self.space_id,
            "epoch": self.epoch,
            "instance_pk": self.instance_pk,
            "scope": self.scope,
            "issued_at": self.issued_at,
        }

    def to_wire(self) -> dict:
        return {**self.signing_body(), "cert_sig": self.cert_sig}

    @classmethod
    def from_wire(cls, raw: object) -> "WriterCert":
        """Parse a wire dict; ``ValueError`` on any malformed / missing field.

        An unknown ``cert_suite`` STRING parses (the verifier rejects it with
        ``UnsupportedWriterCertSuite``); an unknown ``scope`` does not."""
        if not isinstance(raw, dict):
            raise ValueError("writer cert is not an object")
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
        return cls(
            cert_suite=suite,
            space_id=space_id,
            epoch=int(epoch),  # type: ignore[arg-type]
            instance_pk=instance_pk,
            scope=str(scope),
            issued_at=int(issued_at),  # type: ignore[arg-type]
            cert_sig=sig,
        )
