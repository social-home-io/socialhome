"""Release-blocker protocol tests: a periodic §25.6 sync streams only what
changed since the stream the household last confirmed — and falls back to
the whole window whenever that watermark cannot be trusted.

Two real households, each a full app over its own SQLite file:

* **H** — the host; its :class:`SpaceSyncService` streams (the real
  ``stream_initial`` — planning, watermark, keyset paging, chunking), and
  its federation service takes the requester's ``SPACE_SYNC_COMPLETE``;
* **C** — a member household whose receiver applies every chunk.

Only the content crypto is swapped for a transparent codec (the space
content key is not what this tests) so the chunks can be counted.

* the first periodic session has no watermark → the whole window;
* after it is confirmed, a periodic session streams exactly the rows that
  changed — an edit, a reaction, a poll vote, a moderator removal, a
  comment delete, a new post — and C converges;
* a quiet space streams nothing in either direction (no echo);
* a row changed WHILE a stream runs is not lost: it streams next time;
* a chunk that failed to apply, or never arrived, makes the requester
  report the stream unclean (``SPACE_SYNC_COMPLETE {clean: false}``), so the
  next periodic session re-streams it;
* an unconfirmed stream advances nothing, and a ``SPACE_SYNC_COMPLETE``
  from any household but the requester is no confirmation;
* no watermark (a dropped seat), an older / upgraded requester, "Sync now"
  and a failed chunk each fall back to the whole window;
* the requester echoes the snapshot of the last stream it applied cleanly
  (``have_seq`` in the BEGIN, stored in its own database): a requester
  rolled back to an older file snapshot re-streams the gap on the next
  periodic session, an inflated ``have_seq`` is clamped to the provider's
  watermark, and a BEGIN without one streams the whole window.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import orjson
import pytest

from socialhome.app import create_app
from socialhome.app_keys import (
    db_key,
    event_bus_key,
    federation_service_key,
    space_sync_receiver_key,
    space_sync_scheduler_key,
    space_sync_service_key,
)
from socialhome.config import Config
from socialhome.domain.events import SpaceSyncComplete
from socialhome.domain.federation import FederationEvent, FederationEventType
from socialhome.domain.post import Comment, CommentType, Post, PostType
from socialhome.federation.owner_bound_id import (
    SPACE_COMMENT_KIND,
    SPACE_POST_KIND,
    mint_owner_bound_id,
)
from socialhome.federation.sync.space.exporter import (
    SENTINEL_RESOURCE,
    ChunkBuilder,
)
from socialhome.federation.sync.space.watermark import parse_have_seq
from socialhome.federation.sync_rtc import SyncSessionRecord

pytestmark = pytest.mark.security

SPACE = "sp-inc"
HOST = "the-host"
MEMBER = "the-member"
AUTHOR = "u-anna"  # the host's user
CORA = "u-cora"  # C's user
_NOW = datetime.now(timezone.utc)

#: Resources streamed whole on every session (the roster, small state).
KEPT_FULL = {"bans", "members", "member_pictures"}


def _config(tmp_path, name: str) -> Config:
    d = tmp_path / name
    d.mkdir()
    return Config(
        data_dir=str(d),
        db_path=str(d / "sh.db"),
        media_path=str(d / "media"),
        apps_path=str(d / "apps"),
        mode="standalone",
        log_level="ERROR",
        db_write_batch_timeout_ms=1,
        platform_options=MappingProxyType(
            {"standalone": MappingProxyType({"external_url": "https://t.example"})},
        ),
    )


async def _seat(db, instance: str, user: str, *, version: int = 55) -> None:
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (SPACE, instance),
    )
    await db.enqueue(
        "INSERT INTO space_remote_members(space_id, instance_id, user_id, role)"
        " VALUES(?,?,?,'member')",
        (SPACE, instance, user),
    )
    await db.enqueue(
        "INSERT INTO remote_instances(id, display_name, remote_identity_pk,"
        " key_self_to_remote, key_remote_to_self, remote_inbox_url,"
        " local_inbox_id, status, source, proto_version, capabilities_seen_at)"
        " VALUES(?, ?, ?, '00', '00', ?, ?, 'confirmed', 'manual', ?, ?)",
        (
            instance,
            instance,
            "ab" * 32,
            f"https://{instance}/inbox/x",
            f"{instance}_local",
            version,
            _NOW.isoformat(),
        ),
    )


async def _seed(db, *, local: str, remote: tuple[str, str]) -> None:
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key) VALUES(?,?,?,?,?)",
        (SPACE, SPACE, HOST, "anna", "00" * 32),
    )
    await db.enqueue(
        "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
        (local, local, local),
    )
    await db.enqueue(
        "INSERT INTO space_members(space_id, user_id, role) VALUES(?,?,'member')",
        (SPACE, local),
    )
    await _seat(db, *remote)


class _PlainCrypto:
    """Transparent stand-in for the space content key (not under test)."""

    async def encrypt_chunk(self, *, space_id, sync_id, plaintext):
        return 0, base64.urlsafe_b64encode(plaintext).decode("ascii")

    async def decrypt_chunk(self, *, space_id, epoch, sync_id, ciphertext):
        return base64.urlsafe_b64decode(ciphertext)


class _Wire:
    """A DataChannel stand-in: collects the frames, runs ``during`` once
    after the first content chunk (a write racing the stream)."""

    def __init__(self, during=None) -> None:
        self.frames: list[dict] = []
        self._during = during

    async def send_chunk(self, data: bytes) -> None:
        self.frames.append(orjson.loads(data))
        if self._during is not None and self.frames[-1]["resource"] == "posts":
            during, self._during = self._during, None
            await during()

    def close(self) -> None:
        pass


@pytest.fixture
async def houses(aiohttp_client, tmp_path):
    h = create_app(_config(tmp_path, "h"))
    c = create_app(_config(tmp_path, "c"))
    for app in (h, c):
        await aiohttp_client(app)
    await _seed(h[db_key], local=AUTHOR, remote=(MEMBER, CORA))
    await _seed(c[db_key], local=CORA, remote=(HOST, AUTHOR))
    for app in (h, c):
        svc = app[space_sync_service_key]
        svc._builder = ChunkBuilder(
            encoder=svc._builder._encoder, crypto=_PlainCrypto()
        )
        app[space_sync_receiver_key]._crypto = _PlainCrypto()
        # The completion is delivered by ``_stream``; no wire to send it on.
        fed = app[federation_service_key]
        app[event_bus_key].unsubscribe(SpaceSyncComplete, fed._on_space_sync_landed)
    # Each household pins the other's real identity key, so the chunks'
    # signatures verify on the receiver exactly as on the wire.
    for app, peer, other in ((h, MEMBER, c), (c, HOST, h)):
        row = await other[db_key].fetchone(
            "SELECT identity_public_key FROM instance_identity"
        )
        await app[db_key].enqueue(
            "UPDATE remote_instances SET remote_identity_pk=? WHERE id=?",
            (row["identity_public_key"], peer),
        )
    return h, c


def _posts(app):
    return app[space_sync_service_key]._space_post_repo


def _polls(app):
    return app[space_sync_service_key]._exporters["polls"]._poll_repo


def _pid(owner: str) -> str:
    return mint_owner_bound_id(SPACE_POST_KIND, space_id=SPACE, owner_user_id=owner)


def _cid(owner: str) -> str:
    return mint_owner_bound_id(SPACE_COMMENT_KIND, space_id=SPACE, owner_user_id=owner)


async def _write_post(app, pid: str, author: str, text: str) -> None:
    post = Post(
        id=pid,
        author=author,
        type=PostType.TEXT,
        created_at=_NOW - timedelta(hours=1),
        content=text,
    )
    assert await _posts(app).save(SPACE, post) is not None


async def _stream(
    provider_app,
    to_app,
    *,
    provider: str,
    requester: str,
    mode: str = "incremental",
    complete: bool = True,
    during=None,
    confirm_from: str | None = None,
    drop: set[str] = frozenset(),  # type: ignore[assignment]
    have_seq: object = "from-requester",
) -> dict[str, list[dict]]:
    """One real provider session from ``provider_app`` to ``to_app``: every
    frame goes through ``to_app``'s receiver (signature, decode, persist) in
    wire order, the sentinel publishes ``SpaceSyncComplete`` with whether
    the stream applied cleanly, and — when ``complete`` — the requester's
    ``SPACE_SYNC_COMPLETE {clean}`` is delivered to the provider's federation
    handler. Returns the records per resource.

    ``have_seq`` — what the requester's BEGIN echoes: by default what its
    scheduler sends (the snapshot of the last stream it applied cleanly),
    else the given wire value (``None``: the field is absent)."""
    sync_id = uuid.uuid4().hex
    wire = _Wire(during)
    if have_seq == "from-requester":
        fields = await to_app[space_sync_scheduler_key].begin_fields(
            space_id=SPACE, peer_instance_id=provider, sync_mode=mode
        )
        have_seq = fields.get("have_seq")
    session = SyncSessionRecord(
        sync_id=sync_id,
        space_id=SPACE,
        requester_instance_id=requester,
        provider_instance_id=provider,
        sync_mode=mode,
        rtc=None,
        # What the provider's BEGIN handler makes of the wire value.
        have_seq=parse_have_seq(have_seq),
    )
    session.rtc = wire  # type: ignore[assignment]
    fed = provider_app[federation_service_key]
    fed._sync_manager._sessions[sync_id] = session
    await provider_app[space_sync_service_key].stream_initial(session)
    assert wire.frames and wire.frames[-1]["resource"] == SENTINEL_RESOURCE
    streamed: dict[str, list[dict]] = {}
    receiver = to_app[space_sync_receiver_key]
    landed: list[SpaceSyncComplete] = []

    async def _landed(event: SpaceSyncComplete) -> None:
        landed.append(event)

    to_app[event_bus_key].subscribe(SpaceSyncComplete, _landed)
    try:
        for frame in wire.frames:
            if frame["resource"] in drop:
                continue  # lost on the way: the receiver never sees it
            if frame["resource"] != SENTINEL_RESOURCE:
                records = orjson.loads(
                    base64.urlsafe_b64decode(frame["encrypted_payload"])
                )["records"]
                streamed.setdefault(frame["resource"], []).extend(records)
            await receiver.on_chunk(
                orjson.dumps(frame), from_instance=provider, expected_space_id=SPACE
            )
    finally:
        to_app[event_bus_key].unsubscribe(SpaceSyncComplete, _landed)
    [done] = landed
    if complete:
        await fed._handle_space_sync_complete(
            FederationEvent(
                msg_id=uuid.uuid4().hex,
                event_type=FederationEventType.SPACE_SYNC_COMPLETE,
                from_instance=confirm_from or requester,
                to_instance=provider,
                timestamp=_NOW.isoformat(),
                payload={"sync_id": sync_id, "space_id": SPACE, "clean": done.clean},
                space_id=SPACE,
            )
        )
    fed._sync_manager.close_session(sync_id)
    return streamed


def _ids(streamed, resource: str, key: str = "id") -> set[str]:
    return {r[key] for r in streamed.get(resource, [])}


def _content(streamed) -> dict[str, list[dict]]:
    """The covered resources' records — everything but the roster."""
    return {k: v for k, v in streamed.items() if k not in KEPT_FULL}


async def _h_to_c(h, c, **kw):
    return await _stream(h, c, provider=HOST, requester=MEMBER, **kw)


async def _c_to_h(h, c, **kw):
    return await _stream(c, h, provider=MEMBER, requester=HOST, **kw)


async def _post_row(app, pid: str) -> dict | None:
    row = await app[db_key].fetchone(
        "SELECT deleted, content FROM space_posts WHERE id=?", (pid,)
    )
    return dict(row) if row is not None else None


async def _baseline(h, c):
    """Ten posts (one with a comment, one with a poll) on H, streamed to C
    by a first periodic session — which, with no watermark, is full."""
    pids = [_pid(AUTHOR) for _ in range(10)]
    for i, pid in enumerate(pids):
        await _write_post(h, pid, AUTHOR, f"post {i}")
    comment = Comment(
        id=_cid(AUTHOR),
        post_id=pids[0],
        author=AUTHOR,
        type=CommentType.TEXT,
        created_at=_NOW - timedelta(minutes=30),
        content="a reply",
    )
    assert await _posts(h).add_comment(comment, space_id=SPACE)
    await _polls(h).create_poll(
        post_id=pids[1],
        question="Lunch?",
        closes_at=None,
        allow_multiple=False,
        options=[{"id": "opt-yes", "text": "yes"}],
    )
    first = await _h_to_c(h, c)
    assert _ids(first, "posts") == set(pids)
    assert _ids(first, "comments") == {comment.id}
    assert _ids(first, "polls", "post_id") == {pids[1]}
    return pids, comment


# ── Steady state follows the changes ──────────────────────────────────────


async def test_a_periodic_session_streams_only_what_changed_and_converges(houses):
    h, c = houses
    pids, comment = await _baseline(h, c)

    # Quiet: nothing covered streams; the roster still does.
    quiet = await _h_to_c(h, c)
    assert not any(_content(quiet).values()), _content(quiet)
    assert quiet.get("members")

    # Changes on H: an edit, a reaction, a poll vote, a moderator removal,
    # a comment delete, a new post.
    await _posts(h).edit(pids[2], "edited on H", space_id=SPACE)
    await _posts(h).add_reaction(pids[3], "🎉", CORA, space_id=SPACE)
    assert await _polls(h).cast_vote_in_space(
        space_id=SPACE, post_id=pids[1], option_id="opt-yes", voter_user_id=CORA
    )
    assert await _posts(h).soft_delete(pids[4], space_id=SPACE, moderated_by=AUTHOR)
    assert await _posts(h).soft_delete_comment(comment.id, space_id=SPACE)
    new = _pid(AUTHOR)
    await _write_post(h, new, AUTHOR, "brand new")

    changed = await _h_to_c(h, c)
    assert _ids(changed, "posts") == {pids[1], pids[2], pids[3], new}
    assert _ids(changed, "posts_deleted") == {pids[4]}
    assert _ids(changed, "comments_deleted") == {comment.id}
    assert _ids(changed, "polls", "post_id") == {pids[1]}
    [poll] = changed["polls"]
    assert [o["count"] for o in poll["options"]] == [1]
    assert not changed.get("comments")
    # C converged.
    assert (await _post_row(c, pids[2]))["content"] == "edited on H"
    assert (await _post_row(c, pids[4]))["deleted"] == 1
    assert (await _post_row(c, new))["content"] == "brand new"
    gone = await c[db_key].fetchone(
        "SELECT deleted FROM space_post_comments WHERE id=?", (comment.id,)
    )
    assert gone["deleted"] == 1

    # And quiet again.
    assert not any(_content(await _h_to_c(h, c)).values())


async def test_a_quiet_space_streams_nothing_either_way(houses):
    """No echo: a household re-applying rows it already holds stamps
    nothing, so what H streamed to C does not bounce back for ever."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    # C's first stream to H: full (no watermark) — H's posts, as C holds them.
    assert _ids(await _c_to_h(h, c), "posts") == set(pids)
    await _posts(h).edit(pids[0], "touched", space_id=SPACE)
    await _h_to_c(h, c)
    # C stored the edit after its last snapshot, so it relays it once …
    once = await _c_to_h(h, c)
    assert _ids(once, "posts") <= {pids[0]}
    # … H already holds it (no stamp), and from then on both are quiet.
    for _ in range(2):
        assert not any(_content(await _h_to_c(h, c)).values())
        assert not any(_content(await _c_to_h(h, c)).values())


async def test_a_full_restream_of_held_rows_stamps_nothing(houses):
    """The daily full pass (or "Sync now") re-applies every row C already
    holds. An upsert that changes nothing stamps nothing (migration 0086's
    ``IS NOT`` guard), so C's next periodic stream to H stays quiet instead
    of relaying the whole window back."""
    h, c = houses
    await _baseline(h, c)
    await _c_to_h(h, c)  # C's first stream to H (full): sets C's watermark
    assert not any(_content(await _c_to_h(h, c)).values())
    await _h_to_c(h, c, mode="initial")  # the whole window, re-applied on C
    assert not any(_content(await _c_to_h(h, c)).values())


async def test_a_row_changed_while_the_stream_runs_streams_next_time(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[5], "before", space_id=SPACE)

    async def _race() -> None:
        # Lands after the snapshot, while ``posts`` pages are being read.
        await _posts(h).edit(pids[6], "during the stream", space_id=SPACE)

    await _h_to_c(h, c, during=_race)
    after = await _h_to_c(h, c)
    assert pids[6] in _ids(after, "posts")
    assert (await _post_row(c, pids[6]))["content"] == "during the stream"


async def test_an_unconfirmed_stream_advances_nothing(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[7], "x", space_id=SPACE)
    lost = await _h_to_c(h, c, complete=False)
    assert _ids(lost, "posts") == {pids[7]}
    again = await _h_to_c(h, c)
    assert _ids(again, "posts") == {pids[7]}


class _FailOnce:
    """C's post repo, whose ``save`` of one post raises once (a persist that
    fails — an FK, a lock): the chunk carrying it does not apply."""

    def __init__(self, inner, post_id: str) -> None:
        self._inner = inner
        self._post_id = post_id

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def save(self, space_id, post):
        if post.id == self._post_id:
            self._post_id = ""
            raise RuntimeError("FOREIGN KEY constraint failed")
        return await self._inner.save(space_id, post)


async def test_a_chunk_that_failed_to_apply_streams_again_next_time(houses):
    """The requester reports the stream unclean, so the provider keeps its
    watermark and the next periodic session re-streams the row — within 30
    minutes, not at the next daily full pass."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[9], "lost once", space_id=SPACE)
    receiver = c[space_sync_receiver_key]
    real = receiver._space_post_repo
    receiver._space_post_repo = _FailOnce(real, pids[9])
    try:
        first = await _h_to_c(h, c)
    finally:
        receiver._space_post_repo = real
    assert _ids(first, "posts") == {pids[9]}
    assert (await _post_row(c, pids[9]))["content"] != "lost once"
    again = await _h_to_c(h, c)
    assert _ids(again, "posts") == {pids[9]}
    assert (await _post_row(c, pids[9]))["content"] == "lost once"
    assert not any(_content(await _h_to_c(h, c)).values())


async def test_a_chunk_lost_on_the_way_streams_again_next_time(houses):
    """A chunk that never reaches the receiver (a relay that could not open
    it) — the sentinel's signed count tells the requester."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[9], "dropped", space_id=SPACE)
    first = await _h_to_c(h, c, drop={"posts"})
    assert pids[9] not in _ids(first, "posts")
    again = await _h_to_c(h, c)
    assert _ids(again, "posts") == {pids[9]}
    assert (await _post_row(c, pids[9]))["content"] == "dropped"


async def test_only_the_requester_can_confirm_its_stream(houses):
    """A completion from another household (one that learned the sync id)
    never advances the requester's watermark — it could otherwise make the
    provider skip rows the requester never received."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[8], "y", space_id=SPACE)
    await _h_to_c(h, c, confirm_from="some-other-house")
    assert _ids(await _h_to_c(h, c), "posts") == {pids[8]}


# ── Fail safe: the whole window ───────────────────────────────────────────


async def test_a_dropped_seat_loses_the_watermark(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    db = h[db_key]
    await db.enqueue(
        "DELETE FROM space_instances WHERE space_id=? AND instance_id=?",
        (SPACE, MEMBER),
    )
    await db.enqueue(
        "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
        (SPACE, MEMBER),
    )
    assert _ids(await _h_to_c(h, c), "posts") == set(pids)


async def test_an_upgraded_requester_gets_one_full_stream(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await h[db_key].enqueue(
        "UPDATE remote_instances SET proto_version=56 WHERE id=?", (MEMBER,)
    )
    assert _ids(await _h_to_c(h, c), "posts") == set(pids)
    assert not any(_content(await _h_to_c(h, c)).values())


async def test_an_older_requester_that_never_confirms_always_gets_everything(
    houses,
):
    h, c = houses
    pids = [_pid(AUTHOR) for _ in range(3)]
    for i, pid in enumerate(pids):
        await _write_post(h, pid, AUTHOR, f"post {i}")
    for _ in range(2):
        streamed = await _h_to_c(h, c, complete=False)
        assert _ids(streamed, "posts") == set(pids)


async def test_sync_now_streams_the_whole_window(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    assert _ids(await _h_to_c(h, c, mode="initial"), "posts") == set(pids)


async def test_a_stale_full_stream_forces_the_daily_full_pass(houses):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await h[db_key].enqueue(
        "UPDATE space_instances SET synced_full_at=? WHERE instance_id=?",
        ((_NOW - timedelta(days=2)).isoformat(), MEMBER),
    )
    assert _ids(await _h_to_c(h, c), "posts") == set(pids)
    assert not any(_content(await _h_to_c(h, c)).values())


# ── The requester's echo: a rolled-back requester does not miss rows ─────


async def _applied_seq(app, provider: str) -> int | None:
    row = await app[db_key].fetchone(
        "SELECT applied_seq FROM space_instances WHERE space_id=? AND instance_id=?",
        (SPACE, provider),
    )
    return None if row is None else row["applied_seq"]


async def _watermark(app, requester: str) -> int | None:
    row = await app[db_key].fetchone(
        "SELECT synced_seq FROM space_instances WHERE space_id=? AND instance_id=?",
        (SPACE, requester),
    )
    return None if row is None else row["synced_seq"]


async def test_a_requester_rolled_back_to_an_older_snapshot_heals_next_session(
    houses,
):
    """C is restored from an older file snapshot under the same identity
    (e.g. a Home Assistant backup): the posts it applied after the backup
    are gone, and so is the ``applied_seq`` that recorded them — it rolled
    back with C's own database. H's watermark still claims C holds them, but
    the next periodic session streams since ``min(watermark, have_seq)``, so
    the gap re-streams without "Sync now" and without the daily full pass."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    before_backup = await _applied_seq(c, HOST)
    assert before_backup is not None
    assert before_backup == await _watermark(h, MEMBER)

    # After the "backup": two new posts and an edit reach C cleanly.
    late = [_pid(AUTHOR) for _ in range(2)]
    for i, pid in enumerate(late):
        await _write_post(h, pid, AUTHOR, f"late {i}")
    await _posts(h).edit(pids[3], "edited after the backup", space_id=SPACE)
    applied = await _h_to_c(h, c)
    assert _ids(applied, "posts") == {*late, pids[3]}
    assert await _applied_seq(c, HOST) == await _watermark(h, MEMBER)
    assert await _applied_seq(c, HOST) > before_backup

    # The restore: C's database is back at the backup — the late rows, the
    # edit and the applied stamp are gone; H's watermark is untouched.
    cdb = c[db_key]
    for pid in late:
        await cdb.enqueue("DELETE FROM space_posts WHERE id=?", (pid,))
    await cdb.enqueue("UPDATE space_posts SET content='post 3' WHERE id=?", (pids[3],))
    await cdb.enqueue(
        "UPDATE space_instances SET applied_seq=? WHERE space_id=? AND instance_id=?",
        (before_backup, SPACE, HOST),
    )

    # H is quiet — its watermark alone would stream nothing — yet the gap
    # re-streams on the very next periodic session.
    healed = await _h_to_c(h, c)
    assert _ids(healed, "posts") == {*late, pids[3]}
    for i, pid in enumerate(late):
        assert (await _post_row(c, pid))["content"] == f"late {i}"
    assert (await _post_row(c, pids[3]))["content"] == "edited after the backup"
    # Converged: quiet again, and the echo caught up with the watermark.
    assert not any(_content(await _h_to_c(h, c)).values())
    assert await _applied_seq(c, HOST) == await _watermark(h, MEMBER)


async def test_an_inflated_have_seq_is_clamped_to_the_watermark(houses):
    """A ``have_seq`` above what the provider confirmed (forged, or from a
    provider that was itself rolled back) never lets the provider skip rows
    its own watermark says the household has not confirmed."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    await _posts(h).edit(pids[5], "after the watermark", space_id=SPACE)
    forged = await _h_to_c(h, c, have_seq=10**12)
    assert _ids(forged, "posts") == {pids[5]}
    assert (await _post_row(c, pids[5]))["content"] == "after the watermark"
    # C records the provider's signed snapshot, never the forged value.
    assert await _applied_seq(c, HOST) == await _watermark(h, MEMBER)
    assert await _applied_seq(c, HOST) < 10**12


@pytest.mark.parametrize("wire", [None, -1, "7", True, 1.5])
async def test_a_begin_without_a_valid_have_seq_streams_the_whole_window(houses, wire):
    h, c = houses
    pids, _comment = await _baseline(h, c)
    assert _ids(await _h_to_c(h, c, have_seq=wire), "posts") == set(pids)


async def test_an_unclean_stream_records_no_applied_seq(houses):
    """Only a stream the requester reports clean moves its echo."""
    h, c = houses
    pids, _comment = await _baseline(h, c)
    before = await _applied_seq(c, HOST)
    await _posts(h).edit(pids[9], "dropped", space_id=SPACE)
    await _h_to_c(h, c, drop={"posts"})
    assert await _applied_seq(c, HOST) == before
