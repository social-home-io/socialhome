"""Shared fixtures for all test directories."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

# ── Fake aiolibdatachannel ───────────────────────────────────────────────
# Injected into sys.modules BEFORE any production code imports it. The CI
# runner does not ship the native binding; production code must never
# contain stub branches — test-level mocks are the only mechanism.

_STUB_SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=mock\r\n"


@dataclass(slots=True)
class _FakeLocalDescription:
    sdp: str
    type: str


@dataclass(slots=True)
class _FakeIceCandidate:
    candidate: str
    mid: str


class _FakeDataChannel:
    """Minimal async stand-in for :class:`aiolibdatachannel.DataChannel`."""

    def __init__(self, label: str = "fed-v1") -> None:
        self.label = label
        self.sent: list[bytes | str] = []
        self.is_closed = False
        self.is_open = False
        # Tests can set this to simulate backpressure.
        self.buffered_amount: int = 0
        self._low_threshold: int = 0
        self._open = asyncio.Event()
        self._closed = asyncio.Event()
        self._inbox: asyncio.Queue = asyncio.Queue()

    def set_buffered_amount_low_threshold(self, n: int) -> None:
        self._low_threshold = n

    async def wait_open(self) -> None:
        # In tests we treat the channel as opened immediately so
        # is_ready() flips true without the provider having to drive
        # real DTLS. Production code goes through aiolibdatachannel.
        self.is_open = True
        self._open.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def send(self, data) -> None:
        self.sent.append(data)

    async def recv(self):
        return await self._inbox.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Tests rarely want real inbound frames on fakes; the queue
        # stays empty so the async-for loop awaits until the channel
        # closes. Fake the behaviour by waiting on the close event.
        done, _ = await asyncio.wait(
            [
                asyncio.create_task(self._inbox.get()),
                asyncio.create_task(self._closed.wait()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._closed.is_set():
            raise StopAsyncIteration
        for task in done:
            if task.done():
                return task.result()
        raise StopAsyncIteration

    def close(self) -> None:
        self.is_closed = True
        self._closed.set()


class _FakePeerConnection:
    """Minimal stand-in for :class:`aiolibdatachannel.PeerConnection`."""

    def __init__(self, config=None) -> None:
        self._config = config
        self._channels: list[_FakeDataChannel] = []
        self._incoming_queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._tasks: list[asyncio.Task] = []

    async def create_data_channel(self, label: str, options=None):
        ch = _FakeDataChannel(label)
        self._channels.append(ch)
        return ch

    async def set_local_description(self, type_: str = "offer"):
        return _FakeLocalDescription(sdp=_STUB_SDP, type=type_)

    async def set_remote_description(self, sdp: str, type_: str) -> None:
        return None

    async def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        return None

    async def ice_candidates(self):
        # Yield nothing — equivalent to "gathering produced no
        # candidates", enough for tests that only care about SDP flow.
        if False:
            yield _FakeIceCandidate("", "")

    async def incoming_data_channels(self):
        # Drain the queue until closed.
        while not self._closed:
            ch = await self._incoming_queue.get()
            if ch is None:
                return
            yield ch

    def spawn_task(self, coro):
        """Mirror of the real API: spawn a task bound to the pc's lifetime."""
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def close(self) -> None:
        self._closed = True
        # Terminate the incoming-channels iterator.
        try:
            self._incoming_queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        # Cancel every task registered via spawn_task, mirroring the
        # real library's lifetime guarantee.
        for task in self._tasks:
            if not task.done():
                task.cancel()
        self._tasks.clear()


class _FakeRTCConfiguration:
    """Stand-in for :class:`aiolibdatachannel.RTCConfiguration`."""

    def __init__(self, *, ice_servers=None, **_kw) -> None:
        self.ice_servers = list(ice_servers or [])


class _FakeDataChannelOptions:
    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


class _FakeConnectionClosedError(Exception):
    pass


class _FakeRTCError(Exception):
    pass


@dataclass(slots=True)
class _FakeIceServer:
    url: str
    username: str | None = None
    credential: str | None = None


def _fake_install_python_logger(*_a, **_kw):
    """No-op stand-in for :func:`aiolibdatachannel.install_python_logger`."""
    import logging as _logging

    return _logging.getLogger("aiolibdatachannel")


# Build fake module and inject before anything imports it.
_fake_rtc = ModuleType("aiolibdatachannel")
_fake_rtc.PeerConnection = _FakePeerConnection  # type: ignore[attr-defined]
_fake_rtc.RTCConfiguration = _FakeRTCConfiguration  # type: ignore[attr-defined]
_fake_rtc.DataChannel = _FakeDataChannel  # type: ignore[attr-defined]
_fake_rtc.DataChannelOptions = _FakeDataChannelOptions  # type: ignore[attr-defined]
_fake_rtc.IceCandidate = _FakeIceCandidate  # type: ignore[attr-defined]
_fake_rtc.IceServer = _FakeIceServer  # type: ignore[attr-defined]
_fake_rtc.LocalDescription = _FakeLocalDescription  # type: ignore[attr-defined]
_fake_rtc.ConnectionClosedError = _FakeConnectionClosedError  # type: ignore[attr-defined]
_fake_rtc.RTCError = _FakeRTCError  # type: ignore[attr-defined]
_fake_rtc.install_python_logger = _fake_install_python_logger  # type: ignore[attr-defined]
sys.modules["aiolibdatachannel"] = _fake_rtc

# ── Regular fixtures ─────────────────────────────────────────────────────
# Imports below MUST come after the sys.modules injection above so the
# fake aiolibdatachannel is resolved when production modules load.

import pytest  # noqa: E402

from social_home.crypto import generate_identity_keypair, derive_instance_id  # noqa: E402
from social_home.db.database import AsyncDatabase  # noqa: E402
from social_home.infrastructure.event_bus import EventBus  # noqa: E402
from social_home.repositories.conversation_repo import SqliteConversationRepo  # noqa: E402
from social_home.repositories.notification_repo import SqliteNotificationRepo  # noqa: E402
from social_home.repositories.post_repo import SqlitePostRepo  # noqa: E402
from social_home.repositories.space_post_repo import SqliteSpacePostRepo  # noqa: E402
from social_home.repositories.space_repo import SqliteSpaceRepo  # noqa: E402
from social_home.repositories.user_repo import SqliteUserRepo  # noqa: E402
from social_home.services.feed_service import FeedService  # noqa: E402
from social_home.services.space_service import SpaceService  # noqa: E402
from social_home.services.user_service import UserService  # noqa: E402


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
async def db(tmp_dir):
    """A fully-migrated AsyncDatabase in a temp directory."""
    database = AsyncDatabase(tmp_dir / "test.db", batch_timeout_ms=10)
    await database.startup()
    yield database
    await database.shutdown()


@pytest.fixture
def keypair():
    return generate_identity_keypair()


@pytest.fixture
async def seeded_db(db, keypair):
    """DB with instance_identity seeded."""
    iid = derive_instance_id(keypair.public_key)
    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret) VALUES(?,?,?,?)""",
        (iid, keypair.private_key.hex(), keypair.public_key.hex(), "aa" * 32),
    )
    return db, iid


@pytest.fixture
def bus():
    return EventBus()


@pytest.fixture
def user_repo(db):
    return SqliteUserRepo(db)


@pytest.fixture
def post_repo(db):
    return SqlitePostRepo(db)


@pytest.fixture
def space_repo(db):
    return SqliteSpaceRepo(db)


@pytest.fixture
def space_post_repo(db):
    return SqliteSpacePostRepo(db)


@pytest.fixture
def notification_repo(db):
    return SqliteNotificationRepo(db, max_per_user=20)


@pytest.fixture
def conversation_repo(db):
    return SqliteConversationRepo(db)


@pytest.fixture
async def user_service(seeded_db, bus):
    db, iid = seeded_db
    repo = SqliteUserRepo(db)
    _kp = generate_identity_keypair()
    # Re-read the actual public key from the DB
    row = await db.fetchone(
        "SELECT identity_public_key FROM instance_identity WHERE id='self'"
    )
    pk = bytes.fromhex(row["identity_public_key"])
    return UserService(repo, bus, own_instance_public_key=pk)


@pytest.fixture
async def feed_service(seeded_db, bus):
    db, _ = seeded_db
    return FeedService(SqlitePostRepo(db), SqliteUserRepo(db), bus)


@pytest.fixture
async def space_service(seeded_db, bus):
    db, iid = seeded_db
    return SpaceService(
        SqliteSpaceRepo(db),
        SqliteSpacePostRepo(db),
        SqliteUserRepo(db),
        bus,
        own_instance_id=iid,
    )
