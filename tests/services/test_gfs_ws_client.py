"""Tests for ``GfsWebSocketClient`` (spec §24.12, SH-side WS client)."""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from socialhome.crypto import b64url_decode, verify_ed25519
from socialhome.services.gfs_ws_client import GfsWebSocketClient, _to_ws_url


try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them.

        CI does not install ``pytest-socket``; on those runs this fixture
        is not registered and the test uses sockets normally.
        """

except ImportError:  # pragma: no cover - CI path
    pass


# ── Helpers ────────────────────────────────────────────────────────────────────


def _gen_keypair() -> tuple[bytes, bytes]:
    priv = ed25519.Ed25519PrivateKey.generate()
    seed = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return seed, pub


# ── _to_ws_url ─────────────────────────────────────────────────────────────────


def test_to_ws_url_https():
    assert _to_ws_url("https://gfs.example.com") == "wss://gfs.example.com/gfs/ws"


def test_to_ws_url_http():
    assert _to_ws_url("http://localhost:8124") == "ws://localhost:8124/gfs/ws"


def test_to_ws_url_strips_trailing_slash():
    assert _to_ws_url("https://gfs.example.com/") == "wss://gfs.example.com/gfs/ws"


# ── is_alive ─────────────────────────────────────────────────────────────────


def _make_idle_client() -> GfsWebSocketClient:
    """A client whose loop never connects (no server) — used to probe the
    task lifecycle without a network round-trip."""

    async def _noop(_frame: dict) -> None:
        return None

    return GfsWebSocketClient(
        gfs_url="http://gfs.invalid",
        instance_id="sh-1",
        signing_key=b"\x00" * 32,
        session_factory=aiohttp.ClientSession,
        on_relay=_noop,
    )


async def test_is_alive_false_before_start():
    """No loop task spawned yet → not alive."""
    client = _make_idle_client()
    assert client.is_alive() is False


async def test_is_alive_true_while_loop_running():
    client = _make_idle_client()
    await client.start()
    try:
        # The connect-and-listen loop task is running (it'll keep retrying the
        # unreachable URL with backoff), so the supervisor sees it as alive.
        assert client.is_alive() is True
    finally:
        await client.stop()


async def test_is_alive_false_after_stop():
    client = _make_idle_client()
    await client.start()
    await client.stop()
    assert client.is_alive() is False


async def test_is_alive_false_when_loop_task_done():
    """If the loop task finishes/dies, the client reports not alive even though
    ``stop()`` was never called — this is the liveness the supervisor checks."""
    client = _make_idle_client()

    async def _instant() -> None:
        return None

    client._task = asyncio.create_task(_instant())
    await client._task
    assert client.is_alive() is False


# ── In-process fake GFS WebSocket server ──────────────────────────────────────


class _FakeGfsServer:
    """Stub GFS that exposes ``/gfs/ws`` for the client to connect to.

    Records each hello frame and exposes a queue of relay frames the test
    can push to the client.
    """

    def __init__(self) -> None:
        self.hellos: list[dict] = []
        self.last_ws: web.WebSocketResponse | None = None
        self.connect_count: int = 0
        self.outbound: asyncio.Queue[dict] = asyncio.Queue()
        self.close_first_connect: bool = False
        self.close_code: int = 4401
        self.close_message: bytes = b"reject"
        # When True, EVERY connect's hello is closed (not just the first).
        self.always_close: bool = False

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connect_count += 1
        msg = await ws.receive(timeout=5)
        self.hellos.append(json.loads(msg.data))
        if self.always_close or (self.close_first_connect and self.connect_count == 1):
            await ws.close(code=self.close_code, message=self.close_message)
            return ws
        self.last_ws = ws
        # Drain queued outbound frames for the duration of the connection.
        try:
            while not ws.closed:
                try:
                    frame = await asyncio.wait_for(
                        self.outbound.get(),
                        timeout=0.05,
                    )
                except asyncio.TimeoutError:
                    if ws.closed:
                        break
                    continue
                await ws.send_json(frame)
        except Exception:
            pass
        return ws


@pytest.fixture
async def fake_gfs():
    server_obj = _FakeGfsServer()
    app = web.Application()
    app.router.add_get("/gfs/ws", server_obj.handler)
    server = TestServer(app)
    await server.start_server()
    server_obj.url = str(server.make_url("/")).rstrip("/")
    yield server_obj
    await server.close()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


# ── Tests ──────────────────────────────────────────────────────────────────────


async def test_client_sends_signed_hello(fake_gfs, http_session):
    seed, pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-1",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        assert client.connected
        assert len(fake_gfs.hellos) == 1
        hello = fake_gfs.hellos[0]
        assert hello["type"] == "hello"
        assert hello["instance_id"] == "sh-1"
        assert isinstance(hello["ts"], int)
        assert abs(hello["ts"] - int(time.time())) < 5
        # Verify the signature against the test's pub key.
        sig = b64url_decode(hello["sig"])
        msg = f"sh-1|{hello['ts']}".encode("utf-8")
        assert verify_ed25519(pub, msg, sig)
    finally:
        await client.stop()


async def test_client_dispatches_inbound_relay(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s1", "payload": {"x": 1}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        assert relays
        assert relays[0]["space_id"] == "s1"
        assert relays[0]["payload"] == {"x": 1}
    finally:
        await client.stop()


async def test_client_ignores_non_relay_frames(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-3",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put({"type": "noise", "msg": "ignored"})
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s2", "payload": {}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        assert len(relays) == 1
        assert relays[0]["type"] == "relay"
        assert relays[0]["space_id"] == "s2"
    finally:
        await client.stop()


async def test_client_invokes_on_connected_after_connect(fake_gfs, http_session):
    """``on_connected`` fires once a WS handshake succeeds — the hook the
    supervisor uses to re-fetch /gfs/info and refresh the stored name."""
    seed, _pub = _gen_keypair()
    connected: list[int] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_connected() -> None:
        connected.append(1)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-oc",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_connected=on_connected,
    )
    await client.start()
    try:
        for _ in range(100):
            if connected:
                break
            await asyncio.sleep(0.02)
        assert connected
    finally:
        await client.stop()


async def test_client_on_connected_exception_does_not_kill_loop(fake_gfs, http_session):
    """A raising ``on_connected`` is contained — the WS keeps relaying."""
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    async def on_connected() -> None:
        raise RuntimeError("boom")

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-oce",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_connected=on_connected,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        assert client.connected
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s-oce", "payload": {}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        assert relays and relays[0]["space_id"] == "s-oce"
    finally:
        await client.stop()


async def test_client_dispatches_moment_public_frames(fake_gfs, http_session):
    """``incoming_public_moment`` + ``incoming_public_moment_delete``
    frames go to the dedicated handler."""
    seed, _pub = _gen_keypair()
    moments: list[dict] = []
    follows: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_moment(frame: dict) -> None:
        moments.append(frame)

    async def on_follow(frame: dict) -> None:
        follows.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-mp",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_moment_public=on_moment,
        on_follow_changed=on_follow,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "incoming_public_moment", "payload": {"moment_id": "m-1"}},
        )
        await fake_gfs.outbound.put(
            {
                "type": "incoming_public_moment_delete",
                "payload": {"moment_id": "m-1"},
            },
        )
        await fake_gfs.outbound.put(
            {
                "type": "follow_changed",
                "action": "add",
                "follower_user_id": "u-2",
                "follower_instance_id": "inst-2",
                "followed_user_id": "u-1",
            },
        )
        for _ in range(100):
            if len(moments) >= 2 and follows:
                break
            await asyncio.sleep(0.02)
        assert len(moments) == 2
        assert moments[0]["type"] == "incoming_public_moment"
        assert moments[1]["type"] == "incoming_public_moment_delete"
        assert len(follows) == 1
        assert follows[0]["action"] == "add"
    finally:
        await client.stop()


async def test_client_dispatches_new_subscriber_frames(fake_gfs, http_session):
    """``new_subscriber`` frames (the Phase-5b GFS notify) go to the dedicated
    handler so a seed-holder can seal the content key to the new subscriber."""
    seed, _pub = _gen_keypair()
    subs: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_new_subscriber(frame: dict) -> None:
        subs.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ns",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_new_subscriber=on_new_subscriber,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {
                "type": "new_subscriber",
                "space_id": "sp-1",
                "subscriber": {"instance_id": "sub-1"},
            },
        )
        for _ in range(100):
            if subs:
                break
            await asyncio.sleep(0.02)
        assert len(subs) == 1
        assert subs[0]["space_id"] == "sp-1"
        assert subs[0]["subscriber"]["instance_id"] == "sub-1"
    finally:
        await client.stop()


async def test_client_attach_new_subscriber_handler_late_binds(fake_gfs, http_session):
    """The handler can be attached after construction (startup wiring order)."""
    seed, _pub = _gen_keypair()
    subs: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_new_subscriber(frame: dict) -> None:
        subs.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ns2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    client.attach_new_subscriber_handler(on_new_subscriber)
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "new_subscriber", "space_id": "sp-2", "subscriber": {}},
        )
        for _ in range(100):
            if subs:
                break
            await asyncio.sleep(0.02)
        assert len(subs) == 1
    finally:
        await client.stop()


async def test_client_drops_new_subscriber_when_no_handler(fake_gfs, http_session):
    """A ``new_subscriber`` frame with no handler attached is dropped, not
    crashed."""
    seed, _pub = _gen_keypair()

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ns3",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "new_subscriber", "space_id": "sp-3", "subscriber": {}},
        )
        await asyncio.sleep(0.2)
        assert client.connected  # loop survived
    finally:
        await client.stop()


async def test_client_dispatches_moment_signal_frames(fake_gfs, http_session):
    """``moment_signal`` frames (the §Momentum-public answerer) go to the
    dedicated handler, mirroring the highlight_signal branch."""
    seed, _pub = _gen_keypair()
    signals: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def on_moment_signal(frame: dict) -> None:
        signals.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_moment_signal=on_moment_signal,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {
                "type": "moment_signal",
                "kind": "offer",
                "session_id": "s-1",
                "user_id": "u-1",
                "gfs_id": "gfs-1",
                "sdp": "v=0",
            },
        )
        for _ in range(100):
            if signals:
                break
            await asyncio.sleep(0.02)
        assert len(signals) == 1
        assert signals[0]["type"] == "moment_signal"
        assert signals[0]["kind"] == "offer"
        assert signals[0]["user_id"] == "u-1"
    finally:
        await client.stop()


async def test_client_attach_moment_signal_handler_late_binds(fake_gfs, http_session):
    """``attach_moment_signal_handler`` wires the answerer after construction."""
    seed, _pub = _gen_keypair()
    signals: list[dict] = []

    async def on_relay(frame: dict) -> None:
        pass

    async def handler(frame: dict) -> None:
        signals.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    client.attach_moment_signal_handler(handler)
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "moment_signal", "kind": "ice", "session_id": "s"},
        )
        for _ in range(100):
            if signals:
                break
            await asyncio.sleep(0.02)
        assert len(signals) == 1
    finally:
        await client.stop()


async def test_client_drops_moment_signal_when_no_handler(fake_gfs, http_session):
    """No-handler path is a debug log, not a crash."""
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ms-noop",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "moment_signal", "kind": "offer", "session_id": "s"},
        )
        await fake_gfs.outbound.put({"type": "relay", "space_id": "s", "payload": {}})
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        # Relay still dispatched, moment_signal dropped silently.
        assert len(relays) == 1
    finally:
        await client.stop()


async def test_client_drops_moment_public_when_no_handler(fake_gfs, http_session):
    """No-handler path is a debug log, not a crash."""
    seed, _pub = _gen_keypair()
    relays: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relays.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-noop",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {"type": "incoming_public_moment", "payload": {"moment_id": "m-1"}},
        )
        await fake_gfs.outbound.put(
            {"type": "relay", "space_id": "s", "payload": {}},
        )
        for _ in range(100):
            if relays:
                break
            await asyncio.sleep(0.02)
        # Relay still got dispatched, moment_public dropped silently.
        assert len(relays) == 1
    finally:
        await client.stop()


async def test_client_reconnects_with_backoff(fake_gfs, http_session):
    """When the GFS rejects the first connect, the client retries."""
    seed, _pub = _gen_keypair()
    fake_gfs.close_first_connect = True
    fake_gfs.close_code = 4401

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-4",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05,),
    )
    await client.start()
    try:
        for _ in range(200):
            if fake_gfs.connect_count >= 2 and client.connected:
                break
            await asyncio.sleep(0.02)
        assert fake_gfs.connect_count >= 2
        assert client.connected
    finally:
        await client.stop()


async def test_client_stop_idempotent(fake_gfs, http_session):
    seed, _pub = _gen_keypair()

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-5",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    await client.stop()
    await client.stop()  # second stop must not raise


async def test_client_handler_exception_does_not_kill_loop(fake_gfs, http_session):
    seed, _pub = _gen_keypair()
    seen: list[int] = []

    async def on_relay(frame: dict) -> None:
        seen.append(len(seen))
        if len(seen) == 1:
            raise RuntimeError("boom")

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-6",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put({"type": "relay", "n": 1})
        await fake_gfs.outbound.put({"type": "relay", "n": 2})
        for _ in range(100):
            if len(seen) == 2:
                break
            await asyncio.sleep(0.02)
        assert seen == [0, 1]  # second frame still dispatched after the boom
        assert client.connected
    finally:
        await client.stop()


async def test_on_text_dispatch_covers_every_frame_type():
    """Drive ``_on_text`` directly (no socket) so every dispatch branch
    is exercised deterministically — frame routing, the no-handler drop,
    malformed JSON, non-dict, and unknown types."""
    seed, _pub = _gen_keypair()
    got: dict[str, list[dict]] = {
        "relay": [],
        "highlight": [],
        "moment": [],
        "moment_public": [],
        "follow": [],
    }

    def _sink(key: str):
        async def _h(frame: dict) -> None:
            got[key].append(frame)

        return _h

    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-x",
        signing_key=seed,
        session_factory=lambda: None,  # never started
        on_relay=_sink("relay"),
        on_highlight_signal=_sink("highlight"),
        on_moment_signal=_sink("moment"),
        on_moment_public=_sink("moment_public"),
        on_follow_changed=_sink("follow"),
    )

    await client._on_text(json.dumps({"type": "highlight_signal", "kind": "offer"}))
    await client._on_text(json.dumps({"type": "moment_signal", "kind": "offer"}))
    await client._on_text(json.dumps({"type": "incoming_public_moment", "payload": {}}))
    await client._on_text(
        json.dumps({"type": "incoming_public_moment_delete", "payload": {}})
    )
    await client._on_text(json.dumps({"type": "follow_changed", "action": "add"}))
    # Unknown type + non-dict + malformed JSON are all silently ignored.
    await client._on_text(json.dumps({"type": "who_knows"}))
    await client._on_text(json.dumps(123))
    await client._on_text("{not json")

    assert len(got["highlight"]) == 1
    assert len(got["moment"]) == 1
    assert len(got["moment_public"]) == 2
    assert len(got["follow"]) == 1


async def test_on_text_drops_frames_when_handlers_unset():
    """With no handlers attached, signal frames are dropped, not crashed."""
    seed, _pub = _gen_keypair()
    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-y",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=_sink_noop,
    )
    # None of these have handlers — each hits its no-handler return branch.
    await client._on_text(json.dumps({"type": "highlight_signal"}))
    await client._on_text(json.dumps({"type": "moment_signal"}))
    await client._on_text(json.dumps({"type": "incoming_public_moment"}))
    await client._on_text(json.dumps({"type": "follow_changed"}))


async def _sink_noop(_frame: dict) -> None:
    return None


async def test_on_text_swallows_handler_exception():
    """A raising signal handler is logged, not propagated."""
    seed, _pub = _gen_keypair()

    async def _boom(_frame: dict) -> None:
        raise RuntimeError("boom")

    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-z",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=_sink_noop,
        on_highlight_signal=_boom,
        on_moment_signal=_boom,
        on_moment_public=_boom,
        on_follow_changed=_boom,
    )
    # Must not raise.
    await client._on_text(json.dumps({"type": "highlight_signal"}))
    await client._on_text(json.dumps({"type": "moment_signal"}))
    await client._on_text(json.dumps({"type": "incoming_public_moment"}))
    await client._on_text(json.dumps({"type": "follow_changed"}))


async def test_on_text_server_info_updated_invokes_on_connected():
    """A ``server_info_updated`` frame re-runs the reconnect refresh hook
    (``on_connected``) and does NOT fall through to the relay handler."""
    seed, _pub = _gen_keypair()
    refreshed: list[int] = []
    relayed: list[dict] = []

    async def on_connected() -> None:
        refreshed.append(1)

    async def on_relay(frame: dict) -> None:
        relayed.append(frame)

    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-info",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=on_relay,
        on_connected=on_connected,
    )

    await client._on_text(
        json.dumps({"type": "server_info_updated", "server_name": "X"})
    )

    assert refreshed == [1]
    assert relayed == []  # did not fall through to the relay handler


async def test_on_text_server_info_updated_without_hook_is_noop():
    """No ``on_connected`` wired → the frame is a safe no-op (no crash)."""
    seed, _pub = _gen_keypair()
    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-info2",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=_sink_noop,
    )

    # Must not raise.
    await client._on_text(
        json.dumps({"type": "server_info_updated", "server_name": "Y"})
    )


# ── Auth-close detection (4401) + backoff ──────────────────────────────────────


async def test_client_sets_last_auth_error_on_4401(fake_gfs, http_session):
    """A server-initiated 4401 close surfaces the GFS reason on
    ``last_auth_error`` — under the bug the close is never detected so this
    stays ``None`` forever."""
    seed, _pub = _gen_keypair()
    fake_gfs.always_close = True
    fake_gfs.close_code = 4401
    fake_gfs.close_message = b"unknown-instance"

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-ae",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05, 0.1),
    )
    await client.start()
    try:
        for _ in range(200):
            if client.last_auth_error is not None:
                break
            await asyncio.sleep(0.02)
        assert client.last_auth_error == "unknown-instance"
    finally:
        await client.stop()


async def test_client_sanitizes_malicious_close_reason(fake_gfs, http_session):
    """The GFS-controlled close reason flows into ``last_auth_error`` (logged
    + rendered in the SPA). A malicious/compromised GFS could embed newlines /
    control chars (log-injection) or an overlong string. The client MUST strip
    control chars and cap the length before storing it."""
    seed, _pub = _gen_keypair()
    fake_gfs.always_close = True
    fake_gfs.close_code = 4401
    # Newline + control chars + overlong. The WS close reason is ≤123 bytes;
    # stay under that so the close frame itself is valid.
    fake_gfs.close_message = ("evil\nINFO:forged log line" + "x" * 90).encode()

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-evil",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05, 0.1),
    )
    await client.start()
    try:
        for _ in range(200):
            if client.last_auth_error is not None:
                break
            await asyncio.sleep(0.02)
        reason = client.last_auth_error
        assert reason is not None
        # No control characters (incl. newlines) survived.
        assert "\n" not in reason
        assert all(ch.isprintable() for ch in reason)
        # Capped length.
        assert len(reason) <= 80
        # Leading visible text is preserved.
        assert reason.startswith("evil")
    finally:
        await client.stop()


async def test_client_backs_off_on_repeated_auth_rejection(fake_gfs, http_session):
    """An always-rejecting GFS must NOT be hammered ~1/min-delay — the loop
    takes the auth-failure path (so ``attempt`` keeps growing and the backoff
    widens) instead of resetting to the floor delay every time."""
    seed, _pub = _gen_keypair()
    fake_gfs.always_close = True
    fake_gfs.close_code = 4401
    fake_gfs.close_message = b"unknown-instance"

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-bo",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05, 0.1, 0.2),
    )
    await client.start()
    try:
        # Wait until the auth-failure path has been taken at least once.
        for _ in range(200):
            if client.last_auth_error is not None:
                break
            await asyncio.sleep(0.02)
        assert client.last_auth_error == "unknown-instance"

        # Observe a ~1.5s window. With growing backoff (0.05→0.1→0.2 then
        # clamped at 0.2) the worst case is ~1 connect / 0.2s ≈ 8 connects.
        # Under the bug (attempt reset → 0.05s floor) it would be ~30. Assert
        # a bound comfortably between the two so timing jitter can't flip it.
        start = fake_gfs.connect_count
        await asyncio.sleep(1.5)
        delta = fake_gfs.connect_count - start
        assert delta <= 15, f"too many reconnects ({delta}) — backoff not applied"
    finally:
        await client.stop()


async def test_client_clean_close_reconnects_promptly(fake_gfs, http_session):
    """A normal (1000) server close is NOT an auth failure — the client
    resets backoff and reconnects fast, ending up connected. Guards that the
    auth-close fix didn't turn every disconnect into a backoff."""
    seed, _pub = _gen_keypair()
    fake_gfs.close_first_connect = True
    fake_gfs.close_code = 1000
    fake_gfs.close_message = b"bye"

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-cc",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05,),
    )
    await client.start()
    try:
        for _ in range(200):
            if fake_gfs.connect_count >= 2 and client.connected:
                break
            await asyncio.sleep(0.02)
        assert fake_gfs.connect_count >= 2
        assert client.connected
        # A clean close is not an auth failure.
        assert client.last_auth_error is None
    finally:
        await client.stop()


async def test_client_last_auth_error_clears_on_successful_connect(
    fake_gfs, http_session
):
    """After a rejected attempt sets ``last_auth_error``, flipping the server
    to accept must clear it once the session is live."""
    seed, _pub = _gen_keypair()
    fake_gfs.always_close = True
    fake_gfs.close_code = 4401
    fake_gfs.close_message = b"unknown-instance"

    async def on_relay(frame: dict) -> None:
        pass

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-clear",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        reconnect_delays=(0.05,),
    )
    await client.start()
    try:
        for _ in range(200):
            if client.last_auth_error is not None:
                break
            await asyncio.sleep(0.02)
        assert client.last_auth_error == "unknown-instance"

        # Flip the server to accept; the next connect succeeds.
        fake_gfs.always_close = False
        for _ in range(200):
            if client.connected and client.last_auth_error is None:
                break
            await asyncio.sleep(0.02)
        assert client.connected
        assert client.last_auth_error is None
    finally:
        await client.stop()


# ── envelope frames (§D2b invite bootstrap) ───────────────────────────────


async def test_client_dispatches_envelope_frames(fake_gfs, http_session):
    """``envelope`` frames — the sealed §D2b invite blob another household
    addressed to us — go to the invite-bootstrap handler, not ``on_relay``."""
    seed, _pub = _gen_keypair()
    envelopes: list[dict] = []
    relayed: list[dict] = []

    async def on_relay(frame: dict) -> None:
        relayed.append(frame)

    async def on_envelope(frame: dict) -> None:
        envelopes.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-env",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=on_relay,
        on_envelope=on_envelope,
    )
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put(
            {
                "type": "envelope",
                "sealed": {
                    "kem_suite": "x25519",
                    "eph_pk": "ab" * 32,
                    "ciphertext": "deadbeef",
                },
            },
        )
        for _ in range(100):
            if envelopes:
                break
            await asyncio.sleep(0.02)
        assert len(envelopes) == 1
        assert envelopes[0]["sealed"]["ciphertext"] == "deadbeef"
        # Mutation guard: routing the frame to on_relay instead fails here.
        assert relayed == []
    finally:
        await client.stop()


async def test_client_attach_envelope_handler_late_binds(fake_gfs, http_session):
    """The redeem coordinator is wired after the WS client, so the handler
    must be attachable post-construction."""
    seed, _pub = _gen_keypair()
    envelopes: list[dict] = []

    async def on_envelope(frame: dict) -> None:
        envelopes.append(frame)

    client = GfsWebSocketClient(
        gfs_url=fake_gfs.url,
        instance_id="sh-env2",
        signing_key=seed,
        session_factory=lambda: http_session,
        on_relay=_sink_noop,
    )
    client.attach_envelope_handler(on_envelope)
    await client.start()
    try:
        for _ in range(100):
            if client.connected:
                break
            await asyncio.sleep(0.02)
        await fake_gfs.outbound.put({"type": "envelope", "sealed": {}})
        for _ in range(100):
            if envelopes:
                break
            await asyncio.sleep(0.02)
        assert len(envelopes) == 1
    finally:
        await client.stop()


async def test_on_text_drops_envelope_when_no_handler():
    """No handler attached → the frame is dropped at DEBUG, not crashed."""
    seed, _pub = _gen_keypair()
    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-env3",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=_sink_noop,
    )
    await client._on_text(json.dumps({"type": "envelope", "sealed": {}}))


async def test_on_text_swallows_envelope_handler_exception(caplog):
    """A rejected blob (bad signature, replay, …) is logged, never fatal —
    and the log line carries the rejection, not the ciphertext."""
    seed, _pub = _gen_keypair()

    async def boom(_frame: dict) -> None:
        raise ValueError("Replay detected: bootstrap nonce='n1'")

    client = GfsWebSocketClient(
        gfs_url="https://gfs.test",
        instance_id="sh-env4",
        signing_key=seed,
        session_factory=lambda: None,
        on_relay=_sink_noop,
        on_envelope=boom,
    )
    await client._on_text(
        json.dumps({"type": "envelope", "sealed": {"ciphertext": "secret-blob"}}),
    )
    assert "Replay detected" in caplog.text
    assert "secret-blob" not in caplog.text
