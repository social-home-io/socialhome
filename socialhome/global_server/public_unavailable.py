"""The one uniform "not available" reply for the anonymous public-content
streaming routes (§highlights_public, §Momentum-public).

The anonymous viewer routes (``/gfs/highlight_rtc/{offer,relay}``,
``/gfs/moment_rtc/{offer,relay}``) answer every failure with the SAME
response: same status, same body, same headers. That covers an unknown
author, an unknown or withdrawn item, an author's household that is not
connected, a stream that could not be brokered, and an author that never
started streaming. So unknown, revoked and offline cannot be told apart.

Success is different, and that can't be avoided: an offer accepted with
``201`` shows the author's household is connected, because the GFS only
pushes offers to a connected household (``docs/principles.md``).

The failure reply goes out at once, with no added delay. Anyone who can
call a relay can call the matching offer, which already answers 201
(online) or 503 immediately. A latency floor would therefore hide nothing,
and it would let anonymous callers hold a task and a socket open for the
whole budget.
"""

from __future__ import annotations

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
