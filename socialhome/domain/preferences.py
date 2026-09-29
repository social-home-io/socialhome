"""Preferences — household-wide settings + per-user toggles.

A single ``preferences`` table holds both scopes. Rows are keyed by
``id``: ``'household'`` for the household-wide row (admin-controlled)
or the user's literal ``user_id`` for per-user rows. The service layer
enforces the scope policy via :data:`PREFERENCE_SCOPE`.

Reading model is an overlay: compile-time defaults from the dataclass
field defaults form the baseline; the matching row (if present)
overrides them.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Scope policy ──────────────────────────────────────────────────────
#
# Every preference field belongs to exactly one scope. The
# :class:`PreferencesService` consults this map to (a) reject mutations
# from the wrong scope (an admin can't accidentally flip a user-scope
# field on the household row, and a user can't promote a household-
# scope field to user scope) and (b) decide which row id to look up
# when reading.

PREFERENCE_SCOPE: dict[str, str] = {
    # Household-wide (row id = 'household')
    "household_name": "household",
    "tz": "household",
    "feat_feed": "household",
    "feat_pages": "household",
    "feat_tasks": "household",
    "feat_stickies": "household",
    "feat_calendar": "household",
    "feat_presence": "household",
    "feat_gallery": "household",
    "allow_text": "household",
    "allow_image": "household",
    "allow_video": "household",
    "allow_file": "household",
    "allow_poll": "household",
    "allow_schedule": "household",
    "allow_location": "household",
    "allow_highlight_share": "household",
    "allow_link_preview": "household",
    # Per-user (row id = <user_id>)
    "hide_highlights": "user",
    "hide_momentum": "user",
    "hide_bazaar": "user",
}

#: Feature sections (one per toggleable UI surface).
#:
#: ``bazaar`` is intentionally absent — bazaar listings are a
#: per-space feature (gated by ``space.features.bazaar`` /
#: ``space.features.allow_bazaar``); the household-level feed never
#: surfaces bazaar posts, so a household-wide toggle has no effect
#: and only confuses operators. The Bazaar tab in the SPA is
#: always visible (like Spaces) — it lists the user's
#: space-scoped listings.
#:
#: ``highlights`` and ``momentum`` are absent here too — they are
#: per-user surfaces now (hide_highlights / hide_momentum on
#: :class:`UserPreferences`), not household-wide toggles.
SECTIONS: tuple[str, ...] = (
    "feed",
    "pages",
    "tasks",
    "stickies",
    "calendar",
    "presence",
    "gallery",
)

#: Post types mapped to their ``allow_*`` attribute names. Bazaar
#: posts are space-only and never enter the household feed, so
#: ``allow_bazaar`` has no household-level meaning.
POST_TYPE_ALLOW: dict[str, str] = {
    "text": "allow_text",
    "image": "allow_image",
    "video": "allow_video",
    "file": "allow_file",
    "poll": "allow_poll",
    "schedule": "allow_schedule",
    "location": "allow_location",
    "highlight_share": "allow_highlight_share",
}


class FeatureDisabledError(Exception):
    """Raised when a household admin has disabled a section / post type.

    The route layer maps this to HTTP 403 via ``BaseView._iter`` (see
    ``routes/base.py``). The ``section`` attribute lets clients render
    a targeted "Feature disabled by your household admin" message.
    """

    def __init__(self, section: str) -> None:
        super().__init__(f"Feature '{section}' is disabled for this household")
        self.section = section


@dataclass(slots=True, frozen=True)
class HouseholdPreferences:
    """Household-scope preferences. Row id = 'household'."""

    household_name: str = "Home"
    #: IANA timezone name (``"Europe/Berlin"``) — the household's "home"
    #: wall-clock anchor for the calendar. Falls back to ``"UTC"`` at
    #: install. In ``ha`` and ``haos`` modes the HA REST adapter mirrors
    #: ``core.config.time_zone`` here at startup; in ``standalone`` mode
    #: the setup wizard collects it from the operator. Every personal /
    #: space calendar event resolves through this column at the floor of
    #: the event-creation fallback chain.
    tz: str = "UTC"

    feat_feed: bool = True
    feat_pages: bool = True
    feat_tasks: bool = True
    feat_stickies: bool = True
    feat_calendar: bool = True
    feat_presence: bool = True
    feat_gallery: bool = True

    allow_text: bool = True
    allow_image: bool = True
    allow_video: bool = True
    allow_file: bool = True
    allow_poll: bool = True
    allow_schedule: bool = True
    allow_location: bool = True
    allow_highlight_share: bool = True
    #: Build a preview card for the first link in a new text post (the
    #: author's household fetches the page once; see
    #: ``services.link_preview_service``). Not a post type — a household
    #: that turns it off still accepts previews other households send.
    allow_link_preview: bool = True

    def is_enabled(self, section: str) -> bool:
        """``True`` if the ``feat_{section}`` toggle is on."""
        attr = f"feat_{section}"
        if section not in SECTIONS or not hasattr(self, attr):
            # Unknown section → refuse to claim it's enabled; callers
            # would otherwise silently leak new features past an
            # out-of-date toggle set. Better to 403 explicitly.
            return False
        return bool(getattr(self, attr))

    def allows_post_type(self, post_type: str) -> bool:
        """``True`` if the household allows creating posts of this type."""
        attr = POST_TYPE_ALLOW.get(post_type)
        if attr is None:
            return False
        return bool(getattr(self, attr))

    def require_enabled(self, section: str) -> None:
        """Raise :class:`FeatureDisabledError` if *section* is disabled."""
        if not self.is_enabled(section):
            raise FeatureDisabledError(section)

    def require_post_type(self, post_type: str) -> None:
        """Raise :class:`FeatureDisabledError` if the household disallows
        creating posts of *post_type* (spec §23.13)."""
        if not self.allows_post_type(post_type):
            raise FeatureDisabledError(f"post_type:{post_type}")


@dataclass(slots=True, frozen=True)
class UserPreferences:
    """Per-user preferences. Row id = the user's user_id."""

    user_id: str
    hide_highlights: bool = False
    hide_momentum: bool = False
    hide_bazaar: bool = False
