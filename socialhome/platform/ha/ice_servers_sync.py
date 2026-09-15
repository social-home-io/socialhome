"""Pull HA's ``web_rtc/ice_servers`` over the HA Core WebSocket and
push the result to :class:`FederationService.set_ice_servers`.

Replaces the prior arrangement where the HA integration POSTed
the list to ``PUT /api/ha/integration/ice-servers`` on
``EVENT_CORE_CONFIG_UPDATE``:

* the integration's listener only fired on YAML reloads — Nabu Casa
  Cloud's runtime registration didn't trigger it, so a fresh cloud
  TURN credential could sit invisible to SH for hours;
* the diagnostic was opaque (push failed → integration logs WARN,
  SH side has no record);
* it required keeping a write endpoint on SH and an extra
  push pipeline in the integration — two surfaces for one fact.

The pull side is simpler: SH owns the cadence (one fetch at boot,
one daily refresh) and the diagnostic (every fetch logs INFO with
the resolved server count, failures log WARN with the WS error).
The HA integration's ``ice_servers.py`` is deleted in the matching
integration-side PR.

The fetch uses the same :class:`socialhome.platform.ha.client.HaClient`
the adapter already owns for ``config/auth/list`` etc. — that
client knows how to find HA Core (``http://supervisor/core`` under
the addon, the operator-configured ``ha_url`` in plain ``ha`` mode)
and how to perform the WS auth handshake.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import HaClient

log = logging.getLogger(__name__)


#: How often the scheduler re-fetches HA's ICE-server list. 24 h
#: matches the typical TURN-credential TTL for the Nabu Casa Cloud
#: provider; shorter than that risks a race where the credential
#: expires between SH's daily fetch and the next federation
#: handshake. If the user reports stale credentials, drop this to
#: 12 h or 6 h — but past that point we should just subscribe to
#: cloud_started / cloud_disconnected events instead.
DEFAULT_REFRESH_INTERVAL_S: float = 24 * 60 * 60.0

#: How long to back off after a failed fetch before retrying. The
#: typical failure mode is "HA Core is restarting / not yet ready
#: when SH boots" — 60 s is enough to clear that window without
#: hammering. If the failure persists (HA Core unreachable for a
#: long time), the next attempt is the next ``DEFAULT_REFRESH_INTERVAL_S``
#: tick.
ERROR_RETRY_INTERVAL_S: float = 60.0


#: Callable signature for applying a freshly-fetched ICE-server
#: list. In production this wraps ``FederationService.set_ice_servers``;
#: tests pass a recording stub.
ApplyCallback = Callable[[list[dict]], Awaitable[None]]

#: Callable signature for the one-shot "the first fetch attempt is
#: done" hook. In production this releases the federation transport's
#: first-handshake ICE gate; tests pass a recording stub.
FirstAttemptCallback = Callable[[], Awaitable[None]]


class HaIceServerSync:
    """Background task that mirrors HA Core's ICE-server list onto SH.

    Single ``HaClient`` + single ``apply_callback``, lifetime owned
    by the platform adapter that creates it. ``start()`` does an
    immediate initial fetch and then schedules the daily refresh;
    ``stop()`` cancels the loop and is safe to call multiple times.
    """

    __slots__ = (
        "_client",
        "_apply",
        "_on_first_attempt",
        "_first_attempt_done",
        "_interval_s",
        "_error_retry_s",
        "_task",
        "_stop",
    )

    def __init__(
        self,
        *,
        client: "HaClient",
        apply_callback: ApplyCallback,
        interval_s: float = DEFAULT_REFRESH_INTERVAL_S,
        error_retry_s: float = ERROR_RETRY_INTERVAL_S,
        on_first_attempt: FirstAttemptCallback | None = None,
    ) -> None:
        self._client = client
        self._apply = apply_callback
        #: Fired exactly once, after the first ``fetch_and_apply_once``
        #: returns — whatever its outcome. Two of the three outcomes
        #: (HA answered with nothing usable; the fetch failed outright)
        #: never reach ``apply_callback``, so a hook hung off the apply
        #: path would never run on an ordinary HA install without a
        #: cloud subscription.
        self._on_first_attempt = on_first_attempt
        self._first_attempt_done = False
        self._interval_s = interval_s
        self._error_retry_s = error_retry_s
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        """Start the background loop. Idempotent: a second call while
        the task is alive is a no-op.

        The initial fetch is performed inside the loop (not inline
        here) so a misbehaving HA Core can't block app startup —
        the WS handshake is bounded by ``HaClient.ws_command``'s
        own timeouts but a network blackhole still costs us a few
        seconds and we'd rather not pay that on the boot path.
        """
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name="ha-ice-servers-sync",
        )

    async def stop(self) -> None:
        """Stop the loop and wait briefly for the task to exit."""
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                self._task.cancel()
            self._task = None

    async def fetch_and_apply_once(self) -> bool:
        """Run one fetch + apply cycle. Returns ``True`` on success.

        Exposed for tests + for an admin "refresh now" hook the
        UI could call (not wired today).
        """
        servers = await self._fetch()
        if servers is None:
            return False
        if not servers:
            # HA answered, but with nothing usable — no cloud subscription,
            # the WebRTC integration absent, or every entry filtered out by
            # the shape normaliser above. Applying that would REPLACE the
            # operator's config-derived list (``set_ice_servers`` is a
            # wholesale swap), leaving the federation transport with zero ICE
            # servers — not even STUN — and silently degrading every future
            # handshake to HTTPS. Treat it as "HA has nothing to contribute"
            # and keep whatever is in effect.
            log.info(
                "ha-ice-servers-sync: HA Core returned no usable ICE servers;"
                " keeping the current list",
            )
            return True
        try:
            await self._apply(servers)
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "ha-ice-servers-sync: apply callback raised: %s",
                exc,
            )
            return False
        log.info(
            "ha-ice-servers-sync: applied %d ICE server(s) from HA Core",
            len(servers),
        )
        return True

    async def _fetch(self) -> list[dict] | None:
        """Issue the ``web_rtc/ice_servers`` WS command. Returns the
        list on success, ``None`` on transport / auth / handshake
        failure (``HaClient.ws_command`` already logged the cause).

        Filters out any entry HA returns that doesn't match the
        Chrome shape ``{"urls": str | list[str]}`` — defensive: a
        future HA core version could add fields, and we'd rather
        drop unknown shapes than feed garbage into the
        federation transport's ``_build_rtc_config``.
        """
        reply = await self._client.ws_command("web_rtc/ice_servers")
        if reply is None:
            return None
        raw = reply.get("result")
        if not isinstance(raw, list):
            log.warning(
                "ha-ice-servers-sync: WS reply.result is not a list: %r",
                raw,
            )
            return None
        out: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            urls = entry.get("urls")
            if isinstance(urls, str):
                urls = [urls]
            if not isinstance(urls, list) or not urls:
                continue
            # Drop URL slots that are empty strings — a future HA
            # version that emits placeholders (or an integration
            # author serialising ``None`` as ``""``) shouldn't pollute
            # the federation transport's candidate list.
            normalized: dict = {
                "urls": [str(u) for u in urls if isinstance(u, str) and u.strip()],
            }
            for opt in ("username", "credential"):
                v = entry.get(opt)
                if isinstance(v, str):
                    normalized[opt] = v
            if normalized["urls"]:
                out.append(normalized)
        return out

    async def _loop(self) -> None:
        """Initial fetch + every ``interval_s`` thereafter. Failed
        fetches retry on the shorter ``error_retry_s`` window so HA
        Core booting after SH (rare but happens on shared hardware)
        gets a chance to come up without an operator waiting 24 h
        for the next regular tick.

        ``on_first_attempt`` fires once here, after the first cycle
        returns, whatever its outcome — the only place that sees all
        three (applied / nothing usable / fetch failed).
        """
        while not self._stop.is_set():
            success = False
            try:
                success = await self.fetch_and_apply_once()
            except Exception as exc:  # pragma: no cover — defensive
                log.warning(
                    "ha-ice-servers-sync: fetch raised: %s",
                    exc,
                )
            if not self._first_attempt_done:
                self._first_attempt_done = True
                await self._fire_first_attempt()
            wait = self._interval_s if success else self._error_retry_s
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue

    async def _fire_first_attempt(self) -> None:
        """Run the one-shot post-first-attempt hook, fail-soft.

        Mirrors the ``_apply`` guard: a raising callback logs at WARNING
        and the loop carries on, so a broken consumer can't take the
        daily ICE refresh down with it.
        """
        if self._on_first_attempt is None:
            return
        try:
            await self._on_first_attempt()
        except Exception as exc:
            log.warning(
                "ha-ice-servers-sync: on_first_attempt callback raised: %s",
                exc,
            )
