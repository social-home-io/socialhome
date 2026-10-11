"""Custom aiohttp access logger that redacts sensitive query parameters.

Prevents secrets from leaking into operator log files, log shippers, or
log-aggregation services. The WebSocket endpoint uses ``?token=<raw>``
for browser auth (aiohttp can't set custom headers on WS), so the
default aiohttp access log would write that token verbatim. We strip
``token``, ``api_key``, ``access_token`` and ``password`` values from
every formatted access-log field — the request line (``%r``), request
headers such as ``%{Referer}i``, and the matching ``extra`` attributes
aiohttp attaches to the log record. Bearer tokens that ride in the URL
*path* — the GFS's ``/join/{token}`` invite links and
``/highlight/…/{token}`` share links — are redacted too
(:func:`redact_secrets`).

Wired in via ``web.run_app(..., access_log_class=RedactingAccessLogger)``
in :mod:`socialhome.__main__`, :mod:`socialhome.app` and the GFS's
:func:`socialhome.global_server.server.main`.
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


#: One path segment (no ``/``, query, fragment or whitespace).
_SEG = r"[^/?#\s]+"
#: Where a URL path may start in a log field: the start of the value
#: (``%{Referer}i`` relative form), after whitespace (``%r`` request
#: line ``GET /…``), or right after a ``//authority`` (absolute Referer).
#: Anchoring here keeps ``/api/…/join/x`` or ``/static/highlight/…``
#: from matching a root-level shape.
_PATH_START = r"(^|\s|//[^/?#\s]*)"
#: The end of the token segment: end of value, query, fragment or space.
_SEG_END = r"(?=$|[?#\s])"

#: URL paths whose segment IS a bearer credential — anyone holding the
#: string can use it. Each pattern captures ``(start, prefix, token)``;
#: only the token is replaced. GFS shapes:
#:
#: * ``/join/{gfs_token}`` — invite-link landing; the token serves the
#:   space's invite blob.
#: * ``/highlight/{instance_id}/{highlight_id}/{token}`` — public
#:   highlight share link (and the ``Referer`` its viewer's assets send).
#: * ``/gfs/highlight_tokens/{token}/revoke`` and
#:   ``/gfs/spaces/{space_id}/invite/{gfs_token}`` — the owner's signed
#:   revoke calls name the same tokens.
_PATH_TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(rf"{_PATH_START}(/join/)({_SEG}){_SEG_END}"),
    re.compile(rf"{_PATH_START}(/highlight/{_SEG}/{_SEG}/)({_SEG}){_SEG_END}"),
    re.compile(
        rf"{_PATH_START}(/gfs/highlight_tokens/)({_SEG})(?=/revoke(?:$|[?#\s]))"
    ),
    re.compile(rf"{_PATH_START}(/gfs/spaces/{_SEG}/invite/)({_SEG}){_SEG_END}"),
)


def _redact_path_match(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}***"


def redact_secrets(line: str) -> str:
    """Redact sensitive query params AND bearer tokens carried in paths.

    Query handling is :func:`redact_query_string`; on top of it, every
    :data:`_PATH_TOKEN_RES` shape has its token segment replaced with
    ``***`` (the rest of the path stays readable for operators).
    """
    if not line:
        return line
    for pattern in _PATH_TOKEN_RES:
        line = pattern.sub(_redact_path_match, line)
    return redact_query_string(line)


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
            (key, redact_secrets(value) if isinstance(value, str) else value)
            for key, value in super()._format_line(request, response, time)
        ]
