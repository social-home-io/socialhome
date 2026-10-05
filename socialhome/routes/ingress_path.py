"""Trusted HA-ingress path prefix for a request.

HA Supervisor's ingress proxy stamps the dynamic URL prefix
(``/api/hassio_ingress/<token>``) into ``X-Ingress-Path`` on every
request it forwards to the add-on. Anything that builds a browser-facing
path — the SPA ``<base href>`` (:mod:`socialhome.routes.spa`), the app
bundle cookie ``Path`` (:mod:`socialhome.routes.app_bundle`) — needs that
prefix, but outside ingress the header is any client's to forge.

:func:`trusted_ingress_path` is the single rule: honour the header only
when the platform adapter advertises ``Capability.INGRESS`` (a Supervisor
sits in front and sets it) and only when it matches
:data:`INGRESS_PATH_RE`; otherwise return ``""``.
"""

from __future__ import annotations

import logging
import re

from aiohttp import web

from ..app_keys import platform_adapter_key
from ..platform.adapter import Capability

log = logging.getLogger(__name__)

#: The only ``X-Ingress-Path`` shape honoured. HA Core's hassio ingress
#: proxy stamps ``f"/api/hassio_ingress/{token}"``
#: (``homeassistant/components/hassio/ingress.py``), and Supervisor mints
#: the token with ``secrets.token_urlsafe()``
#: (``supervisor/apps/validate.py``) — base64url, so ``[A-Za-z0-9_-]``.
#: A trailing ``/`` is tolerated. Used with ``fullmatch`` so a trailing
#: newline can't slip past ``$``.
INGRESS_PATH_RE = re.compile(r"/api/hassio_ingress/[A-Za-z0-9_-]+/?")


def trusted_ingress_path(request: web.Request) -> str:
    """The validated ingress prefix without trailing ``/``, or ``""``.

    ``""`` unless the adapter advertises ``Capability.INGRESS`` and the
    ``X-Ingress-Path`` header fully matches :data:`INGRESS_PATH_RE`.
    """
    adapter = request.app.get(platform_adapter_key)
    if adapter is None or Capability.INGRESS not in adapter.capabilities:
        return ""
    header = request.headers.get("X-Ingress-Path", "")
    if not INGRESS_PATH_RE.fullmatch(header):
        if header:
            log.debug("ignoring malformed X-Ingress-Path header")
        return ""
    return header.rstrip("/")
