"""The roster digest a member household's ``SPACE_SYNC_BEGIN`` carries.

The host repairs a member household's roster mirror only when it is wrong:
the member hashes the live seats it mirrors for a space, the host hashes
the same seats from its own roster, and only a mismatch (or a writer-cert
epoch the member lacks) gets a ``SPACE_ROSTER_SNAPSHOT`` — in reply to that
BEGIN, at most once per cooldown. When live gossip worked, nothing is sent.

What is hashed is exactly what both sides can compute: every LIVE seat of
the space outside the member household itself, as ``(instance_id, user_id,
role)`` with the role mirrored the way every receiver stores it
(:func:`~socialhome.domain.space.mirrorable_remote_role`: the host's
``owner`` is mirrored as ``member``), sorted, without duplicates, as one
canonical JSON list (:mod:`socialhome.canonical_json`), SHA-256. Names,
keys and versions are left out.

The repair snapshot carries the host's live seats and its recent
tombstones (all of them on a consecutive repair), each merged by the usual
per-entry rule (strictly newer wins). Only the host's OWN seats are
inferred from absence (a complete snapshot names all of them); another
household's seat it does not name stays. A mismatch it cannot heal (a seat
only the member knows, a host restored from an older backup) stays, its
repairs backing off to one a day.

Wire shape (``roster_digest`` on the BEGIN): ``{"suite": ..., "digest":
<64 hex>, "cert_epoch": <int|null>}`` — the writer-cert epoch the member
holds (0 … :data:`~socialhome.domain.writer_cert.MAX_WRITER_CERT_EPOCH`) —
plus, only when true, ``"cert_wanted": true`` (one of its people wants to
comment or react and it holds no fresh cert: a follower household's
on-demand ask) and ``"key_wanted": true`` (it wants to publish over a GFS and
holds no writer group key for the current epoch). A host that does not know
them ignores them.
A receiver refuses an unknown suite — no repair, never a fallback; a suite
that is not a string, or any other malformed field, is malformed.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..canonical_json import canonical_bytes
from .space import mirrorable_remote_role
from .writer_cert import MAX_WRITER_CERT_EPOCH

#: The one digest suite: SHA-256 over the canonical JSON seat list.
ROSTER_DIGEST_SUITE_SHA256: str = "rd1-sha256"
SUPPORTED_ROSTER_DIGEST_SUITES: frozenset[str] = frozenset({ROSTER_DIGEST_SUITE_SHA256})

_HEX64 = re.compile(r"[0-9a-f]{64}")

#: A seat as hashed: ``(instance_id, user_id, mirrored role)``.
RosterSeat = tuple[str, str, str]


class UnsupportedRosterDigestSuite(ValueError):
    """A ``roster_digest`` names a suite this household does not know."""


@dataclass(slots=True, frozen=True)
class RosterDigest:
    """A parsed ``roster_digest``: the seats' hash and the writer-cert
    epoch the member holds (``None``: none)."""

    digest: str
    cert_epoch: int | None
    #: The household asks for its writer cert / writer group key now.
    cert_wanted: bool = False
    key_wanted: bool = False


def roster_seats(seats: Iterable[tuple[str, str, str]]) -> tuple[RosterSeat, ...]:
    """*seats* as hashed: the role mirrored, sorted, without duplicates."""
    return tuple(
        sorted(
            {
                (instance_id, user_id, mirrorable_remote_role(role))
                for instance_id, user_id, role in seats
            }
        )
    )


def digest_of_roster_seats(
    seats: tuple[RosterSeat, ...], *, exclude_instance: str | None = None
) -> str:
    """The digest of already-normalised *seats* (:func:`roster_seats`),
    leaving out household *exclude_instance*."""
    rows = [list(seat) for seat in seats if seat[0] != exclude_instance]
    return hashlib.sha256(canonical_bytes(rows)).hexdigest()


def roster_digest(seats: Iterable[tuple[str, str, str]]) -> str:
    """The digest of *seats* — ``(instance_id, user_id, role)`` of every live
    seat outside the member household itself. Order and duplicates do not
    matter; the role is mirrored first."""
    return digest_of_roster_seats(roster_seats(seats))


def roster_digest_wire(
    digest: str,
    cert_epoch: int | None,
    *,
    cert_wanted: bool = False,
    key_wanted: bool = False,
) -> dict:
    """The ``roster_digest`` field of a BEGIN."""
    wire: dict = {
        "suite": ROSTER_DIGEST_SUITE_SHA256,
        "digest": digest,
        "cert_epoch": cert_epoch,
    }
    if cert_wanted:
        wire["cert_wanted"] = True
    if key_wanted:
        wire["key_wanted"] = True
    return wire


def parse_roster_digest(raw: object) -> RosterDigest | None:
    """The ``roster_digest`` of a BEGIN, or ``None`` when absent or
    malformed (a suite that is not a string included). Raises
    :class:`UnsupportedRosterDigestSuite` for an unknown suite (never a
    default)."""
    if not isinstance(raw, dict):
        return None
    suite = raw.get("suite")
    if not isinstance(suite, str):
        return None
    if suite not in SUPPORTED_ROSTER_DIGEST_SUITES:
        raise UnsupportedRosterDigestSuite(suite)
    digest = raw.get("digest")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return None
    cert_epoch = raw.get("cert_epoch")
    if cert_epoch is not None and (
        not isinstance(cert_epoch, int)
        or isinstance(cert_epoch, bool)
        or not 0 <= cert_epoch <= MAX_WRITER_CERT_EPOCH
    ):
        return None
    wanted = [raw.get(name, False) for name in ("cert_wanted", "key_wanted")]
    if not all(isinstance(flag, bool) for flag in wanted):
        return None
    return RosterDigest(
        digest=digest,
        cert_epoch=cert_epoch,
        cert_wanted=wanted[0],
        key_wanted=wanted[1],
    )
