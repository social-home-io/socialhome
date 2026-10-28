"""Answering a request to message you (§23.47 ``DM_CONTACT_REQUEST``).

A person on a paired household can ask to message someone here; the
request lands in ``dm_contact_requests`` and a "Needs you" row
(``dm_contact_requested``, about ``contact:<requester>``). The recipient
answers it here:

* **Accept** opens (or finds) the 1:1 chat with the requester and returns
  it — the request then reads as accepted. The requester's household is
  not told: it learns of the chat when the recipient first writes in it.
* **Decline** closes the request silently. Nothing leaves this household:
  no household sends ``DM_CONTACT_ACCEPTED`` / ``DM_CONTACT_DECLINED`` or
  handles them, so the requester is never told (the same as a personal
  block).

Every pending request from that person is answered by one conditional
write (:meth:`AbstractDmContactRepo.answer_pending`), so two devices
answering at once can't both win: the loser gets
:class:`ContactRequestHandledError`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..domain.conversation import (
    ContactRequestHandledError,
    ContactRequestNotFoundError,
    Conversation,
)
from ..domain.events import DmContactRequestAnswered

if TYPE_CHECKING:
    from ..infrastructure.event_bus import EventBus
    from ..repositories.dm_contact_repo import AbstractDmContactRepo
    from ..repositories.user_repo import AbstractUserRepo
    from .dm_service import DmService

log = logging.getLogger(__name__)


class DmContactService:
    """Accept or decline someone's request to message you."""

    __slots__ = ("_bus", "_dm", "_repo", "_users")

    def __init__(
        self,
        repo: AbstractDmContactRepo,
        user_repo: AbstractUserRepo,
        dm_service: DmService,
        bus: EventBus,
    ) -> None:
        self._repo = repo
        self._users = user_repo
        self._dm = dm_service
        self._bus = bus

    async def _pending_for(self, username: str, requester_user_id: str) -> str:
        """The caller's user id, once a request from the requester waits
        for them; else :class:`ContactRequestNotFoundError` /
        :class:`ContactRequestHandledError`."""
        me = await self._users.get(username)
        if me is None:
            raise ContactRequestNotFoundError()
        latest = await self._repo.latest_between(requester_user_id, me.user_id)
        if latest is None:
            raise ContactRequestNotFoundError()
        if latest.get("status") != "pending":
            raise ContactRequestHandledError(str(latest.get("status")))
        return me.user_id

    async def _claim(self, requester_user_id: str, user_id: str, status: str) -> None:
        if not await self._repo.answer_pending(requester_user_id, user_id, status):
            latest = await self._repo.latest_between(requester_user_id, user_id)
            raise ContactRequestHandledError(
                str(latest.get("status")) if latest else status
            )
        await self._bus.publish(
            DmContactRequestAnswered(
                requester_user_id=requester_user_id,
                recipient_user_id=user_id,
                accepted=status == "accepted",
            )
        )

    async def accept(self, *, username: str, requester_user_id: str) -> Conversation:
        """Open the 1:1 with the requester and close the request.

        The chat is opened first — it refuses a blocked pair (403) or a
        person this household no longer knows (404) while the request still
        waits; opening it again is idempotent. Then the request is answered
        once."""
        user_id = await self._pending_for(username, requester_user_id)
        if await self._users.get_remote(requester_user_id) is None:
            raise ContactRequestNotFoundError()
        conv = await self._dm.create_dm(
            creator_username=username, other_user_id=requester_user_id
        )
        await self._claim(requester_user_id, user_id, "accepted")
        return conv

    async def decline(self, *, username: str, requester_user_id: str) -> None:
        """Close the request. Silent: the requester is never told."""
        user_id = await self._pending_for(username, requester_user_id)
        await self._claim(requester_user_id, user_id, "declined")
