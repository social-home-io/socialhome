"""End-to-end §D2b: two households join a space through a connection server.

Real crypto, real SQLite, real HTTP — the only stand-in is the connection
server itself, which implements the contract the GFS half is built to:
``POST /gfs/envelope`` stores a blob by ``to_instance`` and pushes it down
the addressed household's socket.

Household **b** owns a space and mints an invite link. Household **a** has
never met b: no pairing, no mesh route, no address. It pastes the link and
ends up seated — and the connection server, which saw every byte that
crossed it, never saw the token, the space, the users or who was asking.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.capabilities_sig import sign_capabilities
from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    generate_identity_keypair,
    generate_x25519_keypair,
    sign_ed25519,
)
from socialhome.db.database import AsyncDatabase
from socialhome.domain.federation import (
    GfsConnection,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.space import JoinMode, SpaceType
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.federation.federation_service import FederationService
from socialhome.federation.invite_bootstrap import InviteBootstrapHint
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_envelope_sender import GfsEnvelopeSender
from socialhome.services.space_service import SpaceService


try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


GFS_INSTANCE_ID = "gfs-e2e"
TOKEN_MARKER = "tok-do-not-leak-me"
SPACE_NAME = "Book club"


class _FakeGfs:
    """A connection server implementing the §D2b relay contract.

    Stores every envelope by ``to_instance`` (that is all it can key on)
    and pushes it to the addressed household's socket. It answers a uniform
    ``202 {"status": "accepted"}`` so a sender learns nothing about the
    recipient — not even whether it exists.
    """

    def __init__(self) -> None:
        kp = generate_identity_keypair()
        self.seed = kp.private_key
        self.public_key_hex = kp.public_key.hex()
        #: (to_instance, body) for every POST — what a curious operator of
        #: this server would have in their logs.
        self.mailbox: list[tuple[str, dict]] = []
        #: instance_id → the household's "socket" (its redeem coordinator).
        self.sockets: dict[str, object] = {}
        self.url = ""
        self._tasks: set[asyncio.Task] = set()

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/gfs/info", self._info)
        app.router.add_post("/gfs/envelope", self._envelope)
        return app

    async def _info(self, _request: web.Request) -> web.Response:
        capabilities = {"envelope_relay": True, "anonymous_publish": True}
        sig, suite = sign_capabilities(self.seed, GFS_INSTANCE_ID, capabilities)
        return web.json_response(
            {
                "instance_id": GFS_INSTANCE_ID,
                "public_key": self.public_key_hex,
                "capabilities": capabilities,
                "capabilities_sig": sig,
                "capabilities_sig_suite": suite,
            },
        )

    async def _envelope(self, request: web.Request) -> web.Response:
        body = await request.json()
        to_instance = str(body.get("to_instance") or "")
        self.mailbox.append((to_instance, body))
        target = self.sockets.get(to_instance)
        if target is not None:
            # The push leg: a real GFS writes the frame to the household's
            # open socket, so delivery is concurrent with this response.
            task = asyncio.create_task(
                target.handle_relayed_envelope(
                    {"sealed": body.get("sealed")},
                    gfs_url=self.url,
                ),
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return web.json_response({"status": "accepted"}, status=202)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def _household(tmp_path, name: str, gfs: _FakeGfs, http_session):
    """One fully-wired household on its own SQLite file."""
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()

    ident = generate_identity_keypair()
    instance_id = derive_instance_id(ident.public_key)
    keywrap = generate_x25519_keypair()
    keywrap_sig = b64url_encode(sign_ed25519(ident.private_key, keywrap.public_key))
    key_manager = KeyManager(bytes([len(name)]) * 32)

    await db.enqueue(
        """INSERT INTO instance_identity(instance_id, identity_private_key,
           identity_public_key, routing_secret, display_name)
           VALUES(?,?,?,?,?)""",
        (
            instance_id,
            ident.private_key.hex(),
            ident.public_key.hex(),
            "aa" * 32,
            f"{name} household",
        ),
    )

    bus = EventBus()
    space_repo = SqliteSpaceRepo(db, key_manager=key_manager)
    user_repo = SqliteUserRepo(db)
    user_repo.attach_key_manager(key_manager)
    federation_repo = SqliteFederationRepo(db)
    remote_members = SqliteSpaceRemoteMemberRepo(db)
    gfs_repo = SqliteGfsConnectionRepo(db)

    federation = FederationService(
        db,
        federation_repo,
        SqliteOutboxRepo(db),
        key_manager,
        bus,
        instance_id,
        ident.private_key,
        ident.public_key,
    )

    await gfs_repo.save(
        GfsConnection(
            id="gfs-1",
            gfs_instance_id=GFS_INSTANCE_ID,
            display_name="Community relay",
            public_key=gfs.public_key_hex,
            inbox_url=gfs.url,
            status="active",
            paired_at="2026-01-01T00:00:00+00:00",
        ),
    )
    gfs_service = GfsConnectionService(gfs_repo, http_client=http_session)

    space_service = SpaceService(
        space_repo,
        SqliteSpacePostRepo(db),
        user_repo,
        bus,
        own_instance_id=instance_id,
    )
    coordinator = SpaceInviteTokenRedeemCoordinator(
        bus=bus,
        federation_service=federation,
        space_repo=space_repo,
        space_remote_member_repo=remote_members,
        user_repo=user_repo,
        federation_repo=federation_repo,
    )
    coordinator.attach_bootstrap(
        relay_sender=GfsEnvelopeSender(gfs_service=gfs_service, gfs_repo=gfs_repo),
        keywrap_private_key=keywrap.private_key,
        keywrap_public_key=keywrap.public_key,
        keywrap_sig=keywrap_sig,
        key_manager=key_manager,
    )
    space_service.attach_redeem_coordinator(coordinator)
    gfs.sockets[instance_id] = coordinator

    user_id = f"{name}-user-id"
    await user_repo.save(
        User(
            user_id=user_id,
            username=f"{name}user",
            display_name=f"{name.upper()} person",
        ),
    )

    return SimpleNamespace(
        db=db,
        name=name,
        instance_id=instance_id,
        identity_pk=ident.public_key,
        keywrap_pub=keywrap.public_key,
        keywrap_sig=keywrap_sig,
        space_repo=space_repo,
        federation_repo=federation_repo,
        remote_members=remote_members,
        space_service=space_service,
        coordinator=coordinator,
        user_id=user_id,
        username=f"{name}user",
    )


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def gfs():
    fake = _FakeGfs()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await fake.drain()
    await server.close()


@pytest.fixture
async def households(tmp_path, gfs, http_session):
    a = await _household(tmp_path, "a", gfs, http_session)
    b = await _household(tmp_path, "b", gfs, http_session)
    yield a, b
    await a.db.shutdown()
    await b.db.shutdown()


async def _mint_invite(b, gfs_url: str):
    """b creates a space and publishes the invite blob for it."""
    space = await b.space_service.create_space(
        owner_username=b.username,
        name=SPACE_NAME,
        space_type=SpaceType.PRIVATE,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await b.space_repo.create_invite_token(space.id, b.user_id, uses=1)
    # The token value is chosen by the repo; re-mint a known one so the
    # leak assertions below can grep for a distinctive marker.
    await b.db.enqueue(
        "UPDATE space_invite_tokens SET token=? WHERE space_id=?",
        (TOKEN_MARKER, space.id),
    )
    return space, InviteBootstrapHint(
        invite_token=TOKEN_MARKER,
        space_id=space.id,
        instance_id=b.instance_id,
        identity_pk=b.identity_pk.hex(),
        keywrap_pk=b.keywrap_pub.hex(),
        keywrap_sig=b.keywrap_sig,
        proto_version=OURS,
        display_hint=SPACE_NAME,
        gfs_url=gfs_url,
    )


async def test_a_joins_bs_space_through_the_connection_server(households, gfs):
    """No pairing, no mesh, no address — just a link and a relay."""
    a, b = households
    space, hint = await _mint_invite(b, gfs.url)

    result = await a.space_service.redeem_invite_token(
        TOKEN_MARKER,
        user_id=a.user_id,
        issuer_instance_id=b.instance_id,
        bootstrap=hint,
    )

    assert result["space_id"] == space.id
    assert result["role"] == "member"

    # a holds a real seat: a local stub + its own membership row.
    seated = await a.space_repo.get(space.id)
    assert seated is not None
    assert seated.name == SPACE_NAME
    member = await a.space_repo.get_member(space.id, a.user_id)
    assert member is not None

    # b seated a as a remote member of the space.
    roster = await b.remote_members.list_for_space(space.id)
    assert [m.user_id for m in roster] == [a.user_id]

    # Both sides hold a SPACE-SCOPED instance row for the other: keyed and
    # CONFIRMED (the space has to federate), but not a social peer and with
    # no address, because neither household published one.
    a_side = await a.federation_repo.get_instance(b.instance_id)
    b_side = await b.federation_repo.get_instance(a.instance_id)
    for row in (a_side, b_side):
        assert row is not None
        assert row.source is InstanceSource.SPACE_SESSION
        assert row.status is PairingStatus.CONFIRMED
        assert row.remote_inbox_url == ""

    # The token is spent — a second paste of the same link fails.
    assert await b.space_repo.consume_invite_token(TOKEN_MARKER) is None


async def test_the_connection_server_sees_only_a_recipient_and_ciphertext(
    households,
    gfs,
):
    """Everything the relay could log, on both legs, holds no identity."""
    a, b = households
    space, hint = await _mint_invite(b, gfs.url)

    await a.space_service.redeem_invite_token(
        TOKEN_MARKER,
        user_id=a.user_id,
        issuer_instance_id=b.instance_id,
        bootstrap=hint,
    )
    await gfs.drain()

    # One request leg + one reply leg.
    assert len(gfs.mailbox) == 2
    assert [to for to, _ in gfs.mailbox] == [b.instance_id, a.instance_id]

    everything = json.dumps(gfs.mailbox)
    for marker in (
        TOKEN_MARKER,  # the invite token
        space.id,  # which space
        SPACE_NAME,  # even the space's name
        a.user_id,  # who is joining
        b.user_id,  # who owns it
        a.username,
        b.username,
    ):
        assert marker not in everything, f"relay saw {marker!r}"

    for body in (gfs.mailbox[0][1], gfs.mailbox[1][1]):
        # No ``from_instance`` — the routing envelope names the recipient
        # and nothing else.
        assert set(body) == {"to_instance", "sealed"}
        assert set(body["sealed"]) == {"kem_suite", "eph_pk", "ciphertext"}

    # The *sender* of each leg is invisible: a's id appears only as the
    # recipient of the reply, b's only as the recipient of the request.
    assert a.instance_id not in json.dumps(gfs.mailbox[0][1])
    assert b.instance_id not in json.dumps(gfs.mailbox[1][1])


async def test_a_relay_that_cannot_carry_invites_fails_the_redeem(
    households,
    gfs,
    monkeypatch,
):
    """The capability gate: an un-upgraded server gets no blob at all, and
    the user reads why instead of waiting out a timeout."""
    from socialhome.services.gfs_envelope_sender import EnvelopeRelayUnavailable

    a, b = households
    _space, hint = await _mint_invite(b, gfs.url)

    async def _no_capability(_self, _conn):
        return False

    monkeypatch.setattr(
        GfsConnectionService,
        "envelope_relay_supported",
        _no_capability,
    )
    with pytest.raises(EnvelopeRelayUnavailable):
        await a.space_service.redeem_invite_token(
            TOKEN_MARKER,
            user_id=a.user_id,
            issuer_instance_id=b.instance_id,
            bootstrap=hint,
        )
    assert gfs.mailbox == []
