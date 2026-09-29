"""Content shape of a ``type='location'`` DM message.

A location message carries a one-shot pin inside ``content`` as a JSON
object — the same ``{lat, lon, label}`` shape as a location post
(:class:`socialhome.domain.post.LocationData`), plus an optional coarse
``accuracy_m``::

    {"lat": 52.3702, "lon": 4.8952, "label": "Marina", "accuracy_m": 50}

:func:`normalise_location_content` is the single authority on that
shape. Every path that stores or federates a location message runs it
first — the local send, an edit, an inbound ``DM_MESSAGE`` and a
``DM_HISTORY_CHUNK`` row — so raw device precision never reaches the
database or the wire (CLAUDE.md: GPS truncated to 4 decimal places):

* ``lat`` / ``lon`` are rounded to 4 dp (~11 m) and range-checked.
* ``accuracy_m`` is rounded *up* to a coarse bucket, so it can neither
  claim more precision than the fix had nor fingerprint the device.
* ``label`` is trimmed, stripped of control characters and capped.
* Anything else in the object is dropped.

Malformed content raises :class:`ValueError` (mapped to 422 on the
local API; an inbound event carrying it is refused).
"""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass

#: Label cap — matches the location-post label cap (``LOCATION_LABEL_MAX``).
DM_LOCATION_LABEL_MAX: int = 80

#: Coarse accuracy buckets in metres. The first bucket sits above the
#: ~11 m resolution of a 4-dp coordinate; anything past the last bucket
#: clamps to it.
ACCURACY_BUCKETS_M: tuple[int, ...] = (25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)

#: Upper bound on the raw ``content`` string before it is parsed. A
#: canonical location object is well under 200 bytes; this only stops
#: a peer from handing the JSON parser something huge.
_MAX_RAW_LEN: int = 1000


@dataclass(slots=True, frozen=True)
class DmLocation:
    """A validated, rounded DM location pin."""

    lat: float
    lon: float
    label: str | None = None
    accuracy_m: int | None = None

    def to_content(self) -> str:
        """Canonical compact JSON for ``ConversationMessage.content``."""
        return json.dumps(
            {
                "lat": self.lat,
                "lon": self.lon,
                "label": self.label,
                "accuracy_m": self.accuracy_m,
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )


def _reject_constant(name: str) -> float:
    raise ValueError(f"location coordinate must be finite, got {name}")


def _coord(raw: object, name: str, limit: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"location {name} must be a number")
    value = float(raw)
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise ValueError(f"location {name} out of range")
    # ``+ 0.0`` folds a rounded ``-0.0`` into ``0.0``.
    return round(value, 4) + 0.0


def _label(raw: object) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("location label must be a string")
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) != "Cc").strip()
    if not cleaned:
        return None
    if len(cleaned) > DM_LOCATION_LABEL_MAX:
        raise ValueError(
            f"location label exceeds {DM_LOCATION_LABEL_MAX} characters",
        )
    return cleaned


def _accuracy(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("location accuracy_m must be a number")
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("location accuracy_m out of range")
    for bucket in ACCURACY_BUCKETS_M:
        if value <= bucket:
            return bucket
    return ACCURACY_BUCKETS_M[-1]


def parse_location_content(content: str) -> DmLocation:
    """Parse + validate + round a location message's ``content``."""
    if not isinstance(content, str) or len(content) > _MAX_RAW_LEN:
        raise ValueError("location content must be a short JSON object")
    try:
        data = json.loads(content, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError("location content must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("location content must be a JSON object")
    if "lat" not in data or "lon" not in data:
        raise ValueError("location content requires lat and lon")
    return DmLocation(
        lat=_coord(data["lat"], "lat", 90.0),
        lon=_coord(data["lon"], "lon", 180.0),
        label=_label(data.get("label")),
        accuracy_m=_accuracy(data.get("accuracy_m")),
    )


def normalise_location_content(content: str) -> str:
    """Return the canonical, rounded ``content`` for a location message.

    Raises :class:`ValueError` when ``content`` isn't a valid location.
    """
    return parse_location_content(content).to_content()
