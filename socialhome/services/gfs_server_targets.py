"""Where a space's items go: every GFS on the owner's signed list (v_57).

A global space is listed on the GFSs its owner chose, and the owner hands
every member household that list (``spaces.gfs_servers``, signed with the
owner household's key, see :mod:`socialhome.domain.gfs_server`). A follower
who uses only one of those servers must still get every item, even while the
host is offline — so whoever publishes (a member, a delegated admin relaying,
the owner) sends to EVERY listed server, not only to the ones its own
household is connected to.

:func:`resolve_server_targets` turns the list into send targets:

* an entry our household holds an ACTIVE connection to — the same pinned
  key, preferably under an id that connection knows (its current id or one
  its server signed that it ``replaces``); the key alone also matches, since
  the owner may already use an id the server adopted that we have not read
  yet — is that connection, ``registered=True``. Identified and anonymous
  requests may go there, exactly as before;
* every other entry becomes an in-memory LISTING target:
  ``registered=False``, id :data:`LISTING_TARGET_PREFIX` + the server's id,
  the key and URL the owner signed. A listing target is never stored and
  never receives anything that names this household: only the anonymous
  member publish, the seed-only epoch notice and the identity-free relay,
  over a cookie-less session pinned to a vetted public address
  (:func:`socialhome.outbound_fetch.open_pinned_session`). The capability
  block on its ``/gfs/info`` is verified against the key from the signed
  list.

An own connection whose id matches an entry but whose pinned key does not is
NOT used for that entry (WARNING): the owner's key wins, so a request meant
for the listed server is never sent somewhere it cannot be verified.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from ..domain.federation import GfsConnection
from ..domain.gfs_server import GfsServer
from ..outbound_fetch import OutboundFetchRefused
from .gfs_publish_retry import PublishOutcome

log = logging.getLogger(__name__)

#: Id prefix of an in-memory listing target (never a ``gfs_connections.id``,
#: which is a UUID).
LISTING_TARGET_PREFIX: str = "listing:"


def listing_target_id(gfs_instance_id: str) -> str:
    """The in-memory id of the listing target for *gfs_instance_id*."""
    return f"{LISTING_TARGET_PREFIX}{gfs_instance_id}"


def is_listing_target_id(conn_id: str) -> bool:
    return conn_id.startswith(LISTING_TARGET_PREFIX)


def listing_target(server: GfsServer) -> GfsConnection:
    """An unregistered send target for one entry of the owner's list."""
    return GfsConnection(
        id=listing_target_id(server.gfs_instance_id),
        gfs_instance_id=server.gfs_instance_id,
        display_name=server.display_name,
        public_key=server.public_key,
        inbox_url=server.inbox_url,
        status="active",
        paired_at="",
        registered=False,
    )


def resolve_server_targets(
    servers: Iterable[GfsServer],
    connections: Iterable[GfsConnection],
    *,
    known_ids: Callable[[GfsConnection], frozenset[str]],
    warned: set[tuple[str, str]] | None = None,
) -> list[GfsConnection]:
    """One target per listed server, in list order, each server once.

    *connections* are this household's connections (only ``active`` ones
    are used); *known_ids* gives a connection's current id plus the former
    ids its server signed that it ``replaces``
    (:meth:`GfsConnectionService.known_instance_ids`). *warned* de-duplicates
    the key-mismatch WARNING across calls."""
    active = [c for c in connections if c.status == "active" and c.registered]
    out: list[GfsConnection] = []
    seen: set[str] = set()
    for server in servers:
        key = server.public_key.lower()
        target: GfsConnection | None = next(
            (
                c
                for c in active
                if c.public_key.lower() == key
                and server.gfs_instance_id in known_ids(c)
            ),
            None,
        )
        if target is None:
            # The key is the trust anchor, the id only a label: the owner may
            # already name the server by a new id its server adopted that we
            # have not read yet (a shared cluster id). Same key, same server.
            target = next((c for c in active if c.public_key.lower() == key), None)
        for conn in active if target is None else ():
            if server.gfs_instance_id not in known_ids(conn):
                continue
            pair = (conn.id, server.gfs_instance_id)
            if warned is None or pair not in warned:
                if warned is not None:
                    warned.add(pair)
                log.warning(
                    "GFS %r (%s): the space owner lists this server under a key "
                    "other than the one this household pinned — using the "
                    "owner's entry for it (anonymous requests only)",
                    conn.display_name,
                    conn.inbox_url,
                )
        if target is None:
            target = listing_target(server)
        if target.id in seen:
            continue
        seen.add(target.id)
        out.append(target)
    return out


def refusal_outcome(exc: OutboundFetchRefused) -> PublishOutcome:
    """How a send treats a listing target its address check refused: a name
    that does not resolve right now is retried; a private / malformed
    address never will be."""
    if exc.reason in {"dns", "timeout", "network"}:
        return PublishOutcome.transient()
    return PublishOutcome.permanent()
