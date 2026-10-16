"""Test-only launcher for the haos ingress harness: ``python -m socialhome``
with two boundary patches applied from *outside* the package.

Lives here, not in ``socialhome/``, on purpose — production code has no
env-var-gated test paths (CLAUDE.md, "What to Never Do"). Run it with the
repository root as the working directory.

1. ``HaClient`` hard-codes ``http://supervisor/core`` when it runs as a
   Home Assistant App (``socialhome/platform/ha/client.py``
   ``build_ha_client``). There is no ``supervisor`` host outside HA OS, so
   the constructor is wrapped to swap that base URL for
   ``$SH_FAKE_SUPERVISOR_CORE`` (the fake Supervisor's ``/core``).
2. The per-user rate limiter is switched off. The route walk reloads the
   SPA dozens of times a minute (every route, desktop + mobile), which a
   real user never does and which would otherwise trip 429 on ``/api/me``
   halfway through and fail checks for a reason unrelated to ingress.
"""

from __future__ import annotations

import os
import runpy
import sys

sys.path.insert(0, os.getcwd())

import socialhome.platform.ha.client as ha_client  # noqa: E402
import socialhome.rate_limiter as rate_limiter  # noqa: E402

_FAKE_CORE = os.environ["SH_FAKE_SUPERVISOR_CORE"]
_orig_init = ha_client.HaClient.__init__


def _init(self, session, base_url, token):  # type: ignore[no-untyped-def]
    if base_url == "http://supervisor/core":
        base_url = _FAKE_CORE
    _orig_init(self, session, base_url, token)


async def _always_allowed(self, *args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
    return True


ha_client.HaClient.__init__ = _init  # type: ignore[method-assign]
rate_limiter.RateLimiter.check = _always_allowed  # type: ignore[method-assign]
rate_limiter.RateLimiter.is_allowed = lambda self, *a, **k: True  # type: ignore[method-assign]

runpy.run_module("socialhome", run_name="__main__", alter_sys=True)
