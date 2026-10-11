"""Custom aiohttp access logger that redacts sensitive query parameters.

Prevents secrets from leaking into operator log files, log shippers, or
log-aggregation services. The WebSocket endpoint uses ``?token=<raw>``
for browser auth (aiohttp can't set custom headers on WS), so the
default aiohttp access log would write that token verbatim. We strip
``token``, ``api_key``, ``access_token`` and ``password`` values from
every formatted access-log field — the request line (``%r``), request
headers such as ``%{Referer}i``, and the matching ``extra`` attributes
aiohttp attaches to the log record.

Wired in via ``web.run_app(..., access_log_class=RedactingAccessLogger)``
in :mod:`socialhome.__main__` + :mod:`socialhome.app`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote_plus

from aiohttp.web_log import AccessLogger

#: Query-param names whose values must be replaced with ``***`` before
#: they are written to the access log. Compared case-insensitively
#: against the *percent-decoded* parameter name.
_SENSITIVE_PARAMS: frozenset[str] = frozenset(
    {
        "token",
        "api_key",
        "access_token",
        "password",
    }
)

#: One ``?name=value`` / ``&name=value`` pair. The name is matched
#: loosely (anything up to ``=``) and checked against
#: :data:`_SENSITIVE_PARAMS` after percent-decoding, so ``%74oken=``
#: is caught the same as ``token=``.
_PARAM_RE = re.compile(r"([?&])([^=&\s#]+)=([^&\s#]*)")


def _redact_match(m: re.Match[str]) -> str:
    sep, name, value = m.group(1), m.group(2), m.group(3)
    if not value or unquote_plus(name).lower() not in _SENSITIVE_PARAMS:
        return m.group(0)
    return f"{sep}{name}=***"


def redact_query_string(line: str) -> str:
    """Replace sensitive query-param values with ``***``.

    Works on URLs, request lines, and any free-text string that may
    contain query-string fragments. Only parameters introduced by
    ``?``/``&`` whose percent-decoded name exactly equals a sensitive
    name are redacted, so lookalikes (``tokenize``, ``tz``, ``at``)
    are left untouched.
    """
    if not line:
        return line
    return _PARAM_RE.sub(_redact_match, line)


class RedactingAccessLogger(AccessLogger):
    """AccessLogger that redacts secrets from every formatted field.

    aiohttp's :meth:`AccessLogger.compile_format` binds the per-atom
    ``_format_*`` helpers from the *base* class
    (``getattr(AccessLogger, ...)``), so overriding e.g. ``_format_r`` on
    a subclass is silently ignored. :meth:`_format_line` is the instance
    method :meth:`AccessLogger.log` calls to build both the message
    values and the ``extra`` dict, so redacting there covers the
    formatted message and every record attribute at once.
    """

    def _format_line(
        self, request: Any, response: Any, time: float
    ) -> Iterable[tuple[str | tuple[str, str], str]]:
        return [
            (key, redact_query_string(value) if isinstance(value, str) else value)
            for key, value in super()._format_line(request, response, time)
        ]
