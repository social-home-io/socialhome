"""A connection server's subscriber seat of this household (``gfs_space_seats``)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class GfsSpaceSeat:
    """Server ``gfs_instance_id`` holds a subscriber seat of ours for
    ``space_id``. The seat was taken over local connection
    ``gfs_connection_id`` that pinned ``gfs_public_key`` at ``gfs_inbox_url``;
    a connection is "this seat's server" only when all three match (an
    impostor can claim an id, and serve a key it copied from ``/gfs/info``,
    but not answer at the real server's URL). ``None`` binding fields
    (never written by current code) match no connection — fail closed."""

    space_id: str
    gfs_instance_id: str
    gfs_connection_id: str | None = None
    gfs_public_key: str | None = None
    gfs_inbox_url: str | None = None
    #: The server was unpaired: the row is kept (a re-pair re-takes or
    #: releases it) and its unsubscribe was sent in the background.
    detached: bool = False
    #: When it was detached (UTC ``YYYY-MM-DD HH:MM:SS``), ``None`` until
    #: the clock looked sane.
    detached_at: str | None = None
    #: The server confirmed the unsubscribe; until then a detached row is
    #: an unsubscribe-only tombstone.
    released: bool = False
    #: First sweep that found a released, detached row past its age —
    #: a second sweep a day later drops it.
    expiry_seen_at: str | None = None
    #: The "server address changed — re-follow" warning was logged.
    refollow_warned: bool = False
