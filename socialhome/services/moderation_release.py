"""The approval scope a moderation release runs in (§4.3, v_43).

Approving a queue item replays the write through the feature's normal
persist path (``ModerationHandler.apply``), which publishes the ordinary
domain event; the feature's outbound bridge turns it into the ordinary
``SPACE_*`` federation event. Receivers must be able to tell that write
from a plain member's — under ``MODERATED`` they refuse the latter — so the
released content carries an approval block,
``moderation: {item_id, approved_by}``, inside its sealed payload.

The block is scoped, not threaded: :class:`SpaceModerationService` runs the
apply inside :func:`release_scope`, and every outbound bridge passes its
payload through :func:`with_release`. The bus awaits its subscribers in the
publishing task (``EventBus.publish``), so the scope reaches exactly the
events the apply produced — never a write that merely happens concurrently
in another task (a :mod:`contextvars` value is per task).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from ..domain.space import MODERATION_BLOCK_KEY, ModerationApproval

_RELEASE: ContextVar[ModerationApproval | None] = ContextVar(
    "socialhome_moderation_release", default=None
)
#: The approver's role, as the queue verified it — for an approver who is
#: not a member of THIS household (the host applying a remote moderator's
#: approval): the content services' access gate reads it for them.
_ROLE: ContextVar[str | None] = ContextVar(
    "socialhome_moderation_release_role", default=None
)


def current_release() -> ModerationApproval | None:
    """The approval the running write is a release of, if any."""
    return _RELEASE.get()


def release_role(user_id: str) -> str | None:
    """The verified role of ``user_id`` when they are the running release's
    approver, else ``None``."""
    release = _RELEASE.get()
    if release is None or release.approved_by != user_id:
        return None
    return _ROLE.get()


@contextmanager
def release_scope(
    item_id: str, approved_by: str, *, approver_role: str | None = None
) -> Iterator[ModerationApproval]:
    """Run a moderation release: content events emitted inside carry the
    approval block."""
    release = ModerationApproval(item_id=item_id, approved_by=approved_by)
    token = _RELEASE.set(release)
    role_token = _ROLE.set(approver_role)
    try:
        yield release
    finally:
        _ROLE.reset(role_token)
        _RELEASE.reset(token)


def with_release(payload: dict) -> dict:
    """``payload`` plus the approval block when inside a release scope."""
    release = _RELEASE.get()
    if release is not None:
        payload[MODERATION_BLOCK_KEY] = release.to_wire()
    return payload
