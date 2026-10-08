"""The one uniform "not available" reply for the anonymous public-content
streaming routes (§highlights_public, §Momentum-public).

The GFS must not be an author-presence oracle. The anonymous viewer
routes (``/gfs/highlight_rtc/{offer,relay}``, ``/gfs/moment_rtc/{offer,relay}``)
therefore answer every non-success state — unknown author, unknown or
withdrawn item, author's household not connected, stream could not be
brokered, author never started streaming — with the SAME response: same
status, same body, same headers. Only genuine success differs, and that is
the irreducible residual: content streamed live from the author's
household shows the household was connected at that moment
(``docs/principles.md``).

:func:`unavailable_at` additionally holds the reply until a caller-chosen
deadline. The relay GETs use it so every failure lands after the same
author-connect budget the online-but-stalled branch has to wait anyway —
otherwise "offline" (instant) and "online, never streamed" (the full
budget) would be told apart by latency alone.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

#: Status of the uniform reply. 503 is what both public viewers already
#: render as "not available right now".
UNAVAILABLE_STATUS: int = 503

#: Body of the uniform reply.
UNAVAILABLE_BODY: dict[str, str] = {"error": "unavailable"}


def unavailable_response() -> web.Response:
    """Build the uniform ``503 {"error": "unavailable"}`` reply.

    ``Cache-Control: no-store`` matches the success stream's header so an
    intermediary never pins a transient state.
    """
    return web.json_response(
        UNAVAILABLE_BODY,
        status=UNAVAILABLE_STATUS,
        headers={"Cache-Control": "no-store"},
    )


async def unavailable_at(deadline: float) -> web.Response:
    """Sleep until the event-loop time ``deadline``, then return
    :func:`unavailable_response`. A deadline already in the past returns
    immediately."""
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining > 0:
        await asyncio.sleep(remaining)
    return unavailable_response()
