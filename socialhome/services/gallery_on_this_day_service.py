"""On this day — one card at the top of the household gallery (D14).

Photos taken on today's calendar day in an earlier year, from one album
the person can see, bring back a memory. Owner decision D14 (2026-11-02):

* on by default, as **one card**, **never a notification** — nothing here
  publishes an event or writes a notification;
* each person can turn it off, hide it for the day (✕) and leave an album
  out for good ("don't show this album again");
* private albums are never used — only household albums open to everyone
  at home (``audience = 'everyone'``), never a space's.

The settings are the person's ``users.preferences_json``
``gallery_on_this_day`` block (:func:`parse_gallery_on_this_day`) — a
handful of keys and a bounded album list, so no table.

"Today" is the viewer's own calendar day (the SPA sends it); it may differ
from the server's UTC day by one either way, never more.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import replace
from datetime import UTC, date, datetime

from ..domain.gallery import (
    ON_THIS_DAY_MAX_EXCLUDED,
    ON_THIS_DAY_PREVIEWS,
    OnThisDayCard,
    OnThisDaySettings,
)
from ..repositories.gallery_repo import AbstractGalleryRepo
from ..repositories.user_repo import AbstractUserRepo
from .gallery_service import GalleryNotFoundError, GalleryService
from .user_preferences import GALLERY_ON_THIS_DAY_KEY, parse_gallery_on_this_day

log = logging.getLogger(__name__)


def resolve_today(raw: str | None, *, now: datetime | None = None) -> date:
    """The viewer's calendar day: *raw* (``YYYY-MM-DD``) when it is
    within a day of the UTC date — every time zone is — else
    :class:`ValueError`; no *raw* is the UTC date."""
    utc_today = (now or datetime.now(UTC)).date()
    if not raw:
        return utc_today
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        raise ValueError("date must be YYYY-MM-DD") from None
    if abs((day - utc_today).days) > 1:
        raise ValueError("date must be today")
    return day


def month_days(today: date) -> tuple[str, ...]:
    """The ``MM-DD`` days *today* remembers: its own, plus 29 February on
    the 28th of a year without one — a leap-day photo still comes back."""
    md = today.strftime("%m-%d")
    if md == "02-28" and not calendar.isleap(today.year):
        return (md, "02-29")
    return (md,)


class GalleryOnThisDayService:
    """Finds the person's "On this day" card and keeps their settings."""

    __slots__ = ("_gallery", "_repo", "_users")

    def __init__(
        self,
        repo: AbstractGalleryRepo,
        user_repo: AbstractUserRepo,
        gallery: GalleryService,
    ) -> None:
        self._repo = repo
        self._users = user_repo
        # The album as the viewer sees it (cover, default-album name).
        self._gallery = gallery

    async def settings(self, user_id: str) -> OnThisDaySettings:
        user = await self._users.get_by_user_id(user_id)
        return parse_gallery_on_this_day(user.preferences_json if user else None)

    async def card(self, *, user_id: str, today: date) -> OnThisDayCard | None:
        """Today's card for *user_id*, or ``None``: turned off, hidden
        for *today*, or nothing taken on this day in an earlier year."""
        prefs = await self.settings(user_id)
        if not prefs.enabled or prefs.hidden_on == today.isoformat():
            return None
        days = month_days(today)
        found = await self._repo.find_on_this_day(
            days,
            before_year=f"{today.year:04d}",
            viewer_user_id=user_id,
            excluded_album_ids=prefs.excluded_album_ids,
        )
        if found is None:
            return None
        album_id, year, count = found
        try:
            album = await self._gallery.get_album(album_id, actor_user_id=user_id)
        except GalleryNotFoundError:
            return None
        items = await self._repo.list_on_this_day_items(
            album_id,
            year=year,
            month_days=days,
            viewer_user_id=user_id,
            limit=ON_THIS_DAY_PREVIEWS,
        )
        if not items:
            return None
        return OnThisDayCard(
            album=album,
            date=items[0].day,
            years_ago=today.year - int(year),
            count=count,
            items=tuple(items),
        )

    async def update(
        self,
        user_id: str,
        *,
        enabled: bool | None = None,
        hide_on: date | None = None,
        show_again: bool = False,
        exclude_album_id: str | None = None,
        include_album_id: str | None = None,
        include_all: bool = False,
    ) -> OnThisDaySettings:
        """Change the person's settings and return them:

        * *enabled* — turn the card on or off;
        * *hide_on* — ✕: hide it for that day (the viewer's today);
        * *show_again* — undo the ✕;
        * *exclude_album_id* / *include_album_id* — leave an album out
          ("don't show this album again") or let it back in;
        * *include_all* — let every left-out album back in.

        Leaving out an album the person can't see is refused like an
        unknown one (``GalleryNotFoundError``), so it is no oracle."""
        current = await self.settings(user_id)
        changed = current
        if enabled is not None:
            changed = replace(changed, enabled=enabled)
        if hide_on is not None:
            changed = replace(changed, hidden_on=hide_on.isoformat())
        if show_again:
            changed = replace(changed, hidden_on=None)
        if exclude_album_id:
            await self._gallery.get_album(exclude_album_id, actor_user_id=user_id)
            changed = replace(
                changed,
                excluded_album_ids=changed.excluded_album_ids | {exclude_album_id},
            )
        if include_album_id:
            changed = replace(
                changed,
                excluded_album_ids=changed.excluded_album_ids - {include_album_id},
            )
        if include_all:
            changed = replace(changed, excluded_album_ids=frozenset())
        if changed == current:
            return current
        if len(changed.excluded_album_ids) > ON_THIS_DAY_MAX_EXCLUDED:
            raise ValueError(
                f"at most {ON_THIS_DAY_MAX_EXCLUDED} albums can be left out"
            )
        await self._users.set_preference_key(
            user_id, GALLERY_ON_THIS_DAY_KEY, _to_blob(changed)
        )
        return changed


def _to_blob(s: OnThisDaySettings) -> dict:
    """The ``gallery_on_this_day`` block as stored — only what differs
    from the defaults."""
    blob: dict = {}
    if not s.enabled:
        blob["off"] = True
    if s.hidden_on:
        blob["hidden_on"] = s.hidden_on
    if s.excluded_album_ids:
        blob["excluded_albums"] = sorted(s.excluded_album_ids)
    return blob
