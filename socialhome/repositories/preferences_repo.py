"""Preferences repo — household-wide + per-user toggles (§22 / §25).

Wraps the SQL surface used by :class:`PreferencesService` so the
service depends only on the abstract protocol — never on raw SQL or
the SQLite implementation.

Two row shapes share the single ``preferences`` table:

* ``id='household'`` — admin-controlled household-wide row.
* ``id=<user_id>`` — per-user toggles owned by the individual user.

Reading model: compile-time dataclass defaults form the baseline;
the matching row (if present) overrides them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..db import AsyncDatabase
from ..domain.preferences import (
    PREFERENCE_SCOPE,
    HouseholdPreferences,
    UserPreferences,
)

HOUSEHOLD_ROW_ID = "household"

# Column allow-lists guard the f-string UPDATE paths against SQL injection.
# The service layer validates keys against PREFERENCE_SCOPE *before* calling
# these methods; these constants are a second line of defence.
_HOUSEHOLD_KEYS: frozenset[str] = frozenset(
    k for k, scope in PREFERENCE_SCOPE.items() if scope == "household"
)
_USER_KEYS: frozenset[str] = frozenset(
    k for k, scope in PREFERENCE_SCOPE.items() if scope == "user"
)


@runtime_checkable
class AbstractPreferencesRepo(Protocol):
    async def get_household(self) -> HouseholdPreferences: ...
    async def get_user(self, user_id: str) -> UserPreferences: ...
    async def ensure_row(self, row_id: str) -> None: ...
    async def set_household_value(self, key: str, value: object) -> None: ...
    async def set_user_value(self, user_id: str, key: str, value: object) -> None: ...


class SqlitePreferencesRepo:
    """SQLite-backed :class:`AbstractPreferencesRepo`."""

    __slots__ = ("_db",)

    def __init__(self, db: AsyncDatabase) -> None:
        self._db = db

    async def get_household(self) -> HouseholdPreferences:
        row = await self._db.fetchone(
            "SELECT * FROM preferences WHERE id = ?",
            (HOUSEHOLD_ROW_ID,),
        )
        if row is None:
            return HouseholdPreferences()
        return HouseholdPreferences(
            household_name=str(row["household_name"]),
            tz=str(row["tz"]),
            feat_feed=bool(row["feat_feed"]),
            feat_pages=bool(row["feat_pages"]),
            feat_tasks=bool(row["feat_tasks"]),
            feat_stickies=bool(row["feat_stickies"]),
            feat_calendar=bool(row["feat_calendar"]),
            feat_presence=bool(row["feat_presence"]),
            feat_gallery=bool(row["feat_gallery"]),
            feat_timetable=bool(row["feat_timetable"]),
            allow_text=bool(row["allow_text"]),
            allow_image=bool(row["allow_image"]),
            allow_video=bool(row["allow_video"]),
            allow_file=bool(row["allow_file"]),
            allow_poll=bool(row["allow_poll"]),
            allow_schedule=bool(row["allow_schedule"]),
            allow_location=bool(row["allow_location"]),
            allow_highlight_share=bool(row["allow_highlight_share"]),
            allow_link_preview=bool(row["allow_link_preview"]),
        )

    async def get_user(self, user_id: str) -> UserPreferences:
        row = await self._db.fetchone(
            "SELECT * FROM preferences WHERE id = ?",
            (user_id,),
        )
        if row is None:
            return UserPreferences(user_id=user_id)
        return UserPreferences(
            user_id=user_id,
            hide_highlights=bool(row["hide_highlights"]),
            hide_momentum=bool(row["hide_momentum"]),
            hide_bazaar=bool(row["hide_bazaar"]),
        )

    async def ensure_row(self, row_id: str) -> None:
        """Insert the row with table-level defaults if it does not yet exist.

        Idempotent — ``INSERT OR IGNORE`` is a no-op when the row already
        exists.
        """
        await self._db.enqueue(
            "INSERT OR IGNORE INTO preferences(id) VALUES(?)",
            (row_id,),
        )

    async def set_household_value(self, key: str, value: object) -> None:
        """Update one column on the household row.

        Callers (service layer) are responsible for validating *key* against
        :data:`~socialhome.domain.preferences.PREFERENCE_SCOPE` before
        calling here; the ``_HOUSEHOLD_KEYS`` allow-list is a second line of
        defence against SQL injection.
        """
        if key not in _HOUSEHOLD_KEYS:
            raise KeyError(f"unknown household preference key: {key!r}")
        # Static allow-list above gates the column name — no SQL injection.
        await self._db.enqueue(
            f"UPDATE preferences SET {key} = ? WHERE id = ?",
            (value, HOUSEHOLD_ROW_ID),
        )

    async def set_user_value(self, user_id: str, key: str, value: object) -> None:
        """Update one column on a per-user preferences row.

        Callers (service layer) are responsible for validating *key* against
        :data:`~socialhome.domain.preferences.PREFERENCE_SCOPE` before
        calling here; the ``_USER_KEYS`` allow-list provides a second line of
        defence against SQL injection.
        """
        if key not in _USER_KEYS:
            raise KeyError(f"unknown user preference key: {key!r}")
        # Static allow-list above gates the column name — no SQL injection.
        await self._db.enqueue(
            f"UPDATE preferences SET {key} = ? WHERE id = ?",
            (value, user_id),
        )
