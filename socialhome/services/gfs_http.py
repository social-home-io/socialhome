"""Bounded JSON reads for GFS HTTP responses.

A paired Global Federation Server is a *remote* party: pairing means we
accept its directory, not that we trust its bytes. ``aiohttp`` applies no
default cap to a response body, so ``await resp.json()`` on a hostile or
compromised GFS happily buffers a multi-gigabyte body into household
memory — a one-request OOM against every home that paired with it.

:func:`read_json_capped` is the single read path for every GFS response
this household parses. It refuses an over-large body *before* parsing it,
and it never trusts ``Content-Length`` on its own: the header is
attacker-supplied, so the actual read is bounded too (``limit + 1`` bytes,
rejected when the extra byte materialises).

The caps are generous on purpose. A single space listing legitimately
carries a base64 ``data:image/webp`` icon, and a directory carries one per
space, so the limits are sized for "a lot of icons", not for a lean JSON
document. Alongside the byte cap the directory poll also caps the number
of *items* it imports per tick (:data:`MAX_GFS_DIRECTORY_ITEMS`) — a body
under the byte limit can still hold a very large number of tiny rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)

#: Cap for a single-space metadata body (``GET /gfs/spaces/{id}``). Room
#: for a base64 icon + cover and generous text fields, nothing more.
MAX_GFS_BODY_BYTES: int = 5 * 1024 * 1024

#: Cap for the public-space directory (``GET /gfs/spaces``) — the same
#: per-space payload, many times over.
MAX_GFS_DIRECTORY_BODY_BYTES: int = 32 * 1024 * 1024

#: Cap on directory rows imported per poll tick, independent of the byte
#: cap: a 32 MB body can still carry a million minimal listings.
MAX_GFS_DIRECTORY_ITEMS: int = 2000


async def read_json_capped(resp: Any, *, url: str, limit: int) -> Any | None:
    """Parse *resp*'s JSON body, or return ``None`` when it is unusable.

    ``None`` means "treat this as a failed fetch": the body was larger than
    *limit* bytes (by the declared ``Content-Length`` or by what actually
    arrived), or it was not valid JSON. Callers decide whether that is
    fail-soft (skip this GFS, retry next tick) or fail-closed (seat
    nothing).
    """
    declared = getattr(resp, "content_length", None)
    if declared is not None and declared > limit:
        log.warning(
            "gfs_http: %s declares a %d-byte body (cap %d) — refusing to read it",
            url,
            declared,
            limit,
        )
        return None
    raw = await resp.content.read(limit + 1)
    if len(raw) > limit:
        log.warning(
            "gfs_http: %s returned more than %d bytes — refusing to parse it",
            url,
            limit,
        )
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        log.warning("gfs_http: %s returned an unparsable body: %s", url, exc)
        return None
