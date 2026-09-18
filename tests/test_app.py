"""Tests for socialhome.app — create_app() factory and startup hook."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome._version import __version__
from socialhome.app import (
    MAP_TILE_USER_AGENT,
    create_app,
    dispatch_gfs_relay_frame,
)
from socialhome.authority_sig import (
    AUTHORITY_EVENT_SPACE_POST_PUBLIC,
    AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF,
)
from socialhome.config import Config
from socialhome.services.app_federation_service import AppFederationService


@pytest.fixture
async def cfg(tmp_dir):
    """Return a minimal standalone Config backed by tmp_dir."""
    return Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )


async def test_create_app_returns_application(cfg):
    """create_app() returns an aiohttp.web.Application instance."""
    app = create_app(cfg)
    assert isinstance(app, web.Application)


async def test_create_app_has_routes(cfg):
    """create_app() registers at least the /healthz and /api/* routes."""
    app = create_app(cfg)
    resource_names = [r.canonical for r in app.router.resources()]
    assert "/healthz" in resource_names


async def test_create_app_stores_config(cfg):
    """create_app() stores the Config in the app dict under config_key."""
    from socialhome.app_keys import config_key

    app = create_app(cfg)
    assert app[config_key] is cfg


async def test_startup_hook_runs_without_error(tmp_dir):
    """Starting the app via TestClient triggers on_startup; identity auto-bootstraps."""
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/healthz")
        assert resp.status == 200
        # ensure_instance_identity ran — row exists, instance_id is in app dict.
        from socialhome.app_keys import instance_id_key

        assert app[instance_id_key] != "unknown"
        assert len(app[instance_id_key]) > 0


async def test_shared_http_session_lifecycle(tmp_dir):
    """A single aiohttp.ClientSession is created at startup and closed on cleanup."""
    from socialhome.app_keys import http_session_key

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        await tc.get("/healthz")
        session = app[http_session_key]
        assert session is not None
        assert session.closed is False

    # After the TestClient context exits, cleanup hooks have run.
    assert session.closed is True


async def test_cleanup_stops_the_routed_handler(tmp_dir):
    """``_on_cleanup`` awaits ``SpaceRoutedHandler.stop()`` so a deferred
    mesh retransmit parked across shutdown is cancelled rather than left to
    die with the loop (or wake after the transport is gone)."""
    import asyncio

    from socialhome.app_keys import federation_service_key

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        await tc.get("/healthz")
        handler = app[federation_service_key]._routed_handler  # noqa: SLF001
        assert handler is not None
        # Park a stand-in for a deferred retransmit sleeping out its delay.
        parked = asyncio.create_task(asyncio.sleep(3600))
        handler._deferred_retransmits["route-under-test"] = parked  # noqa: SLF001

    # After the TestClient context exits, cleanup hooks have run.
    assert parked.cancelled(), "cleanup did not stop the routed handler"
    assert handler._deferred_retransmits == {}  # noqa: SLF001


async def test_create_app_without_config_uses_env_defaults():
    """create_app(None) falls back to Config.from_env() — doesn't raise."""
    import os

    with tempfile.TemporaryDirectory() as d:
        os.environ["SH_DATA_DIR"] = d
        os.environ["SH_DB_PATH"] = str(Path(d) / "test.db")
        try:
            app = create_app()
            assert isinstance(app, web.Application)
        finally:
            os.environ.pop("SH_DATA_DIR", None)
            os.environ.pop("SH_DB_PATH", None)


async def test_app_federation_service_wired_into_app(tmp_dir):
    """AppFederationService is registered in the app dict and handlers are live.

    After startup:
    - ``app[app_federation_service_key]`` is an ``AppFederationService``.
    - The federation event registry has handlers for APP_SESSION and APP_MESSAGE
      (registered by ``federation_service.attach_apps(...)``).
    - The FederationTransport was constructed with a non-None app_inbound_handler
      (federation_service._app_inbound_handler bound method).
    """
    from socialhome.app_keys import (
        app_federation_service_key,
        federation_service_key,
        federation_transport_key,
    )
    from socialhome.domain.federation import FederationEventType

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)):
        # 1. Service is registered under its key.
        app_fed = app[app_federation_service_key]
        assert isinstance(app_fed, AppFederationService)

        # 2. federation_service event registry has handlers for both event types.
        fed_svc = app[federation_service_key]
        registry = fed_svc._event_registry  # noqa: SLF001
        assert registry.handler_count(FederationEventType.APP_SESSION) >= 1, (
            "APP_SESSION handler not registered — attach_apps() not called"
        )
        assert registry.handler_count(FederationEventType.APP_MESSAGE) >= 1, (
            "APP_MESSAGE handler not registered — attach_apps() not called"
        )

        # 3. The transport was built with the binary inbound handler threaded in.
        fed_transport = app[federation_transport_key]
        assert fed_transport._app_inbound_handler is not None, (  # noqa: SLF001
            "FederationTransport._app_inbound_handler is None — "
            "app_inbound_handler= kwarg missing from FederationTransport()"
        )


async def test_federation_service_ice_servers_carry_hmac_credentials(tmp_dir):
    """The FederationService's ICE list must be HMAC-credentialled.

    It is not an idle copy: space sync reads it and ships it to the remote
    peer inside ``SPACE_SYNC_OFFER``. Built without ``hmac_user_id`` it
    yields a TURN entry with no username/credential, so we advertise an
    entry coturn will reject and the session quietly drops to HTTPS —
    permanently in standalone, where no HA pull ever replaces the list.

    Pinned against the real ``create_app`` wiring rather than
    ``_default_ice_servers`` directly, because the bug was precisely that
    the call site omitted the argument while the helper was fine.
    """
    from socialhome.app_keys import federation_service_key

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
        webrtc_turn_url="turn:turn.example.com:3478",
        webrtc_turn_secret="s3cr3t",
    )
    app = create_app(cfg)
    # ``_wire_federation_stack`` runs from the startup hook, not
    # ``create_app``, so the service only exists once the app is started.
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/healthz")
        assert resp.status == 200
        fed = app[federation_service_key]

    turn = [
        s
        for s in fed._ice_servers  # noqa: SLF001 — asserting wiring
        if any(u.startswith(("turn:", "turns:")) for u in s.get("urls", []))
    ]
    assert turn, "no TURN entry in the federation service's ICE list"
    assert turn[0].get("username"), "TURN entry has no HMAC username"
    assert turn[0].get("credential"), "TURN entry has no HMAC credential"


async def test_standalone_boot_releases_the_ice_prime_gate(tmp_dir):
    """Standalone never pushes an ICE-server list, so ``create_app`` must
    release the transport's first-handshake gate at wiring time. Left closed,
    the first outbound federation send waits out the full prime timeout for
    a list that is never coming.
    """
    from socialhome.app_keys import federation_transport_key

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)) as tc:
        resp = await tc.get("/healthz")
        assert resp.status == 200
        transport = app[federation_transport_key]
        assert transport._ice_primed.is_set(), (  # noqa: SLF001 — asserting wiring
            "standalone boot left the ICE-prime gate closed"
        )


def test_map_tile_user_agent_identifies_the_app_and_a_contact():
    """The tile ``User-Agent`` must name the app AND a reachable contact.

    This string is the entire reason the tile proxy exists: the OSMF
    policy blocks requests that don't identify themselves, and a browser
    cannot send the header at all. Strip the contact and OSM can start
    403ing us again — which looks like "every map went grey", the bug
    this whole path was built to fix. So it is pinned here rather than
    left as an incidental f-string.
    """
    assert MAP_TILE_USER_AGENT.startswith(f"SocialHome/{__version__} ")
    assert "@social-home.io" in MAP_TILE_USER_AGENT


async def test_gfs_space_mirror_wired_into_space_service(tmp_dir):
    """The GFS on-ramp is constructed, attached to the LIVE space service
    (the real-instance one built during startup), and carries the shared
    HTTP session — without all three, subscribing to a GFS-discovered space
    404s."""
    from socialhome.app_keys import gfs_connection_service_key, space_service_key
    from socialhome.services.gfs_space_mirror_service import GfsSpaceMirrorService

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)):
        space_svc = app[space_service_key]
        mirror = space_svc._gfs_mirror  # noqa: SLF001
        assert isinstance(mirror, GfsSpaceMirrorService)
        assert mirror._http_client is not None  # noqa: SLF001
        assert mirror._gfs is app[gfs_connection_service_key]  # noqa: SLF001


async def test_gfs_connect_hook_resubscribes_our_seats(tmp_dir):
    """F4b: the GFS-WS (re)connect hook re-registers this household's
    subscriber seats beside the space-pin heal.

    Without the hook a seat the GFS purged is never re-taken — the local row
    says "subscribed" forever while nothing is delivered.
    """
    from unittest.mock import AsyncMock, patch

    from socialhome.app_keys import gfs_ws_supervisor_key
    from socialhome.services.gfs_space_mirror_service import GfsSpaceMirrorService

    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "test.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
        log_level="WARNING",
    )
    app = create_app(cfg)
    async with TestClient(TestServer(app)):
        on_connected = app[gfs_ws_supervisor_key]._on_connected  # noqa: SLF001
        assert on_connected is not None
        with patch.object(
            GfsSpaceMirrorService, "resubscribe_all", new_callable=AsyncMock
        ) as resub:
            await on_connected("gfs-1")
        resub.assert_awaited_once_with("gfs-1")


# ── GFS relay fan-out dispatch (identity-free frame) ──────────────────────


class _RecordingConsumer:
    """Stand-in relay consumer that records the frames handed to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def handle(self, frame: dict, **kwargs) -> None:
        self.frames.append(frame)


def _relay_frame(event_type: str) -> dict:
    """The four-key frame the GFS now fans out — no ``from_instance``."""
    return {
        "type": "relay",
        "space_id": "sp-1",
        "event_type": event_type,
        "payload": {"space_id": "sp-1"},
    }


async def test_gfs_relay_dispatches_a_public_post_frame_without_from_instance():
    posts, keys = _RecordingConsumer(), _RecordingConsumer()
    frame = _relay_frame(AUTHORITY_EVENT_SPACE_POST_PUBLIC)
    await dispatch_gfs_relay_frame(
        frame,
        space_public_inbound=posts,
        space_subscriber_key_inbound=keys,
    )
    assert posts.frames == [frame]
    assert keys.frames == []


async def test_gfs_relay_dispatches_a_key_handoff_frame_without_from_instance():
    posts, keys = _RecordingConsumer(), _RecordingConsumer()
    frame = _relay_frame(AUTHORITY_EVENT_SPACE_SUBSCRIBER_KEY_HANDOFF)
    await dispatch_gfs_relay_frame(
        frame,
        space_public_inbound=posts,
        space_subscriber_key_inbound=keys,
    )
    assert keys.frames == [frame]
    assert posts.frames == []


async def test_gfs_relay_never_logs_an_outer_from_instance(caplog):
    """A legacy GFS may still ship ``from_instance``. It is a household
    identity the GFS is not supposed to know — it must reach no log line."""
    caplog.set_level(logging.DEBUG, logger="socialhome")
    frame = _relay_frame(AUTHORITY_EVENT_SPACE_POST_PUBLIC)
    frame["from_instance"] = "victim.home"
    posts = _RecordingConsumer()
    await dispatch_gfs_relay_frame(
        frame,
        space_public_inbound=posts,
        space_subscriber_key_inbound=None,
    )
    assert posts.frames == [frame]
    leaked = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("socialhome") and "victim.home" in r.getMessage()
    ]
    assert leaked == []


async def test_gfs_relay_is_a_noop_without_consumers():
    """Before the crypto-dependent consumers are built they are ``None`` —
    a frame arriving then must not raise."""
    await dispatch_gfs_relay_frame(
        _relay_frame(AUTHORITY_EVENT_SPACE_POST_PUBLIC),
        space_public_inbound=None,
        space_subscriber_key_inbound=None,
    )
    await dispatch_gfs_relay_frame(
        _relay_frame("some_other_event"),
        space_public_inbound=None,
        space_subscriber_key_inbound=None,
    )
