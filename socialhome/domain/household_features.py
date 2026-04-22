"""Household feature toggles (§22)."""

from __future__ import annotations

from dataclasses import dataclass


class FeatureDisabledError(Exception):
    """Raised when a household admin has disabled a section / post type.

    The route layer maps this to HTTP 403 via ``BaseView._iter`` (see
    ``routes/base.py``). The ``section`` attribute lets clients render
    a targeted "Feature disabled by your household admin" message.
    """

    def __init__(self, section: str) -> None:
        super().__init__(f"Feature '{section}' is disabled for this household")
        self.section = section


#: Feature sections (one per toggleable UI surface).
SECTIONS: tuple[str, ...] = (
    "feed",
    "pages",
    "tasks",
    "stickies",
    "calendar",
    "bazaar",
)

#: Post types mapped to their ``allow_*`` attribute names.
POST_TYPE_ALLOW: dict[str, str] = {
    "text": "allow_text",
    "image": "allow_image",
    "video": "allow_video",
    "file": "allow_file",
    "poll": "allow_poll",
    "schedule": "allow_schedule",
    "bazaar": "allow_bazaar",
}


@dataclass(slots=True, frozen=True)
class HouseholdFeatures:
    """Household-wide feature toggles + post-type allowlist."""

    household_name: str = "Home"
    feat_feed: bool = True
    feat_pages: bool = True
    feat_tasks: bool = True
    feat_stickies: bool = True
    feat_calendar: bool = True
    feat_bazaar: bool = True
    allow_text: bool = True
    allow_image: bool = True
    allow_video: bool = True
    allow_file: bool = True
    allow_poll: bool = True
    allow_schedule: bool = True
    allow_bazaar: bool = True

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
