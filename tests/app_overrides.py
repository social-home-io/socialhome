"""Swap test doubles into an aiohttp app without mutating it after start.

aiohttp freezes an application once it has started; assigning ``app[key]``
after that is deprecated ("Changing state of started or joined
application"). ``on_startup`` handlers run *before* the freeze, so an
override appended after the app factory registered its own hooks replaces
the real service exactly where a post-start swap used to — after the real
startup wiring, before the first request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp.web_runner import AppRunner

#: Override value that removes the key from the app instead of replacing it.
REMOVE: Any = object()

#: What a test swaps into the app: ``{app_key: value}``, or a callable that
#: receives the started-up app and returns that mapping (for a stub built
#: from something startup created, e.g. a repo).
AppOverrides = (
    Mapping[web.AppKey[Any], Any]
    | Callable[[web.Application], Mapping[web.AppKey[Any], Any]]
)


def override_app_on_startup(app: web.Application, overrides: AppOverrides) -> None:
    """Swap test doubles into ``app`` as its LAST startup step (see the
    module docstring)."""

    async def _apply(started: web.Application) -> None:
        items = overrides(started) if callable(overrides) else overrides
        for key, value in items.items():
            if value is REMOVE:
                del started[key]
            else:
                started[key] = value

    app.on_startup.append(_apply)


class ProductionLikeTestServer(TestServer):
    """``TestServer`` with production's handler-cancellation setting.

    aiohttp's ``TestServer`` hard-codes ``handler_cancellation=True``: a
    handler whose client hangs up is cancelled. ``web.run_app`` defaults to
    ``False``, which is what the app runs with. Under the test default a
    client that has read a streamed file's ``Content-Length`` bytes and
    moves on cancels the handler mid EOF-read / close — both executor calls
    — stranding the open file until GC (an unclosed-file
    ``ResourceWarning``). With production's setting the handler finishes
    and teardown's graceful shutdown waits for it.
    """

    async def _make_runner(self, **kwargs: Any) -> AppRunner:
        kwargs["handler_cancellation"] = False
        return AppRunner(self.app, **kwargs)
