"""Tests for ``GfsWebSocketSupervisor`` (spec §24.12, SH-side reconciler)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from socialhome.domain.federation import GfsConnection
from socialhome.infrastructure.gfs_ws_supervisor import (
    GfsWebSocketSupervisor,
    _relay_for,
)


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _FakeRepo:
    """Mock of :class:`AbstractGfsConnectionRepo` exposing only ``list_active``."""

    def __init__(self, conns: list[GfsConnection] | None = None) -> None:
        self._conns = list(conns or [])

    async def list_active(self) -> list[GfsConnection]:
        return list(self._conns)

    def set_active(self, conns: list[GfsConnection]) -> None:
        self._conns = list(conns)

    # The supervisor never calls these, but the Protocol shape needs them.
    async def save(self, conn): ...
    async def get(self, gfs_id): ...
    async def update_status(self, gfs_id, status): ...
    async def delete(self, gfs_id): ...
    async def publish_space(self, space_id, gfs_id): ...
    async def unpublish_space(self, space_id, gfs_id): ...
    async def list_publications(self, gfs_id): ...
    async def list_gfs_for_space(self, space_id): ...
    async def count_published_spaces(self, gfs_id): ...
    async def list_publications_all(self): ...


def _make_conn(gfs_id: str, url: str) -> GfsConnection:
    return GfsConnection(
        id=gfs_id,
        gfs_instance_id=f"gfs-inst-{gfs_id}",
        display_name=gfs_id,
        public_key="aa" * 32,
        inbox_url=url,
        status="active",
        paired_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_supervisor_starts_clients_for_active_pairings(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )

    started: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            started.append(gfs_url)

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 2
            assert sorted(started) == ["http://gfs1.test", "http://gfs2.test"]
            assert supervisor.is_running("g1")
            assert supervisor.is_running("g2")
        finally:
            await supervisor.stop()


async def test_supervisor_binds_on_connected_per_connection(http_session):
    """Each spawned client gets an ``on_connected`` bound to its own gfs id,
    so a WS (re)connect refreshes the right pairing's display name."""
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    refreshed: list[str] = []

    async def on_connected(gfs_id: str) -> None:
        refreshed.append(gfs_id)

    captured: dict[str, object] = {}

    class _StubClient:
        def __init__(self, *, gfs_url, on_connected=None, **_kwargs):
            self.gfs_url = gfs_url
            captured[gfs_url] = on_connected

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            on_connected=on_connected,
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 2
            # Each client got a callable bound to its gfs id.
            cb1 = captured["http://gfs1.test"]
            cb2 = captured["http://gfs2.test"]
            assert callable(cb1) and callable(cb2)
            await cb1()  # type: ignore[operator]
            await cb2()  # type: ignore[operator]
            assert sorted(refreshed) == ["g1", "g2"]
        finally:
            await supervisor.stop()


async def test_supervisor_picks_up_new_pairing_on_reconcile(http_session):
    repo = _FakeRepo([_make_conn("g1", "http://gfs1.test")])
    started: list[str] = []
    stop_calls: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            started.append(gfs_url)

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            stop_calls.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 1

            repo.set_active(
                [
                    _make_conn("g1", "http://gfs1.test"),
                    _make_conn("g2", "http://gfs2.test"),
                ]
            )
            for _ in range(50):
                if supervisor.client_count() == 2:
                    break
                await asyncio.sleep(0.02)
            assert supervisor.client_count() == 2
            assert "http://gfs2.test" in started
        finally:
            await supervisor.stop()


async def test_supervisor_stops_clients_for_removed_pairings(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 2

            # Disconnect g2 — supervisor should drop its client on the next tick.
            repo.set_active([_make_conn("g1", "http://gfs1.test")])
            for _ in range(50):
                if supervisor.client_count() == 1:
                    break
                await asyncio.sleep(0.02)
            assert supervisor.client_count() == 1
            assert "http://gfs2.test" in stops
        finally:
            await supervisor.stop()


async def test_supervisor_restarts_dead_client_on_reconcile(http_session):
    """A client whose loop task died (``is_alive()`` False) is restarted on
    the next reconcile even though its pairing is still active — the
    supervisor must not treat "present in ``self._clients``" as "running"."""
    repo = _FakeRepo([_make_conn("g1", "http://gfs1.test")])
    instances: list["_StubClient"] = []
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            self._alive = True
            instances.append(self)

        def is_alive(self) -> bool:
            return self._alive

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=10.0,  # only the manual reconciles run
        )
        await supervisor.start()
        try:
            assert supervisor.client_count() == 1
            assert len(instances) == 1
            first = instances[0]

            # Simulate the loop task dying — pairing stays active.
            first._alive = False

            # A reconcile with the SAME pairing must replace the dead client.
            await supervisor._reconcile_once()

            assert len(instances) == 2, (
                "a fresh client should have replaced the dead one"
            )
            assert instances[1] is not first
            assert "http://gfs1.test" in stops, (
                "the dead client should have been stopped"
            )
            assert supervisor.client_count() == 1
            assert supervisor.is_running("g1")
        finally:
            await supervisor.stop()


async def test_supervisor_does_not_restart_healthy_client(http_session):
    """A live client (``is_alive()`` True) is never needlessly torn down on a
    reconcile — only dead clients are replaced."""
    repo = _FakeRepo([_make_conn("g1", "http://gfs1.test")])
    instances: list["_StubClient"] = []
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            instances.append(self)

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=10.0,
        )
        await supervisor.start()
        try:
            assert len(instances) == 1
            await supervisor._reconcile_once()
            await supervisor._reconcile_once()
            assert len(instances) == 1, "healthy client must not be replaced"
            assert stops == [], "healthy client must not be stopped"
            assert supervisor.client_count() == 1
        finally:
            await supervisor.stop()


async def test_supervisor_stop_closes_all_clients(http_session):
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    stops: list[str] = []

    class _StubClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            stops.append(self.gfs_url)

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=10.0,
        )
        await supervisor.start()
        await supervisor.stop()
        assert sorted(stops) == ["http://gfs1.test", "http://gfs2.test"]
        assert supervisor.client_count() == 0


async def test_connection_health_unknown_gfs_is_disconnected(http_session):
    """A pairing with no live client reads as disconnected, no error."""
    supervisor = GfsWebSocketSupervisor(
        repo=_FakeRepo(),
        instance_id="sh-1",
        signing_key=b"\x00" * 32,
        session_factory=lambda: http_session,
        on_relay=AsyncMock(),
        reconcile_interval_seconds=10.0,
    )
    assert supervisor.connection_health("absent") == {
        "connected": False,
        "last_error": None,
    }


async def test_connection_health_reflects_live_client(http_session):
    """``connection_health`` reads the client's live ``connected`` /
    ``last_auth_error`` props — not the stored pairing status."""

    class _HealthClient:
        def __init__(self, *, gfs_url, **_kwargs):
            self.gfs_url = gfs_url
            self.connected = False
            self.last_auth_error = "unknown-instance"

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            return None

    repo = _FakeRepo([_make_conn("g1", "http://gfs1.test")])
    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _HealthClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            reconcile_interval_seconds=10.0,
        )
        await supervisor.start()
        try:
            assert supervisor.connection_health("g1") == {
                "connected": False,
                "last_error": "unknown-instance",
            }
            # Flip to a healthy socket → connected, error cleared.
            client = supervisor._clients["g1"]
            client.connected = True
            client.last_auth_error = None
            assert supervisor.connection_health("g1") == {
                "connected": True,
                "last_error": None,
            }
        finally:
            await supervisor.stop()


async def test_supervisor_binds_envelope_handler_per_connection(http_session):
    """Each client's §D2b envelope handler carries its own connection
    server's URL, so the issuer's sealed reply goes back out the server the
    request arrived on rather than whichever pairing came first."""
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    seen: list[tuple[str, str]] = []

    async def on_envelope(frame: dict, *, gfs_url: str = "") -> None:
        seen.append((frame["sealed"]["ciphertext"], gfs_url))

    captured: dict[str, object] = {}

    class _StubClient:
        def __init__(self, *, gfs_url, on_envelope=None, **_kwargs):
            self.gfs_url = gfs_url
            captured[gfs_url] = on_envelope

        def is_alive(self) -> bool:
            return True

        def attach_envelope_handler(self, handler):
            captured[self.gfs_url] = handler

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            on_envelope=on_envelope,
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            await captured["http://gfs1.test"]({"sealed": {"ciphertext": "one"}})
            await captured["http://gfs2.test"]({"sealed": {"ciphertext": "two"}})
            assert seen == [
                ("one", "http://gfs1.test"),
                ("two", "http://gfs2.test"),
            ]
            # Late binding (startup wires the coordinator after the
            # supervisor) re-binds every running client to its own URL.
            late: list[tuple[str, str]] = []

            async def later(frame: dict, *, gfs_url: str = "") -> None:
                late.append((frame["sealed"]["ciphertext"], gfs_url))

            supervisor.attach_envelope_handler(later)
            await captured["http://gfs2.test"]({"sealed": {"ciphertext": "three"}})
            assert late == [("three", "http://gfs2.test")]
        finally:
            await supervisor.stop()


# ── relay observer (seat self-heal) ──────────────────────────────────────


async def test_relay_observer_is_told_the_server_after_the_consumer():
    order: list[tuple] = []

    async def _consume(frame):
        order.append(("consume", frame["space_id"]))

    async def _seen(frame, *, gfs_id):
        order.append(("seen", frame["space_id"], gfs_id))

    await _relay_for(_consume, _seen, "gfs-1")({"space_id": "sp"})
    assert order == [("consume", "sp"), ("seen", "sp", "gfs-1")]


async def test_without_an_observer_the_consumer_is_used_as_is():
    consume = AsyncMock()
    assert _relay_for(consume, None, "gfs-1") is consume


async def test_a_failing_observer_never_costs_the_frame(caplog):
    consume = AsyncMock()

    async def _boom(frame, *, gfs_id):
        raise RuntimeError("bug")

    await _relay_for(consume, _boom, "gfs-1")({"space_id": "sp"})
    consume.assert_awaited_once()
    assert "relay observer failed" in caplog.text


async def test_a_frame_the_consumer_rejected_never_reaches_the_observer():
    seen = AsyncMock()

    async def _reject(frame):
        raise ValueError("bad frame")

    with pytest.raises(ValueError):
        await _relay_for(_reject, seen, "gfs-1")({"space_id": "sp"})
    seen.assert_not_awaited()


async def test_supervisor_binds_moment_signal_handler_per_connection(http_session):
    """Regression: each client's ``moment_signal`` handler is told the LOCAL
    connection it arrived on — the frame's ``gfs_id`` is the server's public
    id (or an alias), never a connection row id, so the answer must not be
    routed by it."""
    repo = _FakeRepo(
        [_make_conn("g1", "http://gfs1.test"), _make_conn("g2", "http://gfs2.test")]
    )
    seen: list[tuple[str, str]] = []

    async def on_moment_signal(frame: dict, *, gfs_id: str | None = None) -> None:
        seen.append((frame["session_id"], gfs_id or ""))

    captured: dict[str, object] = {}

    class _StubClient:
        def __init__(self, *, gfs_url, on_moment_signal=None, **_kwargs):
            captured[gfs_url] = on_moment_signal

        def is_alive(self) -> bool:
            return True

        async def start(self):
            return None

        async def stop(self):
            return None

    with patch(
        "socialhome.infrastructure.gfs_ws_supervisor.GfsWebSocketClient",
        _StubClient,
    ):
        supervisor = GfsWebSocketSupervisor(
            repo=repo,
            instance_id="sh-1",
            signing_key=b"\x00" * 32,
            session_factory=lambda: http_session,
            on_relay=AsyncMock(),
            on_moment_signal=on_moment_signal,
            reconcile_interval_seconds=0.05,
        )
        await supervisor.start()
        try:
            await captured["http://gfs1.test"]({"session_id": "a", "gfs_id": "pub"})
            await captured["http://gfs2.test"]({"session_id": "b", "gfs_id": "pub"})
            assert seen == [("a", "g1"), ("b", "g2")]
        finally:
            await supervisor.stop()
