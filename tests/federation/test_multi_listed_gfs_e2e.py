"""v_91 to every GFS on a space's signed list — end to end on the sending side.

An author household with NO GFS connection at all (it reaches the space over
the mesh) broadcasts a post to members reached only through a GFS, whose
member cards name mailboxes on two GFSs the space owner listed
(``spaces.gfs_servers``). Two real GFS apps, the author's real
:class:`GfsConnectionService` (listing targets, proof of possession),
:class:`GfsEnvelopeSender`, :class:`GfsRelayTransport`,
:class:`FederationTransport`, :class:`FederationService` and
:class:`OutboxProcessor`; each upload is then pushed — as the GFS pushes it —
to real receivers (``GfsRelayInbound`` + the §24.11 pipeline).

* each member stores the post, through exactly ONE upload per GFS;
* the space's host carries nothing;
* a member whose card names a GFS that is neither ours nor listed gets no
  request to that GFS at all — it goes through the host hop;
* a GFS the owner delisted between the enqueue and the retry: its rows go
  through the host hop, nothing more goes to that GFS.
"""

from __future__ import annotations

import dataclasses
import os
import socket
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import orjson
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from socialhome.app import _redeliver_envelope
from socialhome.domain.federation import (
    DELIVERY_ERROR_QUEUED,
    DeliveryResult,
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.federation_capabilities import FederationCapability
from socialhome.domain.gfs_mailbox import ENVELOPE_MULTI_PATH
from socialhome.domain.gfs_server import GfsServer
from socialhome.federation.gfs_relay_transport import GfsRelayTransport, MailboxTarget
from socialhome.federation.transport import FederationTransport
from socialhome.global_server.config import GfsConfig
from socialhome.global_server.server import create_gfs_app
from socialhome.infrastructure.outbox_processor import OutboxProcessor
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_envelope_sender import GFS_ENVELOPE_PATH, GfsEnvelopeSender
from tests.federation.test_federation_service import (
    InMemoryFederationRepo,
    InMemoryOutboxRepo,
    _make_kek_manager,
    _make_remote_instance,
    _make_service,
)
from tests.services.test_gfs_relay_inbound import _SPACE, _MultiHousehold

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


POST = FederationEventType.SPACE_POST_CREATED
V90 = FederationCapability.MIN_FOR_MULTI_MAILBOX
CONTENT = {"post_id": "p-1", "content": "hello from the mesh"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Gfs:
    """One real GFS app; records every envelope upload it took."""

    def __init__(self, tc: TestClient, info: dict, gid: str) -> None:
        self.gid = gid
        self.tc = tc
        self.info = info
        self.url = str(tc.make_url("")).rstrip("/")
        self.requests: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, dict]] = []

    def server(self) -> GfsServer:
        return GfsServer(
            gfs_instance_id=self.gid,
            inbox_url=self.url,
            public_key=self.info["public_key"],
            gfs_key_suite="ed25519",
            display_name=self.gid,
        )

    def multi(self) -> list[dict]:
        return [b for p, b in self.uploads if p == ENVELOPE_MULTI_PATH]

    def single(self) -> list[dict]:
        return [b for p, b in self.uploads if p == GFS_ENVELOPE_PATH]


async def _gfs(tmp_dir, gid: str) -> _Gfs:
    port = _free_port()
    (tmp_dir / gid).mkdir()
    app = create_gfs_app(
        GfsConfig(
            host="127.0.0.1",
            port=port,
            base_url=f"http://127.0.0.1:{port}",
            data_dir=str(tmp_dir / gid),
            instance_id=gid,
            cluster_enabled=False,
            cluster_node_id=gid,
            cluster_peers=(),
        )
    )
    holder: list[_Gfs] = []

    @web.middleware
    async def record(request: web.Request, handler):
        if not holder:  # the fixture's own /gfs/info read
            return await handler(request)
        gfs = holder[0]
        gfs.requests.append((request.method, request.path))
        if request.method == "POST" and request.path in (
            GFS_ENVELOPE_PATH,
            ENVELOPE_MULTI_PATH,
        ):
            gfs.uploads.append((request.path, orjson.loads(await request.read())))
        return await handler(request)

    app.middlewares.append(record)
    tc = TestClient(TestServer(app, host="127.0.0.1", port=port))
    await tc.start_server()
    info = await (await tc.get("/gfs/info")).json()
    holder.append(_Gfs(tc, info, gid))
    return holder[0]


class _NoConnections:
    """The author's ``gfs_connections``: none at all."""

    async def list_active(self) -> list[GfsConnection]:
        return []

    async def list_all(self) -> list[GfsConnection]:
        return []


class _SharedSession:
    """The shared mailbox session — only for a GFS we are registered on."""

    def post(self, *_a, **_kw):  # pragma: no cover - the assertion is the point
        raise AssertionError("a listed GFS was reached over the shared session")


class _HostHop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, FederationEventType, dict, str]] = []

    async def send(self, *, to_instance_id, event_type, payload, space_id):
        self.calls.append((to_instance_id, event_type, payload, space_id))
        return DeliveryResult(instance_id=to_instance_id, ok=True, via="host_hop")


async def _never(_sender: str, _space: str) -> bool:
    return False


class _Author:
    """A mesh-only household: no GFS connection, the owner's signed list
    names *listed* (or *member_servers* answers it, e.g. from the stored
    signed list)."""

    def __init__(
        self,
        http: aiohttp.ClientSession,
        listed: list[_Gfs],
        *,
        member_servers: Callable[[str], Awaitable[tuple[GfsServer, ...] | None]]
        | None = None,
    ) -> None:
        self.km = _make_kek_manager()
        self.repo = InMemoryFederationRepo()
        self.outbox = InMemoryOutboxRepo()
        self.svc, self.kp = _make_service(
            federation_repo=self.repo, outbox_repo=self.outbox, key_manager=self.km
        )
        self.listed = listed
        self.opened: list[str] = []
        repo = _NoConnections()

        async def _listing(url: str) -> aiohttp.ClientSession:
            # Production pins a vetted public address; the GFSs here are on
            # loopback, so the test hands a plain cookie-less session.
            self.opened.append(url)
            return aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())

        self.gfs = GfsConnectionService(
            repo,  # type: ignore[arg-type]
            http_client=http,
            listing_sessions=_listing,
        )

        async def _member_servers(space_id: str):
            if space_id != _SPACE:
                return None
            return tuple(g.server() for g in self.listed)

        self.gfs.attach_member_servers(member_servers or _member_servers)
        sender = GfsEnvelopeSender(gfs_service=self.gfs, gfs_repo=repo)  # type: ignore[arg-type]
        sender.attach_mailbox_session(_SharedSession())  # type: ignore[arg-type]
        self.targets: dict[str, MailboxTarget] = {}

        async def _resolve(instance_id: str, space_id: str | None):
            target = self.targets.get(instance_id)
            if target is None or space_id != _SPACE:
                return None
            return dataclasses.replace(target, space_id=space_id)

        async def _signal(*_a, **_kw):  # pragma: no cover - no RTC here
            raise AssertionError("no signalling in a relay test")

        self.svc.attach_transport(
            FederationTransport(
                own_instance_id=self.svc._own_instance_id,
                https_inbox=object(),  # type: ignore[arg-type]
                gfs_relay=GfsRelayTransport(relay_sender=sender),
                mailbox_targets=_resolve,
                signaling_send=_signal,
            )
        )
        self.host_hop = _HostHop()
        self.svc.attach_member_mesh(
            host_hop=self.host_hop,  # type: ignore[arg-type]
            mailbox_sender_check=_never,
        )

    async def member(self, gfs_url: str) -> _MultiHousehold:
        """A member household seated from the host's directory (no address,
        no ``relay_via``), its card's mailbox on *gfs_url*."""
        rx = _MultiHousehold()
        await rx.repo.save_instance(
            RemoteInstance(
                id=self.svc._own_instance_id,
                display_name="author",
                remote_identity_pk=self.kp.public_key.hex(),
                key_self_to_remote="unused",
                key_remote_to_self="unused",
                remote_inbox_url="",
                local_inbox_id="l-" + self.svc._own_instance_id[:8],
                status=PairingStatus.CONFIRMED,
                source=InstanceSource.SPACE_SESSION,
            )
        )
        rx.cards.add((self.svc._own_instance_id, _SPACE))
        seat, _ = _make_remote_instance(self.km, session_key=os.urandom(32))
        await self.repo.save_instance(
            dataclasses.replace(
                seat,
                id=rx.own_id,
                source=InstanceSource.SPACE_SESSION,
                remote_inbox_url="",
                relay_via="",
                proto_version=V90,
            )
        )
        self.repo.add_space_member(_SPACE, rx.own_id)
        self.targets[rx.own_id] = MailboxTarget(
            mailbox_id=rx.mailbox.mailbox_id,
            kem_pk=rx.mailbox.kem_public,
            gfs_url=gfs_url,
        )
        return rx

    async def drain(self) -> None:
        """One pass of the real outbox; a row in no group goes through the
        real per-row redelivery."""

        async def _single(entry):
            return await _redeliver_envelope(self.svc, self.repo, entry)

        repo = MagicMock()
        repo.list_due = AsyncMock(return_value=self.outbox.entries())
        repo.mark_delivered = AsyncMock()
        repo.mark_failed = AsyncMock()
        repo.reschedule = AsyncMock()
        await OutboxProcessor(repo, _single, multi=self.svc).drain_once()


def _pushed(upload: dict, rx: _MultiHousehold) -> dict:
    """What the GFS pushes to *rx*'s mailbox: the body plus its own wrap."""
    wrap = {e["mailbox"]: e["wrap"] for e in upload["to"]}[rx.mailbox.mailbox_id]
    return {"type": "envelope", "sealed": {**upload["body"], "wrap": wrap}}


def _mailboxes(upload: dict) -> list[str]:
    return sorted(e["mailbox"] for e in upload["to"])


@pytest.fixture
async def world(tmp_dir):
    a = await _gfs(tmp_dir, "gfs-a")
    b = await _gfs(tmp_dir, "gfs-b")
    c = await _gfs(tmp_dir, "gfs-c")  # on nobody's list
    http = aiohttp.ClientSession()
    yield a, b, c, http
    await http.close()
    for g in (a, b, c):
        await g.tc.close()


async def test_members_on_two_listed_gfss_get_one_upload_each_and_the_host_none(
    world,
):
    a, b, _c, http = world
    author = _Author(http, [a, b])
    on_a = [await author.member(a.url) for _ in range(2)]
    on_b = [await author.member(b.url) for _ in range(2)]

    result = await author.svc.broadcast_to_space_members(_SPACE, POST, dict(CONTENT))
    assert {r.error for r in result.results} == {DELIVERY_ERROR_QUEUED}
    await author.drain()

    # ONE upload per GFS, naming exactly that GFS's members' mailboxes.
    [up_a] = a.multi()
    [up_b] = b.multi()
    assert _mailboxes(up_a) == sorted(rx.mailbox.mailbox_id for rx in on_a)
    assert _mailboxes(up_b) == sorted(rx.mailbox.mailbox_id for rx in on_b)
    assert a.single() == b.single() == []
    # Each GFS proved the listed key at the listed URL first, over its own
    # cookie-less session — never the shared one.
    assert ("POST", "/gfs/proof") in a.requests
    assert set(author.opened) == {a.url, b.url}
    # Every member stores the post; the host carried nothing.
    for upload, members in ((up_a, on_a), (up_b, on_b)):
        for rx in members:
            assert await rx.frame(_pushed(upload, rx)) == {"status": "ok"}
            [event] = rx.dispatched
            assert event.from_instance == author.svc._own_instance_id
            assert event.payload == CONTENT
    assert author.host_hop.calls == []


async def test_a_gfs_neither_ours_nor_listed_goes_through_the_host(world):
    a, _b, c, http = world
    author = _Author(http, [a])
    on_a = [await author.member(a.url) for _ in range(2)]
    elsewhere = await author.member(c.url)

    await author.svc.broadcast_to_space_members(_SPACE, POST, dict(CONTENT))
    await author.drain()

    [up_a] = a.multi()
    assert _mailboxes(up_a) == sorted(rx.mailbox.mailbox_id for rx in on_a)
    # Not a single request reached the unlisted GFS — not even a probe.
    assert c.requests == []
    assert author.host_hop.calls == [(elsewhere.own_id, POST, CONTENT, _SPACE)]


async def test_a_gfs_delisted_before_the_retry_hands_its_rows_to_the_host(world):
    a, b, _c, http = world
    author = _Author(http, [a, b])
    on_a = [await author.member(a.url) for _ in range(2)]
    on_b = [await author.member(b.url) for _ in range(2)]

    await author.svc.broadcast_to_space_members(_SPACE, POST, dict(CONTENT))
    # The owner re-signs the list without B before the outbox drains.
    author.listed = [a]
    await author.drain()

    assert len(a.multi()) == 1
    assert b.multi() == b.single() == []
    assert sorted(iid for iid, *_ in author.host_hop.calls) == sorted(
        rx.own_id for rx in on_b
    )
    assert all(
        (t, p, s) == (POST, CONTENT, _SPACE) for _iid, t, p, s in author.host_hop.calls
    )
    assert _mailboxes(a.multi()[0]) == sorted(rx.mailbox.mailbox_id for rx in on_a)
