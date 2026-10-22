"""GFS signalling store for public-highlight viewing sessions (§26.6).

An anonymous viewer of a public highlight link drops its SDP offer here
(``/gfs/highlight_rtc/offer``); the author's household answers and trickles
its ICE candidates back (signed), and the viewer polls for them. The GFS
holds no PeerConnection — the stream runs viewer ↔ author (spec §26.6:
"backend never in media path, DTLS-SRTP mandatory"). See
``docs/protocol/highlights_public.md``.

Every session is an anonymous viewer's, so every session counts against the
per-client and per-author caps. (Paired households signal their own WebRTC
over federation events — ``FEDERATION_RTC_*`` — never through the GFS.)

Each :class:`RtcSession` carries a wall-clock ``created_at``: sessions
older than :data:`SESSION_TTL_SECONDS` are evicted on the next
:meth:`GfsRtcSession.offer`, and at most :data:`MAX_LIVE_SESSIONS` live at
once — the offer is anonymous, so memory must stay bounded without a
scheduler.

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

#: Most live sessions one client address (IPv6 by /64, as the rate
#: limiters bucket it) may hold. A viewer needs one; a retry or two fit.
MAX_SESSIONS_PER_CLIENT: int = 4

#: Most live sessions one author household's links may hold. One
#: popular (or hammered) link can't take the whole pool, and it
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
    #: The anonymous viewer's client bucket (IPv6 by /64).
    client: str = ""


class GfsRtcSession:
    """Public-highlight signalling state (one node's memory).

    * ``offer()``  — stores the SDP offer and returns a session_id.
    * ``answer()`` — stores the SDP answer for the pending session.
    * ``ice_candidate()`` — accumulates ICE candidates for relay.
    * ``gc_expired()`` — drops sessions past :data:`SESSION_TTL_SECONDS`
      so a stalled handshake doesn't leak memory forever.

    By design this class does **not** open SCTP channels. The DataChannel
    is a peer-to-peer link between the viewer and the author's household;
    the GFS only relays the handshake artefacts.
    """

    __slots__ = ("_route_tag", "_sessions")

    def __init__(self, *, route_tag: str = "") -> None:
        self._sessions: dict[str, RtcSession] = {}
        #: This node's ``[cluster] route_tag``, stamped into every id.
        self._route_tag = route_tag

    async def offer(self, instance_id: str, sdp: str, *, client: str) -> str | None:
        """Store a viewer's SDP offer to *instance_id* and return a new
        session_id — ``None`` when :data:`MAX_LIVE_SESSIONS` are live,
        *client* holds :data:`MAX_SESSIONS_PER_CLIENT` or *instance_id*
        :data:`MAX_SESSIONS_PER_AUTHOR`: refused, never evicting someone
        else's session.

        Parameters
        ----------
        instance_id:
            The author's household the offer is for.
        sdp:
            The WebRTC SDP offer body.
        client:
            The anonymous viewer's address bucket (IPv6 by /64).

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
        if not self._slot_free(client, instance_id):
            return None
        session_id = scoped_id(self._route_tag)
        self._sessions[session_id] = RtcSession(
            session_id=session_id,
            initiator_id=instance_id,
            offer_sdp=sdp,
            client=client,
        )
        log.info(
            "GfsRtcSession.offer: session=%s initiator=%s sdp_len=%d",
            session_id,
            instance_id,
            len(sdp),
        )
        return session_id

    def _slot_free(self, client: str, instance_id: str) -> bool:
        """Whether one more session for *client* → *instance_id* fits (one
        pass over at most :data:`MAX_LIVE_SESSIONS` rows)."""
        per_client = per_author = 0
        for s in self._sessions.values():
            per_client += s.client == client
            per_author += s.initiator_id == instance_id
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
