"""Tiny helper for parsing the ``users.preferences_json`` JSON blob.

The blob is owned by the frontend (free-form keys), but a handful of
backend services read it: notification preferences, retention, etc.
This module centralises parsing so callers don't reinvent the
defensive defaults each time.

Shapes so far: :class:`HighlightsPreferences`, :class:`MomentPreferences`,
the timetable home pins and the Home Assistant automation opt-in; add more
as they appear.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import orjson

from ..domain.highlight import HighlightAudience
from ..domain.moment import (
    MOMENT_DEFAULT_RETENTION_DAYS,
    MOMENT_MAX_LIFETIME_DAYS,
    MOMENT_MIN_RETENTION_DAYS,
)
from ..domain.notification_kinds import NotifSettings

log = logging.getLogger(__name__)


#: Default "how many days to keep a highlight listed" if the user has not
#: configured a value. Long enough to feel different from WhatsApp's
#: 24-hour wipe; short enough that the inbox doesn't grow unbounded.
DEFAULT_HIGHLIGHTS_RETENTION_DAYS: int = 30
#: Default ceiling on stored highlights per author. The retention scheduler
#: prunes the oldest beyond this count even if they have not yet expired.
DEFAULT_HIGHLIGHTS_MAX_COUNT: int = 100

_RETENTION_MIN: int = 1
_RETENTION_MAX: int = 90
_MAX_COUNT_MIN: int = 10
_MAX_COUNT_MAX: int = 500


@dataclass(slots=True, frozen=True)
class HighlightsPreferences:
    """Parsed ``preferences_json["highlights"]`` block (always populated)."""

    retention_days: int
    max_count: int
    default_audience_kind: HighlightAudience
    default_audience: tuple[str, ...]


def parse_highlights_preferences(preferences_json: str | None) -> HighlightsPreferences:
    """Pull the ``highlights`` block out of a user's preferences blob.

    Always returns a well-formed :class:`HighlightsPreferences` — bad / missing
    inputs fall back to the defaults defined above. Out-of-range numeric
    values are clamped (so a malformed blob can never DOS the retention
    scheduler with e.g. ``retention_days = 10**9``).
    """
    blob: dict[str, Any] = {}
    try:
        blob = orjson.loads(preferences_json or "{}") if preferences_json else {}
    except orjson.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; using defaults")
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    raw = blob.get("highlights")
    if not isinstance(raw, dict):
        raw = {}

    retention_days = _coerce_int(
        raw.get("retention_days"),
        DEFAULT_HIGHLIGHTS_RETENTION_DAYS,
        _RETENTION_MIN,
        _RETENTION_MAX,
    )
    max_count = _coerce_int(
        raw.get("max_count"),
        DEFAULT_HIGHLIGHTS_MAX_COUNT,
        _MAX_COUNT_MIN,
        _MAX_COUNT_MAX,
    )

    audience_block = raw.get("default_audience")
    if not isinstance(audience_block, dict):
        audience_block = {"kind": "all_paired"}
    try:
        kind = HighlightAudience(audience_block.get("kind") or "all_paired")
    except ValueError:
        kind = HighlightAudience.ALL_PAIRED
    ids_raw = audience_block.get("ids")
    if not isinstance(ids_raw, list):
        ids_raw = []
    ids: tuple[str, ...] = tuple(str(x) for x in ids_raw if x)

    return HighlightsPreferences(
        retention_days=retention_days,
        max_count=max_count,
        default_audience_kind=kind,
        default_audience=ids if kind is not HighlightAudience.ALL_PAIRED else (),
    )


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except TypeError, ValueError:
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


# ─── Momentum visibility prefs (§Momentum-relay-policy) ─────────────────────


#: Default per-user max-hops visibility — matches the global wire cap so
#: new accounts see every moment that reached this instance.
DEFAULT_MOMENT_MAX_HOPS: int = 3


@dataclass(slots=True, frozen=True)
class MomentPreferences:
    """Per-user knobs for the Momentum pillar.

    ``max_hops`` filters what the user sees; ``follower_retention_days``
    is how long people who follow them keep their new moments (decision 6d)
    — it sets the signed ``expires_at`` of every moment they write.
    """

    max_hops: int
    follower_retention_days: int = MOMENT_DEFAULT_RETENTION_DAYS


def parse_moment_preferences(preferences_json: str | None) -> MomentPreferences:
    """Pull the ``moments`` block out of a user's preferences blob.

    Returns a well-formed :class:`MomentPreferences` for any input.
    ``max_hops`` clamps to ``[1, 3]`` since the wire cap is 3 and a value
    < 1 would hide every moment including the user's own;
    ``follower_retention_days`` clamps to ``[1, 30]`` (30 is what every
    v_65 receiver keeps at most).
    """
    blob: dict[str, Any] = {}
    try:
        blob = orjson.loads(preferences_json or "{}") if preferences_json else {}
    except orjson.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; using defaults")
        blob = {}
    if not isinstance(blob, dict):
        blob = {}
    raw = blob.get("moments")
    if not isinstance(raw, dict):
        raw = {}
    max_hops = _coerce_int(raw.get("max_hops"), DEFAULT_MOMENT_MAX_HOPS, 1, 3)
    retention = _coerce_int(
        raw.get("follower_retention_days"),
        MOMENT_DEFAULT_RETENTION_DAYS,
        MOMENT_MIN_RETENTION_DAYS,
        MOMENT_MAX_LIFETIME_DAYS,
    )
    return MomentPreferences(max_hops=max_hops, follower_retention_days=retention)


# ─── Timetable home pins ────────────────────────────────────────────────────


#: The space timetables a user pinned to their Home (the Today card), as
#: ``preferences_json["timetable_home_pins"]`` — written by the SPA. Parsing
#: caps the list so a hostile blob can't make the home read unbounded.
MAX_TIMETABLE_HOME_PINS: int = 20
_MAX_PIN_LEN: int = 64  # the timetable id cap


def parse_timetable_home_pins(preferences_json: str | None) -> tuple[str, ...]:
    """The user's pinned space-timetable ids, in pin order.

    Never raises: bad JSON, a non-list, non-string / blank / over-long
    entries are ignored; duplicates collapse (first wins); at most
    :data:`MAX_TIMETABLE_HOME_PINS`.
    """
    try:
        blob = orjson.loads(preferences_json) if preferences_json else {}
    except orjson.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; no timetable pins")
        return ()
    raw = blob.get("timetable_home_pins") if isinstance(blob, dict) else None
    if not isinstance(raw, list):
        return ()
    pins = dict.fromkeys(
        p for p in raw if isinstance(p, str) and 0 < len(p) <= _MAX_PIN_LEN
    )
    return tuple(pins)[:MAX_TIMETABLE_HOME_PINS]


#: ``preferences_json["ha_automation_events"]`` — the person's opt-in to
#: have their notifications and their own activity fired on Home
#: Assistant's event bus (``socialhome.notification_new`` and every other
#: personal event in :mod:`socialhome.services.ha_bridge_service`) for
#: automations. Off by default (D14):
#: anyone with HA admin, logbook or automation access sees those events.
HA_AUTOMATION_EVENTS_KEY = "ha_automation_events"


def parse_ha_automation_events(preferences_json: str | None) -> bool:
    """Whether the person opted into Home Assistant automation events.

    ``True`` only for a literal JSON ``true`` — anything else (missing,
    bad JSON, a string, a number) reads as off, so a malformed blob never
    opts someone in.
    """
    try:
        blob = orjson.loads(preferences_json) if preferences_json else {}
    except orjson.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; HA automations off")
        return False
    if not isinstance(blob, dict):
        return False
    return blob.get(HA_AUTOMATION_EVENTS_KEY) is True


#: ``preferences_json`` keys the notification settings own (D4, D10).
NOTIF_KINDS_KEY = "notif_kinds"
NOTIF_MUTED_POSTS_KEY = "notif_muted_posts"


def parse_notif_settings(preferences_json: str | None) -> NotifSettings:
    """The person's notification kind choices and muted posts.

    Never raises: bad JSON reads as the defaults (every kind at its
    default, nothing muted)."""
    try:
        blob = orjson.loads(preferences_json) if preferences_json else {}
    except orjson.JSONDecodeError:
        log.debug("preferences_json is not valid JSON; default notifications")
        return NotifSettings()
    return NotifSettings.from_dict(blob)


__all__ = [
    "DEFAULT_HIGHLIGHTS_MAX_COUNT",
    "DEFAULT_HIGHLIGHTS_RETENTION_DAYS",
    "DEFAULT_MOMENT_MAX_HOPS",
    "HA_AUTOMATION_EVENTS_KEY",
    "MAX_TIMETABLE_HOME_PINS",
    "NOTIF_KINDS_KEY",
    "NOTIF_MUTED_POSTS_KEY",
    "HighlightsPreferences",
    "MomentPreferences",
    "parse_ha_automation_events",
    "parse_highlights_preferences",
    "parse_moment_preferences",
    "parse_notif_settings",
    "parse_timetable_home_pins",
]
