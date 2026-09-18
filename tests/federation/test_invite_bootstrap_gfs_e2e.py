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
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import orjson
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
    FederationEvent,
    FederationEventType,
    GfsConnection,
    InstanceSource,
    PairingStatus,
    RemoteInstance,
)
from socialhome.domain.events import LocalHomeLocationUpdated
from socialhome.domain.federation_capabilities import OURS
from socialhome.domain.space import JoinMode, SpaceType
from socialhome.domain.user import User
from socialhome.infrastructure.event_bus import EventBus
from socialhome.federation.federation_service import FederationService
from socialhome.federation.invite_bootstrap import InviteBootstrapHint
from socialhome.federation.private_invite_handler import PrivateSpaceInviteHandler
from socialhome.federation.gfs_relay_transport import (
    GfsRelayTransport,
    seal_relay_envelope,
)
from socialhome.federation.invite_token_redeem import (
    SpaceInviteTokenRedeemCoordinator,
)
from socialhome.federation.transport import (
    FederationTransport,
    HttpsInboxTransport,
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
        """Await every push-leg task, including ones spawned while draining.

        A single ``gather`` over a snapshot is not enough: a push leg can
        POST a sealed reply back through this same fake, which spawns
        another task after the snapshot was taken. Looping until the set
        is empty is what makes "drained" actually mean drained.

        ``return_exceptions=True`` is load-bearing too — a push leg that
        raises (an unsigned envelope is meant to be rejected) must have
        its exception retrieved here, or the task dies with an
        unretrieved exception and only surfaces later, from the garbage
        collector, attached to whatever test happened to be running.
        """
        while self._tasks:
            # Take the batch out of the set before awaiting it: the
            # ``discard`` done-callback only runs on a later loop tick, so
            # re-testing ``self._tasks`` against a set the callbacks have
            # not emptied yet would spin forever. Anything spawned by this
            # batch lands in the now-empty set and is picked up next round.
            batch = list(self._tasks)
            self._tasks.clear()
            await asyncio.gather(*batch, return_exceptions=True)


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
    space_service.attach_federation(federation, federation_repo, remote_members)
    # The §D1b/§D2b inbound family, including the SPACE_SESSION_CLEANUP
    # teardown leg the kick path fires.
    private_invites = PrivateSpaceInviteHandler(
        bus=bus,
        space_repo=space_repo,
        remote_member_repo=remote_members,
        space_service=space_service,
    )
    private_invites.attach_to(federation)
    coordinator = SpaceInviteTokenRedeemCoordinator(
        bus=bus,
        federation_service=federation,
        space_repo=space_repo,
        space_remote_member_repo=remote_members,
        user_repo=user_repo,
        federation_repo=federation_repo,
    )
    envelope_sender = GfsEnvelopeSender(gfs_service=gfs_service, gfs_repo=gfs_repo)
    coordinator.attach_bootstrap(
        relay_sender=envelope_sender,
        keywrap_private_key=keywrap.private_key,
        keywrap_public_key=keywrap.public_key,
        keywrap_sig=keywrap_sig,
        key_manager=key_manager,
    )

    # The real transport facade, including the third tier: a household
    # seated from an invite link has no address, so its envelopes are
    # sealed to its key-wrap key and carried by the connection server.
    async def _client_factory():
        return http_session

    async def _no_signaling(to_instance_id, event_type, payload):
        # No RTC in this fixture: an ordinary paired peer falls back to
        # its HTTPS inbox, and a link-joined peer must never get here at
        # all (pinned in tests/federation/test_federation_transport.py).
        raise RuntimeError("no RTC signalling in this fixture")

    fed_transport = FederationTransport(
        own_instance_id=instance_id,
        https_inbox=HttpsInboxTransport(client_factory=_client_factory),
        gfs_relay=GfsRelayTransport(relay_sender=envelope_sender),
        signaling_send=_no_signaling,
    )
    fed_transport.mark_ice_primed()
    federation.attach_transport(fed_transport)

    #: Every federation event this household's §24.11 pipeline validated,
    #: decrypted and dispatched. Registering a capture handler is the
    #: receiving end of "did the envelope actually arrive and decrypt".
    received: list[FederationEvent] = []

    async def _capture(event: FederationEvent) -> None:
        received.append(event)

    for _evt in (
        FederationEventType.SPACE_POST_CREATED,
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
    ):
        federation._event_registry.register(_evt, _capture)
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
        bus=bus,
        name=name,
        key_manager=key_manager,
        federation=federation,
        transport=fed_transport,
        received=received,
        identity_seed=ident.private_key,
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
    # Drain BEFORE the databases go. The fake GFS's push leg is a detached
    # ``asyncio.create_task`` (a real server writes the frame to the
    # household's socket while the POST is still returning), so a test that
    # sends a relayed envelope and then simply ends leaves that task mid
    # ``handle_relayed_envelope`` — which is mid SQLite read on ``a.db``.
    # ``households`` depends on ``gfs``, so ``gfs``'s own finalizer runs
    # AFTER this one: draining there would already be too late, with the
    # connection closed underneath the task. Draining here is also what
    # retrieves the exception from a push leg that was meant to be rejected.
    await gfs.drain()
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


# ─── The delivery leg: space events over the relay ────────────────────────


POST_MARKER = "post-body-do-not-leak"


def _post_payload(marker: str = POST_MARKER) -> dict:
    return {
        "post_id": "post-1",
        "author_user_id": "author-user-id",
        "content": marker,
        "created_at": "2026-09-18T10:00:00+00:00",
    }


async def _join(a, b, gfs):
    """b mints an invite, a redeems it. Returns the space."""
    space, hint = await _mint_invite(b, gfs.url)
    await a.space_service.redeem_invite_token(
        TOKEN_MARKER,
        user_id=a.user_id,
        issuer_instance_id=b.instance_id,
        bootstrap=hint,
    )
    await gfs.drain()
    a.received.clear()
    b.received.clear()
    gfs.mailbox.clear()
    return space


async def test_the_host_reaches_a_link_joined_member(households, gfs):
    """The gap this closes: before the relay tier, a space fan-out to a
    household seated from an invite link went to a transport with no
    address and was simply lost."""
    a, b = households
    space = await _join(a, b, gfs)

    result = await b.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload(),
    )
    await gfs.drain()

    assert result.succeeded == 1
    assert [e.event_type for e in a.received] == [
        FederationEventType.SPACE_POST_CREATED,
    ]
    # Decrypted under the pair's session key, by the ordinary §24.11
    # pipeline — the relay only carried ciphertext.
    assert a.received[0].payload["content"] == POST_MARKER
    assert a.received[0].space_id == space.id
    assert a.received[0].from_instance == b.instance_id


async def test_a_link_joined_member_reaches_the_host(households, gfs):
    """Both directions, or the member is a read-only guest."""
    a, b = households
    space = await _join(a, b, gfs)

    result = await a.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload("member-wrote-this"),
    )
    await gfs.drain()

    assert result.succeeded == 1
    assert [e.payload["content"] for e in b.received] == ["member-wrote-this"]


async def test_a_roster_change_and_a_rekey_reach_the_link_joined_member(
    households,
    gfs,
):
    """Not just posts: roster events and the next content-key epoch ride
    the same leg, or the member silently rots out of the space."""
    a, b = households
    space = await _join(a, b, gfs)

    for event_type, payload in (
        (FederationEventType.SPACE_MEMBER_JOINED, {"user_id": "new-member"}),
        (
            FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
            {"epoch": 2, "wrapped_key": "sealed-for-the-member"},
        ),
        (FederationEventType.SPACE_POST_CREATED, _post_payload("after-the-rekey")),
    ):
        await b.federation.broadcast_to_space_members(space.id, event_type, payload)
    await gfs.drain()

    assert [e.event_type for e in a.received] == [
        FederationEventType.SPACE_MEMBER_JOINED,
        FederationEventType.SPACE_KEY_EXCHANGE_REKEY,
        FederationEventType.SPACE_POST_CREATED,
    ]
    assert a.received[1].payload["epoch"] == 2
    # The post that FOLLOWS the rekey still decrypts: the pair's session
    # key is independent of the space content key epoch.
    assert a.received[2].payload["content"] == "after-the-rekey"


async def test_every_relayed_body_is_a_recipient_and_ciphertext(households, gfs):
    """§24.11 envelopes carry PLAINTEXT routing fields. If one reached the
    relay unsealed, the connection server would learn who talks to whom,
    about which space, and what event. Nothing but the recipient may."""
    a, b = households
    space = await _join(a, b, gfs)

    await b.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload(),
    )
    await a.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload("and-back-again"),
    )
    await gfs.drain()

    assert len(gfs.mailbox) == 2
    everything = json.dumps(gfs.mailbox)
    for marker in (
        "from_instance",
        "event_type",
        "space_id",
        "space_post_created",
        space.id,
        POST_MARKER,
        "and-back-again",
        a.user_id,
        b.user_id,
        SPACE_NAME,
    ):
        assert marker not in everything, f"relay saw {marker!r}"
    for _to_instance, body in gfs.mailbox:
        assert set(body) == {"to_instance", "sealed"}
        assert set(body["sealed"]) == {"kem_suite", "eph_pk", "ciphertext"}
        assert body["sealed"]["kem_suite"] == "x25519"
    # Each leg names only its recipient — never its sender.
    assert gfs.mailbox[0][0] == a.instance_id
    assert a.instance_id not in json.dumps(gfs.mailbox[1][1])


async def test_a_tampered_blob_is_dropped_at_the_receiver(households, gfs):
    """A relay that flips a byte gets nothing dispatched. (The WS client
    swallows the raise — see tests/services/test_gfs_ws_client.py.)"""
    a, b = households
    space = await _join(a, b, gfs)

    await b.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload(),
    )
    await gfs.drain()
    _to, body = gfs.mailbox[-1]
    a.received.clear()

    sealed = dict(body["sealed"])
    nonce, ct = sealed["ciphertext"].split(":", 1)
    sealed["ciphertext"] = f"{nonce}:{ct[:-4]}AAAA"

    with pytest.raises(ValueError):
        await a.coordinator.handle_relayed_envelope({"sealed": sealed})
    assert a.received == []


async def test_an_envelope_from_a_household_that_is_not_this_pair_is_rejected(
    households,
    gfs,
    caplog,
):
    """The seal is confidentiality, not authorization. Anyone who reads
    the public invite blob knows the key-wrap key and can seal a blob to
    it — the §24.11 signature under the PAIR key is what says who sent
    it, and it runs unchanged on the relay leg."""
    a, b = households
    space = await _join(a, b, gfs)

    stranger = generate_identity_keypair()
    envelope = {
        "msg_id": "forged-1",
        "event_type": FederationEventType.SPACE_POST_CREATED.value,
        # Claims to be b — the household a actually holds keys for.
        "from_instance": b.instance_id,
        "to_instance": a.instance_id,
        # Fresh, so the rejection is the SIGNATURE and not the clock.
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "encrypted_payload": "nonce:ciphertext",
        "space_id": space.id,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = {
        "ed25519": b64url_encode(
            sign_ed25519(stranger.private_key, orjson.dumps(envelope)),
        ),
    }
    sealed = seal_relay_envelope(
        envelope_dict=envelope,
        peer_keywrap_pub=a.keywrap_pub,
    )

    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError, match="signature"):
            await a.coordinator.handle_relayed_envelope({"sealed": sealed})
    assert a.received == []
    # A relay envelope that fails validation is dropped — but never
    # silently. The line names the event type, the claimed sender and the
    # reason, and nothing from inside the payload.
    rejections = [
        r.getMessage()
        for r in caplog.records
        if "gfs_relay: rejected" in r.getMessage()
    ]
    assert len(rejections) == 1
    assert "space_post_created" in rejections[0]
    assert b.instance_id in rejections[0]
    assert "signature" in rejections[0]


async def test_the_seat_survives_a_restart(households, gfs):
    """The key-wrap key and the introducing server are read off the row on
    every send, so they have to be on disk, not in memory."""
    a, b = households
    space = await _join(a, b, gfs)

    # A cold repo over the same file — what the next boot sees.
    reloaded = SqliteFederationRepo(b.db)
    row = await reloaded.get_instance(a.instance_id)
    assert row is not None
    assert row.remote_keywrap_pk == a.keywrap_pub.hex()
    assert row.relay_via == gfs.url
    assert row.source is InstanceSource.SPACE_SESSION

    # And it still delivers off that reloaded row alone.
    ok, _status = await b.transport._gfs_relay.send(
        instance=row,
        envelope_dict={
            "msg_id": "after-restart",
            "event_type": FederationEventType.SPACE_POST_CREATED.value,
            "from_instance": b.instance_id,
            "to_instance": a.instance_id,
            "timestamp": "2026-09-18T10:00:00+00:00",
            "encrypted_payload": "x",
            "space_id": space.id,
            "proto_version": 1,
            "sig_suite": "ed25519",
            "signatures": {"ed25519": "not-checked-here"},
        },
    )
    assert ok is True
    assert gfs.mailbox[-1][0] == a.instance_id


# ─── Mixed fan-out: a link-joined member and an ordinary paired one ───────


async def _pair_over_https(host, peer, peer_base_url: str):
    """Seat ``host`` and ``peer`` as an ordinary CONFIRMED pair."""
    key_h_to_p = bytes([1]) * 32
    key_p_to_h = bytes([2]) * 32
    await host.federation_repo.save_instance(
        RemoteInstance(
            id=peer.instance_id,
            display_name=peer.name,
            remote_identity_pk=peer.identity_pk.hex(),
            key_self_to_remote=host.key_manager.encrypt(key_h_to_p),
            key_remote_to_self=host.key_manager.encrypt(key_p_to_h),
            remote_inbox_url=f"{peer_base_url}/inbox/inbox-{peer.name}",
            local_inbox_id=f"inbox-{host.name}-for-{peer.name}",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )
    await peer.federation_repo.save_instance(
        RemoteInstance(
            id=host.instance_id,
            display_name=host.name,
            remote_identity_pk=host.identity_pk.hex(),
            key_self_to_remote=peer.key_manager.encrypt(key_p_to_h),
            key_remote_to_self=peer.key_manager.encrypt(key_h_to_p),
            remote_inbox_url="",
            local_inbox_id=f"inbox-{peer.name}",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )


async def test_one_fanout_reaches_a_relayed_member_and_a_paired_member(
    households,
    gfs,
    tmp_path,
    http_session,
):
    """The host's own members do not all share a transport: the link-joined
    household rides the relay, an ordinary paired household keeps its
    HTTPS inbox, and one ``broadcast_to_space_members`` serves both."""
    a, b = households
    space = await _join(a, b, gfs)

    c = await _household(tmp_path, "c", gfs, http_session)
    try:
        inbox = web.Application()

        async def _inbox(request: web.Request) -> web.Response:
            await c.federation.handle_inbound_envelope(
                request.match_info["inbox_id"],
                await request.read(),
            )
            return web.json_response({"status": "ok"})

        inbox.router.add_post("/inbox/{inbox_id}", _inbox)
        server = TestServer(inbox)
        await server.start_server()
        try:
            await _pair_over_https(b, c, str(server.make_url("")).rstrip("/"))
            await b.space_repo.add_space_instance(space.id, c.instance_id)

            result = await b.federation.broadcast_to_space_members(
                space.id,
                FederationEventType.SPACE_POST_CREATED,
                _post_payload("everyone-gets-this"),
            )
            await gfs.drain()

            assert result.attempted == 2
            assert result.succeeded == 2
            assert [e.payload["content"] for e in a.received] == [
                "everyone-gets-this",
            ]
            assert [e.payload["content"] for e in c.received] == [
                "everyone-gets-this",
            ]
            # The paired household's envelope never touched the relay.
            assert [to for to, _ in gfs.mailbox] == [a.instance_id]
        finally:
            await server.close()
    finally:
        await c.db.shutdown()


# ─── What a link-joined household must NOT get ───────────────────────────


async def test_the_household_gps_never_reaches_a_link_joined_member(
    households,
    gfs,
):
    """Our home coordinates are a social disclosure.

    ``LOCAL_HOME_LOCATION_CHANGED`` fans out to confirmed peers, and a
    household seated from an invite link is CONFIRMED — so the raw
    ``list_instances`` fan-out shipped the family's street to anybody who
    redeemed a link. Twice over: ``share_home`` also defaults True and the
    §D2b seat never overrode it.
    """
    a, b = households
    space = await _join(a, b, gfs)
    assert space is not None

    # The seat itself is closed by default.
    seat = await b.federation_repo.get_instance(a.instance_id)
    assert seat is not None
    assert seat.share_home is False

    await b.bus.publish(
        LocalHomeLocationUpdated(latitude=52.3676, longitude=4.9041),
    )
    await gfs.drain()

    assert gfs.mailbox == [], "home GPS was relayed to a link-joined household"
    assert a.received == []
    row = await a.federation_repo.get_instance(b.instance_id)
    assert row is not None
    assert row.home_lat is None
    assert row.home_lon is None


NON_SPACE_PROBE_TYPES = [
    FederationEventType.DM_MESSAGE,
    FederationEventType.DM_MESSAGE_DELETED,
    FederationEventType.DM_CONTACT_REQUEST,
    FederationEventType.USERS_SYNC,
    FederationEventType.USER_UPDATED,
    FederationEventType.USER_IDENTITY_RESOLVE,
    FederationEventType.USER_ONLINE,
    FederationEventType.CALL_OFFER,
    FederationEventType.CALL_ICE,
    FederationEventType.PRESENCE_UPDATED,
    FederationEventType.MOMENT_CREATED,
    FederationEventType.HIGHLIGHT_CREATED,
    FederationEventType.NETWORK_SYNC,
    FederationEventType.URL_UPDATED,
    FederationEventType.SPACE_FIND_ROUTE,
    FederationEventType.SPACE_ROUTE_FOUND,
    FederationEventType.SPACE_ROUTED,
    FederationEventType.SPACE_ADMIN_KEY_SHARE,
    FederationEventType.SPACE_JOIN_REQUEST,
    FederationEventType.SPACE_DIRECTORY_SYNC,
]


async def test_a_link_joined_household_cannot_push_non_space_events(
    households,
    gfs,
):
    """The seat is space-scoped. Before the peer-class gate the §24.11
    pipeline had no notion of peer class at all, so every one of these
    validated, decrypted and dispatched on the host."""
    a, b = households
    space = await _join(a, b, gfs)

    dispatched: list[FederationEventType] = []

    async def _spy(event: FederationEvent) -> None:
        dispatched.append(event.event_type)

    for probe in NON_SPACE_PROBE_TYPES:
        b.federation._event_registry.register(probe, _spy)

    for probe in NON_SPACE_PROBE_TYPES:
        result = await a.federation.send_event(
            to_instance_id=b.instance_id,
            event_type=probe,
            payload={"probe": probe.value},
            space_id=space.id if probe.value.startswith("space_") else None,
        )
        assert result.ok is True, "the relay accepts everything by design"
    await gfs.drain()

    assert len(NON_SPACE_PROBE_TYPES) == 20
    assert dispatched == [], f"host dispatched {dispatched} from a link-joined peer"


async def test_the_space_vocabulary_still_flows_both_ways(households, gfs):
    """The gate is a filter, not a wall — the allow-listed families that
    make a space work are untouched."""
    a, b = households
    space = await _join(a, b, gfs)

    await b.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_POST_CREATED,
        _post_payload("still-flows"),
    )
    await a.federation.broadcast_to_space_members(
        space.id,
        FederationEventType.SPACE_MEMBER_JOINED,
        {"user_id": "someone"},
    )
    await gfs.drain()

    assert [e.payload["content"] for e in a.received] == ["still-flows"]
    assert [e.event_type for e in b.received] == [
        FederationEventType.SPACE_MEMBER_JOINED,
    ]


# ─── The relay queue is a mailbox, not a wire ────────────────────────────


async def _sealed_envelope_from(sender, recipient, *, timestamp, msg_id, space_id):
    """One real §24.11 envelope from *sender* to *recipient*, signed and
    encrypted under their pair keys, at an arbitrary timestamp."""
    row = await sender.federation_repo.get_instance(recipient.instance_id)
    session_key = sender.key_manager.decrypt(row.key_self_to_remote)
    envelope = {
        "msg_id": msg_id,
        "event_type": FederationEventType.SPACE_POST_CREATED.value,
        "from_instance": sender.instance_id,
        "to_instance": recipient.instance_id,
        "timestamp": timestamp,
        "encrypted_payload": sender.federation._encrypt_payload(
            orjson.dumps(_post_payload("queued-overnight")).decode(),
            session_key,
        ),
        "space_id": space_id,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = sender.federation._encoder.sign_envelope_all(
        orjson.dumps(envelope),
        suite="ed25519",
    )
    return seal_relay_envelope(
        envelope_dict=envelope,
        peer_keywrap_pub=recipient.keywrap_pub,
    )


async def test_an_envelope_the_relay_queued_overnight_is_accepted(households, gfs):
    """The relay answers ``202`` and holds the blob for up to 24 h, so the
    sender never used the outbox. Judging the drained bytes against the
    ±300 s live-wire window would lose the event outright."""
    a, b = households
    space = await _join(a, b, gfs)

    queued_at = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    sealed = await _sealed_envelope_from(
        b,
        a,
        timestamp=queued_at,
        msg_id="queued-20h",
        space_id=space.id,
    )

    await a.coordinator.handle_relayed_envelope({"sealed": sealed})

    assert [e.payload["content"] for e in a.received] == ["queued-overnight"]


async def test_a_queued_envelope_replayed_hours_later_is_still_rejected(
    households,
    gfs,
):
    """The wider window is paid for by the replay cache outlasting it."""
    a, b = households
    space = await _join(a, b, gfs)

    sealed = await _sealed_envelope_from(
        b,
        a,
        timestamp=(datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(),
        msg_id="replay-me",
        space_id=space.id,
    )
    await a.coordinator.handle_relayed_envelope({"sealed": sealed})
    a.received.clear()

    # The same bytes again, 10 h into the window a relay capture could
    # replay them in.
    with pytest.raises(ValueError, match="[Rr]eplay"):
        await a.coordinator.handle_relayed_envelope({"sealed": sealed})
    assert a.received == []


async def test_a_wire_envelope_keeps_the_300s_window(households, gfs):
    """Only the relay tier is widened; the live wire is unchanged."""
    a, b = households
    space = await _join(a, b, gfs)

    stale = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat()
    row = await b.federation_repo.get_instance(a.instance_id)
    session_key = b.key_manager.decrypt(row.key_self_to_remote)
    envelope = {
        "msg_id": "stale-on-the-wire",
        "event_type": FederationEventType.SPACE_POST_CREATED.value,
        "from_instance": b.instance_id,
        "to_instance": a.instance_id,
        "timestamp": stale,
        "encrypted_payload": b.federation._encrypt_payload(
            orjson.dumps(_post_payload()).decode(),
            session_key,
        ),
        "space_id": space.id,
        "proto_version": 1,
        "sig_suite": "ed25519",
    }
    envelope["signatures"] = b.federation._encoder.sign_envelope_all(
        orjson.dumps(envelope),
        suite="ed25519",
    )

    with pytest.raises(ValueError, match="skew"):
        await a.federation.handle_inbound_rtc(
            b.instance_id,
            orjson.dumps(envelope),
        )


async def test_a_relay_delivery_result_is_labelled_as_acceptance(households, gfs):
    """``ok=True`` off the relay means the server took it, not that the
    other household has it — the label is what stops an operator-facing
    count reading it as confirmed delivery."""
    a, b = households
    space = await _join(a, b, gfs)

    result = await b.federation.send_event(
        to_instance_id=a.instance_id,
        event_type=FederationEventType.SPACE_POST_CREATED,
        payload=_post_payload(),
        space_id=space.id,
    )
    await gfs.drain()

    assert result.ok is True
    assert result.via == "gfs_relay"


# ─── The seat is revoked when the membership that bought it ends ─────────


async def test_a_kick_revokes_the_space_scoped_seat_on_both_sides(households, gfs):
    """Kick / ban / leave / dissolve used to leave the ``space_session``
    row and its live session keys in place forever — a household with no
    remaining relationship to us still holding valid keys to us.

    Both directions: the host drops its row AND tells the kicked household
    to drop the mirror it holds (``SPACE_SESSION_CLEANUP``, allow-listed
    for this peer class because it is the teardown of the relationship
    itself).
    """
    a, b = households
    space = await _join(a, b, gfs)

    assert await a.federation_repo.get_instance(b.instance_id) is not None
    assert await b.federation_repo.get_instance(a.instance_id) is not None

    await b.space_service.remove_remote_member(
        space.id,
        actor_username=b.username,
        instance_id=a.instance_id,
        user_id=a.user_id,
    )
    await gfs.drain()

    # Host side: the seat is gone.
    assert await b.federation_repo.get_instance(a.instance_id) is None
    # Kicked side: the CLEANUP landed and its mirror is gone too.
    assert await a.federation_repo.get_instance(b.instance_id) is None


async def test_a_seat_carrying_a_second_shared_space_survives_a_kick(
    households,
    gfs,
):
    """The choke point asks 'any space at all', not 'this one'."""
    a, b = households
    space = await _join(a, b, gfs)

    # A second shared space with the same household.
    await b.space_repo.add_space_instance("sp-second", a.instance_id)
    await a.space_repo.add_space_instance("sp-second", b.instance_id)

    await b.space_service.remove_remote_member(
        space.id,
        actor_username=b.username,
        instance_id=a.instance_id,
        user_id=a.user_id,
    )
    await gfs.drain()

    assert await b.federation_repo.get_instance(a.instance_id) is not None
    assert await a.federation_repo.get_instance(b.instance_id) is not None


async def test_a_cleanup_we_disagree_with_is_ignored(households, gfs):
    """The receiver re-derives the answer from its OWN rows — a peer that
    shares two spaces with us and leaves one cannot tear down the seat the
    other still needs."""
    a, b = households
    space = await _join(a, b, gfs)
    assert space is not None
    await a.space_repo.add_space_instance("sp-other", b.instance_id)

    dropped = await a.space_service.apply_space_session_cleanup(b.instance_id)

    assert dropped is False
    assert await a.federation_repo.get_instance(b.instance_id) is not None


async def test_a_qr_paired_peer_is_never_revoked(households, gfs, tmp_path):
    """Only ``space_session`` seats have a space-bounded lifetime; a real
    pairing outlives every space."""
    a, b = households
    space = await _join(a, b, gfs)

    await b.federation_repo.save_instance(
        RemoteInstance(
            id="manual-peer",
            display_name="QR friend",
            remote_identity_pk="ab" * 32,
            key_self_to_remote=b.key_manager.encrypt(bytes(32)),
            key_remote_to_self=b.key_manager.encrypt(bytes(32)),
            remote_inbox_url="http://friend.example/inbox/x",
            local_inbox_id="inbox-manual",
            status=PairingStatus.CONFIRMED,
            source=InstanceSource.MANUAL,
        ),
    )
    await b.space_repo.add_space_instance(space.id, "manual-peer")
    await b.space_repo.remove_space_instance(space.id, "manual-peer")

    assert await b.space_service.revoke_space_session_if_orphaned("manual-peer") is (
        False
    )
    assert await b.federation_repo.get_instance("manual-peer") is not None


async def test_the_gps_fanout_skips_a_link_joined_seat_even_with_share_home_on(
    households,
    gfs,
):
    """The second, independent half of the fix.

    ``share_home=False`` on the seat and ``list_social_instances`` in the
    fan-out each stop this on their own — which is the point, since either
    is a one-line edit away from being undone. This test forces the flag
    back on so only the fan-out's peer selection is left holding the line.
    """
    a, b = households
    space = await _join(a, b, gfs)
    assert space is not None
    await b.federation_repo.set_share_home(a.instance_id, value=True)

    await b.bus.publish(
        LocalHomeLocationUpdated(latitude=52.3676, longitude=4.9041),
    )
    await gfs.drain()

    assert gfs.mailbox == [], "home GPS was relayed to a link-joined household"
    assert a.received == []
