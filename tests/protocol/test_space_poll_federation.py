"""§27.9 release blocker: a space poll works across households.

Marked ``@pytest.mark.security``. Five households, shaped like
:mod:`tests.protocol.test_bazaar_offer_federation` (whose loopback network
and pairing this module reuses): space ``SP`` hosted on **a** (anna), bob on
**b** and cara on **c** are members, dora on **d** only follows, eve on
**e** is paired but no member. Real crypto, real SQLite, the real §24.11
inbound pipeline, the real :class:`PollService`, poll outbound bridge,
inbound handlers and parent-wait buffer on every household.

Proves: a poll made on one household exists on every member household
(follower household included) with the same option ids, and on no other;
people on different households vote and every household shows the same
tallies; a retraction and a multiple-choice selection cross too; a vote or
a close that overtakes the poll, and a poll that overtakes its post, are
held and applied; nothing rides outside the ciphertext; a re-sent or forged
definition, a forged vote and a follower's vote change nothing; deleting
the post drops the poll.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.events import SpacePostCreated
from socialhome.domain.federation import FederationEventType
from socialhome.domain.post import poll_option_id
from socialhome.federation.federation_service import FederationService
from socialhome.federation.pending_parent_buffer import PendingParentBuffer
from socialhome.federation.space_authorship import SpaceAuthorship
from socialhome.federation.transport import FederationTransport
from socialhome.infrastructure.event_bus import EventBus
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.repositories.calendar_repo import SqliteSpaceCalendarRepo
from socialhome.repositories.federation_repo import SqliteFederationRepo
from socialhome.repositories.outbox_repo import SqliteOutboxRepo
from socialhome.repositories.page_repo import SqlitePageRepo
from socialhome.repositories.space_poll_repo import SqliteSpacePollRepo
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.sticky_repo import SqliteStickyRepo
from socialhome.repositories.task_repo import SqliteSpaceTaskRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.federation_inbound.space_content import (
    SpaceContentInboundHandlers,
)
from socialhome.services.poll_federation_outbound import PollFederationOutbound
from socialhome.services.poll_service import PollNotFoundError, PollService
from tests.protocol.test_bazaar_offer_federation import (
    _LoopbackInbox,
    _no_signal,
    _pair,
)

pytestmark = pytest.mark.security

FET = FederationEventType
SP = "sp-lake"
POST = "post-anna-poll"
#: household → its local users and their space roles (``None``: no seat).
ROSTER: dict[str, dict[str, str | None]] = {
    "a": {"anna": "owner"},
    "b": {"bob": "member"},
    "c": {"cara": "member"},
    "d": {"dora": "subscriber"},
    "e": {"eve": None},
}
MEMBER_HOUSES = ("a", "b", "c", "d")
#: Long and unique, so finding it in a wire capture can only mean a leak.
QUESTION = "Which evening suits everyone for the lakeside barbecue this month"
OPTIONS = ["Friday the ninth", "Saturday the tenth", "Sunday the eleventh"]


async def _household(tmp_path, name: str, world: dict) -> SimpleNamespace:
    db = AsyncDatabase(tmp_path / f"{name}.db", batch_timeout_ms=10)
    await db.startup()
    ident = generate_identity_keypair()
    iid = derive_instance_id(ident.public_key)
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (iid, ident.private_key.hex(), ident.public_key.hex(), "aa" * 32),
    )
    km = KeyManager(os.urandom(32))
    bus = EventBus()
    fed_repo = SqliteFederationRepo(db)
    federation = FederationService(
        db,
        fed_repo,
        SqliteOutboxRepo(db),
        km,
        bus,
        iid,
        ident.private_key,
        ident.public_key,
    )
    inbox = _LoopbackInbox(world, name)
    transport = FederationTransport(
        own_instance_id=iid, https_inbox=inbox, signaling_send=_no_signal
    )
    transport.mark_ice_primed()
    federation.attach_transport(transport)
    users = SqliteUserRepo(db)
    spaces = SqliteSpaceRepo(db)
    seats = SqliteSpaceRemoteMemberRepo(db)
    federation.attach_space_write_gate(spaces, seats)
    posts = SqliteSpacePostRepo(db)
    poll_repo = SqliteSpacePollRepo(db)
    polls = PollService(poll_repo, bus)
    PollFederationOutbound(bus=bus, federation_service=federation).wire()
    pending = PendingParentBuffer()
    pending.attach_bus(bus)
    SpaceContentInboundHandlers(
        bus=bus,
        authorship=SpaceAuthorship(
            space_repo=spaces, remote_member_repo=seats, user_repo=users
        ),
        post_repo=posts,
        page_repo=SqlitePageRepo(db),
        sticky_repo=SqliteStickyRepo(db),
        task_repo=SqliteSpaceTaskRepo(db),
        calendar_repo=SqliteSpaceCalendarRepo(db),
        poll_repo=poll_repo,
        pending_parents=pending,
    ).attach_to(federation)
    h = SimpleNamespace(
        name=name,
        db=db,
        iid=iid,
        ident=ident,
        km=km,
        bus=bus,
        fed_repo=fed_repo,
        federation=federation,
        transport=transport,
        inbox=inbox,
        posts=posts,
        polls=polls,
        poll_repo=poll_repo,
        pending=pending,
    )
    world[name] = h
    return h


async def _add_post(h, post_id: str = POST, *, announce: bool = False) -> None:
    """anna's poll post, as ``SPACE_POST_CREATED`` leaves it on a member
    household (``announce``: tell the household's bus, as that handler does)."""
    await h.db.enqueue(
        "INSERT INTO space_posts(id, space_id, author, type, content)"
        " VALUES(?,?,?,?,?)",
        (post_id, SP, "u-anna", "poll", ""),
    )
    if announce:
        got = await h.posts.get(post_id)
        assert got is not None
        await h.bus.publish(SpacePostCreated(post=got[1], space_id=SP))


async def _seed(world: dict[str, SimpleNamespace], *, post_on: str) -> None:
    host = world["a"]
    for h in world.values():
        for username in ROSTER[h.name]:
            await h.db.enqueue(
                "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
                (username, f"u-{username}", username.title()),
            )
        if h.name not in MEMBER_HOUSES:
            continue
        await h.db.enqueue(
            "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
            " identity_public_key) VALUES(?, 'Lake', ?, 'anna', ?)",
            (SP, host.iid, "ab" * 32),
        )
        for other_name in MEMBER_HOUSES:
            if other_name != h.name:
                await h.db.enqueue(
                    "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
                    (SP, world[other_name].iid),
                )
        for house in MEMBER_HOUSES:
            for username, role in ROSTER[house].items():
                if house == h.name:
                    await h.db.enqueue(
                        "INSERT INTO space_members(space_id, user_id, role)"
                        " VALUES(?,?,?)",
                        (SP, f"u-{username}", role),
                    )
                else:
                    # The roster mirror: a remote seat is never ``owner``.
                    await h.db.enqueue(
                        "INSERT INTO space_remote_members(space_id, instance_id,"
                        " user_id, role, display_name) VALUES(?,?,?,?,?)",
                        (
                            SP,
                            world[house].iid,
                            f"u-{username}",
                            "member" if role == "owner" else role,
                            username.title(),
                        ),
                    )
        if h.name in post_on:
            await _add_post(h)


async def _world(tmp_path, *, post_on: str = "abcd") -> SimpleNamespace:
    households: dict[str, SimpleNamespace] = {}
    for name in ROSTER:
        await _household(tmp_path, name, households)
    a, b, c, d, e = (households[n] for n in "abcde")
    for x, y in ((a, b), (a, c), (a, d), (a, e), (b, c), (b, d), (b, e), (c, d)):
        await _pair(x, y)
    await _seed(households, post_on=post_on)
    return SimpleNamespace(**households)


async def _close(world) -> None:
    for h in (world.a, world.b, world.c, world.d, world.e):
        await h.db.shutdown()


@pytest.fixture
async def world(tmp_path):
    w = await _world(tmp_path)
    yield w
    await _close(w)


async def _make_poll(h, *, allow_multiple: bool = False, post_id: str = POST) -> dict:
    return await h.polls.create_poll(
        post_id=post_id,
        question=QUESTION,
        options=OPTIONS,
        allow_multiple=allow_multiple,
        space_id=SP,
    )


async def _tally(h, post_id: str = POST) -> dict[str, int] | None:
    """Option text → votes, as ``GET …/posts/{id}/poll`` reads it; ``None``
    when the household holds no poll (the route's 404)."""
    try:
        summary = await h.polls.summary(post_id, space_id=SP)
    except PollNotFoundError:
        return None
    return {o["text"]: o["vote_count"] for o in summary["options"]}


def _option(summary: dict, text: str) -> str:
    return next(o["id"] for o in summary["options"] if o["text"] == text)


def _traffic(world, *types: str) -> list[tuple[str, str, dict]]:
    out = []
    for h in (world.a, world.b, world.c, world.d, world.e):
        for to, env in h.inbox.posted:
            if env.get("event_type") in types:
                out.append((h.name, to, env))
    return out


async def _send(sender, target, event_type, payload: dict) -> None:
    """``sender`` signs and ships one event to ``target`` directly."""
    await sender.federation.send_event(
        to_instance_id=target.iid,
        event_type=event_type,
        payload={"space_id": SP, **payload},
        space_id=SP,
    )


# ── The round trip ────────────────────────────────────────────────────────


async def test_a_poll_exists_on_every_member_household_with_the_same_options(world):
    a, b, c, d, e = world.a, world.b, world.c, world.d, world.e
    made = await _make_poll(a)
    want = [(o["id"], o["text"]) for o in made["options"]]
    assert [text for _id, text in want] == OPTIONS
    for h in (a, b, c, d):
        got = await h.polls.summary(POST, space_id=SP)
        assert [(o["id"], o["text"]) for o in got["options"]] == want, h.name
        assert got["question"] == QUESTION
        assert got["allow_multiple"] is False and got["closed"] is False
    # A paired household that is no member of the space hears nothing.
    assert await _tally(e) is None
    assert sorted(t for _f, t, _ in _traffic(world, "space_poll_created")) == [
        "b",
        "c",
        "d",
    ]


async def test_two_households_vote_and_everyone_sees_the_same_tallies(world):
    a, b, c, d = world.a, world.b, world.c, world.d
    made = await _make_poll(a)
    friday, saturday = _option(made, OPTIONS[0]), _option(made, OPTIONS[1])
    await a.polls.cast_vote(
        post_id=POST, option_id=saturday, voter_user_id="u-anna", space_id=SP
    )
    await b.polls.cast_vote(
        post_id=POST, option_id=saturday, voter_user_id="u-bob", space_id=SP
    )
    await c.polls.cast_vote(
        post_id=POST, option_id=friday, voter_user_id="u-cara", space_id=SP
    )
    want = {OPTIONS[0]: 1, OPTIONS[1]: 2, OPTIONS[2]: 0}
    for h in (a, b, c, d):
        assert await _tally(h) == want, h.name
    # bob changes his mind, cara takes hers back.
    await b.polls.cast_vote(
        post_id=POST, option_id=friday, voter_user_id="u-bob", space_id=SP
    )
    await c.polls.retract_vote(post_id=POST, voter_user_id="u-cara", space_id=SP)
    want = {OPTIONS[0]: 1, OPTIONS[1]: 1, OPTIONS[2]: 0}
    for h in (a, b, c, d):
        assert await _tally(h) == want, h.name
    # Each household knows its own person's vote, and nobody else's as theirs.
    assert (await b.polls.summary(POST, voter_user_id="u-bob"))["user_vote"] == [friday]
    assert (await a.polls.summary(POST, voter_user_id="u-anna"))["user_vote"] == [
        saturday
    ]


async def test_a_multiple_choice_selection_crosses_whole(world):
    a, b, c = world.a, world.b, world.c
    made = await _make_poll(a, allow_multiple=True)
    friday, sunday = _option(made, OPTIONS[0]), _option(made, OPTIONS[2])
    for option_id in (friday, sunday):
        await b.polls.cast_vote(
            post_id=POST, option_id=option_id, voter_user_id="u-bob", space_id=SP
        )
    want = {OPTIONS[0]: 1, OPTIONS[1]: 0, OPTIONS[2]: 1}
    for h in (a, b, c):
        assert await _tally(h) == want, h.name
    # Voting an option again toggles it off — everywhere.
    await b.polls.cast_vote(
        post_id=POST, option_id=friday, voter_user_id="u-bob", space_id=SP
    )
    for h in (a, b, c):
        assert await _tally(h) == {OPTIONS[0]: 0, OPTIONS[1]: 0, OPTIONS[2]: 1}, h.name


async def test_the_authors_close_reaches_every_household(world):
    a, b, c, d = world.a, world.b, world.c, world.d
    await _make_poll(a)
    await a.polls.close_poll(post_id=POST, actor_user_id="u-anna", space_id=SP)
    for h in (a, b, c, d):
        assert (await h.polls.summary(POST, space_id=SP))["closed"] is True, h.name


async def test_no_poll_field_rides_outside_the_ciphertext(world):
    a, b = world.a, world.b
    made = await _make_poll(a)
    await b.polls.cast_vote(
        post_id=POST,
        option_id=_option(made, OPTIONS[0]),
        voter_user_id="u-bob",
        space_id=SP,
    )
    traffic = _traffic(world, "space_poll_created", "space_poll_vote_cast")
    assert len(traffic) == 6
    leaks = [QUESTION, *OPTIONS, POST, "u-bob", *(o["id"] for o in made["options"])]
    for _frm, _to, env in traffic:
        raw = json.dumps(env)
        for leak in leaks:
            assert leak not in raw, leak
        assert "encrypted_payload" in env
        for field in ("question", "options", "option_ids", "voter_user_id"):
            assert field not in env


# ── Arrival order ─────────────────────────────────────────────────────────


async def test_a_vote_that_overtakes_the_poll_is_held_and_counted(tmp_path):
    """bob's vote reaches cara's household before anna's poll does: it waits
    for the poll instead of being lost (nothing resends a vote)."""
    world = await _world(tmp_path)
    try:
        a, b, c = world.a, world.b, world.c
        made = await _make_poll(a)
        # c never heard of the poll: it holds the post alone.
        await c.db.enqueue("DELETE FROM space_polls WHERE post_id=?", (POST,))
        assert await _tally(c) is None
        await b.polls.cast_vote(
            post_id=POST,
            option_id=_option(made, OPTIONS[1]),
            voter_user_id="u-bob",
            space_id=SP,
        )
        assert await _tally(c) is None and len(c.pending) == 1
        # … and a's close overtakes it too.
        await a.polls.close_poll(post_id=POST, actor_user_id="u-anna", space_id=SP)
        assert len(c.pending) == 2
        await _send(
            a,
            c,
            FET.SPACE_POLL_CREATED,
            {
                "post_id": POST,
                "question": QUESTION,
                "allow_multiple": False,
                "closes_at": None,
                "options": [
                    {"id": o["id"], "text": o["text"]} for o in made["options"]
                ],
            },
        )
        assert len(c.pending) == 0
        assert await _tally(c) == await _tally(a) == await _tally(b)
        assert (await _tally(c))[OPTIONS[1]] == 1
        assert (await c.polls.summary(POST, space_id=SP))["closed"] is True
    finally:
        await _close(world)


async def test_a_poll_that_overtakes_its_post_is_held_until_the_post_lands(tmp_path):
    world = await _world(tmp_path, post_on="abd")
    try:
        a, c = world.a, world.c
        made = await _make_poll(a)
        assert await _tally(c) is None and len(c.pending) == 1
        await _add_post(c, announce=True)
        assert len(c.pending) == 0
        got = await c.polls.summary(POST, space_id=SP)
        assert [o["id"] for o in got["options"]] == [o["id"] for o in made["options"]]
    finally:
        await _close(world)


# ── Refusals over the real pipeline ───────────────────────────────────────


def _definition(post_id: str = POST, **over) -> dict:
    """A well-formed definition anna's household could send for ``post_id``
    (its option ids are bound to the post), with ``over`` on top."""
    return {
        "post_id": post_id,
        "question": "Hijacked?",
        "allow_multiple": True,
        "closes_at": None,
        "options": [
            {"id": poll_option_id(post_id, 0), "text": "yes"},
            {"id": poll_option_id(post_id, 1), "text": "no"},
        ],
        **over,
    }


async def test_a_resent_definition_never_rewrites_a_held_poll(world):
    a, b = world.a, world.b
    await _make_poll(a)
    before = await b.polls.summary(POST, space_id=SP)
    await _send(a, b, FET.SPACE_POLL_CREATED, _definition())
    assert await b.polls.summary(POST, space_id=SP) == before


@pytest.mark.parametrize("sender", ["b", "d", "e"])
async def test_only_the_post_authors_household_defines_its_poll(world, sender):
    """A co-member, a follower and a non-member household cannot put a poll
    on anna's post."""
    c = world.c
    await _send(getattr(world, sender), c, FET.SPACE_POLL_CREATED, _definition())
    assert await _tally(c) is None
    assert len(c.pending) == 0


def _bad_definitions() -> list[tuple[str, dict]]:
    good = _definition()["options"]
    third = {"id": poll_option_id(POST, 2), "text": "maybe"}
    return [
        ("no options", {"options": None}),
        ("empty", {"options": []}),
        ("one option", {"options": good[:1]}),
        ("empty text", {"options": [good[0], {**good[1], "text": ""}]}),
        ("long text", {"options": [good[0], {**good[1], "text": "b" * 501}]}),
        ("not an object", {"options": [good[0], "b"]}),
        ("same id twice", {"options": [good[0], {**good[0], "text": "b"}]}),
        ("too many", {"options": [{"id": f"x{i}", "text": "a"} for i in range(21)]}),
        # Option ids are bound to the post: random ids, another post's ids
        # and a shuffled list are not this poll's.
        (
            "free ids",
            {"options": [{"id": "x1", "text": "a"}, {"id": "x2", "text": "b"}]},
        ),
        ("another post's ids", {"options": _definition("post-other")["options"]}),
        ("out of order", {"options": [good[1], good[0]]}),
        ("skipped position", {"options": [good[0], third]}),
        ("no question", {"question": " "}),
        ("long question", {"question": "q" * 501}),
        ("deadline not a string", {"closes_at": 5}),
        ("flag not a bool", {"allow_multiple": "yes"}),
    ]


@pytest.mark.parametrize(
    "over",
    [over for _label, over in _bad_definitions()],
    ids=[label for label, _over in _bad_definitions()],
)
async def test_a_malformed_definition_creates_nothing(world, over):
    a, b = world.a, world.b
    await _send(a, b, FET.SPACE_POLL_CREATED, _definition(**over))
    assert await _tally(b) is None


async def test_a_well_formed_definition_from_the_author_is_the_control(world):
    """…and the same event without a defect lands."""
    a, b = world.a, world.b
    await _send(a, b, FET.SPACE_POLL_CREATED, _definition())
    assert await _tally(b) == {"yes": 0, "no": 0}


async def test_nobody_can_take_a_polls_option_ids_first(world):
    """bob's household announces a poll on bob's own post using the option
    ids of anna's poll — to make cara's household refuse anna's poll when
    it arrives. The ids are not his post's: his poll is refused, hers lands."""
    a, b, c = world.a, world.b, world.c
    for h in (a, b, c):
        await h.db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES('post-bob', ?, 'u-bob', 'poll', '')",
            (SP,),
        )
    await c.db.enqueue("DELETE FROM space_polls")
    squat = _definition("post-bob", options=_definition()["options"])
    await _send(b, c, FET.SPACE_POLL_CREATED, squat)
    assert await _tally(c, "post-bob") is None
    await _send(a, c, FET.SPACE_POLL_CREATED, _definition())
    assert await _tally(c) == {"yes": 0, "no": 0}


@pytest.mark.parametrize("level", ["moderated", "admin_only"])
async def test_a_members_poll_cannot_skip_the_posts_access_level(world, level):
    """Where posts are reviewed (or admin-only) a plain member's household
    cannot attach a poll to its post by itself — the local route refuses
    it, and so does every household that receives it. The owner still can."""
    a, b, c = world.a, world.b, world.c
    for h in (a, b, c):
        await h.db.enqueue("UPDATE spaces SET posts_access=?", (level,))
        await h.db.enqueue(
            "INSERT INTO space_posts(id, space_id, author, type, content)"
            " VALUES('post-bob', ?, 'u-bob', 'poll', '')",
            (SP,),
        )
    await _send(b, c, FET.SPACE_POLL_CREATED, _definition("post-bob"))
    assert await _tally(c, "post-bob") is None
    await _send(a, c, FET.SPACE_POLL_CREATED, _definition())
    assert await _tally(c) == {"yes": 0, "no": 0}


async def test_a_vote_after_the_close_changes_nothing(world):
    """A closed poll's result stands on every household, whoever sends a
    late vote or retraction."""
    a, b, c = world.a, world.b, world.c
    made = await _make_poll(a)
    friday, saturday = _option(made, OPTIONS[0]), _option(made, OPTIONS[1])
    await b.polls.cast_vote(
        post_id=POST, option_id=friday, voter_user_id="u-bob", space_id=SP
    )
    await a.polls.close_poll(post_id=POST, actor_user_id="u-anna", space_id=SP)
    want = {OPTIONS[0]: 1, OPTIONS[1]: 0, OPTIONS[2]: 0}
    for option_ids in ([saturday], []):
        for target in (a, c):
            await _send(
                b,
                target,
                FET.SPACE_POLL_VOTE_CAST,
                {"post_id": POST, "option_ids": option_ids, "voter_user_id": "u-bob"},
            )
    for h in (a, b, c):
        assert await _tally(h) == want, h.name


async def test_nobody_votes_for_another_households_member(world):
    a, b, c = world.a, world.b, world.c
    made = await _make_poll(a)
    friday = _option(made, OPTIONS[0])
    vote = {"post_id": POST, "option_ids": [friday], "voter_user_id": "u-bob"}
    # c (and even the host) cannot cast bob's vote; bob's household can.
    await _send(c, a, FET.SPACE_POLL_VOTE_CAST, vote)
    await _send(a, c, FET.SPACE_POLL_VOTE_CAST, vote)
    for h in (a, c):
        assert await _tally(h) == dict.fromkeys(OPTIONS, 0), h.name
    await _send(b, a, FET.SPACE_POLL_VOTE_CAST, vote)
    assert (await _tally(a))[OPTIONS[0]] == 1


async def test_a_followers_vote_is_refused_everywhere(world):
    a, d = world.a, world.d
    made = await _make_poll(a)
    await _send(
        d,
        a,
        FET.SPACE_POLL_VOTE_CAST,
        {
            "post_id": POST,
            "option_ids": [_option(made, OPTIONS[0])],
            "voter_user_id": "u-dora",
        },
    )
    assert await _tally(a) == dict.fromkeys(OPTIONS, 0)


async def test_a_vote_must_fit_the_poll(world):
    """Two options on a single-choice poll, or an option of no poll here,
    change nothing — the voter's earlier vote stays."""
    a, b = world.a, world.b
    made = await _make_poll(a)
    friday, saturday = _option(made, OPTIONS[0]), _option(made, OPTIONS[1])
    await b.polls.cast_vote(
        post_id=POST, option_id=friday, voter_user_id="u-bob", space_id=SP
    )
    for option_ids in ([friday, saturday], ["not-an-option"], "friday", [1]):
        await _send(
            b,
            a,
            FET.SPACE_POLL_VOTE_CAST,
            {"post_id": POST, "option_ids": option_ids, "voter_user_id": "u-bob"},
        )
        assert await _tally(a) == {OPTIONS[0]: 1, OPTIONS[1]: 0, OPTIONS[2]: 0}


# ── The post's life ───────────────────────────────────────────────────────


async def test_deleting_the_post_drops_the_poll_and_refuses_a_new_one(world):
    a, b = world.a, world.b
    made = await _make_poll(a)
    await b.polls.cast_vote(
        post_id=POST,
        option_id=_option(made, OPTIONS[0]),
        voter_user_id="u-bob",
        space_id=SP,
    )
    # The delete as SPACE_POST_DELETED applies it on a member household.
    assert await b.posts.soft_delete(POST, space_id=SP)
    assert await _tally(b) is None
    await _send(
        a,
        b,
        FET.SPACE_POLL_CREATED,
        {
            "post_id": POST,
            "question": QUESTION,
            "options": [{"id": o["id"], "text": o["text"]} for o in made["options"]],
        },
    )
    assert await _tally(b) is None
