"""DM history sync (§12 + §25.6 analogue for conversations).

When a peer is unreachable their outbound DMs pile up on our side. Once
the link comes back the two instances reconcile their conversation
histories by exchanging three event types:

* ``DM_HISTORY_REQUEST`` — requester → provider, carries
  ``(conversation_id, since_iso)``.
* ``DM_HISTORY_CHUNK`` — provider → requester, each frame up to
  :data:`CHUNK_SIZE` messages ordered ASC by ``created_at``.
* ``DM_HISTORY_COMPLETE`` — provider → requester, terminal marker.

``DM_HISTORY_CHUNK_ACK`` is the receiver → provider acknowledgement; it
carries ``(conversation_id, chunk_index)`` so the provider can track
per-peer progress. Replay safety remains via ``INSERT OR IGNORE`` on
the message PK — the ack is an efficiency + observability signal, not
a correctness requirement.

Request initiation lives in :class:`DmHistoryScheduler`. It subscribes
to :class:`PairingConfirmed` (first-time pair-up) and
:class:`ConnectionReachable` (peer came back online after unreachable)
and enqueues one request per (peer, conversation) at P4 on the
reconnect queue. A 1 h rate limit protects against flood on
pair-confirm when the two households share many conversations.
"""

from .provider import CHUNK_SIZE, DmHistoryProvider
from .receiver import DmHistoryReceiver
from .scheduler import DmHistoryScheduler

__all__ = [
    "CHUNK_SIZE",
    "DmHistoryProvider",
    "DmHistoryReceiver",
    "DmHistoryScheduler",
]
