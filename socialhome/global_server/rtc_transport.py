"""GFS WebRTC DataChannel signalling relay (§4.2, §24.12, §26.6).

The GFS is a **signalling relay** — it stores SDP offer + answer +
trickle ICE candidates so two paired household instances can complete
their WebRTC handshake, then bring up a DataChannel peer-to-peer. The
media / DataChannel itself never passes through the GFS (spec §26.6:
"backend never in media path, DTLS-SRTP mandatory").

This module implements the signalling side only — by design. SCTP /
aiolibdatachannel wiring lives on each *household* instance
(:mod:`socialhome.federation.sync_rtc`), not on the relay.

Each :class:`RtcSession` carries a wall-clock ``created_at``: sessions
older than :data:`SESSION_TTL_SECONDS` are evicted on the next
:meth:`GfsRtcSession.offer`, and at most :data:`MAX_LIVE_SESSIONS` live at
once — the public-highlight offer is anonymous, so memory must stay bounded
without a scheduler.

Session ids name the node that holds them (:mod:`.node_route`): in a
cluster the load balancer routes every later request of the session here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .node_route import scoped_id

log = logging.getLogger(__name__)


#: Session time-to-live. Once a session is older than this and has
#: never been answered, ``gc_expired`` drops it.
SESSION_TTL_SECONDS: float = 120.0

#: Most signalling sessions held at once. Past it :meth:`GfsRtcSession.offer`
#: returns ``None`` and the route sheds the request — no session, no push.
MAX_LIVE_SESSIONS: int = 1024

#: Of those, most an ANONYMOUS source may hold (public-highlight offers,
#: ``client`` set). The rest stay for the signed household signalling of
#: ``/gfs/rtc/offer``, so anonymous viewers can never take every slot.
MAX_PUBLIC_SESSIONS: int = 768

#: Most live public sessions one client address (IPv6 by /64, as the rate
#: limiters bucket it) may hold. A viewer needs one; a retry or two fit.
MAX_SESSIONS_PER_CLIENT: int = 4

#: Most live public sessions one author household's links may hold. One
#: popular (or hammered) link can't take the whole public pool, and it
#: bounds the offers pushed to that author (a household answers each with a
#: peer of its own).
MAX_SESSIONS_PER_AUTHOR: int = 64

#: Most ICE candidates one session accepts from each side. A browser gathers
#: a handful (host, server-reflexive, relay per interface); each viewer
#: candidate is pushed to the author, so it is bounded like the offer.
MAX_ICE_CANDIDATES_PER_SIDE: int = 32


@dataclass(slots=True)
class RtcSession:
    """In-memory state for a single WebRTC signalling session.

    Not frozen because ``answer()`` mutates ``answer_sdp`` and
    ``ice_candidate()`` appends to ``ice_candidates``. Not a DB row,
    so it stays here rather than in ``domain.py``.
    """

    session_id: str
    initiator_id: str
    offer_sdp: str
    answer_sdp: str | None = None
    ice_candidates: list[dict[str, Any]] = field(default_factory=list)
    #: Viewer candidates forwarded to the author (public highlights) — counted
    #: against :data:`MAX_ICE_CANDIDATES_PER_SIDE`, never stored: the viewer
    #: polls ``ice_candidates`` for the AUTHOR's.
    viewer_ice_count: int = 0
    created_at: float = field(default_factory=time.time)
    #: The anonymous viewer's client bucket (public highlights); ``""`` for
    #: a signed household session.
    client: str = ""


class GfsRtcSession:
    """Signalling-relay state (spec §24.12).

    * ``offer()``  — stores the SDP offer and returns a session_id.
    * ``answer()`` — stores the SDP answer for the pending session.
    * ``ice_candidate()`` — accumulates ICE candidates for relay.
    * ``gc_expired()`` — drops sessions past :data:`SESSION_TTL_SECONDS`
      so a stalled handshake doesn't leak memory forever.

    By design this class does **not** open SCTP channels. The DataChannel
    is a peer-to-peer link between two paired households; the GFS only
    relays the handshake artefacts.
    """

    __slots__ = ("_route_tag", "_sessions")

    def __init__(self, *, route_tag: str = "") -> None:
        self._sessions: dict[str, RtcSession] = {}
        #: This node's ``[cluster] route_tag``, stamped into every id.
        self._route_tag = route_tag

    async def offer(
        self, instance_id: str, sdp: str, *, client: str | None = None
    ) -> str | None:
        """Store an SDP offer for *instance_id* and return a new session_id
        (``None`` when :data:`MAX_LIVE_SESSIONS` are live).

        With *client* (an anonymous viewer's address bucket) the offer is a
        public one: also ``None`` past :data:`MAX_PUBLIC_SESSIONS`,
        :data:`MAX_SESSIONS_PER_CLIENT` for that client or
        :data:`MAX_SESSIONS_PER_AUTHOR` for *instance_id* — refused, never
        evicting someone else's session.

        Parameters
        ----------
        instance_id:
            The household instance initiating the connection.
        sdp:
            The WebRTC SDP offer body.

        Returns
        -------
        str | None
            An unguessable id naming this node (:func:`.node_route.scoped_id`)
            the caller must pass to :meth:`answer`.
        """
        self.gc_expired()
        if len(self._sessions) >= MAX_LIVE_SESSIONS:
            log.warning(
                "GfsRtcSession.offer: %d sessions live — shedding the offer",
                len(self._sessions),
            )
            return None
        if client is not None and not self._public_slot_free(client, instance_id):
            return None
        session_id = scoped_id(self._route_tag)
        self._sessions[session_id] = RtcSession(
            session_id=session_id,
            initiator_id=instance_id,
            offer_sdp=sdp,
            client=client or "",
        )
        log.info(
            "GfsRtcSession.offer: session=%s initiator=%s sdp_len=%d",
            session_id,
            instance_id,
            len(sdp),
        )
        return session_id

    def _public_slot_free(self, client: str, instance_id: str) -> bool:
        """Whether one more public session for *client* → *instance_id* fits
        (one pass over at most :data:`MAX_LIVE_SESSIONS` rows)."""
        public = per_client = per_author = 0
        for s in self._sessions.values():
            if not s.client:
                continue
            public += 1
            per_client += s.client == client
            per_author += s.initiator_id == instance_id
        if public >= MAX_PUBLIC_SESSIONS:
            log.warning(
                "GfsRtcSession.offer: %d public sessions live — shedding", public
            )
            return False
        return per_client < MAX_SESSIONS_PER_CLIENT and (
            per_author < MAX_SESSIONS_PER_AUTHOR
        )

    async def answer(self, session_id: str, sdp: str) -> None:
        """Store the SDP answer for an existing signalling session.

        Parameters
        ----------
        session_id:
            The session identifier returned by :meth:`offer`.
        sdp:
            The WebRTC SDP answer body.

        Raises
        ------
        KeyError
            When *session_id* does not correspond to a known session.
        """
        session = self._sessions[session_id]  # raises KeyError if missing
        session.answer_sdp = sdp
        log.info(
            "GfsRtcSession.answer: session=%s sdp_len=%d",
            session_id,
            len(sdp),
        )

    async def ice_candidate(self, session_id: str, candidate: dict[str, Any]) -> bool:
        """Relay an ICE candidate for *session_id*; ``False`` (not stored)
        past :data:`MAX_ICE_CANDIDATES_PER_SIDE`.

        Parameters
        ----------
        session_id:
            The session identifier returned by :meth:`offer`.
        candidate:
            A dict with at minimum ``{"candidate": str, "sdpMid": str}``.

        Raises
        ------
        KeyError
            When *session_id* does not correspond to a known session.
        """
        session = self._sessions[session_id]  # raises KeyError if missing
        if len(session.ice_candidates) >= MAX_ICE_CANDIDATES_PER_SIDE:
            return False
        session.ice_candidates.append(candidate)
        log.debug(
            "GfsRtcSession.ice_candidate: session=%s candidate=%s",
            session_id,
            candidate.get("candidate", ""),
        )
        return True

    def count_viewer_ice(self, session_id: str) -> bool:
        """Count one viewer candidate of *session_id* that is about to be
        forwarded to the author; ``False`` past
        :data:`MAX_ICE_CANDIDATES_PER_SIDE`.

        Raises
        ------
        KeyError
            When *session_id* does not correspond to a known session.
        """
        session = self._sessions[session_id]  # raises KeyError if missing
        if session.viewer_ice_count >= MAX_ICE_CANDIDATES_PER_SIDE:
            return False
        session.viewer_ice_count += 1
        return True

    def drop(self, session_id: str) -> None:
        """Forget *session_id* (an offer that reached no author)."""
        self._sessions.pop(session_id, None)

    def session_count(self) -> int:
        """How many sessions are live."""
        return len(self._sessions)

    def get_session(self, session_id: str) -> RtcSession | None:
        """Return the internal session state, or None if not found.

        Intended for testing and diagnostics only.
        """
        return self._sessions.get(session_id)

    def gc_expired(self, *, now: float | None = None) -> int:
        """Drop sessions older than :data:`SESSION_TTL_SECONDS`.

        Called on every :meth:`offer` (and on demand by tests). Returns the
        number of sessions evicted.
        """
        cutoff = (now if now is not None else time.time()) - SESSION_TTL_SECONDS
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
        return len(stale)
