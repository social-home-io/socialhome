"""Tests for socialhome.domain.space_bot."""

from __future__ import annotations

from datetime import datetime, timezone

from socialhome.domain.space_bot import (
    BOT_TOKEN_PREFIX,
    MAX_BOT_NAME_LEN,
    MAX_BOT_SLUG_LEN,
    BotScope,
    SpaceBot,
    SpaceBotDisabledError,
    SpaceBotError,
    SpaceBotNotFoundError,
    SpaceBotSlugTakenError,
)


def test_bot_scope_values():
    """Scope enum serialises to the strings the schema expects."""
    assert BotScope.SPACE.value == "space"
    assert BotScope.MEMBER.value == "member"


def test_space_bot_is_frozen_dataclass():
    """SpaceBot is frozen so the service layer can share references safely."""
    bot = SpaceBot(
        bot_id="b1",
        space_id="s1",
        scope=BotScope.SPACE,
        slug="doorbell",
        name="Doorbell",
        icon="🔔",
        created_by="u1",
        token_hash="deadbeef",
        created_at=datetime.now(timezone.utc),
    )
    # frozen=True → AttributeError on any field mutation.
    try:
        bot.name = "Other"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("SpaceBot should be frozen")


def test_error_hierarchy():
    """Typed exceptions map to distinct HTTP statuses via BaseView._iter."""
    assert issubclass(SpaceBotNotFoundError, KeyError)
    assert issubclass(SpaceBotSlugTakenError, SpaceBotError)
    assert issubclass(SpaceBotDisabledError, SpaceBotError)


def test_bot_constants_are_sensible():
    """Bounds are tight enough to reject obvious garbage but loose enough for UI use."""
    assert BOT_TOKEN_PREFIX == "shb_"
    assert MAX_BOT_SLUG_LEN == 32
    assert MAX_BOT_NAME_LEN == 48
