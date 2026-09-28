"""Shared fixtures for all test directories."""

from __future__ import annotations

import asyncio
import sys
import tempfile
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import ModuleType
from typing import AsyncIterator

# ── Fake aiolibdatachannel ───────────────────────────────────────────────
# Injected into sys.modules BEFORE any production code imports it. The CI
# runner does not ship the native binding; production code must never
# contain stub branches — test-level mocks are the only mechanism.

_STUB_SDP = "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\na=mock\r\n"


# Vendored copies of the enum values + state-event dataclasses the
# library publishes. Production code references these (e.g.
# ``rtc.SignalingState.STABLE``); the fake module needs to expose the
# same names so production import paths resolve identically.
class _FakeSignalingState(IntEnum):
    STABLE = 0
    HAVE_LOCAL_OFFER = 1
    HAVE_REMOTE_OFFER = 2
    HAVE_LOCAL_PRANSWER = 3
    HAVE_REMOTE_PRANSWER = 4


class _FakeRTCState(IntEnum):
    NEW = 0
    CONNECTING = 1
    CONNECTED = 2
    DISCONNECTED = 3
    FAILED = 4
    CLOSED = 5


class _FakeICEState(IntEnum):
    NEW = 0
    CHECKING = 1
    CONNECTED = 2
    COMPLETED = 3
    FAILED = 4
    DISCONNECTED = 5
    CLOSED = 6


class _FakeGatheringState(IntEnum):
    NEW = 0
    IN_PROGRESS = 1
    COMPLETE = 2


@dataclass(slots=True, frozen=True)
class _FakeStateChangeEvent:
    state: _FakeRTCState


@dataclass(slots=True, frozen=True)
class _FakeIceStateChangeEvent:
    state: _FakeICEState


@dataclass(slots=True, frozen=True)
class _FakeSignalingStateChangeEvent:
    state: _FakeSignalingState


@dataclass(slots=True, frozen=True)
class _FakeGatheringStateChangeEvent:
    state: _FakeGatheringState


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
        # Bind the sub-tasks so the pending one (and both on cancel)
        # can be cancelled — otherwise they orphan and the HA-custom-
        # component pytest plugin flags them as lingering at teardown.
        get_task = asyncio.create_task(self._inbox.get())
        close_task = asyncio.create_task(self._closed.wait())
        try:
            done, pending = await asyncio.wait(
                [get_task, close_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            get_task.cancel()
            close_task.cancel()
            raise
        for task in pending:
            task.cancel()
        if self._closed.is_set():
            raise StopAsyncIteration
        for task in done:
            if task.done() and not task.cancelled():
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
        self._ice_queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._tasks: list[asyncio.Task] = []
        # Tracks the last set_local_description / set_remote_description calls
        # to expose a realistic ``signaling_state`` used by perfect negotiation.
        self._local_type: str | None = None
        self._remote_type: str | None = None

    @property
    def signaling_state(self):
        """Return a ``SignalingState`` IntEnum value based on local/remote
        types.  Earlier versions of this stub returned plain strings, which
        masked a production bug: ``transport.py`` compared the real
        ``aiolibdatachannel.SignalingState`` (an ``IntEnum``) against
        string literals and the comparison always returned True
        (IntEnum != str), so every legitimate RTC handshake was silently
        rejected.  Returning the IntEnum here keeps the stub honest
        against the library's contract."""
        if self._closed:
            # No ``CLOSED`` in this enum — the lib drops the PC; the
            # closest neutral pre-close state is STABLE.
            return _FakeSignalingState.STABLE
        if self._local_type == "offer" and self._remote_type is None:
            return _FakeSignalingState.HAVE_LOCAL_OFFER
        if self._remote_type == "offer" and self._local_type is None:
            return _FakeSignalingState.HAVE_REMOTE_OFFER
        if self._local_type == "answer":
            # The lib doesn't surface "have-local-answer" as a distinct
            # state; transition straight to STABLE the moment the answer
            # is set locally.
            return _FakeSignalingState.STABLE
        if self._remote_type == "answer":
            return _FakeSignalingState.STABLE
        return _FakeSignalingState.STABLE

    def events(self) -> AsyncIterator:
        """Empty events iterator — production code uses this to drain
        state-change events; tests don't exercise transitions."""

        async def _empty():
            if False:
                yield  # pragma: no cover

        return _empty()

    async def create_data_channel(self, label: str, options=None):
        ch = _FakeDataChannel(label)
        self._channels.append(ch)
        return ch

    async def set_local_description(self, type_: str = "offer"):
        self._local_type = type_
        return _FakeLocalDescription(sdp=_STUB_SDP, type=type_)

    async def create_answer(self):
        """Non-trickle answer: waits for ICE gathering to finish (no-op
        in the stub) and returns an SDP with candidates inlined.
        Matches the real library's :meth:`PeerConnection.create_answer`
        contract used by ``HighlightSignalingHandler``'s production
        peer factory."""
        self._local_type = "answer"
        return _FakeLocalDescription(sdp=_STUB_SDP, type="answer")

    async def set_remote_description(self, sdp: str, type_: str) -> None:
        self._remote_type = type_
        return None

    async def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        return None

    async def ice_candidates(self):
        # Tests inject candidates by pushing onto ``self._ice_queue``
        # before triggering the gathering path. ``None`` is the
        # close-sentinel that mirrors the real lib's iterator-exit
        # contract. By default the queue is empty so callers that
        # don't push anything see an empty stream — same shape as
        # "gathering produced no candidates".
        while not self._closed:
            try:
                cand = await asyncio.wait_for(
                    self._ice_queue.get(),
                    timeout=0.01,
                )
            except asyncio.TimeoutError:
                return
            if cand is None:
                return
            yield cand

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

    async def aclose(self) -> None:
        """Async-close — mirrors the real library's contract of waiting
        for spawned tasks to drain before returning. Production code
        uses ``aclose()`` for guaranteed teardown order."""
        self.close()
        await asyncio.sleep(0)


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
# Vendored enums + state-event dataclasses for code paths that reference
# them by ``rtc.SignalingState`` / ``rtc.RTCState`` / etc.
_fake_rtc.SignalingState = _FakeSignalingState  # type: ignore[attr-defined]
_fake_rtc.RTCState = _FakeRTCState  # type: ignore[attr-defined]
_fake_rtc.ICEState = _FakeICEState  # type: ignore[attr-defined]
_fake_rtc.GatheringState = _FakeGatheringState  # type: ignore[attr-defined]
_fake_rtc.StateChangeEvent = _FakeStateChangeEvent  # type: ignore[attr-defined]
_fake_rtc.IceStateChangeEvent = _FakeIceStateChangeEvent  # type: ignore[attr-defined]
_fake_rtc.SignalingStateChangeEvent = _FakeSignalingStateChangeEvent  # type: ignore[attr-defined]
_fake_rtc.GatheringStateChangeEvent = _FakeGatheringStateChangeEvent  # type: ignore[attr-defined]
sys.modules["aiolibdatachannel"] = _fake_rtc

# ── Regular fixtures ─────────────────────────────────────────────────────
# Imports below MUST come after the sys.modules injection above so the
# fake aiolibdatachannel is resolved when production modules load.

import dataclasses  # noqa: E402
import functools  # noqa: E402
import inspect  # noqa: E402

import pytest  # noqa: E402

from socialhome.config import Config  # noqa: E402
from socialhome.crypto import generate_identity_keypair, derive_instance_id  # noqa: E402
from socialhome.db import database as _db_module  # noqa: E402
from socialhome.db.database import AsyncDatabase  # noqa: E402
from socialhome.global_server.config import GfsConfig  # noqa: E402
from socialhome.infrastructure.event_bus import EventBus  # noqa: E402
from socialhome.repositories.conversation_repo import SqliteConversationRepo  # noqa: E402
from socialhome.repositories.notification_repo import SqliteNotificationRepo  # noqa: E402
from socialhome.repositories.post_repo import SqlitePostRepo  # noqa: E402
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo  # noqa: E402
from socialhome.repositories.space_repo import SqliteSpaceRepo  # noqa: E402
from socialhome.repositories.user_repo import SqliteUserRepo  # noqa: E402
from socialhome.infrastructure.key_manager import KeyManager  # noqa: E402
from socialhome.services.feed_service import FeedService  # noqa: E402
from socialhome.services.space_service import SpaceService  # noqa: E402
from socialhome.services.user_service import UserService  # noqa: E402

from tests.migration_template import MigrationTemplateCache  # noqa: E402


#: Write-coalescing window every test database runs with; see
#: ``_fast_test_databases``.
TEST_WRITE_WINDOW_MS = 1

#: Windows at or below this are the suite's "don't make me wait" value (the
#: ~220 ``batch_timeout_ms=10`` / ``db_write_batch_timeout_ms=10`` call
#: sites) and are collapsed to :data:`TEST_WRITE_WINDOW_MS`.
_FAST_WINDOW_CEILING_MS = 10

#: The production default window, as ``Config``, ``GfsConfig`` and
#: ``AsyncDatabase`` each declare it (5 ms today; it was 500 ms). A test that never chose a window runs
#: with this one, so it is collapsed too. Any OTHER explicit window is a test
#: choosing one on purpose (the batching tests use 200 ms) and is kept.
_PRODUCTION_WINDOWS_MS = frozenset(
    {
        next(
            f.default
            for f in dataclasses.fields(Config)
            if f.name == "db_write_batch_timeout_ms"
        ),
        inspect.signature(AsyncDatabase.__init__)
        .parameters["batch_timeout_ms"]
        .default,
        GfsConfig().write_batch_window_ms,
    }
)


def _test_write_window_ms(requested: int) -> int:
    """The window a test database actually runs with."""
    if requested <= _FAST_WINDOW_CEILING_MS or requested in _PRODUCTION_WINDOWS_MS:
        return TEST_WRITE_WINDOW_MS
    return requested


@pytest.fixture(scope="session", autouse=True)
def _fast_test_databases():
    """Two test-boundary speed-ups for every ``AsyncDatabase`` in the suite.

    * **Migrated-schema template.** ``AsyncDatabase.startup`` replays the
      full migration chain on every fresh file. Wrap the runner it calls so
      the first empty DB per process pays for the real chain and every later
      one is a page copy of that result — see ``tests/migration_template.py``.
    * **Write-batch window.** The writer waits the full window for
      companions before it commits (no lock held while it waits), so each *sequential* ``enqueue`` costs
      at least one window. Tests pass 10 ms to mean "fast", yet 10 ms per
      write was the single largest cost of the suite (a fixture seeding 40
      rows spent 0.4 s just waiting), and every test that never chose a
      window paid the production default per write (500 ms until that
      default came down to a few ms). Collapse those to
      :data:`TEST_WRITE_WINDOW_MS`. Writes queued in the same tick still
      coalesce (the drain takes whatever is already queued), and a window a
      test set on purpose is kept, so the database's own batching tests are
      unaffected.
    """
    cache = MigrationTemplateCache(_db_module.run_migrations)
    real_init = AsyncDatabase.__init__
    default_window = inspect.signature(real_init).parameters["batch_timeout_ms"].default

    @functools.wraps(real_init)
    def _init(self, path, *, batch_timeout_ms=default_window, **kwargs):
        real_init(
            self,
            path,
            batch_timeout_ms=_test_write_window_ms(batch_timeout_ms),
            **kwargs,
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_db_module, "run_migrations", cache)
        mp.setattr(AsyncDatabase, "__init__", _init)
        yield cache
    cache.close()


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
    return SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32))


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
        SqliteSpaceRepo(db, key_manager=KeyManager(b"\x0c" * 32)),
        SqliteSpacePostRepo(db),
        SqliteUserRepo(db),
        bus,
        own_instance_id=iid,
    )
