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
    from ..domain.space import Space

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


def log_not_applied(
    event: "FederationEvent",
    *,
    what: str,
    row_id: str,
    reason: str,
) -> None:
    """DEBUG for a write that is a benign no-op, never a security event.

    A replayed delete of a row that is already gone or already deleted, a
    status change for a listing already in a terminal state, an edit that
    beat its create here: redelivery and out-of-order arrival produce these
    all the time, and logging them at WARNING next to real cross-space or
    foreign-author refusals would bury the ones that matter.
    """
    log.debug(
        "%s from %s: %s %s not applied — %s",
        getattr(event, "event_type", "?"),
        getattr(event, "from_instance", "?"),
        what,
        row_id,
        reason,
    )


def archive_refusal(space: "Space | None", sender: str) -> str | None:
    """Why a content write from ``sender`` may not land in ``space``, or ``None``.

    An archived space is a **read-only snapshot** — locally
    (``SpaceService._require_writable_space`` and its siblings refuse new
    content over REST) and therefore to peers too: a federated write is the same
    write arriving by another door. This is the one decision both inbound
    doors share — the §24.11 ``check_space_archived`` step (live events,
    their mesh-routed and held replays, and the §25.6 resume replay, which
    re-sends live events) and the §25.6 sync receiver — so the two cannot
    drift.

    * **not archived, not dissolved → ``None``** (the write is judged by
      the other gates as usual);
    * **terminated** (``dissolved``, or ``archived_reason`` set: the host
      dissolved the space or removed us) → refused from **everybody**. The
      content is frozen for good; nobody, the host included, writes into
      it again;
    * **reversibly archived** (``archived_reason`` NULL) → refused from
      every household **except the space's host**. The archive flag on a
      member's copy is the host's own decision (it arrives over the
      authority-signed ``SPACE_CONFIG_CHANGED``; a member's archive request
      is forwarded to the host), and the host's local API is read-only for
      the same space — so what the host still sends is the pre-archive
      state a member missed (resume replay, catch-up sync), which is part
      of the snapshot. Exempting it costs nothing: a host could unarchive
      over the same signed channel at will.

    Removals are the callers' exception, not this function's: the live gate
    lets :data:`~socialhome.domain.federation.ARCHIVED_ALLOWED_REMOVAL_TYPES`
    through before asking.

    ``space is None`` (we don't hold the space) is ``None`` — whether a
    write may land in a space we never seated is the handlers' question.
    """
    if space is None:
        return None
    if space.dissolved:
        return "dissolved"
    if not space.archived:
        return None
    if space.archived_reason:
        return f"archived ({space.archived_reason})"
    if sender and sender == space.owner_instance_id:
        return None
    return "archived"
