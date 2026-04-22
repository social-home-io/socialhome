"""Space-bot domain types — named personas used by the bot-bridge.

Bot posts in a space feed are rendered with the bot's icon + name rather
than a generic "Home Assistant" avatar, so members can tell a doorbell
alert apart from a laundry timer at a glance. Each bot carries its own
Bearer token (stored hashed); the token IS the bot's identity — sharing
it across devices is the intended mechanism. Token leak is bounded to
that one bot in that one space.

Two scopes:

* ``SPACE`` — admin/owner-created shared household bot. Posts render with
  ``via Home Assistant`` subtext; no member attribution.
* ``MEMBER`` — member-created personal bot. Posts always render with
  ``via {member}`` subtext so they cannot masquerade as a household alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

# Token format: 40 url-safe chars after the ``shb_`` prefix (space-home-bot).
# Length fits comfortably in an HTTP header and leaves room for entropy.
BOT_TOKEN_PREFIX = "shb_"

# Input validation bounds.
MAX_BOT_NAME_LEN = 48
MIN_BOT_NAME_LEN = 1
MAX_BOT_SLUG_LEN = 32
MIN_BOT_SLUG_LEN = 1
# Emojis range 1–8 codepoints (covers ZWJ sequences like 👨‍👩‍👧); HA entity_id
# allowlist is ``domain.object`` — validated separately by the service layer.
MAX_BOT_ICON_LEN = 64
MIN_BOT_ICON_LEN = 1


class BotScope(StrEnum):
    """Who owns a bot in a space.

    * ``SPACE`` — admin-curated shared bot; post has no member attribution.
    * ``MEMBER`` — member's personal bot; post always shows ``via {member}``.
    """

    SPACE = "space"
    MEMBER = "member"


@dataclass(slots=True, frozen=True)
class SpaceBot:
    """A named bot persona scoped to a space.

    The raw Bearer token is *never* stored on this dataclass — only its
    sha256 hash (``token_hash``). ``SqliteSpaceBotRepo.create`` returns a
    ``(SpaceBot, raw_token)`` tuple so callers can show the plaintext
    token to the user exactly once.
    """

    bot_id: str
    space_id: str
    scope: BotScope
    slug: str
    name: str
    icon: str
    created_by: str
    token_hash: str
    created_at: datetime


class SpaceBotError(Exception):
    """Base class for bot-bridge domain errors.

    Maps to HTTP 422 by default via :class:`BaseView._iter`. Subclasses
    carry richer semantics where the HTTP status differs.
    """


class SpaceBotNotFoundError(KeyError):
    """Raised when looking up a bot that doesn't exist — maps to 404."""


class SpaceBotSlugTakenError(SpaceBotError):
    """Raised when a caller picks a slug already used in (space, scope).

    Maps to HTTP 409 via the route layer.
    """


class SpaceBotDisabledError(SpaceBotError):
    """Raised when posting to a space whose ``notify_enabled`` is off.

    Admin kill-switch: tokens stay valid; the feature is temporarily muted.
    Maps to HTTP 403 via the route layer.
    """
