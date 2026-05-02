"""BotBridgeService — HA automations post into Social Home via HTTP.

Direction: *inbound* (HA → Social Home). The *outbound* direction
(Social Home → HA, for automations that react to SH activity) lives in
:mod:`socialhome.services.ha_bridge_service` — don't confuse the two.

Two endpoints feed this service:

* ``POST /api/bot-bridge/spaces/{id}`` — authenticated by a **per-bot**
  Bearer token. Each :class:`SpaceBot` carries its own token; the token
  IS the bot identity, so a leak is bounded to that one bot in that one
  space. The route handler resolves the token to a :class:`SpaceBot` via
  :meth:`AbstractSpaceBotRepo.get_by_token_hash` and hands it to
  :meth:`notify_space` below. There is no ``bot_id`` in the request
  body — it would be redundant.
* ``POST /api/bot-bridge/conversations/{id}`` — authenticated by a
  regular user Bearer token; DMs have no named bot personas (the 1:1
  context makes a generic "Home Assistant" voice adequate), so the post
  is attributed to ``SYSTEM_AUTHOR`` with the HA logo.

Posts authored by this service always set ``post.author = SYSTEM_AUTHOR``
(``"system-integration"``) on disk; the feed renderer resolves the bot
identity via ``post.bot_id`` at read time. If the bot is later deleted,
the FK goes to NULL and old posts fall back to "Home Assistant".
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from ..domain.conversation import ConversationMessage
from ..domain.events import DmMessageCreated, SpacePostCreated
from ..domain.post import Post, PostType
from ..domain.space_bot import SpaceBot, SpaceBotDisabledError
from ..domain.user import SYSTEM_AUTHOR
from ..infrastructure.event_bus import EventBus
from ..repositories.conversation_repo import AbstractConversationRepo
from ..repositories.space_post_repo import AbstractSpacePostRepo
from ..repositories.space_repo import AbstractSpaceRepo

log = logging.getLogger(__name__)

# Hard caps on inbound text — HA automations can easily be wired to a
# sensor attribute that grows unboundedly (log lines, raw JSON). Accept
# the generic sizes here; the renderer truncates for display separately.
MAX_TITLE_LEN = 200
MAX_MESSAGE_LEN = 4000


class BotBridgeError(Exception):
    """Base for bot-bridge service errors. Routed to HTTP 422 by default."""


class BotBridgeInvalidError(BotBridgeError):
    """Raised when the incoming payload fails basic validation."""


class BotBridgeService:
    """Inbound HA bridge: turn a ``{title, message}`` payload into a post.

    The service is deliberately thin — most of the security work lives at
    the route layer (per-bot token → SpaceBot resolution) so the service
    can treat the :class:`SpaceBot` as authoritative. The checks inside
    are domain-level: ``bot_enabled`` admin kill-switch, content caps,
    event publication for realtime broadcast.
    """

    __slots__ = (
        "_space_posts",
        "_spaces",
        "_conversations",
        "_bus",
    )

    def __init__(
        self,
        space_post_repo: AbstractSpacePostRepo,
        space_repo: AbstractSpaceRepo,
        conversation_repo: AbstractConversationRepo,
        bus: EventBus,
    ) -> None:
        self._space_posts = space_post_repo
        self._spaces = space_repo
        self._conversations = conversation_repo
        self._bus = bus

    # ── Public API ───────────────────────────────────────────────────────

    async def notify_space(
        self,
        bot: SpaceBot,
        *,
        title: str | None,
        message: str,
    ) -> Post:
        """Create a space feed post on behalf of ``bot``.

        ``bot`` has already been resolved from the inbound Bearer token at
        the route layer. The method still re-checks the space's
        ``bot_enabled`` flag so admins can kill-switch posting without
        invalidating every outstanding token.
        """
        self._validate_payload(title, message)
        space = await self._spaces.get(bot.space_id)
        if space is None:
            # Bot outlived its space (racing with space deletion). The FK
            # is ON DELETE CASCADE on space_bots.space_id, so this branch
            # is rare — treat as 404 via the route's KeyError mapping.
            raise KeyError(f"space {bot.space_id!r} not found")
        if not space.bot_enabled:
            raise SpaceBotDisabledError("bot posting is disabled for this space")
        content = f"**{title}**\n{message}" if title else message
        post = Post(
            id=str(uuid.uuid4()),
            author=SYSTEM_AUTHOR,
            type=PostType.TEXT,
            created_at=datetime.now(timezone.utc),
            content=content,
            bot_id=bot.bot_id,
        )
        saved = await self._space_posts.save(bot.space_id, post)
        await self._bus.publish(SpacePostCreated(post=saved, space_id=bot.space_id))
        log.info(
            "bot-bridge: space post %s by bot %s (%s) in space %s",
            saved.id,
            bot.slug,
            bot.scope.value,
            bot.space_id,
        )
        return saved

    async def notify_conversation(
        self,
        *,
        conversation_id: str,
        sender_user_id: str,
        recipient_user_ids: tuple[str, ...],
        title: str | None,
        message: str,
    ) -> ConversationMessage:
        """Post a system message into a DM / group DM.

        Authenticated at the route layer via the regular user API token.
        The DB-level ``sender_user_id`` is set to :data:`SYSTEM_AUTHOR`
        so the UI renders the message with the "Home Assistant" system
        chrome; ``recipient_user_ids`` drives the WS + push fan-out via
        :class:`DmMessageCreated`.
        """
        self._validate_payload(title, message)
        conv = await self._conversations.get(conversation_id)
        if conv is None:
            raise KeyError(f"conversation {conversation_id!r} not found")
        if not conv.bot_enabled:
            raise SpaceBotDisabledError("bot posting is disabled for this conversation")
        content = f"**{title}**\n{message}" if title else message
        msg = ConversationMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            sender_user_id=SYSTEM_AUTHOR,
            content=content,
            created_at=datetime.now(timezone.utc),
        )
        saved = await self._conversations.save_message(msg)
        await self._bus.publish(
            DmMessageCreated(
                conversation_id=conversation_id,
                message_id=saved.id,
                sender_user_id=saved.sender_user_id,
                sender_display_name="Home Assistant",
                recipient_user_ids=recipient_user_ids,
                content=saved.content,
                message_type=saved.type,
                media_url=saved.media_url,
                reply_to_id=saved.reply_to_id,
                occurred_at=saved.created_at,
            )
        )
        log.info(
            "bot-bridge: DM message %s in conversation %s (by %s)",
            saved.id,
            conversation_id,
            sender_user_id,
        )
        return saved

    # ── Internal helpers ─────────────────────────────────────────────────

    def _validate_payload(self, title: str | None, message: str) -> None:
        if not isinstance(message, str) or not message.strip():
            raise BotBridgeInvalidError("message is required and must be non-empty")
        if len(message) > MAX_MESSAGE_LEN:
            raise BotBridgeInvalidError(
                f"message must not exceed {MAX_MESSAGE_LEN} characters"
            )
        if title is not None:
            if not isinstance(title, str):
                raise BotBridgeInvalidError("title must be a string or null")
            if len(title) > MAX_TITLE_LEN:
                raise BotBridgeInvalidError(
                    f"title must not exceed {MAX_TITLE_LEN} characters"
                )
