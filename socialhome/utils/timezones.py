"""IANA timezone validation — one shared check for every tz boundary.

Calendar rows carry an IANA zone name (``calendar_events.tz``) that the
SPA feeds straight to ``Intl``. ``Intl`` raises ``RangeError`` on an
unknown name, and a single bad row takes the whole calendar view down
with it — so every zone name entering the system (an API body, a
federation payload) is validated against the local tz database before
it is persisted.

Two entry points, matching the two policies already in the codebase:

* :func:`is_valid_tz` — a bare predicate, for call sites that have their
  own fallback chain (``CalendarService._resolve_personal_tz``) or that
  reject with a domain error (``update_event``).
* :func:`coerce_tz` — validate-or-``"UTC"``, for untrusted input where
  the event must still land. Fails closed on the *value*, not the event.
"""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

#: Anchor used whenever a zone name can't be trusted or resolved. Also
#: the ``NOT NULL DEFAULT`` of every ``tz`` column (``0002_calendar_
#: timezone.sql``), so this is the value a pre-tz row already reads back.
DEFAULT_TZ = "UTC"


def is_valid_tz(value: str) -> bool:
    """Return whether ``value`` names a zone in the local IANA database.

    ``ZoneInfo`` raises ``ZoneInfoNotFoundError`` (a ``KeyError``) for an
    unknown-but-well-formed key like ``"Foo/Bar"`` and ``ValueError`` for
    a malformed one (absolute or ``..``-containing paths) — both are just
    "not a zone" here.
    """
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        # Well-formed key that isn't in the database ("Foo/Bar").
        return False
    except ValueError:
        # Malformed key — absolute or ``..``-containing path.
        return False
    except OSError:
        # The key maps to a filesystem path; an overlong one raises
        # ENAMETOOLONG from the tzdata lookup.
        return False
    return True


def coerce_tz(value: object, *, context: str) -> str:
    """Validate an untrusted zone name, falling back to :data:`DEFAULT_TZ`.

    Used at trust boundaries (federation inbound) where a buggy or
    hostile peer can put an arbitrary string on the wire. A rejected
    value logs at WARNING with ``context`` (event type + sending
    instance) so a misbehaving peer is diagnosable rather than silent.
    """
    name = str(value or DEFAULT_TZ)
    if is_valid_tz(name):
        return name
    log.warning(
        "%s carried unknown IANA timezone %r — anchoring the row to %s",
        context,
        name,
        DEFAULT_TZ,
    )
    return DEFAULT_TZ
