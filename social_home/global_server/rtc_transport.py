"""GFS WebRTC DataChannel signalling relay (§4.2, §24.12, §26.6).

The GFS is a **signalling relay** — it stores SDP offer + answer +
trickle ICE candidates so two paired household instances can complete
their WebRTC handshake, then bring up a DataChannel peer-to-peer. The
media / DataChannel itself never passes through the GFS (spec §26.6:
"backend never in media path, DTLS-SRTP mandatory").

This module implements the signalling side only — by design. SCTP /
libdatachannel wiring lives on each *household* instance
(:mod:`social_home.federation.sync_rtc`), not on the relay.

Each :class:`RtcSession` gains a wall-clock ``created_at`` so a
background GC can evict stale sessions whose peers dropped out before
completing the handshake.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


#: Session time-to-live. Once a session is older than this and has
#: never been answered, ``gc_expired`` drops it.
SESSION_TTL_SECONDS: float = 120.0


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
    created_at: float = field(default_factory=time.time)


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

    __slots__ = ("_sessions",)

    def __init__(self) -> None:
        self._sessions: dict[str, RtcSession] = {}

    async def offer(self, instance_id: str, sdp: str) -> str:
        """Store an SDP offer from *instance_id* and return a new session_id.

        Parameters
        ----------
        instance_id:
            The household instance initiating the connection.
        sdp:
            The WebRTC SDP offer body.

        Returns
        -------
        str
            A UUID4 session identifier the caller must pass to :meth:`answer`.
        """
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = RtcSession(
            session_id=session_id,
            initiator_id=instance_id,
            offer_sdp=sdp,
        )
        log.info(
            "GfsRtcSession.offer: session=%s initiator=%s sdp_len=%d",
            session_id,
            instance_id,
            len(sdp),
        )
        return session_id

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

    async def ice_candidate(self, session_id: str, candidate: dict[str, Any]) -> None:
        """Relay an ICE candidate for *session_id*.

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
        session.ice_candidates.append(candidate)
        log.debug(
            "GfsRtcSession.ice_candidate: session=%s candidate=%s",
            session_id,
            candidate.get("candidate", ""),
        )

    def get_session(self, session_id: str) -> RtcSession | None:
        """Return the internal session state, or None if not found.

        Intended for testing and diagnostics only.
        """
        return self._sessions.get(session_id)

    def gc_expired(self, *, now: float | None = None) -> int:
        """Drop sessions older than :data:`SESSION_TTL_SECONDS`.

        Called periodically by the cluster scheduler (or on-demand by
        tests). Returns the number of sessions evicted.
        """
        cutoff = (now if now is not None else time.time()) - SESSION_TTL_SECONDS
        stale = [sid for sid, s in self._sessions.items() if s.created_at < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)
        return len(stale)
