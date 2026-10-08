"""Who of this household's seats gets a conversation's local fan-out.

One rule for every in-process event a conversation emits to its local
members — a new message, an edit or transcript patch, a reaction, a typing
indicator — so none of them can drift from the others:

* the actor is left out unless ``include_actor`` (a reaction goes to the
  reactor's other tabs too);
* ``withheld`` users never get it — the guardian-block counterparts
  (§CP.F2) the caller computed for the actor, and for a reaction also
  those of the message's author;
* in a system chat (household / space chat, ``Conversation.is_system``) a
  removed seat or an account that is no longer active is skipped: its seat
  rows are only per-user state, kept by a reconciler that may trail.
"""

from __future__ import annotations

from collections.abc import Collection

from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.user_repo import AbstractUserRepo


async def local_audience(
    convos: AbstractConversationRepo,
    users: AbstractUserRepo,
    conversation_id: str,
    *,
    actor_user_id: str,
    withheld: Collection[str] = (),
    include_actor: bool = False,
) -> tuple[str, ...]:
    """``user_id`` of every local member the event for ``conversation_id``
    goes to, in seat order, without duplicates."""
    conv = await convos.get(conversation_id)
    system = conv is not None and conv.is_system
    out: list[str] = []
    for m in await convos.list_members(conversation_id):
        if system and m.deleted_at is not None:
            continue
        user = await users.get(m.username)
        if user is None or user.user_id in out or user.user_id in withheld:
            continue
        if user.user_id == actor_user_id and not include_actor:
            continue
        if system and (user.state != "active" or user.deleted_at is not None):
            continue
        out.append(user.user_id)
    return tuple(out)
