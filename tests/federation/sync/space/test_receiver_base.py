"""``receiver_base``: the slots and cross-resource helpers of the space-sync
receiver — the v_80 media names and v_85 content hashes every resource
takes, who counts as the host, and the shared module helpers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from socialhome.domain.federation import FederationEventType
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.domain.post import Post, PostType
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.federation.sync.space.receiver_base import (
    VERIFIED_ROW,
    _bound_here,
    _log_sync_refusal,
    _log_tombstone_refusals,
    _parse_iso,
    _sync_event,
)
from tests.federation.sync.space.receiver_fakes import (
    _SYNC_REFS,
    _GfsMediaSink,
    _here,
    _PostRepoStub,
    _send,
)


class _SavingPostRepo(_PostRepoStub):
    """Reads back what the receiver stored (the base stub reads only the
    pre-held posts)."""

    async def get(self, post_id):
        for space_id, post in reversed(self._c.posts):
            if post.id == post_id:
                return space_id, post
        return await super().get(post_id)


async def test_streamed_post_media_refs_are_kept(setup):
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-m",
                "author": "u-1",
                "type": "image",
                "content": "pic",
                "image_urls": ["api/media/a.webp"],
                "created_at": "2026-04-18T00:00:00+00:00",
                "media_refs": _SYNC_REFS,
            },
            {
                "id": "p-n",
                "author": "u-1",
                "type": "text",
                "content": "no pictures",
                "created_at": "2026-04-18T00:00:00+00:00",
            },
        ],
    )
    assert len(c.posts) == 2
    # Bound to the record's item and the household that streamed it.
    assert sink.received == [
        ("sp-1", _SYNC_REFS, ["api/media/a.webp"], "p-m", "peer-a")
    ]
    # #1309 re-review 1: the stored post shows the reader's own name.
    stored = {post.id: post for _s, post in c.posts}
    assert stored["p-m"].image_urls == ("api/media/sg-a.webp",)
    assert sink.kept == [{"a.webp": "sg-a.webp"}]


async def test_streamed_records_without_a_fetcher_store_nothing(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-m",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp"],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert len(c.posts) == 1


async def test_media_refs_of_a_record_persist_refused_are_not_kept(setup):
    """#1309 review I: only records ``_persist`` accepted keep their
    descriptor — a post deleted here (never re-saved) keeps none."""
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)
    c.held_posts["p-gone"] = Post(
        id="p-gone",
        author="u-1",
        type=PostType.TEXT,
        content="x",
        created_at=None,
        deleted=True,
    )
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-gone",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp"],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert c.posts == []
    assert sink.kept == []


async def test_media_refs_are_checked_against_the_stored_row(setup):
    """The allowed files are the STORED post's (what the household shows),
    never the record's own claim: a reserved name the record lists is not
    in the stored post, so it is not offered."""
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-r",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/gm-moment.webp", "api/media/a.webp"],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert sink.received == [
        ("sp-1", _SYNC_REFS, ["api/media/a.webp"], "p-r", "peer-a")
    ]


async def test_a_non_host_stream_cannot_attach_refs_to_a_held_post(setup, monkeypatch):
    """#1309 re-review 2: a member household streaming a post this
    household already holds (A's) can't attach ITS descriptor to it — the
    post's files keep what they are here. Only a record that creates the
    item here, or the host's stream, brings a descriptor."""
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)
    c.held_posts["p-a"] = Post(
        id="p-a",
        author="u-1",
        type=PostType.IMAGE,
        content="A's picture",
        created_at=None,
        image_urls=("api/media/a.webp",),
    )

    async def _not_host(self, space_id, provider):
        return False

    monkeypatch.setattr(type(r), "_is_host", _not_host)
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-a",
                "author": "u-1",
                "type": "image",
                "content": "A's picture",
                "image_urls": ["api/media/a.webp"],
                "media_refs": _SYNC_REFS,
            }
        ],
    )
    assert sink.received == []
    assert sink.kept == []
    (_space, stored) = c.posts[-1]
    assert stored.image_urls == ("api/media/a.webp",)


def _hashed(**names: str) -> dict:
    return {
        "files": {n.replace("_", "."): {"h": h} for n, h in names.items()},
        "files_suite": "sha256",
    }


def _content(name: str, h: str, space_id: str = "sp-1") -> str:
    return f"api/media/{space_content_media_name(space_id, h, name)}"


async def test_the_hosts_hashed_records_take_content_names(setup):
    r, c, kp = setup
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-h",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp", "api/media/b.webp"],
                **_hashed(a_webp="a" * 64),
            }
        ],
    )
    _sid, post = c.posts[0]
    # A hashed file takes its content name; one the map misses stays legacy.
    assert post.image_urls == (_content("a.webp", "a" * 64), _here("b.webp"))


async def test_a_hashed_record_with_an_unknown_suite_stays_legacy(setup):
    r, c, kp = setup
    files = _hashed(a_webp="a" * 64)
    files["files_suite"] = "md5"
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-u",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp"],
                **files,
            }
        ],
    )
    _sid, post = c.posts[0]
    assert post.image_urls == (_here("a.webp"),)


async def test_the_hosts_hash_map_reaches_the_gfs_namer(setup):
    """A hashed file the host's record also describes keeps its one content
    name: the GFS namer gets the same map the direct names took."""
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-h",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp"],
                "media_refs": _SYNC_REFS,
                **_hashed(a_webp="a" * 64),
            }
        ],
    )
    assert {n: fh.h for n, fh in sink.hashes["p-h"].items()} == {"a.webp": "a" * 64}


async def test_a_non_host_providers_hash_map_never_reaches_the_gfs_namer(
    setup, monkeypatch
):
    r, c, kp = setup
    r._space_post_repo = _SavingPostRepo(c)

    async def _not_host(self, space_id, provider):
        return False

    monkeypatch.setattr(type(r), "_is_host", _not_host)
    sink = _GfsMediaSink()
    r.attach_gfs_media(sink)
    await _send(
        r,
        kp,
        "posts",
        [
            {
                "id": "p-n",
                "author": "u-1",
                "type": "image",
                "image_urls": ["api/media/a.webp"],
                "media_refs": _SYNC_REFS,
                **_hashed(a_webp="a" * 64),
            }
        ],
    )
    assert "p-n" in sink.hashes
    assert sink.hashes["p-n"] is None


# ─── Direct helpers ──────────────────────────────────────────────────


def test_parse_iso_reads_a_stamp_and_falls_back_to_now():
    assert _parse_iso("2026-01-02T03:04:05+00:00") == datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=UTC
    )
    before = datetime.now(UTC)
    for bad in (None, "", "not a date", 42):
        assert _parse_iso(bad) >= before


def test_sync_event_names_the_provider_and_the_space():
    event = _sync_event("sp-1", "posts", "peer-a")
    assert event.event_type is FederationEventType.SPACE_SYNC_CHUNK
    assert event.from_instance == "peer-a"
    assert event.space_id == "sp-1"
    assert event.msg_id == "sync:sp-1:posts"


def test_bound_here_only_for_an_id_bound_to_that_owner_and_space():
    rid = mint_owner_bound_id(SPACE_POST_KIND, space_id="sp-1", owner_user_id="u-1")
    assert _bound_here(SPACE_POST_KIND, rid, "sp-1", "u-1")
    assert not _bound_here(SPACE_POST_KIND, rid, "sp-2", "u-1")
    assert not _bound_here(SPACE_POST_KIND, rid, "sp-1", "u-2")
    assert not _bound_here(SPACE_POST_KIND, "legacy-id", "sp-1", "u-1")


async def test_only_the_owner_is_the_host_without_an_authorship_binder(setup):
    r, _c, _kp = setup
    owned = SimpleNamespace(owner_instance_id="peer-a")
    assert await r._acts_as_host(owned, "peer-a")
    assert not await r._acts_as_host(owned, "peer-b")
    assert not await r._acts_as_host(owned, "")
    assert not await r._acts_as_host(None, "peer-a")
    assert await r._is_host("sp-1", "peer-a")


async def test_a_verified_row_passes_as_the_hosts_for_its_space_and_deliverer_only(
    setup,
):
    """Sync pages Phase 3: while one row checked against the signed page is
    applied, its deliverer passes where the host passes — for that space and
    that deliverer, and never outside the context."""
    r, _c, _kp = setup
    space = SimpleNamespace(id="sp-1", owner_instance_id="peer-a")
    other = SimpleNamespace(id="sp-2", owner_instance_id="peer-a")
    assert not await r._acts_as_host(space, "peer-b")
    token = VERIFIED_ROW.set(("sp-1", "peer-b"))
    try:
        assert await r._acts_as_host(space, "peer-b")
        assert not await r._acts_as_host(space, "peer-c")
        assert not await r._acts_as_host(other, "peer-b")
    finally:
        VERIFIED_ROW.reset(token)
    assert not await r._acts_as_host(space, "peer-b")


async def test_a_vetted_actor_from_a_member_without_a_binder_is_nobody(setup):
    r, _c, _kp = setup
    record = {"actor_user_id": "u-mod"}
    assert await r._vetted_actor(record, "sp-1", "peer-b", True) == "u-mod"
    assert await r._vetted_actor(record, "sp-1", "peer-b", False) == ""
    assert await r._vetted_actor({}, "sp-1", "peer-b", False) == ""


def test_refusals_are_logged_once_per_chunk(caplog):
    with caplog.at_level(logging.INFO, logger="socialhome"):
        _log_tombstone_refusals("post", "peer-a", "sp-1", ["p-1", "p-2"], 3)
        _log_sync_refusal("page", "pg-1", "sp-1")
    assert "2 post tombstone(s) from peer-a for sp-1" in caplog.text
    assert "3 post tombstone(s)" in caplog.text
    assert "page pg-1 is not writable in space sp-1" in caplog.text
