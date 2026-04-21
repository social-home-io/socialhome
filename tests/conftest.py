"""Shared fixtures for all test directories."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import ModuleType

# ── Fake libdatachannel ──────────────────────────────────────────────────
# Injected into sys.modules BEFORE any production code imports it. The CI
# runner does not ship the native binding; production code must never
# contain stub branches — test-level mocks are the only mechanism.

_STUB_SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=mock\r\n"


class _FakeChannel:
    """Minimal stand-in for a libdatachannel DataChannel."""

    def __init__(self, label: str = "fed-v1") -> None:
        self._label = label
        self.sent: list[bytes | str] = []
        self._on_open = None
        self._on_closed = None
        self._on_message = None

    def getLabel(self) -> str:
        return self._label

    def onOpen(self, cb):
        self._on_open = cb

    def onClosed(self, cb):
        self._on_closed = cb

    def onMessage(self, cb):
        self._on_message = cb

    def sendMessage(self, data):
        self.sent.append(data)


class _FakePeerConnection:
    """Minimal stand-in for libdatachannel.PeerConnection."""

    def __init__(self, cfg=None) -> None:
        self._local_desc = _STUB_SDP
        self._on_data_channel = None
        self._on_local_candidate = None
        self._channels: list[_FakeChannel] = []

    # SDP lifecycle
    def setLocalDescription(self, sdp_type: str = "offer") -> None:
        pass

    def setRemoteDescription(self, sdp: str, sdp_type: str = "answer") -> None:
        pass

    def localDescription(self) -> str:  # noqa: N802 — matches real C++ binding API
        return self._local_desc

    # ICE
    def addRemoteCandidate(self, candidate: str, sdp_mid: str = "0") -> None:
        pass

    def onLocalCandidate(self, cb):
        self._on_local_candidate = cb

    # DataChannel
    def createDataChannel(self, label: str) -> _FakeChannel:
        ch = _FakeChannel(label)
        self._channels.append(ch)
        return ch

    def onDataChannel(self, cb):
        self._on_data_channel = cb

    # Cleanup
    def close(self) -> None:
        pass


class _FakeConfiguration:
    """Stand-in for libdatachannel.Configuration."""

    def __init__(self) -> None:
        self.iceServers: list = []


class _FakeIceServer:
    """Stand-in for libdatachannel.IceServer."""

    def __init__(self, url: str = "", username: str = "", password: str = "") -> None:
        self.url = url
        self.username = username
        self.password = password


# Build fake module and inject before anything imports it.
_fake_ldc = ModuleType("libdatachannel")
_fake_ldc.PeerConnection = _FakePeerConnection  # type: ignore[attr-defined]
_fake_ldc.Configuration = _FakeConfiguration  # type: ignore[attr-defined]
_fake_ldc.IceServer = _FakeIceServer  # type: ignore[attr-defined]
sys.modules["libdatachannel"] = _fake_ldc

# ── Regular fixtures ─────────────────────────────────────────────────────
# Imports below MUST come after the sys.modules injection above so the
# fake libdatachannel is resolved when production modules load.

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
