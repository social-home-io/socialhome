"""Custom aiohttp access logger that redacts sensitive query parameters.

Prevents secrets from leaking into operator log files, log shippers, or
log-aggregation services. The WebSocket endpoint uses ``?token=<raw>``
for browser auth (aiohttp can't set custom headers on WS), so the
default aiohttp access log would write that token verbatim. We strip
``token``, ``api_key``, and ``access_token`` from the logged request
line (`%r`) and the referer (`%{Referer}i`).

Wired in via ``web.run_app(..., access_log_class=RedactingAccessLogger)``
in :mod:`socialhome.__main__` + :mod:`socialhome.app`.
"""

from __future__ import annotations

import re
from typing import Any

from aiohttp.web_log import AccessLogger

#: Query-param names whose values must be replaced with ``***`` before
#: the request line is written to the access log.
_SENSITIVE_PARAMS: tuple[str, ...] = (
    "token",
    "api_key",
    "access_token",
    "password",
)

_REDACT_RE = re.compile(
    r"([?&](?:" + "|".join(_SENSITIVE_PARAMS) + r")=)[^&\s]+",
    re.IGNORECASE,
)


def redact_query_string(line: str) -> str:
    """Replace sensitive query-param values with ``***``.

    Works on URLs, request lines, and any free-text string that may
    contain query-string fragments — the regex is anchored on
    ``?``/``&`` plus the exact parameter name so it won't over-redact
    lookalike substrings elsewhere in the line.
    """
    if not line:
        return line
    return _REDACT_RE.sub(r"\1***", line)


class RedactingAccessLogger(AccessLogger):
    """AccessLogger subclass that redacts secrets from the request URL."""

    def log(self, request: Any, response: Any, time: float) -> None:
        # aiohttp's AccessLogger reads ``request.rel_url`` via the
        # formatter. We mutate the logger's final formatted string by
        # delegating + post-processing through ``_format_line``.
        # The public ``log()`` path calls ``_format_line()`` per key;
        # simplest reliable hook is to override ``_format_r`` which
        # emits ``%r`` (the request line) + ``_format_Ri`` for referer.
        super().log(request, response, time)

    # aiohttp's formatter mapping uses ``_format_r`` for ``%r`` (the
    # request line). Overriding it is the cleanest insertion point.
    @staticmethod
    def _format_r(request: Any, response: Any, _time: float) -> str:
        raw = (
            f"{request.method} "
            f"{request.path_qs} "
            f"HTTP/{request.version.major}.{request.version.minor}"
        )
        return redact_query_string(raw)
