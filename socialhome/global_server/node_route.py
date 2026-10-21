"""Session-sticky routing for the public-highlight signalling (spec §24.10).

A public-highlight viewing session lives in ONE GFS node's memory: the
signalling state (:class:`~.rtc_transport.GfsRtcSession`) and the relay
byte pipe (:class:`~.relay_bridge.RelayBridge`). Production runs several
nodes behind a round-robin balancer, so the viewer's polls and ICE posts and
the author's answer, ICE and relay upload used to land on whichever node the
balancer picked — three in four on four nodes, and a session that is not in
that node's memory is a 404.

The fix is sticky routing, not shared state. The node that creates a session
names itself in the id it mints: ``"<route tag>.<random>"``, where the route
tag is this node's ``[cluster] route_tag`` (``GFS_CLUSTER_ROUTE_TAG``). The
balancer in front reads the tag back — from the path
(``/gfs/highlight_rtc/session/<id>``, ``/gfs/highlight_rtc/relay-stream/<id>``)
or from the JSON body's ``session_id`` (``/gfs/highlight_rtc/answer``,
``/gfs/highlight_rtc/ice/viewer``, ``/gfs/highlight_rtc/ice/author``) — and
sends the request to that node. No client changes: the viewer and the author
treat the id as opaque, as they always did. No state is shared and no media
byte crosses nodes. ``docs/operations/gfs-cluster.md`` has the HAProxy rules.

The tag is an operator-chosen label for a node (production: the Nomad
allocation id). It names a process, not a person or a household, and it
travels only inside ids this node already handed out.

A request that names another node's tag reached the wrong node — the
balancer does not route by tag. It is answered like any unknown session
(404: the viewer falls back to the relay, the author's post is dropped), and
:class:`MisrouteWarning` logs it once a minute so the operator can see why.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

#: What a route tag may look like: what HAProxy can put in a backend name and
#: a URL path segment carries unescaped. A Nomad allocation id (a UUID) fits.
ROUTE_TAG_RE = re.compile(r"[A-Za-z0-9-]{1,64}")

#: Separates the route tag from the random part of a node-scoped id. Never
#: part of a tag, so the balancer can split on the first one.
ROUTE_TAG_SEPARATOR = "."

#: Random bytes in a node-scoped id (hex-encoded): the id is a capability for
#: an anonymous viewer's session, so it must not be guessable.
ID_RANDOM_BYTES = 16

#: Minimum seconds between two "misrouted" WARNINGs.
MISROUTE_WARN_INTERVAL_S = 60.0


def valid_route_tag(raw: object) -> bool:
    """Whether *raw* is a usable ``[cluster] route_tag``."""
    return isinstance(raw, str) and ROUTE_TAG_RE.fullmatch(raw) is not None


def scoped_id(route_tag: str) -> str:
    """A fresh unguessable id naming the node *route_tag* (no tag: a bare
    random id, the single-node shape)."""
    random_part = secrets.token_hex(ID_RANDOM_BYTES)
    if not route_tag:
        return random_part
    return f"{route_tag}{ROUTE_TAG_SEPARATOR}{random_part}"


def route_tag_of(scoped: str) -> str:
    """The route tag an id names, or ``""`` when it names none."""
    tag, sep, _rest = scoped.partition(ROUTE_TAG_SEPARATOR)
    return tag if sep and valid_route_tag(tag) else ""


class MisrouteWarning:
    """Notes requests for a session another node owns; WARNs at most once a
    minute with how many arrived since the last warning."""

    __slots__ = ("_clock", "_count", "_route_tag", "_warned_at")

    def __init__(
        self, route_tag: str, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._route_tag = route_tag
        self._clock = clock
        self._count = 0
        self._warned_at = -math.inf

    def check(self, scoped: str) -> bool:
        """``True`` when *scoped* names a node other than this one (and
        counts it toward the next WARNING)."""
        tag = route_tag_of(scoped)
        if not tag or tag == self._route_tag:
            return False
        self._count += 1
        now = self._clock()
        if now - self._warned_at >= MISROUTE_WARN_INTERVAL_S:
            log.warning(
                "gfs: %d public-highlight request(s) for a session of another "
                "node reached this node (route tag %r) — the load balancer "
                "does not route by [cluster] route_tag, so these viewers "
                "fall back to the relay or fail; see "
                "docs/operations/gfs-cluster.md",
                self._count,
                self._route_tag,
            )
            self._count = 0
            self._warned_at = now
        return True
