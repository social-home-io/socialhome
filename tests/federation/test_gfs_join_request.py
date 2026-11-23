"""Ask to join a global space through the GFS (v_84), end to end.

Household **b** hosts a global space in "ask to join" mode, listed on a GFS
with its join contact (b's keys, signed by the space authority key).
Household **a** is connected to that GFS and to nothing else: no pairing,
no mesh route, no invite link. It found the space in its GFS directory.

Before v_84 that was the end of it: ``POST /api/public_spaces/{id}/join-request``
answered ``403 NOT_PAIRED`` and Discover offered no button. Here a asks, b's
admin approves, and a ends up a member of b's space — over the GFS relay
only, which routes sealed blobs and never reads the request, the token or
who is asking, and is never asked for anything.

Real crypto, real SQLite, real HTTP; the only stand-in is the GFS relay.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import orjson
import pytest
from aiohttp.test_utils import TestServer

from socialhome.domain.events import (
    RemoteJoinRequestBusy,
    SpaceJoinRequested,
    SpaceMemberJoined,
)
from socialhome.domain.federation import (
    FederationEventType,
    InstanceSource,
    PairingStatus,
)
from socialhome.domain.federation_capabilities import OURS, FederationCapability
from socialhome.domain.space import (
    HostNotPairedError,
    HostNotReachableError,
    JoinMode,
    SpaceFeatures,
    SpaceRole,
    SpaceType,
    join_request_flags,
)
from socialhome.federation import gfs_join_request as gfs_join_module
from socialhome.federation import invite_bootstrap as bootstrap_module
from socialhome.federation.gfs_join_request import GfsJoinRequestCoordinator
from socialhome.federation.invite_bootstrap import (
    KIND_JOIN_REQUEST,
    KIND_JOIN_REQUEST_ANSWER,
    sign_bootstrap_body,
)
from socialhome.federation.join_contact_pin import candidate_from_listing, merge_pin
from socialhome.rate_limiter import RateLimiter
from socialhome.repositories.gfs_connection_repo import SqliteGfsConnectionRepo
from socialhome.repositories.public_space_repo import (
    PublicSpaceListing,
    SqlitePublicSpaceRepo,
)
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gfs_connection_service import GfsConnectionService
from socialhome.services.gfs_envelope_sender import GfsEnvelopeSender
from tests.federation.test_invite_bootstrap_gfs_e2e import _FakeGfs, _household

try:
    import pytest_socket  # noqa: F401

    @pytest.fixture(autouse=True)
    def _enable_sockets(socket_enabled):
        """Re-enable sockets if the HA pytest plugin disabled them."""

except ImportError:  # pragma: no cover - CI path
    pass


MESSAGE = "hello-from-a-do-not-leak"


async def _wire_join(h, http_session):
    """Wire the v_84 coordinator onto a household the way ``app.py`` does."""
    gfs_repo = SqliteGfsConnectionRepo(h.db)
    gfs_service = GfsConnectionService(gfs_repo, http_client=http_session)
    gfs_service.attach_publish_context(
        space_repo=h.space_repo,
        own_instance_id=h.instance_id,
        own_signing_key=h.identity_seed,
    )
    gfs_service.attach_join_contact_keys(h.keywrap_pub.hex(), h.keywrap_sig)
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
        keywrap_public_key=h.keywrap_pub,
        keywrap_sig=h.keywrap_sig,
    )
    h.relay_inbound.attach_join_requests(join)
    h.space_service.attach_gfs_join_requests(join)
    # As ``app.py`` does (the shared e2e fixture leaves it out): the §D2
    # redeem handlers on the registry, for a household already seated.
    h.coordinator.attach_to(h.federation)
    return SimpleNamespace(join=join, public_spaces=public_spaces, gfs=gfs_service)


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
async def http_session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
async def households(tmp_path, gfs, http_session):
    a = await _household(tmp_path, "a", gfs, http_session)
    b = await _household(tmp_path, "b", gfs, http_session)
    a.gfsjoin = await _wire_join(a, http_session)
    b.gfsjoin = await _wire_join(b, http_session)
    yield a, b
    await gfs.drain()
    for h in (a, b):
        for task in list(h.gfsjoin.join._tasks):
            await task
    await a.db.shutdown()
    await b.db.shutdown()


async def _listing_item(b, space) -> dict:
    """The directory row a GFS serves for b's space: b's real signed publish
    body (with its authority-signed join contact)."""
    return await b.gfsjoin.gfs._build_publish_body(space.id, with_contact=True)


async def _cache(a, item: dict, gfs_url: str) -> None:
    """What a's directory poll does with one listing row."""
    held = await a.gfsjoin.public_spaces.get_pin(item["space_id"])
    pin = merge_pin(
        held,
        candidate_from_listing(item, gfs_url=gfs_url),
        space_id=item["space_id"],
    )
    if pin is not None:
        await a.gfsjoin.public_spaces.set_pin(item["space_id"], pin)
    await a.gfsjoin.public_spaces.upsert(
        PublicSpaceListing(
            space_id=item["space_id"],
            instance_id=str(item["owning_instance"]),
            name=str(item["name"]),
            join_mode=str(item["join_mode"]),
        )
    )


async def _global_space(b, gfs, a, *, join_mode=JoinMode.REQUEST):
    """b hosts a global space; the GFS lists it and a's poll cached it."""
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Ask Me Global",
        space_type=SpaceType.GLOBAL,
        join_mode=join_mode,
    )
    await _cache(a, await _listing_item(b, space), gfs.url)
    return space


async def _settle(gfs, *households) -> None:
    await gfs.drain()
    for h in households:
        for task in list(h.gfsjoin.join._tasks):
            await task
    await gfs.drain()


async def _ask(a, b, space, message: str | None = None) -> str:
    return await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
        message=message,
    )


class _Later(datetime):
    """``datetime`` whose ``now`` runs *shift* ahead — a body read after it
    sat in the GFS queue."""

    shift = timedelta(0)

    @classmethod
    def now(cls, tz=None):
        return datetime.now(tz) + cls.shift


# ─── The whole way ───────────────────────────────────────────────────────


async def test_unpaired_household_asks_through_the_gfs_and_becomes_a_member(
    households, gfs
):
    """The owner's bug: a household on the same GFS but not paired with the
    host can ask to join a global request-mode space, the host approves, and
    it is seated — with no pairing and no invite link."""
    a, b = households
    space = await _global_space(b, gfs, a)
    assert await a.federation_repo.get_instance(b.instance_id) is None

    request_id = await _ask(a, b, space, MESSAGE)
    await _settle(gfs, a, b)

    pending = await b.space_repo.list_pending_join_requests(space.id)
    assert [(r["id"], r["user_id"], r["message"]) for r in pending] == [
        (request_id, a.user_id, MESSAGE)
    ]
    assert space.id in await a.space_repo.list_pending_join_request_space_ids_for_user(
        a.user_id, own_instance_id=a.instance_id
    )

    joined: list[SpaceMemberJoined] = []
    a.bus.subscribe(SpaceMemberJoined, joined.append)
    await b.space_service.approve_join_request(
        request_id, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)

    assert await a.space_repo.get_member(space.id, a.user_id) is not None
    # The applicant's household hears of its new member at once (its
    # fan-out seat on the space's connection servers hangs off this).
    assert [(j.space_id, j.user_id, j.role) for j in joined] == [
        (space.id, a.user_id, "member")
    ]
    roster = await b.remote_members.list_for_space(space.id)
    assert [m.user_id for m in roster] == [a.user_id]
    assert (await a.space_repo.get_join_request(request_id))["status"] == "approved"
    assert await a.space_service.list_joining_space_ids(a.user_id) == []

    # The GFS routed blobs and read none of them.
    tokens = [
        r["token"] for r in await b.db.fetchall("SELECT token FROM space_invite_tokens")
    ]
    for _to, body in gfs.mailbox:
        assert set(body) == {"to_instance", "sealed"}
        blob = str(body)
        for secret in (MESSAGE, a.user_id, space.id, *tokens):
            assert secret not in blob


async def test_denied_request_reaches_the_applicant(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    request_id = await _ask(a, b, space)
    await _settle(gfs, a, b)
    await b.space_service.deny_join_request(
        request_id, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    assert (await a.space_repo.get_join_request(request_id))["status"] == "denied"
    assert await a.space_repo.get_member(space.id, a.user_id) is None


async def test_a_seat_in_one_space_is_no_pairing_for_the_next(households, gfs):
    """Once seated in one of b's spaces, a holds a space-session row for b.
    Asking to join ANOTHER of b's global spaces still goes through the GFS:
    b refuses a SPACE_JOIN_REQUEST from a space-scoped peer."""
    a, b = households
    first = await _global_space(b, gfs, a)
    rid = await _ask(a, b, first)
    await _settle(gfs, a, b)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=first.id
    )
    await _settle(gfs, a, b)
    seat = await a.federation_repo.get_instance(b.instance_id)
    assert seat is not None and seat.source is InstanceSource.SPACE_SESSION

    second = await _global_space(b, gfs, a)
    rid2 = await _ask(a, b, second)
    await _settle(gfs, a, b)
    assert [
        r["id"] for r in await b.space_repo.list_pending_join_requests(second.id)
    ] == [rid2]
    await b.space_service.approve_join_request(
        rid2, actor_username=b.username, space_id=second.id
    )
    await _settle(gfs, a, b)
    assert await a.space_repo.get_member(second.id, a.user_id) is not None


# ─── 1. A body that waited in the GFS queue ──────────────────────────────


async def test_a_request_queued_for_an_offline_host_is_filed_hours_later(
    households, gfs
):
    """b is offline: the GFS queues the request. b comes back hours later
    (well past the ±300 s live window) and still files it."""
    a, b = households
    space = await _global_space(b, gfs, a)
    socket = gfs.sockets.pop(b.instance_id)
    rid = await _ask(a, b, space, MESSAGE)
    queued = [body for to, body in gfs.mailbox if to == b.instance_id]
    assert len(queued) == 1
    _Later.shift = timedelta(hours=6)
    with patch.object(bootstrap_module, "datetime", _Later):
        await socket.handle_frame({"sealed": queued[0]["sealed"]}, gfs_url=gfs.url)
    gfs.sockets[b.instance_id] = socket
    pending = await b.space_repo.list_pending_join_requests(space.id)
    assert [r["id"] for r in pending] == [rid]


async def test_an_answer_queued_for_an_offline_applicant_still_seats_it(
    households, gfs
):
    """a is offline when b approves: the GFS queues the answer; a drains it
    6 minutes later and is seated."""
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    socket = gfs.sockets.pop(a.instance_id)
    before = len(gfs.mailbox)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    answer = [body for to, body in gfs.mailbox[before:] if to == a.instance_id]
    assert len(answer) == 1
    gfs.sockets[a.instance_id] = socket
    _Later.shift = timedelta(minutes=6)
    with patch.object(bootstrap_module, "datetime", _Later):
        await socket.handle_frame({"sealed": answer[0]["sealed"]}, gfs_url=gfs.url)
    await _settle(gfs, a, b)
    assert await a.space_repo.get_member(space.id, a.user_id) is not None


async def test_a_body_older_than_the_queue_is_refused(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    socket = gfs.sockets.pop(b.instance_id)
    await _ask(a, b, space)
    queued = [body for to, body in gfs.mailbox if to == b.instance_id]
    _Later.shift = timedelta(hours=25)
    with (
        patch.object(bootstrap_module, "datetime", _Later),
        pytest.raises(ValueError, match="skew"),
    ):
        await socket.handle_frame({"sealed": queued[0]["sealed"]}, gfs_url=gfs.url)
    gfs.sockets[b.instance_id] = socket
    assert await b.space_repo.list_pending_join_requests(space.id) == []


# ─── 3+4. The contact comes off the listing, authority-signed and pinned ─


async def test_nobody_asks_the_gfs_anything(households, gfs):
    """The contact is on the listing: asking costs the GFS nothing but the
    sealed envelope — no lookup it could record."""
    a, b = households
    space = await _global_space(b, gfs, a)
    await _ask(a, b, space)
    await _settle(gfs, a, b)
    assert [to for to, _ in gfs.mailbox] == [b.instance_id]


async def test_a_gfs_that_swaps_the_keys_gets_nothing_sealed(households, gfs):
    """A listing whose contact keys were swapped no longer verifies under
    the space authority key: no contact, nothing sent."""
    a, b = households
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Swapped",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.REQUEST,
    )
    item = await _listing_item(b, space)
    item["join_contact"] = {**item["join_contact"], "keywrap_pk": a.keywrap_pub.hex()}
    await _cache(a, item, gfs.url)
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)
    assert gfs.mailbox == []


async def test_a_listing_under_another_authority_is_refused_for_good(households, gfs):
    """First sight pins the space authority key. A listing that later shows
    another key (another GFS, or a swap) makes the space unaskable — it
    never re-opens because a matching listing comes back."""
    a, b = households
    space = await _global_space(b, gfs, a)
    forged_space = await b.space_service.create_space(
        owner_username=b.username,
        name="Forger",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.REQUEST,
    )
    forged = await _listing_item(b, forged_space)
    # Another authority (and its own validly signed contact) for OUR space.
    forged["space_id"] = space.id
    await _cache(a, forged, "https://evil.example")
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)
    await _cache(a, await _listing_item(b, space), gfs.url)
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)


async def test_the_pinning_gfs_cannot_move_the_key_with_its_own_counter(
    households, gfs
):
    """A higher ``authority_rotation_seq`` from the very GFS that gave the
    pin is that GFS's own word: without the owner's cert it is a conflict."""
    a, b = households
    space = await _global_space(b, gfs, a)
    other = await b.space_service.create_space(
        owner_username=b.username,
        name="Rotated",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.REQUEST,
    )
    rotated = await _listing_item(b, other)
    rotated["space_id"] = space.id
    rotated["authority_rotation_seq"] = 1
    await _cache(a, rotated, gfs.url)
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)


async def test_a_space_the_gfs_does_not_list_under_that_host_still_needs_pairing(
    households, gfs
):
    a, b = households
    space = await _global_space(b, gfs, a)
    with pytest.raises(HostNotPairedError):
        await a.space_service.request_join_remote(
            space.id, applicant_user_id=a.user_id, host_instance_id="z" * 32
        )
    assert gfs.mailbox == []


async def test_a_listing_without_a_contact_is_host_not_reachable(households, gfs):
    """An older owner (no contact on the listing) — or a GFS that dropped
    it: the ask fails clearly, nothing is sent."""
    a, b = households
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Old",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.REQUEST,
    )
    item = await _listing_item(b, space)
    item.pop("join_contact")
    await _cache(a, item, gfs.url)
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)
    # The coordinator itself says why, for a caller that asks it directly.
    with pytest.raises(HostNotReachableError):
        await a.gfsjoin.join.request_join(
            space.id, applicant_user_id=a.user_id, host_instance_id=b.instance_id
        )


# ─── 2. Spam ─────────────────────────────────────────────────────────────


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


async def test_a_space_keeps_at_most_the_cap_and_refuses_new_ones(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 3):
        for n in range(5):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=f"u{n}", request_id=f"r{n}")
            )
    pending = await b.space_repo.list_pending_join_requests(space.id)
    # The first three wait; the last two were refused, nothing pushed out.
    assert [r["id"] for r in pending] == ["r0", "r1", "r2"]


async def test_one_flooded_space_does_not_starve_another(households, gfs):
    a, b = households
    flooded = await _global_space(b, gfs, a)
    quiet = await _global_space(b, gfs, a)
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with (
        patch.object(gfs_join_module, "PER_SPACE_LIMIT", 2),
        patch.object(gfs_join_module, "PER_SENDER_LIMIT", 100),
    ):
        for n in range(4):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=flooded.id, user_id=f"f{n}", request_id=f"f{n}")
            )
        filed = await b.gfsjoin.join.handle_body(
            _signed(a, space_id=quiet.id, user_id="q", request_id="q0")
        )
    assert filed["filed"] is True
    assert len(await b.space_repo.list_pending_join_requests(flooded.id)) == 2


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


# ─── 5. An approval survives a failed redeem ─────────────────────────────


async def test_an_approval_waits_out_an_offline_host_and_seats_later(households, gfs):
    """The redeem fails (b went offline right after approving): the request
    reads "approved, joining…", and the next resume seats the person."""
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    real_redeem = a.space_service.redeem_invite_token

    async def _host_offline(*_args, **_kwargs):
        raise TimeoutError("issuer did not respond")

    a.gfsjoin.join.attach_redeem(_host_offline)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    row = await a.space_repo.get_join_request(rid)
    assert row["status"] == "pending"
    assert await a.space_service.list_joining_space_ids(a.user_id) == [space.id]
    assert await a.space_repo.get_member(space.id, a.user_id) is None

    a.gfsjoin.join.attach_redeem(real_redeem)
    assert await a.gfsjoin.join.resume_approvals() == 1
    assert await a.space_repo.get_member(space.id, a.user_id) is not None
    assert (await a.space_repo.get_join_request(rid))["status"] == "approved"


async def test_an_approval_whose_token_expired_is_marked_expired(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)

    async def _host_offline(*_args, **_kwargs):
        raise TimeoutError("issuer did not respond")

    a.gfsjoin.join.attach_redeem(_host_offline)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    row = await a.space_repo.get_join_request(rid)
    relay = row["gfs_relay"].replace(
        row["gfs_relay"].split('"expires_at":"')[1].split('"')[0],
        "2020-01-01T00:00:00+00:00",
    )
    await a.space_repo.set_join_request_gfs_relay(rid, relay)
    assert await a.gfsjoin.join.resume_approvals() == 0
    assert (await a.space_repo.get_join_request(rid))["status"] == "expired"


# ─── 6. Admins see who asked — marked unverified ─────────────────────────


async def test_the_admins_see_the_names_marked_unverified(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    rows = await b.space_service.list_pending_join_requests(
        space.id, actor_username=b.username
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == rid
    assert row["via_gfs"] is True
    assert row["unverified_name"] == "A person"
    assert row["unverified_household"] == "a household"
    assert "gfs_relay" not in row
    raw = await b.space_repo.get_join_request(rid)
    assert join_request_flags(raw, min_age=0, household_paired=False) == ("gfs",)


# ─── 7 + 8. Ids, and a token for the approved household only ─────────────


async def test_malformed_ids_are_refused(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    for bad in ("x" * 65, "a/b", "a b"):
        with pytest.raises(ValueError, match="malformed"):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=bad, request_id="r-ok")
            )
        with pytest.raises(ValueError, match="malformed"):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id="u-ok", request_id=bad)
            )
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_the_approval_token_is_dead_to_anyone_but_the_applicant(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)

    async def _hold(*_args, **_kwargs):
        raise TimeoutError("not now")

    a.gfsjoin.join.attach_redeem(_hold)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    (token,) = [
        r["token"] for r in await b.db.fetchall("SELECT token FROM space_invite_tokens")
    ]
    # Another household, or another person of a's, or a local accept: dead.
    assert (
        await b.space_repo.consume_invite_token(
            token, redeemer_user_id=a.user_id, redeemer_instance_id="z" * 32
        )
        is None
    )
    assert (
        await b.space_repo.consume_invite_token(
            token, redeemer_user_id="someone-else", redeemer_instance_id=a.instance_id
        )
        is None
    )
    assert await b.space_repo.consume_invite_token(token) is None
    # The approved household and person: alive.
    assert (
        await b.space_repo.consume_invite_token(
            token, redeemer_user_id=a.user_id, redeemer_instance_id=a.instance_id
        )
        is not None
    )


# ─── Odds and ends ───────────────────────────────────────────────────────


async def test_an_answer_from_another_household_changes_nothing(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    await a.db.enqueue(
        "UPDATE space_join_requests SET remote_applicant_instance_id=? WHERE id=?",
        ("z" * 32, rid),
    )
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    assert (await a.space_repo.get_join_request(rid))["status"] == "pending"
    assert await a.space_repo.get_member(space.id, a.user_id) is None


async def test_a_request_id_already_in_use_is_refused(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    first = _signed(a, space_id=space.id, user_id="u-1", request_id="r-same")
    assert (await b.gfsjoin.join.handle_body(first))["filed"] is True
    await b.space_service.deny_join_request(
        "r-same", actor_username=b.username, space_id=space.id
    )
    bells: list[SpaceJoinRequested] = []

    async def _bell(event: SpaceJoinRequested) -> None:
        bells.append(event)

    b.bus.subscribe(SpaceJoinRequested, _bell)
    with pytest.raises(ValueError, match="already in use"):
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="u-2", request_id="r-same")
        )
    assert bells == []


async def test_only_a_space_that_takes_requests_lists_a_contact(households, gfs):
    a, b = households
    closed = await b.space_service.create_space(
        owner_username=b.username,
        name="Closed",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    assert "join_contact" not in await _listing_item(b, closed)
    assert not await b.gfsjoin.gfs._wants_join_contact(closed.id)
    asking = await _global_space(b, gfs, a)
    assert await b.gfsjoin.gfs._wants_join_contact(asking.id)


async def test_invite_only_space_files_nothing_and_says_nothing(households, gfs):
    a, b = households
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Closed",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    out = await b.gfsjoin.join.handle_body(
        _signed(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out["filed"] is False
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_answers_that_make_no_sense_are_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)

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


async def test_a_replayed_body_is_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    body = _signed(a, space_id=space.id, user_id=a.user_id, request_id="r-1")
    assert (await b.gfsjoin.join.handle_body(body))["filed"] is True
    with pytest.raises(ValueError, match="Replay"):
        await b.gfsjoin.join.handle_body(body)


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


async def test_an_answer_without_a_host_contact_is_not_sent(households, gfs):
    a, b = households
    sent = await b.gfsjoin.join.send_answer(
        {
            "id": "r-x",
            "space_id": "sp-x",
            "remote_applicant_instance_id": a.instance_id,
            "gfs_relay": "not json",
        },
        answer="denied",
    )
    assert sent is False
    assert gfs.mailbox == []


# ─── Re-review round ─────────────────────────────────────────────────────


async def test_junk_that_fails_the_cheap_checks_spends_no_budget(households, gfs):
    """Requests for a space that takes none, from a banned person, or
    repeats are dropped BEFORE the per-space budget is touched — so they
    cannot use it up for real applicants."""
    a, b = households
    space = await _global_space(b, gfs, a)
    await b.space_repo.ban_member(space.id, "banned-u", banned_by=b.user_id)
    closed = await b.space_service.create_space(
        owner_username=b.username,
        name="Closed",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with (
        patch.object(gfs_join_module, "PER_SPACE_LIMIT", 2),
        patch.object(gfs_join_module, "PER_SENDER_LIMIT", 100),
    ):
        for n in range(4):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id="banned-u", request_id=f"b{n}")
            )
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=closed.id, user_id=f"c{n}", request_id=f"c{n}")
            )
        first = await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="real-1", request_id="real-1")
        )
        for n in range(3):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id="real-1", request_id=f"dup{n}")
            )
        second = await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="real-2", request_id="real-2")
        )
    assert first["filed"] is True
    assert second["filed"] is True


async def test_expired_requests_free_the_cap(households, gfs):
    """A request nobody answered expires (REQUEST_TTL_DAYS) and its slot
    under the cap is free again."""
    a, b = households
    space = await _global_space(b, gfs, a)
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 2):
        for n in range(2):
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=f"u{n}", request_id=f"r{n}")
            )
        await b.db.enqueue(
            "UPDATE space_join_requests SET expires_at='2020-01-01T00:00:00+00:00'"
        )
        assert await b.space_service.expire_due_join_requests() == 2
        out = await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="u9", request_id="r9")
        )
    assert out["filed"] is True
    assert [
        r["id"] for r in await b.space_repo.list_pending_join_requests(space.id)
    ] == ["r9"]


async def test_admins_can_decline_every_gfs_request_at_once(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    for n in range(3):
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id=f"x{n}", request_id=f"x{n}")
        )
    # A local request is left alone.
    local = await b.space_repo.save_join_request(space.id, "local-u")
    n = await b.space_service.decline_gfs_join_requests(
        space.id, actor_username=b.username
    )
    await _settle(gfs, a, b)
    assert n == 4
    assert [
        r["id"] for r in await b.space_repo.list_pending_join_requests(space.id)
    ] == [local]
    assert (await a.space_repo.get_join_request(rid))["status"] == "denied"


async def test_an_approval_without_an_expiry_is_treated_as_expired(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)

    async def _host_offline(*_args, **_kwargs):
        raise TimeoutError("issuer did not respond")

    a.gfsjoin.join.attach_redeem(_host_offline)
    await b.space_service.approve_join_request(
        rid, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    row = await a.space_repo.get_join_request(rid)
    relay = orjson.loads(row["gfs_relay"])
    relay["approval"]["expires_at"] = ""
    await a.space_repo.set_join_request_gfs_relay(rid, orjson.dumps(relay).decode())
    assert await a.gfsjoin.join.resume_approvals() == 0
    assert (await a.space_repo.get_join_request(rid))["status"] == "expired"


async def test_one_bad_row_does_not_stop_the_resume(households, gfs):
    a, b = households
    first = await _global_space(b, gfs, a)
    second = await _global_space(b, gfs, a)
    r1 = await _ask(a, b, first)
    r2 = await _ask(a, b, second)
    await _settle(gfs, a, b)
    real = a.space_service.redeem_invite_token

    async def _hold(*_args, **_kwargs):
        raise TimeoutError("not now")

    a.gfsjoin.join.attach_redeem(_hold)
    for rid, sp in ((r1, first), (r2, second)):
        await b.space_service.approve_join_request(
            rid, actor_username=b.username, space_id=sp.id
        )
    await _settle(gfs, a, b)

    async def _breaks_on_first(token, *, user_id, issuer_instance_id, bootstrap):
        if bootstrap.space_id == first.id:
            raise RuntimeError("corrupt row")
        return await real(
            token,
            user_id=user_id,
            issuer_instance_id=issuer_instance_id,
            bootstrap=bootstrap,
        )

    a.gfsjoin.join.attach_redeem(_breaks_on_first)
    assert await a.gfsjoin.join.resume_approvals() == 1
    assert await a.space_repo.get_member(second.id, a.user_id) is not None


async def test_a_conflict_stops_mattering_once_the_owner_is_paired(households, gfs):
    """The way out of a conflicted pin: pair with the space's owner
    household. A confirmed pairing is asked over the pairing — the GFS pin
    is not consulted — so a space the GFS route refuses can still be asked."""
    a, b = households
    space = await _global_space(b, gfs, a)
    forged_space = await b.space_service.create_space(
        owner_username=b.username,
        name="Forger",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.REQUEST,
    )
    forged = await _listing_item(b, forged_space)
    forged["space_id"] = space.id
    await _cache(a, forged, "https://evil.example")
    with pytest.raises(HostNotPairedError):
        await _ask(a, b, space)

    paired = SimpleNamespace(
        status=PairingStatus.CONFIRMED,
        source=InstanceSource.MANUAL,
        home_lat=None,
        home_lon=None,
    )
    real_get = a.federation_repo.get_instance

    async def _get(instance_id):
        return paired if instance_id == b.instance_id else await real_get(instance_id)

    sent = AsyncMock()
    with (
        patch.object(a.federation_repo, "get_instance", _get),
        patch.object(type(a.federation), "send_event", sent),
    ):
        await _ask(a, b, space)
    assert (
        sent.await_args.kwargs["event_type"] is FederationEventType.SPACE_JOIN_REQUEST
    )
    assert sent.await_args.kwargs["to_instance_id"] == b.instance_id


async def test_a_request_with_no_usable_expiry_is_swept(households, gfs):
    """``expires_at`` empty or unreadable reads as expired — a request is
    never pending forever."""
    a, b = households
    space = await _global_space(b, gfs, a)
    for n, bad in enumerate(("", "soon")):
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id=f"u{n}", request_id=f"r{n}")
        )
        await b.db.enqueue(
            "UPDATE space_join_requests SET expires_at=? WHERE id=?", (bad, f"r{n}")
        )
    assert await b.space_service.expire_due_join_requests() == 2
    assert await b.space_repo.list_pending_join_requests(space.id) == []


# ─── A room asks at once: capacity refusals are answered ─────────────────


def test_the_budgets_fit_a_meetup():
    """40 households asking within minutes, and every edge-of-area join of
    a local community, land here: the per-space budget and the pending cap
    are sized for a room; the per-sender budget stays tight (the sender is
    signature-verified)."""
    assert gfs_join_module.PER_SPACE_LIMIT == 120
    assert gfs_join_module.PER_SPACE_WINDOW_S == 3600
    assert gfs_join_module.MAX_PENDING_PER_SPACE == 200
    assert gfs_join_module.PER_SENDER_LIMIT == 10
    assert gfs_join_module.PER_SENDER_WINDOW_S == 3600


async def _answers_to(gfs, household) -> int:
    await gfs.drain()
    return sum(1 for to, _body in gfs.mailbox if to == household.instance_id)


async def test_a_full_space_tells_the_applicant_it_is_busy(households, gfs):
    """Past the pending cap the host answers "busy" instead of dropping the
    request: the applicant's request ends at once (no 7-day wait), the
    person is told, and they can ask again."""
    a, b = households
    space = await _global_space(b, gfs, a)
    told: list[RemoteJoinRequestBusy] = []
    a.bus.subscribe(RemoteJoinRequestBusy, told.append)
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 0):
        rid = await _ask(a, b, space)
        await _settle(gfs, a, b)
    assert await b.space_repo.list_pending_join_requests(space.id) == []
    row = await a.space_repo.get_join_request(rid)
    assert row["status"] == "expired"
    assert [(e.request_id, e.space_id) for e in told] == [(rid, space.id)]
    assert await a.space_service.list_busy_join_request_space_ids(a.user_id) == [
        space.id
    ]
    assert space.id not in await a.space_service.list_pending_join_request_space_ids(
        a.user_id
    )

    # They ask again once there is room: filed, and no longer "busy".
    again = await _ask(a, b, space)
    await _settle(gfs, a, b)
    assert [
        r["id"] for r in await b.space_repo.list_pending_join_requests(space.id)
    ] == [again]
    assert await a.space_service.list_busy_join_request_space_ids(a.user_id) == []


async def test_a_spent_space_budget_tells_the_applicant_it_is_busy(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with patch.object(gfs_join_module, "PER_SPACE_LIMIT", 1):
        # Someone else spent this hour's budget.
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="early", request_id="early")
        )
        rid = await _ask(a, b, space)
        await _settle(gfs, a, b)
    assert [
        r["id"] for r in await b.space_repo.list_pending_join_requests(space.id)
    ] == ["early"]
    assert (await a.space_repo.get_join_request(rid))["status"] == "expired"
    assert await a.space_service.list_busy_join_request_space_ids(a.user_id) == [
        space.id
    ]


async def test_banned_repeat_and_closed_are_still_dropped_without_a_word(
    households, gfs
):
    """Only capacity refusals are answered: a banned person, a repeat and a
    space that takes no requests get nothing back, as before."""
    a, b = households
    space = await _global_space(b, gfs, a)
    closed = await b.space_service.create_space(
        owner_username=b.username,
        name="Closed",
        space_type=SpaceType.GLOBAL,
        join_mode=JoinMode.INVITE_ONLY,
    )
    await b.space_repo.ban_member(space.id, "banned-u", banned_by=b.user_id)
    filed = await b.gfsjoin.join.handle_body(
        _signed(a, space_id=space.id, user_id="repeat-u", request_id="first")
    )
    assert filed["filed"] is True
    before = await _answers_to(gfs, a)
    for body in (
        _signed(a, space_id=space.id, user_id="banned-u", request_id="ban"),
        _signed(a, space_id=space.id, user_id="repeat-u", request_id="again"),
        _signed(a, space_id=closed.id, user_id="c", request_id="closed"),
        _signed(a, space_id="sp-unknown", user_id="u", request_id="unknown"),
    ):
        out = await b.gfsjoin.join.handle_body(body)
        assert out == {"ok": True, "filed": False}
    assert await _answers_to(gfs, a) == before


async def test_a_full_space_answers_everyone_alike(households, gfs):
    """At a full space the answer says nothing about the person: a banned
    user id, a repeat and anyone else all hear "busy" — never silence for
    one and "busy" for the other (that would let a prober read the ban
    list)."""
    a, b = households
    space = await _global_space(b, gfs, a)
    await b.space_repo.ban_member(space.id, "banned-u", banned_by=b.user_id)
    assert (
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="repeat-u", request_id="first")
        )
    )["filed"] is True
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 1):
        outs = [
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=user, request_id=rid)
            )
            for user, rid in (("banned-u", "x1"), ("repeat-u", "x2"), ("anyone", "x3"))
        ]
    assert [o.get("busy") for o in outs] == [True, True, True]
    # Same with the per-space budget spent instead.
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with (
        patch.object(gfs_join_module, "PER_SPACE_LIMIT", 0),
        patch.object(gfs_join_module, "PER_SENDER_LIMIT", 100),
    ):
        outs = [
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=user, request_id=rid)
            )
            for user, rid in (("banned-u", "y1"), ("anyone", "y2"))
        ]
    assert [o["filed"] for o in outs] == [False, False]
    assert outs[0] == outs[1]


async def test_a_busy_never_cancels_an_approval_being_seated(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    row = await a.space_repo.get_join_request(rid)
    relay = orjson.loads(row["gfs_relay"])
    relay["approval"] = {"invite_token": "t", "expires_at": "2099-01-01T00:00:00+00:00"}
    await a.space_repo.set_join_request_gfs_relay(rid, orjson.dumps(relay).decode())
    body = _signed(
        b,
        kind=KIND_JOIN_REQUEST_ANSWER,
        request_id=rid,
        space_id=space.id,
        answer="busy",
    )
    with pytest.raises(ValueError, match="approved"):
        await a.gfsjoin.join.handle_body(body)
    assert (await a.space_repo.get_join_request(rid))["status"] == "pending"


async def test_a_later_request_replaces_the_busy_note(households, gfs):
    """Busy, then asked again and declined: Discover no longer says busy."""
    a, b = households
    space = await _global_space(b, gfs, a)
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 0):
        await _ask(a, b, space)
        await _settle(gfs, a, b)
    assert await a.space_service.list_busy_join_request_space_ids(a.user_id) == [
        space.id
    ]
    again = await _ask(a, b, space)
    await _settle(gfs, a, b)
    await b.space_service.deny_join_request(
        again, actor_username=b.username, space_id=space.id
    )
    await _settle(gfs, a, b)
    assert (await a.space_repo.get_join_request(again))["status"] == "denied"
    assert await a.space_service.list_busy_join_request_space_ids(a.user_id) == []


async def test_busy_answers_are_bounded_per_space(households, gfs):
    """A flood at a full space (fresh keypairs pass the per-sender budget)
    gets at most the space's budget of answers an hour; the rest are
    dropped as before."""
    a, b = households
    space = await _global_space(b, gfs, a)
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with (
        patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 0),
        patch.object(gfs_join_module, "PER_SPACE_LIMIT", 2),
        patch.object(gfs_join_module, "PER_SENDER_LIMIT", 100),
    ):
        outs = [
            await b.gfsjoin.join.handle_body(
                _signed(a, space_id=space.id, user_id=f"u{n}", request_id=f"r{n}")
            )
            for n in range(5)
        ]
    assert [o.get("busy", False) for o in outs] == [True, True, False, False, False]
    assert await _answers_to(gfs, a) == 2


async def test_a_busy_answer_for_no_pending_request_is_dropped(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    rid = await _ask(a, b, space)
    await _settle(gfs, a, b)
    body = _signed(
        b,
        kind=KIND_JOIN_REQUEST_ANSWER,
        request_id=rid,
        space_id="sp-other",
        answer="busy",
    )
    with pytest.raises(ValueError, match="no pending request"):
        await a.gfsjoin.join.handle_body(body)
    assert (await a.space_repo.get_join_request(rid))["status"] == "pending"


def test_a_version_that_is_no_number_reads_as_one():
    assert gfs_join_module._int("84") == 84
    assert gfs_join_module._int(None) == 1
    assert gfs_join_module._int(True) == 1
    assert gfs_join_module._int("x") == 1


def test_the_capability_names_the_version_that_shipped_it():
    assert FederationCapability.MIN_FOR_GFS_JOIN_REQUEST == 84
    assert OURS >= FederationCapability.MIN_FOR_GFS_JOIN_REQUEST


# ─── 7. Follow through the GFS (v_96) ────────────────────────────────────
#
# A follow is a join for the read-only ``subscriber`` role: the same sealed
# request (``requested_role`` inside the sealed body — the GFS never sees
# it), the same budgets on the host, then the host's automatic approval
# answers with a bound subscriber token the applicant redeems as usual.


async def _followable_space(
    b, gfs, a, *, join_mode=JoinMode.INVITE_ONLY, allow_subscribers=True
):
    """b hosts a global space that takes followers (invite-only by default:
    it takes follows, no requests); a's poll cached its listing."""
    space = await b.space_service.create_space(
        owner_username=b.username,
        name="Follow Me Global",
        space_type=SpaceType.GLOBAL,
        join_mode=join_mode,
        features=SpaceFeatures(allow_subscribers=allow_subscribers),
    )
    await _cache(a, await _listing_item(b, space), gfs.url)
    return space


def _follow(sender, **fields) -> dict:
    return _signed(sender, requested_role="subscriber", **fields)


def _approver(b, *, approved: bool = True) -> AsyncMock:
    fn = AsyncMock(return_value=approved)
    b.gfsjoin.join.attach_follow_approver(fn)
    return fn


def _spy_seal(sealed: list[dict]):
    real_seal = gfs_join_module.seal_bootstrap_envelope

    def _spy(**kwargs):
        sealed.append(kwargs["body"])
        return real_seal(**kwargs)

    return patch.object(gfs_join_module, "seal_bootstrap_envelope", _spy)


async def test_an_unpaired_household_follows_through_the_gfs(households, gfs):
    """a follows b's invite-only space (followers on) through the GFS only:
    b approves on its own and a is seated as a subscriber."""
    a, b = households
    space = await _followable_space(b, gfs, a)
    bells: list[SpaceJoinRequested] = []
    b.bus.subscribe(SpaceJoinRequested, bells.append)

    rid = await a.space_service.request_join_remote(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
        requested_role=SpaceRole.SUBSCRIBER,
    )
    await _settle(gfs, a, b)

    member = await a.space_repo.get_member(space.id, a.user_id)
    assert member is not None and member.role == SpaceRole.SUBSCRIBER
    roster = await b.remote_members.list_for_space(space.id)
    assert [(m.user_id, m.role) for m in roster] == [(a.user_id, "subscriber")]
    assert (await a.space_repo.get_join_request(rid))["status"] == "approved"
    assert (await b.space_repo.get_join_request(rid))["status"] == "approved"
    # Nobody's queue: a follow needs no admin.
    assert bells == []
    assert await b.space_repo.list_pending_join_requests(space.id) == []
    # The GFS saw sealed blobs only — not even that it was a follow.
    for _to, body in gfs.mailbox:
        assert set(body) == {"to_instance", "sealed"}
        assert "subscriber" not in str(body)


async def test_the_follow_role_rides_inside_the_sealed_body(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a)
    sealed: list[dict] = []
    with _spy_seal(sealed):
        await a.gfsjoin.join.request_join(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id=b.instance_id,
            requested_role=SpaceRole.SUBSCRIBER,
        )
    (body,) = sealed
    assert body["requested_role"] == "subscriber"
    # One field on both paths; the kind is the join request's own.
    assert body["kind"] == KIND_JOIN_REQUEST


async def test_a_plain_request_carries_no_role(households, gfs):
    a, b = households
    space = await _global_space(b, gfs, a)
    sealed: list[dict] = []
    with _spy_seal(sealed):
        await _ask(a, b, space)
    assert "requested_role" not in sealed[0]


@pytest.mark.parametrize("role", [SpaceRole.MEMBER, SpaceRole.ADMIN, SpaceRole.OWNER])
async def test_the_applicant_asks_for_no_other_role(households, gfs, role):
    a, b = households
    space = await _global_space(b, gfs, a)
    with pytest.raises(ValueError):
        await a.gfsjoin.join.request_join(
            space.id,
            applicant_user_id=a.user_id,
            host_instance_id=b.instance_id,
            requested_role=role,
        )
    assert gfs.mailbox == []


async def test_a_follow_is_handed_to_the_approver_with_the_relay_contact(
    households, gfs
):
    a, b = households
    space = await _followable_space(b, gfs, a)
    approve = _approver(b)
    out = await b.gfsjoin.join.handle_body(
        _follow(
            a,
            space_id=space.id,
            user_id="u-fan",
            request_id="rq-f",
            user_public_key="pk-fan",
            user_display_name="Fan",
            display_name="Fan Home",
        ),
        gfs_url=gfs.url,
    )
    assert out == {"ok": True, "filed": True}
    kwargs = dict(approve.await_args.kwargs)
    relay = orjson.loads(kwargs.pop("gfs_relay"))
    assert kwargs == {
        "space_id": space.id,
        "instance_id": a.instance_id,
        "user_id": "u-fan",
        "request_id": "rq-f",
        "applicant_pk": "pk-fan",
        "area_verdict": None,
    }
    assert relay == {
        "side": "host",
        "identity_pk": a.identity_pk.hex(),
        "keywrap_pk": a.keywrap_pub.hex(),
        "keywrap_sig": a.keywrap_sig,
        "gfs_url": gfs.url,
        "person_name": "Fan",
        "household_name": "Fan Home",
    }
    # Never an admin's pending request.
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_a_follow_the_approver_refuses_is_not_filed(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a)
    _approver(b, approved=False)
    out = await b.gfsjoin.join.handle_body(
        _follow(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out == {"ok": True, "filed": False}


async def test_a_request_mode_space_with_followers_takes_a_follow(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a, join_mode=JoinMode.REQUEST)
    approve = _approver(b)
    out = await b.gfsjoin.join.handle_body(
        _follow(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out["filed"] is True
    approve.assert_awaited_once()


@pytest.mark.parametrize("join_mode", [JoinMode.INVITE_ONLY, JoinMode.REQUEST])
async def test_a_follow_of_a_space_without_followers_is_dropped(
    households, gfs, join_mode
):
    a, b = households
    space = await _followable_space(
        b, gfs, a, join_mode=join_mode, allow_subscribers=False
    )
    approve = _approver(b)
    before = await _answers_to(gfs, a)
    out = await b.gfsjoin.join.handle_body(
        _follow(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out == {"ok": True, "filed": False}
    approve.assert_not_awaited()
    assert await _answers_to(gfs, a) == before
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_a_follow_of_a_public_or_unknown_space_is_dropped(households, gfs):
    """Only a global space is reached through the GFS."""
    a, b = households
    approve = _approver(b)
    public = await b.space_service.create_space(
        owner_username=b.username,
        name="Ours",
        space_type=SpaceType.PUBLIC,
        join_mode=JoinMode.INVITE_ONLY,
        features=SpaceFeatures(allow_subscribers=True),
    )
    for space_id in (public.id, "sp-unknown"):
        out = await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space_id, user_id="u", request_id=f"r-{space_id}")
        )
        assert out == {"ok": True, "filed": False}
    approve.assert_not_awaited()


@pytest.mark.parametrize("join_mode", [JoinMode.OPEN, JoinMode.INVITE_ONLY])
async def test_a_follow_of_a_local_community_through_the_gfs_is_refused(
    households, gfs, join_mode
):
    """Owner 2026-11-23: following a local community is inside only, and a
    GFS follow carries no location — it never reaches one (only a public
    space is a local community, and only a global space is asked through a
    GFS). Refused whatever the join mode: no approval, nothing filed."""
    a, b = households
    approve = _approver(b)
    community = await b.space_service.create_space(
        owner_username=b.username,
        name="Quartier",
        space_type=SpaceType.PUBLIC,
        join_mode=join_mode,
        lat=47.3769,
        lon=8.5417,
        radius_km=5,
        features=SpaceFeatures(allow_subscribers=True),
    )
    out = await b.gfsjoin.join.handle_body(
        _follow(a, space_id=community.id, user_id="u", request_id="r-lc")
    )
    assert out == {"ok": True, "filed": False}
    approve.assert_not_awaited()
    assert await b.space_repo.list_pending_join_requests(community.id) == []


async def test_a_member_request_to_a_follows_only_space_is_still_dropped(
    households, gfs
):
    """Followers on do not open an invite-only space to member requests."""
    a, b = households
    space = await _followable_space(b, gfs, a)
    approve = _approver(b)
    out = await b.gfsjoin.join.handle_body(
        _signed(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out == {"ok": True, "filed": False}
    approve.assert_not_awaited()
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_without_an_approver_a_follow_is_dropped(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a)
    b.gfsjoin.join._follow_approver = None
    out = await b.gfsjoin.join.handle_body(
        _follow(a, space_id=space.id, user_id="u", request_id="r")
    )
    assert out == {"ok": True, "filed": False}
    assert await b.space_repo.get_join_request("r") is None


@pytest.mark.parametrize("role", ["member", "admin", "owner", "follower", 1])
async def test_an_unknown_requested_role_is_dropped(households, gfs, role):
    a, b = households
    space = await _followable_space(b, gfs, a, join_mode=JoinMode.REQUEST)
    approve = _approver(b)
    with pytest.raises(ValueError, match="requested_role"):
        await b.gfsjoin.join.handle_body(
            _signed(
                a, space_id=space.id, user_id="u", request_id="r", requested_role=role
            )
        )
    approve.assert_not_awaited()
    assert await b.space_repo.list_pending_join_requests(space.id) == []


async def test_a_follow_at_the_pending_cap_answers_busy(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a)
    approve = _approver(b)
    with patch.object(gfs_join_module, "MAX_PENDING_PER_SPACE", 0):
        out = await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space.id, user_id="u", request_id="r")
        )
    assert out["filed"] is False and out["busy"] is True
    approve.assert_not_awaited()


async def test_a_follow_over_the_space_budget_answers_busy(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a)
    approve = _approver(b)
    b.gfsjoin.join._rate_limiter = RateLimiter()
    with (
        patch.object(gfs_join_module, "PER_SPACE_LIMIT", 1),
        patch.object(gfs_join_module, "PER_SENDER_LIMIT", 100),
    ):
        first = await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space.id, user_id="u1", request_id="r1")
        )
        second = await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space.id, user_id="u2", request_id="r2")
        )
    assert first["filed"] is True
    assert second["filed"] is False and second["busy"] is True
    assert approve.await_count == 1


async def test_a_banned_or_pending_follower_is_dropped_without_a_word(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a, join_mode=JoinMode.REQUEST)
    await b.space_repo.ban_member(space.id, "banned-u", banned_by=b.user_id)
    # A member request of "asker" already waits for the admins.
    assert (
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="asker", request_id="first")
        )
    )["filed"] is True
    approve = _approver(b)
    before = await _answers_to(gfs, a)
    for user, rid in (("banned-u", "f1"), ("asker", "f2")):
        out = await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space.id, user_id=user, request_id=rid)
        )
        assert out == {"ok": True, "filed": False}
    approve.assert_not_awaited()
    assert await _answers_to(gfs, a) == before


async def test_a_follow_under_a_used_request_id_is_refused(households, gfs):
    a, b = households
    space = await _followable_space(b, gfs, a, join_mode=JoinMode.REQUEST)
    # A member request filed under the id, then declined.
    assert (
        await b.gfsjoin.join.handle_body(
            _signed(a, space_id=space.id, user_id="u1", request_id="r-same")
        )
    )["filed"] is True
    await b.space_service.deny_join_request(
        "r-same", actor_username=b.username, space_id=space.id
    )
    approve = _approver(b)
    with pytest.raises(ValueError, match="already in use"):
        await b.gfsjoin.join.handle_body(
            _follow(a, space_id=space.id, user_id="u2", request_id="r-same")
        )
    approve.assert_not_awaited()


async def test_an_approved_follow_answer_is_redeemed_as_a_subscriber(households, gfs):
    """The applicant's side is the request's: the approval is kept, the
    redeem seats the person, and the seat's role is the token's."""
    a, b = households
    space = await _followable_space(b, gfs, a)
    seen: list[dict] = []
    real_redeem = a.gfsjoin.join._redeem

    async def _redeem(*args, **kwargs):
        result = await real_redeem(*args, **kwargs)
        seen.append(result)
        return result

    a.gfsjoin.join.attach_redeem(_redeem)
    rid = await a.gfsjoin.join.request_join(
        space.id,
        applicant_user_id=a.user_id,
        host_instance_id=b.instance_id,
        requested_role=SpaceRole.SUBSCRIBER,
    )
    await _settle(gfs, a, b)
    assert [(r["space_id"], r["role"]) for r in seen] == [(space.id, "subscriber")]
    assert (await a.space_repo.get_join_request(rid))["status"] == "approved"
    assert await a.space_service.list_joining_space_ids(a.user_id) == []
