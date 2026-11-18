"""The inbox's "I already hold this envelope" answer — both ends of it.

A sender's inbox POST is bounded (10 s). When the receiver is still working
on the envelope at that point, the sender gives up and its outbox sends the
same envelope again (same ``msg_id``, re-signed). The receiver's replay step
refuses the second copy — correctly, nothing may be applied twice — and
answered it like every other refusal: ``410 {"error": "gone"}``, the same
answer as for a timestamp outside the window. The sender could not tell
"you already delivered this" from "this will never be accepted", so it
dropped the row as *permanently rejected* (two WARNINGs) for a message that
had arrived.

The receiver now adds a fixed code to that one answer::

    410 {"error": "gone", "code": "REPLAY"}

and a sender that reads it counts the envelope as delivered.

* **Additive.** The status stays 410 and ``error`` stays ``gone``: a sender
  that predates the code drops the row as before, which loses nothing (the
  receiver holds the envelope).
* **Fail-safe.** Only a 410 whose JSON body carries exactly this code
  counts. A body something in front of the peer rewrote or dropped is
  "permanently rejected", as before.
* **No oracle.** The replay step runs after the signature step, so the
  code is only ever returned for an envelope the pinned key of a paired
  household signed — never to somebody probing inbox ids. It says nothing
  a 410 for an in-window envelope did not already say, and carries no
  detail (no ``msg_id``, no reason text).
"""

from __future__ import annotations

import logging
from typing import Any, Final

import orjson

log = logging.getLogger(__name__)

#: The ``code`` of the inbox's answer to an envelope it already holds.
INBOX_CODE_REPLAY: Final = "REPLAY"

#: The status that answer is sent with (unchanged: "gone").
INBOX_REPLAY_STATUS: Final = 410

#: How much of an answer's body a sender reads to look for the code.
_MAX_BODY_CHARS: Final = 200


async def inbox_holds_envelope(resp: Any) -> bool:
    """Whether a peer inbox's response says it already holds the envelope
    we just POSTed: a 410 whose JSON body is an object with
    ``"code": "REPLAY"``. Never raises — an unreadable or foreign body is
    simply not that answer."""
    if resp.status != INBOX_REPLAY_STATUS:
        return False
    try:
        body = orjson.loads((await resp.text())[:_MAX_BODY_CHARS])
    except Exception:
        log.debug("inbox answer: unreadable 410 body", exc_info=True)
        return False
    return isinstance(body, dict) and body.get("code") == INBOX_CODE_REPLAY


__all__ = ["INBOX_CODE_REPLAY", "INBOX_REPLAY_STATUS", "inbox_holds_envelope"]
