"""SpaceBotService — admin/member CRUD for named bot personas.

Thin service around :class:`SqliteSpaceBotRepo` that centralises
*who can do what*:

* **Space-scope bots** — owner/admin only. These are the shared
  household voices; a member creating one would effectively grant
  themselves an un-attributed channel.
* **Member-scope bots** — any space member can create one for
  themselves. Posts always carry ``via {member}`` attribution so they
  cannot masquerade as a household alert.
* **Delete / rotate** — owner + admin always; the creating member for
  their own member-scope bot.

Input validation (name / slug / icon length, slug charset) lives here so
the route layer stays a thin dispatcher and tests can exercise the rules
without spinning up aiohttp.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..domain.events import DomainEvent
from ..domain.space import SpacePermissionError
from ..domain.space_bot import (
    MAX_BOT_ICON_LEN,
    MAX_BOT_NAME_LEN,
    MAX_BOT_SLUG_LEN,
    MIN_BOT_ICON_LEN,
    MIN_BOT_NAME_LEN,
    MIN_BOT_SLUG_LEN,
    BotScope,
    SpaceBot,
    SpaceBotError,
    SpaceBotNotFoundError,
)
from ..infrastructure.event_bus import EventBus
from ..repositories.space_bot_repo import AbstractSpaceBotRepo
from ..repositories.space_repo import AbstractSpaceRepo
from ..repositories.user_repo import AbstractUserRepo

log = logging.getLogger(__name__)

# Slug: lowercase, digits, hyphen, underscore. No leading/trailing hyphen —
# that produces weird-looking derived HA service names.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?$")


# ─── Events ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True, frozen=True)
class SpaceBotCreated(DomainEvent):
    """A new :class:`SpaceBot` was registered in a space."""

    bot: SpaceBot
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceBotUpdated(DomainEvent):
    """A bot's ``name`` or ``icon`` changed. Slug/scope are immutable."""

    bot: SpaceBot
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceBotDeleted(DomainEvent):
    """A bot was removed. Old posts keep working via FK ON DELETE SET NULL."""

    bot_id: str
    space_id: str
    occurred_at: datetime = field(default_factory=_now)


@dataclass(slots=True, frozen=True)
class SpaceBotTokenRotated(DomainEvent):
    """A bot's Bearer token was rotated. The HA integration must re-auth."""

    bot_id: str
    space_id: str
    occurred_at: datetime = field(default_factory=_now)


# ─── Service ──────────────────────────────────────────────────────────────


class SpaceBotService:
    """Owner/admin + member CRUD for :class:`SpaceBot` rows."""

    __slots__ = ("_bots", "_spaces", "_users", "_bus")

    def __init__(
        self,
        bot_repo: AbstractSpaceBotRepo,
        space_repo: AbstractSpaceRepo,
        user_repo: AbstractUserRepo,
        bus: EventBus,
    ) -> None:
        self._bots = bot_repo
        self._spaces = space_repo
        self._users = user_repo
        self._bus = bus

    # ── Reads ────────────────────────────────────────────────────────────

    async def list_bots(self, space_id: str, *, actor_username: str) -> list[SpaceBot]:
        """Return every bot in the space.

        Every member can list — there is no sensitive data in the
        :class:`SpaceBot` dataclass (the token hash is a one-way digest
        and still sanitised out at the route layer before serialisation).
        """
        await self._require_member(space_id, actor_username)
        return await self._bots.list_for_space(space_id)

    async def get_bot(
        self, space_id: str, bot_id: str, *, actor_username: str
    ) -> SpaceBot:
        await self._require_member(space_id, actor_username)
        bot = await self._bots.get(bot_id)
        if bot is None or bot.space_id != space_id:
            raise SpaceBotNotFoundError(f"bot {bot_id!r} not found")
        return bot

    # ── Writes ───────────────────────────────────────────────────────────

    async def create_bot(
        self,
        space_id: str,
        *,
        actor_username: str,
        scope: BotScope,
        slug: str,
        name: str,
        icon: str,
    ) -> tuple[SpaceBot, str]:
        """Create a new bot persona. Returns ``(bot, plaintext_token)``.

        The caller MUST surface the plaintext token to the user once and
        then drop it — it is never retrievable again.
        """
        actor = await self._require_member(space_id, actor_username)
        if scope is BotScope.SPACE:
            await self._require_admin_or_owner(space_id, actor_username)
        self._validate_fields(slug=slug, name=name, icon=icon)
        bot, raw_token = await self._bots.create(
            bot_id=str(uuid.uuid4()),
            space_id=space_id,
            scope=scope,
            slug=slug,
            name=name,
            icon=icon,
            created_by=actor.user_id,
        )
        await self._bus.publish(SpaceBotCreated(bot=bot))
        log.info(
            "space_bot: created %s (%s/%s) in space %s by %s",
            bot.bot_id,
            scope.value,
            slug,
            space_id,
            actor_username,
        )
        return bot, raw_token

    async def update_bot(
        self,
        space_id: str,
        bot_id: str,
        *,
        actor_username: str,
        name: str | None = None,
        icon: str | None = None,
    ) -> SpaceBot:
        """Partial update. ``slug`` and ``scope`` are immutable by design —
        they appear in derived HA entity unique_ids and bot tokens, so
        changing them would silently break automations."""
        existing = await self._require_bot(space_id, bot_id)
        await self._require_can_manage(existing, actor_username)
        if name is not None:
            self._validate_name(name)
        if icon is not None:
            self._validate_icon(icon)
        updated = await self._bots.update(bot_id, name=name, icon=icon)
        # Repo guarantees the row exists here — _require_bot just verified.
        assert updated is not None
        await self._bus.publish(SpaceBotUpdated(bot=updated))
        return updated

    async def delete_bot(
        self, space_id: str, bot_id: str, *, actor_username: str
    ) -> None:
        existing = await self._require_bot(space_id, bot_id)
        await self._require_can_manage(existing, actor_username)
        await self._bots.delete(bot_id)
        await self._bus.publish(SpaceBotDeleted(bot_id=bot_id, space_id=space_id))
        log.info(
            "space_bot: deleted %s from space %s by %s",
            bot_id,
            space_id,
            actor_username,
        )

    async def rotate_token(
        self, space_id: str, bot_id: str, *, actor_username: str
    ) -> tuple[SpaceBot, str]:
        existing = await self._require_bot(space_id, bot_id)
        await self._require_can_manage(existing, actor_username)
        result = await self._bots.rotate_token(bot_id)
        if result is None:
            raise SpaceBotNotFoundError(f"bot {bot_id!r} not found")
        bot, raw_token = result
        await self._bus.publish(
            SpaceBotTokenRotated(bot_id=bot.bot_id, space_id=bot.space_id)
        )
        log.info(
            "space_bot: rotated token for %s in space %s by %s",
            bot_id,
            space_id,
            actor_username,
        )
        return bot, raw_token

    # ── Validation ───────────────────────────────────────────────────────

    def _validate_fields(self, *, slug: str, name: str, icon: str) -> None:
        self._validate_slug(slug)
        self._validate_name(name)
        self._validate_icon(icon)

    def _validate_slug(self, slug: str) -> None:
        if not isinstance(slug, str):
            raise SpaceBotError("slug must be a string")
        if not (MIN_BOT_SLUG_LEN <= len(slug) <= MAX_BOT_SLUG_LEN):
            raise SpaceBotError(
                f"slug length must be {MIN_BOT_SLUG_LEN}-{MAX_BOT_SLUG_LEN}"
            )
        if not _SLUG_RE.match(slug):
            raise SpaceBotError(
                "slug may only contain lowercase letters, digits, "
                "'-' and '_' (no leading/trailing dash)"
            )

    def _validate_name(self, name: str) -> None:
        if not isinstance(name, str) or not name.strip():
            raise SpaceBotError("name is required and must be non-empty")
        if not (MIN_BOT_NAME_LEN <= len(name) <= MAX_BOT_NAME_LEN):
            raise SpaceBotError(
                f"name length must be {MIN_BOT_NAME_LEN}-{MAX_BOT_NAME_LEN}"
            )

    def _validate_icon(self, icon: str) -> None:
        if not isinstance(icon, str) or not icon.strip():
            raise SpaceBotError("icon is required (emoji or HA entity_id)")
        if not (MIN_BOT_ICON_LEN <= len(icon) <= MAX_BOT_ICON_LEN):
            raise SpaceBotError(
                f"icon length must be {MIN_BOT_ICON_LEN}-{MAX_BOT_ICON_LEN}"
            )

    # ── Authorisation ────────────────────────────────────────────────────

    async def _require_bot(self, space_id: str, bot_id: str) -> SpaceBot:
        bot = await self._bots.get(bot_id)
        if bot is None or bot.space_id != space_id:
            raise SpaceBotNotFoundError(f"bot {bot_id!r} not found")
        return bot

    async def _require_member(self, space_id: str, username: str):
        actor = await self._users.get(username)
        if actor is None:
            raise KeyError(f"user {username!r} not found")
        member = await self._spaces.get_member(space_id, actor.user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        return actor

    async def _require_admin_or_owner(self, space_id: str, username: str) -> None:
        actor = await self._users.get(username)
        if actor is None:
            raise KeyError(f"user {username!r} not found")
        member = await self._spaces.get_member(space_id, actor.user_id)
        if member is None or member.role not in ("owner", "admin"):
            raise SpacePermissionError(
                "owner/admin required to manage space-scope bots"
            )

    async def _require_can_manage(self, bot: SpaceBot, actor_username: str) -> None:
        """Admin/owner always; creating member for their own scope=member bot."""
        actor = await self._users.get(actor_username)
        if actor is None:
            raise KeyError(f"user {actor_username!r} not found")
        member = await self._spaces.get_member(bot.space_id, actor.user_id)
        if member is None:
            raise SpacePermissionError("not a member of this space")
        if member.role in ("owner", "admin"):
            return
        # Non-admins can only manage their own member-scope bots.
        if bot.scope is BotScope.MEMBER and bot.created_by == actor.user_id:
            return
        raise SpacePermissionError("only the bot owner or an admin can manage this bot")
