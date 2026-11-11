"""v_93 — reactions on space posts reach every member household.

Marked ``@pytest.mark.security``. Runs on real applications over real SQLite
databases, through the real handler registry.

A reaction is a delta ``{space_id, post_id, user_id, emoji, action, at}``
fanned out by the reactor's household. What is proven here:

* **authorship** — the reactor is seated on the sending household (a writer
  seat, or a follower seat while ``allow_subscriber_react`` is on); never a
  local user of ours, never a banned one;
* **order-independent, idempotent apply** — the newest ``at`` per (post,
  user, emoji) wins, in whatever order the events arrive, and a late or
  repeated add never brings back a removed reaction;
* **parity** — two households that each react see the same counts, also
  after one of them takes its reaction back;
* **catch-up** — a reaction missed while offline heals from the host's §25.6
  stream (the v_92 ``reaction_stamps``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

import orjson
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    federation_service_key,
    space_service_key,
    space_sync_receiver_key,
)
from socialhome.config import Config
from socialhome.domain.federation import FederationEvent
from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.federation_capabilities import FederationCapability
from tests.protocol.test_space_content_authorship import (
    AUTHOR,
    HOST,
    LOCAL_USER,
    OTHER,
    SP,
    _deliver,
    env,
)

pytestmark = pytest.mark.security

__all__ = ["env"]

POST = "post-l"  # a text post by our local user, no reactions seeded


def _at(seconds: float = 0.0) -> str:
    """A stamp ``seconds`` from now (negative: in the past)."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _reaction(user="u-o", *, action="add", at=None, emoji="👍", post=POST) -> dict:
    return {
        "space_id": SP,
        "post_id": post,
        "user_id": user,
        "emoji": emoji,
        "action": action,
        "at": at or _at(),
    }


async def _reactions(db, post=POST) -> dict[str, list[str]]:
    row = await db.fetchone("SELECT reactions FROM space_posts WHERE id=?", (post,))
    return {k: sorted(v) for k, v in orjson.loads(row["reactions"] or "{}").items()}


async def _send(app, payload, *, sender=OTHER) -> None:
    await _deliver(app, FET.SPACE_POST_REACTION, payload, sender=sender)


# ── Apply: add, remove, order, idempotence ───────────────────────────


async def test_a_members_reaction_lands_and_is_taken_back(env):
    app, db = env
    await _send(app, _reaction(at=_at(-10)))
    assert await _reactions(db) == {"👍": ["u-o"]}
    await _send(app, _reaction(action="remove", at=_at(-5)))
    assert await _reactions(db) == {}


async def test_a_redelivered_reaction_is_a_no_op(env):
    app, db = env
    add = _reaction(at=_at(-10))
    await _send(app, add)
    await _send(app, add)
    assert await _reactions(db) == {"👍": ["u-o"]}


async def test_a_late_add_never_brings_back_a_removed_reaction(env):
    """The add was made BEFORE the remove but arrives after it (a queued
    copy, a second path): the newest stamp wins, whatever the order."""
    app, db = env
    add, remove = _reaction(at=_at(-10)), _reaction(action="remove", at=_at(-5))
    await _send(app, add)
    await _send(app, remove)
    await _send(app, add)
    assert await _reactions(db) == {}


async def test_a_remove_that_overtakes_its_add_keeps_it_removed(env):
    app, db = env
    await _send(app, _reaction(action="remove", at=_at(-5)))
    await _send(app, _reaction(at=_at(-10)))
    assert await _reactions(db) == {}
    # A NEWER add is a new reaction.
    await _send(app, _reaction(at=_at(-1)))
    assert await _reactions(db) == {"👍": ["u-o"]}


async def test_reactions_of_two_households_count_together(env):
    app, db = env
    await _send(app, _reaction("u-o"), sender=OTHER)
    await _send(app, _reaction("u-a"), sender=AUTHOR)
    await _send(app, _reaction("u-a", emoji="🎉"), sender=AUTHOR)
    assert await _reactions(db) == {"👍": ["u-a", "u-o"], "🎉": ["u-a"]}


# ── Authorship and input ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "payload", "sender"),
    [
        ("another household's member", _reaction("u-a"), OTHER),
        ("our own local user", _reaction(LOCAL_USER), OTHER),
        ("the host for a member of another household", _reaction("u-o"), HOST),
        ("a user the space never had", _reaction("u-ghost"), OTHER),
        ("a follower without the opt-in", _reaction("u-sub"), AUTHOR),
        ("a stamp from the future", _reaction(at=_at(3600)), OTHER),
        ("a stamp with no zone", _reaction(at="2026-06-01T10:00:00"), OTHER),
        ("no stamp", {**_reaction(), "at": None}, OTHER),
        ("an unknown action", _reaction(action="toggle"), OTHER),
        ("an empty emoji", _reaction(emoji=""), OTHER),
        ("an oversized emoji", _reaction(emoji="x" * 33), OTHER),
        ("an untrimmed emoji", _reaction(emoji=" 👍"), OTHER),
        ("a post this household does not hold", _reaction(post="post-nope"), OTHER),
        ("a deleted post", _reaction(post="post-del"), OTHER),
    ],
)
async def test_a_reaction_that_must_not_land_changes_nothing(
    env, label, payload, sender
):
    app, db = env
    before = await db.fetchall(
        "SELECT id, reactions, reaction_stamps_json FROM space_posts ORDER BY id", ()
    )
    await _send(app, payload, sender=sender)
    after = await db.fetchall(
        "SELECT id, reactions, reaction_stamps_json FROM space_posts ORDER BY id", ()
    )
    assert [tuple(r) for r in after] == [tuple(r) for r in before], label


async def test_a_banned_member_cannot_react(env):
    app, db = env
    await db.enqueue(
        "INSERT INTO space_bans(space_id, user_id, banned_by) VALUES(?,?,?)",
        (SP, "u-o", "u-h"),
    )
    await _send(app, _reaction("u-o"))
    assert await _reactions(db) == {}


async def test_a_follower_reacts_only_with_the_opt_in(env):
    """``allow_subscriber_react`` — the same admin toggle that decides what
    a LOCAL follower may do — and the reactor IS a follower seat of the
    sending household."""
    app, db = env
    await _send(app, _reaction("u-sub"), sender=AUTHOR)
    assert await _reactions(db) == {}
    await db.enqueue("UPDATE spaces SET allow_subscriber_react=1 WHERE id=?", (SP,))
    await _send(app, _reaction("u-sub"), sender=OTHER)  # not ITS follower
    assert await _reactions(db) == {}
    await _send(app, _reaction("u-sub"), sender=AUTHOR)
    assert await _reactions(db) == {"👍": ["u-sub"]}


async def test_a_reaction_that_overtakes_its_post_lands_with_the_post(env):
    """A reaction can arrive before the post it is on (another path): it is
    held and applied when the post lands."""
    app, db = env
    await _send(app, _reaction("u-o", post="post-late"))
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "post-late", "author": "u-a", "type": "text", "content": "x"},
        sender=AUTHOR,
    )
    assert await _reactions(db, "post-late") == {"👍": ["u-o"]}


# ── Two households ───────────────────────────────────────────────────


def _config(tmp_dir, name: str) -> Config:
    home = tmp_dir / name
    home.mkdir()
    return Config(
        data_dir=str(home),
        db_path=str(home / "home.db"),
        media_path=str(home / "media"),
        apps_path=str(home / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=10,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://test.example"})},
        ),
    )


class _Home:
    """One household on a real app: a member of ``SP`` (hosted elsewhere)
    with one local person, holding post ``p-1``."""

    def __init__(self, app, user: str) -> None:
        self.app = app
        self.db = app[db_key]
        self.user = user
        self.fed = app[federation_service_key]
        self.id = self.fed.own_instance_id
        self.sent: list[tuple[FET, dict, dict]] = []

    async def seed(self, peer: _Home) -> None:
        for sql, params in (
            (
                "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
                (self.user, self.user, self.user),
            ),
            (
                (
                    "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
                    " identity_public_key) VALUES(?,?,?,?,?)"
                ),
                (SP, SP, HOST, "anna", "00" * 32),
            ),
            (
                (
                    "INSERT INTO space_members(space_id, user_id, role)"
                    " VALUES(?,?,'member')"
                ),
                (SP, self.user),
            ),
            (
                (
                    "INSERT INTO space_remote_members(space_id, instance_id, user_id,"
                    " role) VALUES(?,?,?,'member')"
                ),
                (SP, peer.id, peer.user),
            ),
            (
                (
                    "INSERT INTO space_posts(id, space_id, author, type, content)"
                    " VALUES('p-1', ?, 'u-h', 'text', 'hello')"
                ),
                (SP,),
            ),
        ):
            await self.db.enqueue(sql, params)

    async def react(self, emoji: str = "👍") -> None:
        await self.app[space_service_key].add_reaction(
            "p-1", space_id=SP, user_id=self.user, emoji=emoji
        )

    async def unreact(self, emoji: str = "👍") -> None:
        await self.app[space_service_key].remove_reaction(
            "p-1", space_id=SP, user_id=self.user, emoji=emoji
        )

    async def take(self, sender: _Home, event_type: FET, payload: dict) -> None:
        """Deliver through the registry, as the §24.11 pipeline does."""
        for handler in self.fed._event_registry.handlers_for(event_type):
            await handler(
                FederationEvent(
                    msg_id=f"m-{len(sender.sent)}",
                    event_type=event_type,
                    from_instance=sender.id,
                    to_instance=self.id,
                    timestamp=datetime.now(UTC).isoformat(),
                    payload=dict(payload),
                    space_id=SP,
                )
            )

    async def counts(self) -> dict[str, list[str]]:
        return await _reactions(self.db, "p-1")


@pytest.fixture
async def homes(aiohttp_client, tmp_dir, monkeypatch):
    """Two member households, a and b. What each broadcasts to the space's
    members is recorded (``sent``) — the test decides when the other one
    gets it, so "offline" is simply "not delivered yet"."""
    homes = []
    for name, user in (("a", "u-anna"), ("b", "u-ben")):
        app = create_app(_config(tmp_dir, name))
        await aiohttp_client(app)
        homes.append(_Home(app, user))
    a, b = homes
    by_service = {id(home.fed): home for home in homes}

    async def broadcast(self, space_id, event_type, payload, **kwargs):
        assert space_id == SP
        by_service[id(self)].sent.append((event_type, dict(payload), kwargs))

    monkeypatch.setattr(type(a.fed), "broadcast_to_space_members", broadcast)
    assert a.id != b.id
    await a.seed(b)
    await b.seed(a)
    return a, b


async def _flush(src: _Home, dst: _Home) -> None:
    sent, src.sent = src.sent, []
    for event_type, payload, _kwargs in sent:
        await dst.take(src, event_type, payload)


async def test_two_households_react_and_see_the_same_counts(homes):
    a, b = homes
    await a.react()
    await b.react()
    await b.react("🎉")
    # Each sent one delta per reaction, gated on v_93, and nothing else.
    for home, n in ((a, 1), (b, 2)):
        assert [t for t, _p, _k in home.sent] == [FET.SPACE_POST_REACTION] * n
        assert all(
            k
            == {"min_proto_version": FederationCapability.MIN_FOR_SPACE_POST_REACTIONS}
            for _t, _p, k in home.sent
        )
    await _flush(a, b)
    await _flush(b, a)
    both = {"👍": ["u-anna", "u-ben"], "🎉": ["u-ben"]}
    assert await a.counts() == both
    assert await b.counts() == both
    # Applying a remote reaction is never echoed back.
    assert a.sent == [] and b.sent == []

    await a.unreact()
    await _flush(a, b)
    after = {"👍": ["u-ben"], "🎉": ["u-ben"]}
    assert await a.counts() == after
    assert await b.counts() == after


async def test_a_reaction_and_its_removal_delivered_out_of_order_converge(homes):
    a, b = homes
    await a.react()
    await a.unreact()
    (add, remove), a.sent = a.sent, []
    await b.take(a, *remove[:2])
    await b.take(a, *add[:2])
    assert await a.counts() == {} == await b.counts()


async def test_a_reaction_missed_while_offline_heals_from_the_host(homes):
    """b was offline: a's add never reached it (the live event is lost for
    good). The host's next §25.6 stream carries the post with its reaction
    sets and stamps; b ends with the same counts as a — and its own
    person's reaction, which the host has not heard of, stays."""
    a, b = homes
    await a.react()
    await b.react("🎉")
    a.sent.clear()  # lost
    stamp = (datetime.now(UTC) - timedelta(seconds=1)).isoformat(
        sep=" ", timespec="microseconds"
    )[:26]
    await b.app[space_sync_receiver_key]._dispatch(
        "posts",
        SP,
        [
            {
                "id": "p-1",
                "author": "u-h",
                "type": "text",
                "content": "hello",
                "created_at": "2026-06-01T10:00:00+00:00",
                "reactions": {"👍": ["u-anna"]},
                "reaction_stamps": {"u-anna\x00👍": [stamp, True]},
            }
        ],
        provider=HOST,
    )
    assert await b.counts() == {"👍": ["u-anna"], "🎉": ["u-ben"]}
    # A stale copy of the add that was lost changes nothing; a's later
    # removal still wins.
    await a.unreact()
    await _flush(a, b)
    assert await b.counts() == {"🎉": ["u-ben"]}


async def test_a_hosts_stream_never_drops_a_reaction_it_does_not_hold(homes):
    """The host may never have received a reaction (it was below v_93 when
    it was made, or the event was lost): its reaction sets lacking it is no
    removal — a removal travels only as a stamp. a's reaction, long past
    the stamp horizon, stays on b, whatever version the host is at."""
    a, b = homes
    await a.react()
    await _flush(a, b)
    old = "2026-01-01 00:00:00.000000"
    await b.db.enqueue(
        "UPDATE space_posts SET reaction_stamps_json=? WHERE id='p-1'",
        (orjson.dumps({"u-anna\x00👍": [old, True]}).decode(),),
    )
    record = {
        "id": "p-1",
        "author": "u-h",
        "type": "text",
        "content": "hello",
        "created_at": "2026-06-01T10:00:00+00:00",
        "reactions": {},
    }
    receiver = b.app[space_sync_receiver_key]
    await receiver._dispatch("posts", SP, [record], provider=HOST)
    assert await b.counts() == {"👍": ["u-anna"]}
    await b.db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id, proto_version)"
        " VALUES(?,?,?)",
        (SP, HOST, int(FederationCapability.MIN_FOR_SPACE_POST_REACTIONS)),
    )
    await receiver._dispatch("posts", SP, [record], provider=HOST)
    assert await b.counts() == {"👍": ["u-anna"]}
    # The removal itself still lands, as a stamp.
    await a.unreact()
    await _flush(a, b)
    assert await b.counts() == {}


async def _seat_reactors(db, n: int) -> list[str]:
    users = [f"u-r{i}" for i in range(n)]
    for user in users:
        await db.enqueue(
            "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
            " VALUES(?,?,?,'member')",
            (SP, OTHER, user),
        )
    return users


def _heard(app) -> list:
    from socialhome.app_keys import event_bus_key
    from socialhome.domain.events import PostReactionChanged

    heard: list = []

    async def _on(event) -> None:
        heard.append(event)

    app[event_bus_key].subscribe(PostReactionChanged, _on)
    return heard


async def test_a_reconnect_burst_of_queued_reactions_all_lands(env):
    """A household that comes back with 100 reactions queued for one space
    delivers them in one burst: every one is applied."""
    app, db = env
    users = await _seat_reactors(db, 100)
    for user in users:
        await _send(app, _reaction(user, at=_at(-60)))
    assert await _reactions(db) == {"👍": sorted(users)}


async def test_the_inbound_bound_never_loses_a_reaction(env, monkeypatch):
    """The backstop per (sending household, space) bounds the WORK a flood
    triggers here — the bus event behind notifications — never the state:
    a reaction past it is still stored, so every household converges (a
    drop could not be retried, and the host would drop it too). Only
    reactions that were applied count: a stale copy, a refused one and one
    held for its post (replayed later) cost nothing."""
    from socialhome.services import federation_inbound_service as mod

    monkeypatch.setattr(mod, "REACTION_ARRIVALS_PER_MIN", 3)
    app, db = env
    heard = _heard(app)
    users = await _seat_reactors(db, 5)
    first = _reaction(users[0], at=_at(-60))
    await _send(app, first)
    for _ in range(10):
        await _send(app, first)  # stale re-deliveries
        await _send(app, _reaction("u-a"))  # refused: not OTHER's member
    await _send(app, _reaction(users[1], post="post-late"))  # held for its post
    await _deliver(
        app,
        FET.SPACE_POST_CREATED,
        {"id": "post-late", "author": "u-a", "type": "text", "content": "x"},
        sender=AUTHOR,
    )
    for user in users[2:]:
        await _send(app, _reaction(user, at=_at(-60)))
    assert await _reactions(db) == {"👍": sorted([users[0], *users[2:]])}
    assert await _reactions(db, "post-late") == {"👍": [users[1]]}
    # Five applied, three announced; another household is not held back.
    assert len(heard) == 3
    await _send(app, _reaction("u-a"), sender=AUTHOR)
    assert len(heard) == 4


async def test_an_old_free_text_reaction_can_still_be_taken_back(env):
    """Before v_93 a reaction could be any short text. Adding one is
    refused now; removing one — here or from another household — works."""
    app, db = env
    await db.enqueue(
        "UPDATE space_posts SET reactions=? WHERE id=?",
        (orjson.dumps({"nice": ["u-o"], "+1": [LOCAL_USER]}).decode(), POST),
    )
    await _send(app, _reaction(emoji="nice"))  # an add: refused
    await _send(app, _reaction(emoji="nice", action="remove"))
    assert await _reactions(db) == {"+1": [LOCAL_USER]}
    await app[space_service_key].remove_reaction(
        POST, space_id=SP, user_id=LOCAL_USER, emoji="+1"
    )
    assert await _reactions(db) == {}
    # Still bounded: 32 characters, trimmed.
    await _send(app, _reaction(emoji="x" * 33, action="remove"))
    await _send(app, _reaction(emoji=" x", action="remove"))


async def test_the_sender_and_the_receiver_hold_the_same_stamp(homes):
    """The stamp on the wire is the one the reactor's household stored."""
    a, b = homes
    await a.react()
    await _flush(a, b)
    rows = [
        (
            await h.db.fetchone(
                "SELECT reaction_stamps_json FROM space_posts WHERE id='p-1'", ()
            )
        )["reaction_stamps_json"]
        for h in (a, b)
    ]
    assert rows[0] == rows[1]
