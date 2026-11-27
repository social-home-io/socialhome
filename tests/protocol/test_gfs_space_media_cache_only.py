"""A GFS space's files reach a cache-only household through the GFS cache only.

Owner rule B (2026-11-27): in a GFS space, a v_97 household whose space's
host is v_97 and whose channel is stored, once the 14-day window closed
(``SpaceMediaGfsUploader.cache_only``), gets no ``SPACE_MEDIA_BLOB`` push
and sends no ``SPACE_MEDIA_WANTED`` — every file comes from the GFS cache
(pieces / ``sm1p``, v_83 on demand). Nothing may be lost: every path that
sends such a household an item with files describes them (``media_refs``).

Two halves, across ALL paths:

* **No push, no ask.** Every module that queues a space file push goes
  through :class:`~socialhome.services.space_media_sync_service
  .SpaceMediaSyncService`, whose one filter drops a cache-only target for
  live, catch-up and replay rows alike; only an answer to a household's own
  ``SPACE_MEDIA_WANTED`` passes (``asked=True``), and a household asks only
  for a file it holds no descriptor for (a v_96 author's). The scan pins
  that list: a new module queuing pushes fails here until it names how a
  cache-only household gets the same files.
* **Every file arrives.** Each listed sender names the test proving its
  items describe their files for a cache-only household.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from socialhome.db import AsyncDatabase
from socialhome.repositories.space_media_outbox_repo import (
    SqliteSpaceMediaOutboxRepo,
)
from socialhome.services.space_media_sync_service import SpaceMediaSyncService

pytestmark = pytest.mark.security

_ROOT = Path(__file__).resolve().parents[2]
_PKG = _ROOT / "socialhome"

#: Every module that queues a space file push → the test that proves a
#: cache-only household gets the same files described instead. EXACT: a
#: module that stops queuing pushes must be removed too.
_PUSH_SENDERS: dict[str, str] = {
    "socialhome/services/space_post_outbound.py": (
        "tests/services/test_space_post_outbound.py::"
        "test_a_gfs_space_comment_picture_rides_the_gfs_cache"
    ),
    "socialhome/services/calendar_service.py": (
        "tests/services/test_calendar_service.py::"
        "test_a_new_cover_carries_media_refs_for_every_household"
    ),
    "socialhome/services/gallery_federation_outbound.py": (
        "tests/services/test_gallery_federation_outbound.py::"
        "test_a_space_photo_carries_media_refs_for_every_household"
    ),
    "socialhome/services/bazaar_outbound.py": (
        "tests/services/test_bazaar_outbound.py::"
        "test_a_gfs_space_listing_photos_ride_the_gfs_cache"
    ),
    "socialhome/services/page_federation_outbound.py": (
        "tests/services/test_page_federation_outbound.py::"
        "test_an_open_sides_files_ship_and_are_described"
    ),
    "socialhome/services/page_proposal_forwarder.py": (
        "tests/services/test_page_proposal_forwarder.py::"
        "test_a_cache_only_host_gets_the_new_files_described_never_pushed"
    ),
    "socialhome/services/space_moderation_federation.py": (
        "tests/services/test_space_moderation_federation.py::"
        "test_a_submissions_files_reach_a_cache_only_reviewer_through_the_gfs"
    ),
    "socialhome/federation/sync/space/provider.py": (
        "tests/federation/sync/space/test_provider.py::"
        "test_a_cache_only_requester_gets_a_descriptor_for_every_record"
    ),
    "socialhome/federation/sync/space/resume.py": (
        "tests/federation/sync/space/test_resume.py::"
        "test_a_cache_only_household_gets_replayed_files_described"
    ),
    # Answers SPACE_MEDIA_WANTED (``asked=True``): a household asks only
    # for a file it holds no descriptor for (a v_96 author's).
    "socialhome/services/space_media_wanted.py": (
        "tests/services/test_space_media_wanted.py::"
        "test_a_gfs_space_asks_only_for_a_file_without_a_descriptor"
    ),
    # ``enqueue_for_post`` delegates to ``enqueue_for_blob`` (the one filter).
    "socialhome/services/space_media_sync_service.py": (
        "tests/services/test_space_media_sync_service.py::"
        "test_a_gfs_spaces_file_is_never_pushed_to_a_cache_reader"
    ),
}

_PUSH_CALLS = frozenset({"enqueue_for_post", "enqueue_for_blob"})


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PUSH_CALLS
    ]


def _scan() -> dict[str, list[ast.Call]]:
    out: dict[str, list[ast.Call]] = {}
    for path in sorted(_PKG.rglob("*.py")):
        calls = _calls(ast.parse(path.read_text(), filename=str(path)))
        if calls:
            out[path.relative_to(_ROOT).as_posix()] = calls
    return out


def test_every_module_queuing_a_space_file_push_is_listed():
    """A new sender of space file pushes names how a cache-only household
    of a GFS space gets the same files (described, from the GFS cache)."""
    assert set(_scan()) == set(_PUSH_SENDERS)


@pytest.mark.parametrize("module", sorted(_PUSH_SENDERS))
def test_each_sender_names_a_test_that_proves_its_files_arrive(module):
    """Every file still arrives: each sender's proof exists."""
    path, _sep, name = _PUSH_SENDERS[module].partition("::")
    assert f"def {name}(" in (_ROOT / path).read_text(), _PUSH_SENDERS[module]


def test_only_an_answer_to_a_households_own_ask_bypasses_the_filter():
    """``asked=True`` (the one way past the cache-only filter) only answers
    ``SPACE_MEDIA_WANTED``."""
    bypass = {
        module
        for module, calls in _scan().items()
        for call in calls
        if any(
            kw.arg == "asked"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)
            for kw in call.keywords
        )
    }
    assert bypass == {"socialhome/services/space_media_wanted.py"}


@pytest.fixture
async def db(tmp_dir):
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO spaces(id, name, owner_instance_id, owner_username,"
        " identity_public_key, space_type, join_mode) VALUES(?,?,?,?,?,?,?)",
        ("sp", "S", "host", "owner", "aa" * 32, "global", "open"),
    )
    for peer in ("cache", "paired"):
        await db.enqueue(
            "INSERT INTO space_instances(space_id, instance_id) VALUES(?,?)",
            ("sp", peer),
        )
    yield db
    await db.shutdown()


@pytest.mark.parametrize(
    ("entry", "kwargs", "passes"),
    [
        ("enqueue_for_post", {"post_id": "p-1"}, False),
        ("enqueue_for_blob", {"correlation_id": "p-1"}, False),
        ("enqueue_for_blob", {"correlation_id": "p-1", "push": True}, False),
        (
            "enqueue_for_blob",
            {"correlation_id": "p-1", "push": True, "asked": True},
            True,
        ),
    ],
    ids=["live post", "live item", "catch-up / replay", "answer to its own ask"],
)
async def test_no_push_row_is_queued_for_a_cache_only_household(
    db, tmp_path, entry, kwargs, passes
):
    """Live, catch-up and replay rows alike: the one filter drops a
    cache-only household and keeps every other target's push; only the
    answer to the household's own ask (a file it holds no descriptor for)
    is queued for it."""
    (tmp_path / "a.webp").write_bytes(b"x" * 16)
    outbox = SqliteSpaceMediaOutboxRepo(db)
    fed = AsyncMock()
    svc = SpaceMediaSyncService(outbox=outbox, federation=fed, media_dir=tmp_path)

    async def readers(space_id, filename, targets):
        return [t for t in targets if t != "cache"]

    svc.attach_cache_readers(readers)
    await getattr(svc, entry)(
        space_id="sp",
        target_instance_ids=["cache"] if passes else ["cache", "paired"],
        media_urls=["api/media/a.webp"],
        **kwargs,
    )
    targets = {r.target_instance_id for r in await outbox.list_for_correlation("p-1")}
    assert targets == ({"cache"} if passes else {"paired"})
