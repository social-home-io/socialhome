"""Who may read and write a system chat — decided live, never by seats.

A *system chat* (``Conversation.system_scope`` set) is a group-DM
conversation the household creates itself: the household chat today, a
space's chat later. Its seat rows (``conversation_members``) only hold
per-user state — read watermark, mute, notification level. Access is
answered here from facts that are current right now, so a seat a
reconciler has not caught up on yet never grants (or withholds) anything:

* **household** — an active local user, while ``feat_household_chat`` is
  on (:class:`HouseholdChatAccess`).
* **space** — added with the space chat: a space-scope
  :class:`SystemChatAccess` registered on :class:`SystemChatPolicy`.

A scope with no registered access is refused (fail closed), so a
``'space'`` row can't be read before its rules exist.

:class:`~socialhome.services.dm_service.DmService` defers its membership
check to :meth:`SystemChatPolicy.require` for every system conversation
(reads, sends, edits, deletes, reactions, read marks, mute and level).
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..domain.conversation import Conversation, SystemChatScope
from ..domain.preferences import FeatureDisabledError
from ..domain.user import User
from ..repositories.user_repo import AbstractUserRepo
from .preferences_service import PreferencesService

log = logging.getLogger(__name__)

#: ``HouseholdPreferences.is_enabled`` section of the household chat.
HOUSEHOLD_CHAT_SECTION = "household_chat"


class SystemChatAccess(Protocol):
    """The access rules of one :class:`SystemChatScope`."""

    #: Notification level a newly seated member starts at.
    default_notif_level: str

    async def check(self, conv: Conversation, user: User, *, write: bool) -> None:
        """Return when ``user`` may read (``write=False``) or post in
        ``conv``; raise :class:`FeatureDisabledError` when the chat is off,
        :class:`PermissionError` when the user may not."""
        ...


class HouseholdChatAccess:
    """Household chat: every active local user, while the toggle is on."""

    __slots__ = ("_prefs",)

    default_notif_level = "all"

    def __init__(self, preferences: PreferencesService) -> None:
        self._prefs = preferences

    async def enabled(self) -> bool:
        """``feat_household_chat`` is on."""
        prefs = await self._prefs.get_household()
        return prefs.is_enabled(HOUSEHOLD_CHAT_SECTION)

    async def check(self, conv: Conversation, user: User, *, write: bool) -> None:
        if not await self.enabled():
            raise FeatureDisabledError(HOUSEHOLD_CHAT_SECTION)
        if user.state != "active" or user.deleted_at is not None:
            raise PermissionError("not a member of the household chat")


class SystemChatPolicy:
    """Live read / write decisions for system chats, one strategy per scope."""

    __slots__ = ("_users", "_scopes")

    def __init__(
        self,
        user_repo: AbstractUserRepo,
        *,
        household: HouseholdChatAccess | None = None,
    ) -> None:
        self._users = user_repo
        self._scopes: dict[SystemChatScope, SystemChatAccess] = {}
        if household is not None:
            self._scopes[SystemChatScope.HOUSEHOLD] = household

    def register(self, scope: SystemChatScope, access: SystemChatAccess) -> None:
        """Plug in the access rules of ``scope`` (replaces earlier ones)."""
        self._scopes[scope] = access

    def access_for(self, conv: Conversation) -> SystemChatAccess:
        """The rules of ``conv``'s scope; :class:`PermissionError` when the
        conversation is no system chat or its scope has none (fail closed)."""
        if conv.system_scope is None:
            raise PermissionError("not a system chat")
        access = self._scopes.get(conv.system_scope)
        if access is None:
            raise PermissionError(f"no access rules for {conv.system_scope} chats")
        return access

    def default_notif_level(self, conv: Conversation) -> str:
        """The level a member newly seated in ``conv`` starts at."""
        return self.access_for(conv).default_notif_level

    async def require(self, conv: Conversation, user_id: str, *, write: bool) -> User:
        """Return the local user ``user_id`` when they may read (or, with
        ``write``, post in) ``conv``; raise otherwise.

        Only local users are ever members of a system chat here: a remote
        or unknown ``user_id`` is refused.
        """
        access = self.access_for(conv)
        user = await self._users.get_by_user_id(user_id)
        if user is None:
            raise PermissionError("not a member of this chat")
        await access.check(conv, user, write=write)
        return user

    async def can_read(self, conv: Conversation, user_id: str) -> bool:
        """Whether ``user_id`` may read ``conv`` right now."""
        return await self._allowed(conv, user_id, write=False)

    async def can_write(self, conv: Conversation, user_id: str) -> bool:
        """Whether ``user_id`` may post in ``conv`` right now."""
        return await self._allowed(conv, user_id, write=True)

    async def _allowed(self, conv: Conversation, user_id: str, *, write: bool) -> bool:
        try:
            await self.require(conv, user_id, write=write)
        except PermissionError, FeatureDisabledError:
            return False
        return True
