"""The roster digest a member household's ``SPACE_SYNC_BEGIN`` carries.

The host repairs a member household's roster mirror only when it is wrong:
the member hashes the live seats it mirrors for a space, the host hashes
the same seats from its own roster, and only a mismatch (or a writer-cert
epoch the member lacks) gets a ``SPACE_ROSTER_SNAPSHOT`` — in reply to that
BEGIN. When live gossip worked, nothing is sent.

What is hashed is exactly what both sides can compute: every LIVE seat of
the space outside the member household itself, as ``(instance_id, user_id,
role)`` with the role mirrored the way every receiver stores it
(:func:`~socialhome.domain.space.mirrorable_remote_role`: the host's
``owner`` is mirrored as ``member``). Names, keys, versions and removals are
left out: removals show up as a live seat the host no longer has, and the
rest heals on its own paths. Sorted, one line per seat, SHA-256.

Wire shape (``roster_digest`` on the BEGIN): ``{"suite": ..., "digest":
<64 hex>, "cert_epoch": <int|null>}``. A receiver refuses an unknown suite.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from .space import mirrorable_remote_role

#: The one digest suite: SHA-256 over the sorted seat lines.
ROSTER_DIGEST_SUITE_SHA256: str = "rd1-sha256"
SUPPORTED_ROSTER_DIGEST_SUITES: frozenset[str] = frozenset({ROSTER_DIGEST_SUITE_SHA256})

_HEX64 = re.compile(r"[0-9a-f]{64}")


class UnsupportedRosterDigestSuite(ValueError):
    """A ``roster_digest`` names a suite this household does not know."""


@dataclass(slots=True, frozen=True)
class RosterDigest:
    """A parsed ``roster_digest``: the seats' hash and the writer-cert
    epoch the member holds (``None``: none)."""

    digest: str
    cert_epoch: int | None


def roster_digest(seats: Iterable[tuple[str, str, str]]) -> str:
    """The digest of *seats* — ``(instance_id, user_id, role)`` of every live
    seat outside the member household itself. Order and duplicates do not
    matter; the role is mirrored first."""
    lines = sorted(
        {
            f"{instance_id}\x1f{user_id}\x1f{mirrorable_remote_role(role)}"
            for instance_id, user_id, role in seats
        }
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def roster_digest_wire(digest: str, cert_epoch: int | None) -> dict:
    """The ``roster_digest`` field of a BEGIN."""
    return {
        "suite": ROSTER_DIGEST_SUITE_SHA256,
        "digest": digest,
        "cert_epoch": cert_epoch,
    }


def parse_roster_digest(raw: object) -> RosterDigest | None:
    """The ``roster_digest`` of a BEGIN, or ``None`` when absent or
    malformed. Raises :class:`UnsupportedRosterDigestSuite` for an unknown
    suite (never a default)."""
    if not isinstance(raw, dict):
        return None
    suite = raw.get("suite")
    if suite not in SUPPORTED_ROSTER_DIGEST_SUITES:
        raise UnsupportedRosterDigestSuite(str(suite))
    digest = raw.get("digest")
    cert_epoch = raw.get("cert_epoch")
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
        return None
    if cert_epoch is not None and (
        not isinstance(cert_epoch, int) or isinstance(cert_epoch, bool)
    ):
        return None
    return RosterDigest(digest=digest, cert_epoch=cert_epoch)
