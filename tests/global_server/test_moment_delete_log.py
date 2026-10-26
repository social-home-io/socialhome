"""Tests for :class:`MomentDeleteLog` — the GFS's 30-day log of signed
public-moment delete certificates (v_66) and its reconnect push."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import orjson
import pytest

from socialhome.global_server import moment_delete_log as mdl
from socialhome.global_server.moment_delete_log import (
    DELETE_CERT_POSTS_PER_MINUTE,
    DELETE_CERTS_PER_AUTHOR_PER_PUSH,
    DELETE_CERTS_PER_FRAME,
    DELETE_LOG_TTL_S,
    FRAME_DELETE_CERTS,
    MAX_DELETE_CERTS_PER_PUSH,
    SEEN_SLACK_S,
    MomentDeleteLog,
)
from socialhome.global_server.repositories import (
    SqliteGfsMomentDeleteLogRepo,
    SqliteGfsMomentFollowRepo,
)
from socialhome.global_server.ws_registry import GfsWebSocketRegistry

NOW = 1_900_000_000


class _StubWs:
    def __init__(self) -> None:
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)


@pytest.fixture
async def env(gfs_db):
    for iid in ("inst-author", "inst-other", "inst-follower"):
        await gfs_db.enqueue(
            "INSERT INTO client_instances(instance_id, display_name, public_key,"
            " status) VALUES(?, ?, 'aa', 'active')",
            (iid, iid),
        )
    for uid, iid in (("u-author", "inst-author"), ("u-other", "inst-other")):
        await gfs_db.enqueue(
            "INSERT INTO gfs_user_registrations(user_id, instance_id, username,"
            " display_name, home_instance_pk, registered_at, status)"
            " VALUES(?, ?, ?, ?, 'aa', 1, 'active')",
            (uid, iid, uid, uid),
        )
    ws = GfsWebSocketRegistry()
    sock = _StubWs()
    await ws.register("inst-follower", sock)  # type: ignore[arg-type]
    repo = SqliteGfsMomentDeleteLogRepo(gfs_db)
    log = MomentDeleteLog(repo, SqliteGfsMomentFollowRepo(gfs_db), ws)
    return SimpleNamespace(db=gfs_db, log=log, repo=repo, sock=sock, ws=ws)


async def _follow(env, author: str, *, at: int = NOW - 40 * 86_400) -> None:
    await env.db.enqueue(
        "INSERT INTO gfs_moment_follows(follower_user_id, follower_instance_id,"
        " followed_user_id, created_at) VALUES(?, 'inst-follower', ?, ?)",
        (f"f-{author}", author, at),
    )


async def _seen(env, at: int | None) -> None:
    await env.db.enqueue(
        "UPDATE client_instances SET relay_seen_at=? WHERE instance_id='inst-follower'",
        (at,),
    )


def _cert(author: str, moment_id: str) -> dict:
    return {"author_user_id": author, "moment_id": moment_id, "occurred_at": "x"}


async def _add(env, author: str, mid: str, at: int) -> None:
    await env.repo.add(
        author_user_id=author,
        moment_id=mid,
        cert=orjson.dumps(_cert(author, mid)).decode(),
        now=at,
    )


def _pushed(env) -> list[list[str]]:
    frames = [orjson.loads(s) for s in env.sock.sent]
    assert all(f["type"] == FRAME_DELETE_CERTS for f in frames)
    return [[c["moment_id"] for c in f["certs"]] for f in frames]


async def test_a_certificate_is_kept_once_and_only_when_someone_follows(env):
    cert = _cert("u-author", "m1")
    assert await env.log.keep(author_user_id="u-author", cert=cert, now=NOW) == (
        "no_followers"
    )
    await _follow(env, "u-author")
    assert await env.log.keep(author_user_id="u-author", cert=cert, now=NOW) == "kept"
    # A replay changes nothing — not even when its 30 days run.
    assert await env.log.keep(author_user_id="u-author", cert=cert, now=NOW + 99) == (
        "already"
    )
    row = await env.db.fetchone(
        "SELECT cert, created_at FROM gfs_moment_delete_log WHERE moment_id='m1'"
    )
    assert orjson.loads(row["cert"]) == cert and row["created_at"] == NOW


async def test_an_author_past_the_cap_keeps_no_more(env, monkeypatch):
    monkeypatch.setattr(mdl, "MAX_DELETE_CERTS_PER_AUTHOR", 2)
    await _follow(env, "u-author")
    for mid in ("m1", "m2"):
        assert (
            await env.log.keep(
                author_user_id="u-author", cert=_cert("u-author", mid), now=NOW
            )
            == "kept"
        )
    assert (
        await env.log.keep(
            author_user_id="u-author", cert=_cert("u-author", "m3"), now=NOW
        )
        == "full"
    )
    # Certificates past their 30 days don't count.
    assert (
        await env.log.keep(
            author_user_id="u-author",
            cert=_cert("u-author", "m3"),
            now=NOW + DELETE_LOG_TTL_S + 1,
        )
        == "kept"
    )


async def test_a_reconnect_gets_only_followed_authors_certs_since_it_left(env):
    await _follow(env, "u-author")
    await _follow(env, "u-other", at=NOW - 86_400)
    await _seen(env, NOW - 3 * 86_400)
    rows = [
        ("u-author", "m-old", NOW - DELETE_LOG_TTL_S - 1),  # past its 30 days
        ("u-author", "m-before", NOW - 3 * 86_400 - SEEN_SLACK_S - 1),
        ("u-author", "m-slack", NOW - 3 * 86_400 - SEEN_SLACK_S + 1),
        ("u-author", "m-away", NOW - 86_400),
        ("u-other", "m-before-follow", NOW - 2 * 86_400),
        ("u-other", "m-after-follow", NOW - 3600),
    ]
    for author, mid, at in rows:
        await env.repo.add(
            author_user_id=author,
            moment_id=mid,
            cert=orjson.dumps(_cert(author, mid)).decode(),
            now=at,
        )
    # An author nobody here follows: never pushed.
    await env.db.enqueue(
        "INSERT INTO gfs_user_registrations(user_id, instance_id, username,"
        " display_name, home_instance_pk, registered_at, status)"
        " VALUES('u-stranger', 'inst-other', 's', 'S', 'aa', 1, 'active')"
    )
    await env.repo.add(
        author_user_id="u-stranger", moment_id="m-stranger", cert="{}", now=NOW
    )
    assert await env.log.push_to("inst-follower", now=NOW) == 3
    # Round-robin over authors, the newest of each round first.
    assert _pushed(env) == [["m-after-follow", "m-slack", "m-away"]]


async def test_a_household_never_seen_gets_the_whole_window(env):
    await _follow(env, "u-author")
    await _seen(env, None)
    for mid, at in (
        ("m-old", NOW - DELETE_LOG_TTL_S - 1),
        ("m-new", NOW - 20 * 86_400),
    ):
        await env.repo.add(
            author_user_id="u-author",
            moment_id=mid,
            cert=orjson.dumps(_cert("u-author", mid)).decode(),
            now=at,
        )
    assert await env.log.push_to("inst-follower", now=NOW) == 1
    assert _pushed(env) == [["m-new"]]


async def test_a_reconnect_is_capped_and_framed(env):
    await _follow(env, "u-author")
    await _follow(env, "u-other")
    for author in ("u-author", "u-other"):
        for i in range(DELETE_CERTS_PER_AUTHOR_PER_PUSH + 30):
            await _add(env, author, f"{author}-{i:04d}", NOW - 3600 + i)
    assert await env.log.push_to("inst-follower", now=NOW) == (
        MAX_DELETE_CERTS_PER_PUSH
    )
    frames = _pushed(env)
    assert len(frames) == MAX_DELETE_CERTS_PER_PUSH // DELETE_CERTS_PER_FRAME
    assert all(len(f) == DELETE_CERTS_PER_FRAME for f in frames)
    # Round-robin: each author's oldest owed one comes first.
    assert set(frames[0][:2]) == {"u-author-0000", "u-other-0000"}


async def test_no_socket_no_push_and_never_a_raise(env):
    await _follow(env, "u-author")
    await env.repo.add(
        author_user_id="u-author",
        moment_id="m1",
        cert=orjson.dumps(_cert("u-author", "m1")).decode(),
        now=NOW,
    )
    await env.ws.unregister("inst-follower", env.sock)  # type: ignore[arg-type]
    assert await env.log.push_to("inst-follower", now=NOW) == 0

    class _Broken:
        async def open_cursors(self, *a, **kw):
            raise RuntimeError("db gone")

    broken = MomentDeleteLog(_Broken(), None, env.ws)  # type: ignore[arg-type]
    assert await broken.push_to("inst-follower") == 0


async def test_schedule_runs_the_push_in_the_background(env):
    await _follow(env, "u-author", at=int(time.time()) - 60)
    await env.repo.add(
        author_user_id="u-author",
        moment_id="m1",
        cert=orjson.dumps(_cert("u-author", "m1")).decode(),
        now=int(time.time()),
    )
    env.log.schedule_household_connected("inst-follower")
    await asyncio.gather(*list(env.log._tasks))
    assert _pushed(env) == [["m1"]]


async def test_prune_drops_certificates_past_thirty_days(env):
    await _follow(env, "u-author")
    for mid, at in (("old", NOW - DELETE_LOG_TTL_S - 1), ("new", NOW - 60)):
        await env.repo.add(author_user_id="u-author", moment_id=mid, cert="{}", now=at)
    assert await env.log.prune(NOW) == 1
    rows = await env.db.fetchall("SELECT moment_id FROM gfs_moment_delete_log")
    assert [r["moment_id"] for r in rows] == ["new"]


async def test_posts_are_rate_limited_per_household_across_the_cluster(env):
    for _ in range(DELETE_CERT_POSTS_PER_MINUTE):
        assert env.log.allow_post("inst-author")
    assert not env.log.allow_post("inst-author")
    assert env.log.allow_post("inst-other")
    # Two live nodes: each allows its share.
    shared = MomentDeleteLog(
        env.repo,
        None,  # type: ignore[arg-type]
        env.ws,
        cluster=SimpleNamespace(live_node_count=lambda: 2),  # type: ignore[arg-type]
    )
    allowed = sum(shared.allow_post("inst-x") for _ in range(60))
    assert allowed == DELETE_CERT_POSTS_PER_MINUTE // 2


def _pushed_ids(env) -> list[str]:
    return [mid for frame in _pushed(env) for mid in frame]


async def test_a_flood_of_certificates_never_crowds_out_another_authors_delete(env):
    """Adversarial review F1: a followed author posts 200 certificates for
    made-up ids while the household is away; a real delete by another author
    must still arrive — even after the household is marked seen and
    reconnects."""
    await _follow(env, "u-author")
    await _follow(env, "u-other")
    await _seen(env, NOW - 10 * 86_400)
    flood_at = NOW - 86_400
    for i in range(200):
        await _add(env, "u-other", f"fake-{i:03d}", flood_at + 2 * i)
    await _add(env, "u-author", "m-real", flood_at + 500)
    await env.log.push_to("inst-follower", now=NOW)
    await _seen(env, NOW + 120)
    await env.log.push_to("inst-follower", now=NOW + 7200)
    pushed = _pushed_ids(env)
    flood = sum(1 for m in pushed if m.startswith("fake-"))
    real = pushed.count("m-real")
    assert (flood, real) == (200, 1)
    # Fair share: the real delete goes out in the first push's first frame.
    assert "m-real" in _pushed(env)[0]


async def test_a_backlog_past_one_push_is_never_lost_to_the_seen_mark(env):
    await _follow(env, "u-author")
    await _seen(env, NOW - 10 * 86_400)
    total = MAX_DELETE_CERTS_PER_PUSH + 30
    for i in range(total):
        await _add(env, "u-author", f"m{i:04d}", NOW - 86_400 + i)
    for k in range(10):
        await env.log.push_to("inst-follower", now=NOW + k)
        await _seen(env, NOW + k)
    assert sorted(_pushed_ids(env)) == [f"m{i:04d}" for i in range(total)]
    # Drained: nothing goes out twice, and no cursor stays behind.
    assert await env.log.push_to("inst-follower", now=NOW + 99) == 0
    assert await env.db.fetchall("SELECT * FROM gfs_moment_delete_cursor") == []


async def test_a_push_cut_short_resumes_where_it_stopped(env, monkeypatch):
    await _follow(env, "u-author")
    await _seen(env, NOW - 10 * 86_400)
    for i in range(DELETE_CERTS_PER_FRAME + 5):
        await _add(env, "u-author", f"m{i:04d}", NOW - 86_400 + i)

    sends = 0
    real_send = GfsWebSocketRegistry.send

    async def _drop_after_first(self, instance_id, frame):
        nonlocal sends
        sends += 1
        if sends > 1:
            return False  # the socket went away mid-push
        return await real_send(self, instance_id, frame)

    monkeypatch.setattr(GfsWebSocketRegistry, "send", _drop_after_first)
    assert await env.log.push_to("inst-follower", now=NOW) == DELETE_CERTS_PER_FRAME
    monkeypatch.setattr(GfsWebSocketRegistry, "send", real_send)
    await _seen(env, NOW + 3600)
    assert await env.log.push_to("inst-follower", now=NOW + 7200) == 5
    assert len(set(_pushed_ids(env))) == DELETE_CERTS_PER_FRAME + 5


async def test_one_push_shares_fairly_between_authors(env):
    await _follow(env, "u-author")
    await _follow(env, "u-other")
    for i in range(MAX_DELETE_CERTS_PER_PUSH):
        await _add(env, "u-other", f"fake-{i:04d}", NOW - 3600 + i)
    for i in range(3):
        await _add(env, "u-author", f"real-{i}", NOW - 1800 + i)
    sent = await env.log.push_to("inst-follower", now=NOW)
    pushed = _pushed_ids(env)
    assert sent == len(pushed)
    assert sum(1 for m in pushed if m.startswith("real-")) == 3
    assert sum(1 for m in pushed if m.startswith("fake-")) == (
        DELETE_CERTS_PER_AUTHOR_PER_PUSH
    )


async def test_the_drain_loop_keeps_going_while_the_socket_is_open(env, monkeypatch):
    monkeypatch.setattr(mdl, "DELETE_PUSH_PAUSE_S", 0)
    await _follow(env, "u-author", at=int(time.time()) - 86_400)
    now = int(time.time())
    total = DELETE_CERTS_PER_AUTHOR_PER_PUSH * 2 + 1
    for i in range(total):
        await _add(env, "u-author", f"m{i:04d}", now - 3600 + i)
    env.log.schedule_household_connected("inst-follower")
    # A second connect while the first drain runs asks it to go again, never
    # starts a second one alongside.
    env.log.schedule_household_connected("inst-follower")
    while env.log._tasks:
        await asyncio.gather(*list(env.log._tasks))
    assert sorted(_pushed_ids(env)) == [f"m{i:04d}" for i in range(total)]


async def test_prune_drops_cursors_untouched_for_thirty_days(env):
    await _follow(env, "u-author")
    await env.db.enqueue(
        "INSERT INTO gfs_moment_delete_cursor(instance_id, author_user_id,"
        " after_created_at, after_moment_id, updated_at)"
        " VALUES('inst-follower', 'u-author', 1, '', ?)",
        (NOW - DELETE_LOG_TTL_S - 1,),
    )
    await env.log.prune(NOW)
    assert await env.db.fetchall("SELECT * FROM gfs_moment_delete_cursor") == []


async def test_close_stops_a_drain_waiting_for_its_next_push(env, monkeypatch):
    monkeypatch.setattr(mdl, "DELETE_PUSH_PAUSE_S", 3600)
    await _follow(env, "u-author", at=int(time.time()) - 86_400)
    now = int(time.time())
    for i in range(DELETE_CERTS_PER_AUTHOR_PER_PUSH + 1):
        await _add(env, "u-author", f"m{i:04d}", now - 3600 + i)
    env.log.schedule_household_connected("inst-follower")
    for _ in range(200):
        if len(_pushed_ids(env)) == DELETE_CERTS_PER_AUTHOR_PER_PUSH:
            break
        await asyncio.sleep(0.01)
    await asyncio.wait_for(env.log.close(), 5)
    assert not env.log._tasks
    # The one left is still owed — and a stopped log starts no new drain.
    env.log.schedule_household_connected("inst-follower")
    assert not env.log._tasks
    assert len(_pushed_ids(env)) == DELETE_CERTS_PER_AUTHOR_PER_PUSH


async def test_a_cursor_with_nothing_left_past_it_is_closed(env):
    """Its certificates aged out (or the follow ended): the next push that
    finds nothing owed drops the cursor instead of leaving it behind."""
    await _follow(env, "u-author")
    await env.db.enqueue(
        "INSERT INTO gfs_moment_delete_cursor(instance_id, author_user_id,"
        " after_created_at, after_moment_id, updated_at)"
        " VALUES('inst-follower', 'u-author', ?, '', ?)",
        (NOW - DELETE_LOG_TTL_S - 10, NOW - 60),
    )
    await _add(env, "u-author", "m-old", NOW - DELETE_LOG_TTL_S - 5)
    assert await env.log.push_to("inst-follower", now=NOW) == 0
    assert await env.db.fetchall("SELECT * FROM gfs_moment_delete_cursor") == []
