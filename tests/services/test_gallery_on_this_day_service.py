"""Tests for GalleryOnThisDayService — the "On this day" card (D14)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import orjson
import pytest

from socialhome.config import Config
from socialhome.crypto import derive_instance_id, generate_identity_keypair
from socialhome.db.database import AsyncDatabase
from socialhome.domain.gallery import (
    ON_THIS_DAY_MAX_EXCLUDED,
    GalleryItem,
    OnThisDaySettings,
)
from socialhome.infrastructure.event_bus import EventBus
from socialhome.repositories.gallery_repo import SqliteGalleryRepo
from socialhome.repositories.instance_config_repo import SqliteInstanceConfigRepo
from socialhome.repositories.space_repo import SqliteSpaceRepo
from socialhome.repositories.user_repo import SqliteUserRepo
from socialhome.services.gallery_on_this_day_service import (
    GalleryOnThisDayService,
    month_days,
    resolve_today,
)
from socialhome.services.gallery_service import GalleryNotFoundError, GalleryService
from socialhome.services.user_preferences import GALLERY_ON_THIS_DAY_KEY

TODAY = date(2026, 11, 1)


class Env:
    def __init__(self, db, repo, users, gallery, svc) -> None:
        self.db = db
        self.repo = repo
        self.users = users
        self.gallery = gallery
        self.svc = svc

    async def photo(
        self, item_id: str, album_id: str, taken: str, *, by: str = "a-id"
    ) -> None:
        await self.repo.create_item(
            GalleryItem(
                id=item_id,
                album_id=album_id,
                uploaded_by=by,
                item_type="photo",
                url=f"api/media/{item_id}.webp",
                thumbnail_url=f"api/media/{item_id}-t.webp",
                width=10,
                height=10,
                taken_at=taken,
                created_at="2026-10-01T10:00:00+00:00",
            )
        )


@pytest.fixture
async def env(tmp_dir):
    kp = generate_identity_keypair()
    db = AsyncDatabase(tmp_dir / "t.db", batch_timeout_ms=10)
    await db.startup()
    await db.enqueue(
        "INSERT INTO instance_identity(instance_id, identity_private_key,"
        " identity_public_key, routing_secret) VALUES(?,?,?,?)",
        (
            derive_instance_id(kp.public_key),
            kp.private_key.hex(),
            kp.public_key.hex(),
            "aa" * 32,
        ),
    )
    for username, uid in (("alice", "a-id"), ("bob", "b-id")):
        await db.enqueue(
            "INSERT INTO users(username, user_id, display_name) VALUES(?,?,?)",
            (username, uid, username.title()),
        )
    cfg = Config(
        data_dir=str(tmp_dir),
        db_path=str(tmp_dir / "t.db"),
        media_path=str(tmp_dir / "media"),
        mode="standalone",
    )
    repo = SqliteGalleryRepo(db)
    users = SqliteUserRepo(db)
    gallery = GalleryService(
        repo,
        SqliteSpaceRepo(db),
        EventBus(),
        cfg,
        user_repo=users,
        instance_config=SqliteInstanceConfigRepo(db),
    )
    yield Env(db, repo, users, gallery, GalleryOnThisDayService(repo, users, gallery))
    await db.shutdown()


# ─── The viewer's day ────────────────────────────────────────────────────


def test_resolve_today_takes_the_viewers_day_within_one_of_utc():
    now = datetime(2026, 11, 1, 23, 30, tzinfo=UTC)
    assert resolve_today(None, now=now) == date(2026, 11, 1)
    assert resolve_today("2026-11-02", now=now) == date(2026, 11, 2)  # Tokyo
    assert resolve_today("2026-10-31", now=now) == date(2026, 10, 31)
    for bad in ("2026-11-03", "2025-11-01", "1.11.2026", "x"):
        with pytest.raises(ValueError):
            resolve_today(bad, now=now)


def test_month_days_brings_back_a_leap_day_on_the_28th():
    assert month_days(date(2026, 11, 1)) == ("11-01",)
    assert month_days(date(2027, 2, 28)) == ("02-28", "02-29")
    assert month_days(date(2028, 2, 28)) == ("02-28",)  # 2028 has a 29th
    assert month_days(date(2028, 2, 29)) == ("02-29",)


# ─── The card ────────────────────────────────────────────────────────────


async def test_the_card_is_one_album_of_one_earlier_year(env):
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="Autumn break"
    )
    for n in range(5):
        await env.photo(f"p-{n}", album.id, "2024-11-01")
    await env.photo("later", album.id, "2026-11-01")
    card = await env.svc.card(user_id="b-id", today=TODAY)
    assert card is not None
    assert card.album.id == album.id
    assert card.album.name == "Autumn break"
    assert (card.date, card.years_ago, card.count) == ("2024-11-01", 2, 5)
    assert [i.id for i in card.items] == ["p-0", "p-1", "p-2"]


async def test_no_card_without_a_memory_or_from_a_private_album(env):
    assert await env.svc.card(user_id="a-id", today=TODAY) is None
    secret = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="Gifts"
    )
    await env.repo.set_album_audience(secret.id, "chosen")
    await env.photo("g-1", secret.id, "2025-11-01")
    # Not even for its owner: private albums are never used (D7).
    assert await env.svc.card(user_id="a-id", today=TODAY) is None


async def test_turned_off_or_hidden_today_means_no_card(env):
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="A"
    )
    await env.photo("p-1", album.id, "2020-11-01")
    off = await env.svc.update("b-id", enabled=False)
    assert off == OnThisDaySettings(enabled=False)
    assert await env.svc.card(user_id="b-id", today=TODAY) is None
    # Only for Bob: Alice still sees it.
    assert await env.svc.card(user_id="a-id", today=TODAY) is not None
    await env.svc.update("b-id", enabled=True, hide_on=TODAY)
    assert await env.svc.card(user_id="b-id", today=TODAY) is None
    # ✕ hides it for the day only.
    tomorrow = await env.svc.card(user_id="b-id", today=date(2027, 11, 1))
    assert tomorrow is not None and tomorrow.years_ago == 7
    await env.svc.update("b-id", show_again=True)
    assert await env.svc.card(user_id="b-id", today=TODAY) is not None


async def test_dont_show_this_album_again(env):
    first = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="Busy"
    )
    second = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="Quiet"
    )
    for n in range(3):
        await env.photo(f"b-{n}", first.id, "2023-11-01")
    await env.photo("q-1", second.id, "2019-11-01")
    card = await env.svc.card(user_id="b-id", today=TODAY)
    assert card is not None and card.album.id == first.id
    settings = await env.svc.update("b-id", exclude_album_id=first.id)
    assert settings.excluded_album_ids == frozenset({first.id})
    card = await env.svc.card(user_id="b-id", today=TODAY)
    assert card is not None and card.album.id == second.id
    await env.svc.update("b-id", exclude_album_id=second.id)
    assert await env.svc.card(user_id="b-id", today=TODAY) is None
    settings = await env.svc.update("b-id", include_album_id=first.id)
    assert settings.excluded_album_ids == frozenset({second.id})
    await env.svc.update("b-id", exclude_album_id=first.id)
    settings = await env.svc.update("b-id", include_all=True)
    assert settings.excluded_album_ids == frozenset()


async def test_settings_live_in_the_preferences_blob_beside_other_keys(env):
    await env.db.enqueue(
        "UPDATE users SET preferences_json = ? WHERE user_id = 'b-id'",
        ('{"locale": "de"}',),
    )
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="A"
    )
    await env.svc.update("b-id", enabled=False, exclude_album_id=album.id)
    user = await env.users.get_by_user_id("b-id")
    assert orjson.loads(user.preferences_json) == {
        "locale": "de",
        GALLERY_ON_THIS_DAY_KEY: {"off": True, "excluded_albums": [album.id]},
    }
    # Back to the defaults stores an empty block.
    await env.svc.update("b-id", enabled=True, include_album_id=album.id)
    user = await env.users.get_by_user_id("b-id")
    assert orjson.loads(user.preferences_json)[GALLERY_ON_THIS_DAY_KEY] == {}


async def test_no_change_writes_nothing(env):
    same = await env.svc.update("b-id", enabled=True, show_again=True)
    assert same == OnThisDaySettings()
    user = await env.users.get_by_user_id("b-id")
    assert GALLERY_ON_THIS_DAY_KEY not in user.preferences_json


async def test_leaving_out_an_album_one_cannot_see_is_refused(env):
    """An unknown album (or, from PR 7 on, one the person may not see)
    answers like a missing one."""
    with pytest.raises(GalleryNotFoundError):
        await env.svc.update("b-id", exclude_album_id="missing")


async def test_the_left_out_list_is_bounded(env):
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="A"
    )
    full = [f"x{i}" for i in range(ON_THIS_DAY_MAX_EXCLUDED)]
    await env.users.set_preference_key(
        "b-id", GALLERY_ON_THIS_DAY_KEY, {"excluded_albums": full}
    )
    with pytest.raises(ValueError):
        await env.svc.update("b-id", exclude_album_id=album.id)


async def test_a_vanished_album_gives_no_card(env, monkeypatch):
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="A"
    )
    await env.photo("p-1", album.id, "2020-11-01")

    async def gone(*_a, **_k):
        raise GalleryNotFoundError("gone")

    monkeypatch.setattr(GalleryService, "get_album", gone)
    assert await env.svc.card(user_id="b-id", today=TODAY) is None


async def test_a_blocked_uploaders_photos_are_not_a_memory(env):
    album = await env.gallery.create_album(
        space_id=None, owner_user_id="a-id", name="A"
    )
    await env.photo("p-1", album.id, "2020-11-01", by="a-id")
    await env.db.enqueue(
        "INSERT INTO user_blocks(blocker_user_id, blocked_user_id)"
        " VALUES('b-id', 'a-id')"
    )
    assert await env.svc.card(user_id="b-id", today=TODAY) is None
