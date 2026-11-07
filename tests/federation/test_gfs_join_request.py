"""Ask to join a global space through the GFS (v_84), end to end.

Household **b** hosts a global space in "ask to join" mode, listed on a GFS.
Household **a** is connected to that GFS and to nothing else: no pairing,
no mesh route, no invite link. It found the space in its GFS directory.

Before v_84 that was the end of it: ``POST /api/public_spaces/{id}/join-request``
answered ``403 NOT_PAIRED`` and Discover offered no button. Here a asks, b's
admin approves, and a ends up a member of b's space — over the GFS relay
only, which routes sealed blobs and never reads the request, the token or
who is asking.

Real crypto, real SQLite, real HTTP; the only stand-in is the GFS (the
envelope relay + the signed ``/gfs/spaces/{id}/contact`` lookup).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from socialhome.canonical_json import canonical_bytes
from socialhome.capabilities_sig import sign_capabilities
from socialhome.crypto import b64url_decode, verify_ed25519
from socialhome.domain.events import SpaceJoinRequested
from socialhome.domain.federation import InstanceSource
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.space import (
    HostNotPairedError,
    HostNotReachableError,
    JoinMode,
    SpaceType,
)
from socialhome.federation import gfs_join_request as gfs_join_module
from socialhome.federation.gfs_join_request import GfsJoinRequestCoordinator
from socialhome.federation.invite_bootstrap import (
    KIND_JOIN_REQUEST,
    KIND_JOIN_REQUEST_ANSWER,
    sign_bootstrap_body,
)
from socialhome.rate_limiter import RateLimiter
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.public_space_repo import (
    PublicSpaceListing,
    SqlitePublicSpaceRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_envelope_sender import GfsEnvelopeSender
from tests.federation.test_invite_bootstrap_gfs_e2e import (
    GFS_INSTANCE_ID,
    _FakeGfs,
    _household,
)

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


MESSAGE = "hello-from-a-do-not-leak"


class _ContactGfs(_FakeGfs):
    """The relay fake plus the ``space_contact`` lookup: the owner's
    REGISTERED keys for a listed space, only on a request signed by a
    registered household."""

    def __init__(self) -> None:
        super().__init__()
        #: instance id → (identity pk hex, keywrap pk hex, keywrap sig)
        self.registered: dict[str, tuple[str, str, str]] = {}
        #: space id → owning instance
        self.listed: dict[str, str] = {}
        self.contact_asks: list[str] = []

    def app(self) -> web.Application:
        app = super().app()
        app.router.add_post("/gfs/spaces/{space_id}/contact", self._contact)
        return app

    async def _info(self, _request: web.Request) -> web.Response:
        capabilities = {
            "envelope_relay": True,
            "anonymous_publish": True,
            "space_contact": True,
        }
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

    async def _contact(self, request: web.Request) -> web.Response:
        space_id = request.match_info["space_id"]
        body = await request.json()
        asker = self.registered.get(str(body.get("instance_id")))
        if asker is None:
            return web.json_response({"error": "unknown"}, status=403)
        signed = canonical_bytes(
            {
                "action": "space_contact",
                "instance_id": body["instance_id"],
                "space_id": space_id,
                "ts": body["ts"],
            }
        )
        if not verify_ed25519(
            bytes.fromhex(asker[0]), signed, b64url_decode(body["signature"])
        ):
            return web.json_response({"error": "bad sig"}, status=403)
        self.contact_asks.append(space_id)
        owner = self.listed.get(space_id)
        if owner is None:
            return web.json_response({"error": "not found"}, status=404)
        identity_pk, keywrap_pk, keywrap_sig = self.registered[owner]
        return web.json_response(
            {
                "instance_id": owner,
                "identity_public_key": identity_pk,
                "keywrap_public_key": keywrap_pk,
                "kem_suite": "x25519",
                "keywrap_sig": keywrap_sig,
            }
        )


async def _wire_join(h, gfs: _ContactGfs, http_session):
    """Wire the v_84 coordinator onto a household the way ``app.py`` does."""
    gfs_repo = SqliteGfsConnectionRepo(h.db)
    gfs_service = GfsConnectionService(gfs_repo, http_client=http_session)
    gfs_service.attach_publish_context(
        space_repo=h.space_repo,
        own_instance_id=h.instance_id,
        own_signing_key=h.identity_seed,
    )
    user_repo = SqliteUserRepo(h.db)
    user_repo.attach_key_manager(h.key_manager)
    public_spaces = SqlitePublicSpaceRepo(h.db)
    join = GfsJoinRequestCoordinator(
        federation=h.federation,
        federation_repo=h.federation_repo,
        space_repo=h.space_repo,
        public_space_repo=public_spaces,
        user_repo=user_repo,
        bus=h.bus,
        relay_sender=GfsEnvelopeSender(gfs_service=gfs_service, gfs_repo=gfs_repo),
        lookup_contact=gfs_service.fetch_space_contact,
        keywrap_public_key=h.keywrap_pub,
        keywrap_sig=h.keywrap_sig,
    )
    h.relay_inbound.attach_join_requests(join)
    h.space_service.attach_gfs_join_requests(join)
    # As ``app.py`` does (the shared e2e fixture leaves it out): the §D2
    # redeem handlers on the registry, for a household already seated.
    h.coordinator.attach_to(h.federation)
    gfs.registered[h.instance_id] = (
        h.identity_pk.hex(),
        h.keywrap_pub.hex(),
        h.keywrap_sig,
    )
    return SimpleNamespace(join=join, public_spaces=public_spaces)


@pytest.fixture
async def gfs():
    fake = _ContactGfs()
    server = TestServer(fake.app())
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    yield fake
    await fake.drain()
    await server.close()


@pytest.fixture
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def households(tmp_path, gfs, http_session):
    a = await _household(tmp_path, "a", gfs, http_session)
    b = await _household(tmp_path, "b", gfs, http_session)
    a.gfsjoin = await _wire_join(a, gfs, http_session)
    b.gfsjoin = await _wire_join(b, gfs, http_session)
    yield a, b
    await gfs.drain()
    await a.db.shutdown()
    await b.db.shutdown()


async def _global_space(b, gfs: _ContactGfs, a, *, join_mode=JoinMode.REQUEST):
    """b hosts a global space; the GFS lists it and a's poll cached it."""
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Ask Me Global",
        space_type=SpaceType.GLOBAL,
        join_mode=join_mode,
    )
    gfs.listed[space.id] = b.instance_id
    await a.gfsjoin.public_spaces.upsert(
        PublicSpaceListing(
            space_id=space.id,
            instance_id=b.instance_id,
            name="Ask Me Global",
            description=None,
            emoji=None,
            lat=None,
            lon=None,
            radius_km=None,
            member_count=0,
            join_mode=str(join_mode),
        )
    )
    return space


async def test_unpaired_household_asks_through_the_gfs_and_becomes_a_member(
    households, gfs
):
    """The owner's bug: a household on the same GFS but not paired with the
    host can ask to join a global request-mode space, the host approves, and
    it is seated — with no pairing and no invite link."""
    a, b = households
    space = await _global_space(b, gfs, a)
    assert await a.federation_repo.get_instance(b.instance_id) is None

    request_id = await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
        message=MESSAGE,
    )
    await gfs.drain()

    # b holds an ordinary pending request, with the message, in front of its
    # admins…
    pending = await b.space_repo.list_pending_join_requests(space.id)
    assert [(r["id"], r["user_id"], r["message"]) for r in pending] == [
        (request_id, a.user_id, MESSAGE)
    ]
    assert pending[0]["remote_applicant_instance_id"] == a.instance_id
    # …and a shows "Requested".
    assert space.id in await a.space_repo.list_pending_join_request_space_ids_for_user(
        a.user_id
    )

    await b.space_service.approve_join_request(
        request_id, actor_username=b.username, space_id=space.id
    )
    await gfs.drain()
    # The approval's redeem runs as its own task on a.
    for task in list(a.space_service._join_approvals):
        await task
    await gfs.drain()

    member = await a.space_repo.get_member(space.id, a.user_id)
    assert member is not None
    roster = await b.remote_members.list_for_space(space.id)
    assert [m.user_id for m in roster] == [a.user_id]
    # a's copy of the request is answered.
    row = await a.space_repo.get_join_request(request_id)
    assert row["status"] == "approved"

    # The GFS routed blobs and read none of them: no message, no token, no
    # user, no space id inside anything it relayed.
    tokens = [
        r["token"] for r in await b.db.fetchall("SELECT token FROM space_invite_tokens")
    ]
    for _to, body in gfs.mailbox:
        assert set(body) == {"to_instance", "sealed"}
        blob = str(body)
        for secret in (MESSAGE, a.user_id, space.id, *tokens):
            assert secret not in blob


async def test_a_seat_in_one_space_is_no_pairing_for_the_next(households, gfs):
    """Once seated in one of b's spaces, a holds a space-session row for b.
    Asking to join ANOTHER of b's global spaces still goes through the GFS:
    b refuses a SPACE_JOIN_REQUEST from a space-scoped peer (found in the
    demo)."""
    a, b = households
    first = await _global_space(b, gfs, a)
    rid = await a.space_service.request_join_remote(
        first.id, applicant_user_id=a.user_id, host_instance_id=b.instance_id
    )
    await gfs.drain()
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=first.id
    )
    await gfs.drain()
    for task in list(a.space_service._join_approvals):
        await task
    await gfs.drain()
    seat = await a.federation_repo.get_instance(b.instance_id)
    assert seat is not None and seat.source is InstanceSource.SPACE_SESSION

    second = await _global_space(b, gfs, a)
    rid2 = await a.space_service.request_join_remote(
        second.id, applicant_user_id=a.user_id, host_instance_id=b.instance_id
    )
    await gfs.drain()
    pending = await b.space_repo.list_pending_join_requests(second.id)
    assert [r["id"] for r in pending] == [rid2]
    await b.space_service.approve_join_request(
        rid2, actor_username=b.username, space_id=second.id
    )
    await gfs.drain()
    for task in list(a.space_service._join_approvals):
        await task
    await gfs.drain()
    assert await a.space_repo.get_member(second.id, a.user_id) is not None


async def test_denied_request_reaches_the_applicant(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    request_id = await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
    )
    await gfs.drain()
    await b.space_service.deny_join_request(
        request_id, actor_username=b.username, space_id=space.id
    )
    await gfs.drain()
    row = await a.space_repo.get_join_request(request_id)
    assert row["status"] == "denied"
    assert await a.space_repo.get_member(space.id, a.user_id) is None


async def test_a_space_the_gfs_does_not_list_under_that_host_still_needs_pairing(
    households, gfs
):
    """Only a listing in our own GFS directory, under that very host, opens
    the GFS path — anything else answers NOT_PAIRED as before."""
    a, b = households
    space = await _global_space(b, gfs, a)
    with pytest.raises(HostNotPairedError):
        await a.space_service.request_join_remote(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id="z" * 32,
        )
    assert gfs.contact_asks == []


async def test_invite_only_space_files_nothing_and_says_nothing(households, gfs):
    """A space that takes no requests files nothing in front of the
    admins, and says nothing back — the applicant can't tell."""
    a, b = households
    space = await _global_space(b, gfs, a, join_mode=JoinMode.INVITE_ONLY)
    await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
    )
    await gfs.drain()
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_no_contact_on_any_gfs_is_host_not_reachable(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    gfs.listed.pop(space.id)
    with pytest.raises(HostNotReachableError):
        await a.space_service.request_join_remote(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id=b.instance_id,
        )


async def test_keys_not_bound_to_the_listed_host_are_refused(households, gfs):
    """A GFS that hands out somebody else's keys under the listed host's id
    gets nothing sealed: the binding is checked end to end."""
    a, b = households
    space = await _global_space(b, gfs, a)
    # The GFS swaps in a's own keys for b's.
    gfs.registered[b.instance_id] = gfs.registered[a.instance_id]
    with pytest.raises(HostNotReachableError):
        await a.space_service.request_join_remote(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id=b.instance_id,
        )
    assert gfs.mailbox == []


async def test_an_older_host_drops_it_and_the_request_waits(households, gfs):
    """A host below v_84 has no handler: the body is dropped there, nothing
    is filed, and the applicant's request stays "Requested" until it
    expires — as an unanswered paired request does."""
    a, b = households
    space = await _global_space(b, gfs, a)
    b.relay_inbound.attach_join_requests(None)  # type: ignore[arg-type]
    await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
    )
    await gfs.drain()
    assert await b.space_repo.list_pending_join_requests(space.id) == []
    assert await a.space_repo.list_pending_join_request_space_ids_for_user(
        a.user_id
    ) == [space.id]


async def test_an_answer_from_another_household_changes_nothing(households, gfs):
    """Only the household the request went to answers it: a forged answer
    sealed by anyone else (here: a fresh household c, also on the GFS)
    leaves the request pending."""
    a, b = households
    space = await _global_space(b, gfs, a)
    request_id = await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
    )
    await gfs.drain()
    # b's admin answers; a's row is re-pointed at another host first, so
    # b is now "someone else" for that request.
    await a.db.enqueue(
        "UPDATE space_join_requests SET remote_applicant_instance_id=? WHERE id=?",
        ("z" * 32, request_id),
    )
    await b.space_service.approve_join_request(
        request_id, actor_username=b.username, space_id=space.id
    )
    await gfs.drain()
    row = await a.space_repo.get_join_request(request_id)
    assert row["status"] == "pending"
    assert await a.space_repo.get_member(space.id, a.user_id) is None


async def test_a_request_id_already_in_use_is_refused(households, gfs):
    """The applicant picks the id; one that already names a request on the
    host is refused rather than filed twice under one id."""
    a, b = households
    space = await _global_space(b, gfs, a)
    first = await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
    )
    await gfs.drain()
    await b.space_service.deny_join_request(
        first, actor_username=b.username, space_id=space.id
    )
    await gfs.drain()
    bells: list[SpaceJoinRequested] = []

    async def _bell(event: SpaceJoinRequested) -> None:
        bells.append(event)

    b.bus.subscribe(SpaceJoinRequested, _bell)
    real_uuid4 = uuid.uuid4
    uuid_mod_calls = iter([uuid.UUID(hex=first), real_uuid4()])
    with patch.object(gfs_join_module.uuid, "uuid4", lambda: next(uuid_mod_calls)):
        await a.space_service.request_join_remote(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id=b.instance_id,
        )
    await gfs.drain()
    assert await b.space_repo.list_pending_join_requests(space.id) == []
    # …and no bell for a request that was never filed.
    assert bells == []


def test_the_capability_names_the_version_that_shipped_it():
    assert FederationCapability.MIN_FOR_GFS_JOIN_REQUEST == 84
    assert OURS >= FederationCapability.MIN_FOR_GFS_JOIN_REQUEST


# ─── The inbound gates, body by body ─────────────────────────────────────


def _signed(sender, **fields) -> dict:
    """A join-request body signed by *sender*'s identity key, as the relay
    hands it over after the unseal."""
    body = {
        "kind": KIND_JOIN_REQUEST,
        "request_nonce": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "instance_id": sender.instance_id,
        "identity_pk": sender.identity_pk.hex(),
        "keywrap_pk": sender.keywrap_pub.hex(),
        "keywrap_sig": sender.keywrap_sig,
        "proto_version": OURS,
        **fields,
    }
    return sign_bootstrap_body(body, identity_seed=sender.identity_seed)


async def test_a_replayed_body_is_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    body = _signed(a, space_id=space.id, user_id=a.user_id, request_id="r-1")
    assert (await b.gfsjoin.join.handle_body(body))["filed"] is True
    with pytest.raises(ValueError, match="Replay"):
        await b.gfsjoin.join.handle_body(body)


async def test_a_body_without_a_nonce_or_fields_is_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    with pytest.raises(ValueError, match="request_nonce"):
        await b.gfsjoin.join.handle_body(
            _signed(a, request_nonce="", space_id=space.id, user_id=a.user_id)
        )
    with pytest.raises(ValueError, match="missing"):
        await b.gfsjoin.join.handle_body(_signed(a, space_id=space.id))


async def test_a_keywrap_key_not_bound_to_the_sender_is_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    body = _signed(
        a,
        keywrap_pk=b.keywrap_pub.hex(),
        space_id=space.id,
        user_id=a.user_id,
        request_id="r-2",
    )
    with pytest.raises(ValueError, match="not bound"):
        await b.gfsjoin.join.handle_body(body)


async def test_a_banned_person_files_nothing(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    await b.space_repo.ban_member(space.id, a.user_id, banned_by=b.user_id)
    body = _signed(a, space_id=space.id, user_id=a.user_id, request_id="r-3")
    assert (await b.gfsjoin.join.handle_body(body))["filed"] is False
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_a_repeat_while_pending_files_nothing_more(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    first = _signed(a, space_id=space.id, user_id=a.user_id, request_id="r-4")
    again = _signed(a, space_id=space.id, user_id=a.user_id, request_id="r-5")
    assert (await b.gfsjoin.join.handle_body(first))["filed"] is True
    assert (await b.gfsjoin.join.handle_body(again))["filed"] is False
    pending = await b.space_repo.list_pending_join_requests(space.id)
    assert [r["id"] for r in pending] == ["r-4"]


async def test_the_per_sender_budget_drops_a_flood(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    b.gfsjoin.join._rate_limiter = RateLimiter()
    for n in range(gfs_join_module.PER_SENDER_LIMIT):
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id=f"u-{n}", request_id=f"f-{n}")
        )
    with pytest.raises(ValueError, match="rate limit"):
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="u-x", request_id="f-x")
        )


async def test_answers_that_make_no_sense_are_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await a.space_service.request_join_remote(
        space.id, applicant_user_id=a.user_id, host_instance_id=b.instance_id
    )
    await gfs.drain()

    def answer(**fields) -> dict:
        base = {
            "kind": KIND_JOIN_REQUEST_ANSWER,
            "request_id": rid,
            "space_id": space.id,
        }
        return _signed(b, **{**base, **fields})

    with pytest.raises(ValueError, match="without a token"):
        await a.gfsjoin.join.handle_body(answer(answer="approved"))
    with pytest.raises(ValueError, match="unknown gfs join answer"):
        await a.gfsjoin.join.handle_body(answer(answer="maybe"))
    with pytest.raises(ValueError, match="no pending request"):
        await a.gfsjoin.join.handle_body(answer(answer="denied", space_id="other"))
    assert (await a.space_repo.get_join_request(rid))["status"] == "pending"


async def test_an_answer_without_a_relay_contact_is_not_sent(households, gfs):
    a, b = households
    sent = await b.gfsjoin.join.send_answer(
        {
            "id": "r-x",
            "space_id": "sp-x",
            "remote_applicant_instance_id": a.instance_id,
            "applicant_relay": "not json",
        },
        answer="denied",
    )
    assert sent is False
    assert gfs.mailbox == []


def test_a_version_that_is_no_number_reads_as_one():
    assert gfs_join_module._int("83") == 83
    assert gfs_join_module._int(None) == 1
    assert gfs_join_module._int(True) == 1
    assert gfs_join_module._int("x") == 1
