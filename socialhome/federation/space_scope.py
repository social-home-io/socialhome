"""Authoritative space id for an inbound space-content event (§24.11).

The §24.11 pipeline judges a sender's right to write against ONE space:
``make_ban_check`` and ``make_check_space_writer`` both resolve
``event.space_id or payload["space_id"]`` and gate on that. Content
handlers must therefore mutate rows *in that same space* — a handler that
takes a bare row id from the payload lets a household with a seat in space
A name a row of space B and have the write land there.

:func:`resolve_space_id` is the single place that answers "which space is
this envelope authorised for". Rules:

* the routing field (``event.space_id``) wins;
* a payload copy that is present and *different* is a refusal, not a
  tiebreak — the envelope was gated as one space and asks to write another;
* a payload copy is the fallback only when the routing field is absent
  (older peers that shipped the space only in the body).

The result is passed down into the repository mutators, which scope every
statement with ``AND space_id = ?``. Reference caller for the same rule
outside this module: ``federation/private_invite_handler.py``'s
``SPACE_LOCATION_UPDATED`` handler.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.federation import FederationEvent

log = logging.getLogger(__name__)


def resolve_space_id(event: "FederationEvent") -> str | None:
    """Return the space this envelope may write to, or ``None`` to refuse.

    ``None`` means one of: no space id at all, or the routing field and the
    payload copy disagree (logged at WARNING — a mismatch is either a bug
    on the sender or an attempt to write across the space boundary).
    """
    routing = str(getattr(event, "space_id", "") or "")
    payload = str((event.payload or {}).get("space_id") or "")
    if routing and payload and routing != payload:
        log.warning(
            "%s from %s: routing space %s does not match payload space %s — dropping",
            getattr(event, "event_type", "?"),
            getattr(event, "from_instance", "?"),
            routing,
            payload,
        )
        return None
    return (routing or payload) or None


def log_cross_space_refusal(
    event: "FederationEvent",
    *,
    space_id: str,
    what: str,
    row_id: str,
) -> None:
    """WARNING for a mutator that matched no row in the gated space.

    Called when a scoped repo mutator reports zero affected rows: the row
    either does not exist locally (benign out-of-order delivery) or lives
    in another space (a cross-space write attempt). Both are worth one
    line — the second is a security event and must not be silent.
    """
    log.warning(
        "%s from %s: %s %s is not in space %s — refusing the write",
        getattr(event, "event_type", "?"),
        getattr(event, "from_instance", "?"),
        what,
        row_id,
        space_id,
    )
