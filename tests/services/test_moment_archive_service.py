"""Tests for socialhome.services.moment_archive_service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from socialhome.domain.moment import MOMENT_ARCHIVE_DEFAULT_CAP_BYTES, Moment
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.media_reference_repo import SqliteMediaReferenceRepo
from socialhome.repositories.moment_repo import SqliteMomentRepo
from socialhome.services.moment_archive_service import (
    ARCHIVE_CAP_KEY,
    MomentArchiveService,
)


def _moment(
    id: str,
    *,
    author: str = "u-me",
    days_ago: int,
    media: str | None = None,
    media_type: str = "image",
    via: str = "self",
) -> Moment:
    created = datetime.now(UTC) - timedelta(days=days_ago)
    return Moment(
        id=id,
        author_user_id=author,
        content=f"text of {id}",
        media_url=f"api/media/{media}" if media else None,
        media_type=media_type if media else None,
        duration_ms=None,
        parent_moment_id=None,
        origin_instance_id="here",
        created_at=created.isoformat(),
        expires_at=(created + timedelta(days=7)).isoformat(),
        received_via=via,
    )


@pytest.fixture
async def stack(db, tmp_dir):
    media_dir = tmp_dir / "media"
    media_dir.mkdir()
    moments = SqliteMomentRepo(db)
    config = SqliteInstanceConfigRepo(db)
    svc = MomentArchiveService(
        moments,
        config,
        media_dir=media_dir,
        media_refs=SqliteMediaReferenceRepo(db),
    )

    class S:
        pass

    s = S()
    s.svc, s.moments, s.config, s.media_dir = svc, moments, config, media_dir

    async def add(m: Moment, size: int = 0) -> None:
        if m.media_url:
            (media_dir / m.media_url.rsplit("/", 1)[-1]).write_bytes(b"x" * size)
        await moments.save(m)

    s.add = add
    return s


async def test_the_cap_defaults_to_two_gib(stack):
    assert await stack.svc.cap_bytes() == MOMENT_ARCHIVE_DEFAULT_CAP_BYTES
    await stack.config.set(ARCHIVE_CAP_KEY, "not a number")
    assert await stack.svc.cap_bytes() == MOMENT_ARCHIVE_DEFAULT_CAP_BYTES


async def test_only_an_admin_sets_the_cap(stack):
    with pytest.raises(PermissionError):
        await stack.svc.set_cap_bytes(10, actor_is_admin=False)
    for bad in (-1, True, "5", 1.5):
        with pytest.raises(ValueError):
            await stack.svc.set_cap_bytes(bad, actor_is_admin=True)
    assert await stack.svc.set_cap_bytes(0, actor_is_admin=True) == 0
    assert await stack.svc.cap_bytes() == 0


async def test_usage_counts_your_files_and_video_posters(stack):
    await stack.add(_moment("m1", days_ago=10, media="a.webp"), 300)
    await stack.add(_moment("m2", days_ago=1, media="v.webm", media_type="video"), 1000)
    (stack.media_dir / "v.webp").write_bytes(b"p" * 50)  # the video's poster
    await stack.add(_moment("m3", days_ago=1, media="gone.webp"))
    (stack.media_dir / "gone.webp").unlink()  # a missing file counts 0
    await stack.add(_moment("m4", author="u-bea", days_ago=1, media="b.webp"), 999)
    usage = await stack.svc.usage_for("u-me")
    assert (usage.used_bytes, usage.cap_bytes) == (
        1350,
        MOMENT_ARCHIVE_DEFAULT_CAP_BYTES,
    )


async def test_past_the_cap_the_oldest_archived_pictures_go_first(stack):
    await stack.svc.set_cap_bytes(1000, actor_is_admin=True)
    await stack.add(_moment("m-oldest", days_ago=30, media="1.webp"), 600)
    await stack.add(_moment("m-older", days_ago=20, media="2.webp"), 600)
    await stack.add(_moment("m-old", days_ago=10, media="3.webp"), 300)
    await stack.add(_moment("m-live", days_ago=1, media="4.webp"), 300)
    await stack.add(_moment("m-bea", author="u-bea", days_ago=30, media="b.webp"), 900)

    assert await stack.svc.enforce_caps() == 2
    # 1800 → 1200 → 600 bytes: the two oldest lost their picture, their
    # text stays; the newer ones and Bea (under the cap) keep theirs.
    for mid, name in (("m-oldest", "1.webp"), ("m-older", "2.webp")):
        m = await stack.moments.get(mid)
        assert (m.media_url, m.content) == (None, f"text of {mid}")
        assert not (stack.media_dir / name).exists()
    for name in ("3.webp", "4.webp", "b.webp"):
        assert (stack.media_dir / name).exists()
    assert (await stack.svc.usage_for("u-me")).used_bytes == 600
    assert await stack.svc.enforce_caps() == 0


async def test_a_moment_others_still_see_keeps_its_picture(stack):
    """Only archived (expired) moments lose media; a live one stays even
    when a person is over the cap on live moments alone."""
    await stack.svc.set_cap_bytes(100, actor_is_admin=True)
    await stack.add(_moment("m-live", days_ago=1, media="l.webp"), 500)
    assert await stack.svc.enforce_caps() == 0
    assert (stack.media_dir / "l.webp").exists()


async def test_work_per_tick_is_bounded(stack):
    await stack.svc.set_cap_bytes(1, actor_is_admin=True)
    for i in range(3):
        await stack.add(_moment(f"m{i}", days_ago=20 + i, media=f"{i}.webp"), 10)
    assert await stack.svc.enforce_caps(max_drops=2) == 2
    assert await stack.svc.enforce_caps(max_drops=2) == 1


async def test_no_limit_drops_nothing(stack):
    await stack.svc.set_cap_bytes(0, actor_is_admin=True)
    await stack.add(_moment("m", days_ago=30, media="m.webp"), 10_000)
    assert await stack.svc.enforce_caps() == 0
    assert (stack.media_dir / "m.webp").exists()


async def test_a_file_another_row_uses_is_kept(stack):
    """The file goes through the media-reference check: a file another
    row still names stays on disk."""
    await stack.svc.set_cap_bytes(1, actor_is_admin=True)
    await stack.add(_moment("m-old", days_ago=30, media="shared.webp"), 10)
    await stack.moments.save(_moment("m-new", days_ago=1, media="shared.webp"))
    assert await stack.svc.enforce_caps() == 1
    assert (await stack.moments.get("m-old")).media_url is None
    assert (stack.media_dir / "shared.webp").exists()


async def test_without_a_media_dir_nothing_is_measured(db):
    svc = MomentArchiveService(
        SqliteMomentRepo(db), SqliteInstanceConfigRepo(db), media_dir=None
    )
    assert (await svc.usage_for("u-me")).used_bytes == 0
    assert await svc.enforce_caps() == 0


# ── Saved moments count toward the saver's cap (decision 6b) ─────────────


async def _save(db, user_id: str, moment_id: str, *, days_ago: int) -> None:
    await db.enqueue(
        "INSERT OR IGNORE INTO users(user_id, username, display_name) VALUES(?, ?, ?)",
        (user_id, user_id, user_id),
    )
    await db.enqueue(
        "INSERT INTO moment_saves(user_id, moment_id, saved_at) VALUES(?, ?, ?)",
        (
            user_id,
            moment_id,
            (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(),
        ),
    )


async def test_usage_counts_saved_pictures_of_others(db, stack):
    await stack.add(_moment("m-own", days_ago=1, media="o.webp"), 100)
    await stack.add(
        _moment("m-peer", author="u-peer", days_ago=1, media="p.webp", via="household"),
        40,
    )
    # A housemate's moment counts toward its author's archive, not mine.
    await stack.add(_moment("m-mia", author="u-mia", days_ago=1, media="h.webp"), 7)
    await _save(db, "u-me", "m-peer", days_ago=0)
    await _save(db, "u-me", "m-mia", days_ago=0)
    assert (await stack.svc.usage_for("u-me")).used_bytes == 140
    assert (await stack.svc.usage_for("u-mia")).used_bytes == 7


async def test_past_the_cap_oldest_kept_goes_first_saved_or_own(db, stack):
    """One oldest-first order per person: an own moment by when it was
    written, a saved one by when it was saved. A saved picture loses its
    file like an own one (the text stays) — unless someone else saved it
    too, then it stays for them."""
    await stack.svc.set_cap_bytes(1000, actor_is_admin=True)
    await stack.add(_moment("m-own-old", days_ago=30, media="1.webp"), 400)
    await stack.add(
        _moment(
            "m-saved", author="u-peer", days_ago=25, media="2.webp", via="household"
        ),
        400,
    )
    await stack.add(
        _moment(
            "m-shared", author="u-peer", days_ago=40, media="3.webp", via="household"
        ),
        400,
    )
    await stack.add(_moment("m-own-new", days_ago=10, media="4.webp"), 400)
    await _save(db, "u-me", "m-shared", days_ago=35)
    await _save(db, "u-bob", "m-shared", days_ago=34)
    await _save(db, "u-me", "m-saved", days_ago=20)
    # u-me: 1600 bytes over a 1000 cap. Oldest kept: m-shared (35 d, but
    # Bob keeps it too → skipped), m-own-old (30 d), m-saved (20 d).
    assert await stack.svc.enforce_caps() == 2
    assert (await stack.moments.get("m-own-old")).media_url is None
    saved = await stack.moments.get("m-saved")
    assert (saved.media_url, saved.content) == (None, "text of m-saved")
    assert not (stack.media_dir / "2.webp").exists()
    assert (await stack.moments.get("m-shared")).media_url == "api/media/3.webp"
    assert (await stack.moments.get("m-own-new")).media_url == "api/media/4.webp"
