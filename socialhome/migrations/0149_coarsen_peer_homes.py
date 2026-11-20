"""0149 — a paired household's home is kept only as a ~3 km area.

Follows 0148 (one pending admin elevation).

Until now a paired household with ``share_home`` on sent its home at 4 dp
(about 11 m — the house) and we stored it so in
``remote_instances.home_lat`` / ``home_lon``. From this release a home
leaves a household only as the centre of the ~3 km grid cell it lies in
(``socialhome.domain.home_location.coarse_home_for_peer``, owner decision
2026-11-20), and every write of the two columns coarsens again. This
migration does the same to the homes already received, so no precise
peer home stays on disk. A half-set or out-of-range pair (never written
by Social Home) is cleared: no usable home means no home, never a precise
one left behind. Our own home (``instance_identity``) stays at 4 dp.

A Python migration because SQL has no ``floor`` / ``cos`` on every
SQLite build. Self-contained (no app imports) so a later edit of the
helper cannot change what this migration did; a test checks that the
copy below agrees with ``coarse_home_for_peer``.

CLAUDE.md "audit before a migration":

  (1) Audited: ``remote_instances.home_lat`` / ``home_lon`` are the only
      place a peer's home is stored (written by the pairing handshake,
      ``LOCAL_HOME_LOCATION_CHANGED`` and the confirm / seat-upgrade
      rebuilds, all in ``repositories/federation_repo.py``; read by the
      Connections and Friends maps). ``instance_identity`` holds our own
      home and stays 4 dp; zones, presence and space pins are other
      data the owner kept as they are.
  (2) Alternatives rejected: coarsening only at read time (the precise
      homes would stay on disk and in every backup / export); waiting for
      each peer's next ``LOCAL_HOME_LOCATION_CHANGED`` (sent only when a
      home moves — most rows would keep 4 dp for good).
  (3) Smallest change: values only, no shape change — one UPDATE per row
      that holds a home; a no-op on a database with no peer homes.
"""

from __future__ import annotations

import math
import sqlite3

#: ``PEER_HOME_GRID_DEG`` and its helpers, verbatim as of this migration.
_GRID = 0.03
_MIN_COS_LAT = 0.1
_DP = 4


def _usable(lat: object, lon: object) -> tuple[float, float] | None:
    if lat is None or lon is None:
        return None
    values: list[float] = []
    for raw, limit in ((lat, 90.0), (lon, 180.0)):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        value = float(raw)
        if not math.isfinite(value) or abs(value) > limit:
            return None
        values.append(round(value, _DP))
    if values[0] == 0.0 and values[1] == 0.0:
        return None
    return values[0] + 0.0, values[1] + 0.0


def _coarse(lat: object, lon: object) -> tuple[float, float] | None:
    point = _usable(lat, lon)
    if point is None:
        return None
    la, lo = point
    lat_cells = round(180.0 / _GRID)
    lat_cell = min(math.floor((la + 90.0) / _GRID), lat_cells - 1)
    lat_centre = -90.0 + (lat_cell + 0.5) * _GRID
    cos_lat = max(math.cos(math.radians(lat_centre)), _MIN_COS_LAT)
    lon_cells = max(1, int(360.0 * cos_lat / _GRID))
    lon_width = 360.0 / lon_cells
    lon_cell = min(math.floor((lo + 180.0) / lon_width), lon_cells - 1)
    lon_centre = -180.0 + (lon_cell + 0.5) * lon_width
    return round(lat_centre, _DP) + 0.0, round(lon_centre, _DP) + 0.0


def migrate(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, home_lat, home_lon FROM remote_instances"
        " WHERE home_lat IS NOT NULL OR home_lon IS NOT NULL"
    ).fetchall()
    for iid, lat, lon in rows:
        coarse = _coarse(lat, lon)
        new_lat, new_lon = coarse if coarse is not None else (None, None)
        if (new_lat, new_lon) != (lat, lon):
            conn.execute(
                "UPDATE remote_instances SET home_lat=?, home_lon=? WHERE id=?",
                (new_lat, new_lon, iid),
            )
