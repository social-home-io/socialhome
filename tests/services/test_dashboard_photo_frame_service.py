"""Tests for :class:`DashboardPhotoFrameService` — the wall's photo
screensaver read model (gallery decision D10)."""

from __future__ import annotations

import os
from dataclasses import replace

import pytest

from socialhome.domain.household_dashboard import (
    FRAME_PHOTOS_SCANNED,
    FRAME_PHOTOS_SHOWN,
    DashboardPhoto,
    FrameAlbumChoice,
)
from socialhome.infrastructure.key_manager import KeyManager
from socialhome.services.dashboard_photo_frame_service import (
    MAX_PHOTO_BYTES,
    DashboardPhotoFrameService,
)
from socialhome.services.dashboard_settings_service import DashboardSettingsService
from socialhome.services.gallery_service import (
    DEFAULT_ALBUM_KEY,
    DEFAULT_ALBUM_RENAMED_KEY,
)


class _Config:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.rows.get(key)

    async def set(self, key: str, value: str) -> None:
        self.rows[key] = value

    async def set_many(self, items) -> None:
        self.rows.update(items)

    async def delete(self, key: str) -> None:
        self.rows.pop(key, None)


def _photo(item_id: str, album_id: str, day: str = "2026-07-12") -> DashboardPhoto:
    return DashboardPhoto(
        id=item_id,
        album_id=album_id,
        album_name=f"raw {album_id}",
        filename=f"{item_id}.webp",
        width=1600,
        height=1200,
        day=day,
    )


class _Repo:
    """The gallery repo's screensaver reads (``AbstractGalleryRepo``)."""

    def __init__(self) -> None:
        self.albums = [
            FrameAlbumChoice(id="sys", name="Posts", photo_count=1, is_system=True),
            FrameAlbumChoice(id="al-default", name="Fotos", photo_count=1),
            FrameAlbumChoice(id="al-summer", name="Summer", photo_count=2),
        ]
        self.photos = [
            _photo("p-sys", "sys"),
            _photo("p-def", "al-default"),
            _photo("p-1", "al-summer", "2026-07-12"),
            _photo("p-2", "al-summer", "2026-07-13"),
        ]
        self.asked: list[tuple[list[str], int]] = []

    async def list_frame_albums(self) -> list[FrameAlbumChoice]:
        return list(self.albums)

    async def list_frame_photos(self, album_ids, *, limit):
        self.asked.append((list(album_ids), limit))
        return [p for p in self.photos if p.album_id in album_ids][:limit]

    async def get_frame_photo(self, item_id, album_ids):
        return next(
            (p for p in self.photos if p.id == item_id and p.album_id in album_ids),
            None,
        )


@pytest.fixture
def config() -> _Config:
    return _Config()


@pytest.fixture
def settings(config) -> DashboardSettingsService:
    svc = DashboardSettingsService(config, fixed_port=None, main_port=8080)
    svc.attach_key_manager(KeyManager(os.urandom(32)))
    return svc


@pytest.fixture
def repo() -> _Repo:
    return _Repo()


@pytest.fixture
def service(repo, settings, config, tmp_path) -> DashboardPhotoFrameService:
    return DashboardPhotoFrameService(repo, settings, config, media_dir=tmp_path)


async def _on(settings, albums=("al-summer",)) -> None:
    await settings.update(
        actor_is_admin=True,
        patch={"photo_frame": True, "photo_frame_albums": list(albums)},
    )


async def test_choices_flag_the_default_album_until_renamed(service, config):
    choices = await service.album_choices()
    assert [c.default_name for c in choices] == [False, False, False]
    config.rows[DEFAULT_ALBUM_KEY] = "al-default"
    flags = {c.id: c.default_name for c in await service.album_choices()}
    assert flags == {"sys": False, "al-default": True, "al-summer": False}
    config.rows[DEFAULT_ALBUM_RENAMED_KEY] = "al-default"
    assert not any(c.default_name for c in await service.album_choices())


async def test_nothing_while_off(service, settings, repo):
    assert await service.photos("2026-11-02") == ()
    assert await service.photo("p-1") is None
    # Albums picked but the switch off — still nothing.
    await settings.update(
        actor_is_admin=True, patch={"photo_frame_albums": ["al-summer"]}
    )
    assert await service.photos("2026-11-02") == ()
    # On but no album: nothing either.
    await settings.update(
        actor_is_admin=True, patch={"photo_frame": True, "photo_frame_albums": []}
    )
    assert await service.photos("2026-11-02") == ()
    assert repo.asked == []


async def test_round_of_the_picked_albums_only(service, settings, repo):
    await _on(settings)
    photos = await service.photos("2026-11-02")
    assert [p.id for p in photos] == ["p-1", "p-2"]
    assert {p.album_name for p in photos} == {"Summer"}
    assert repo.asked == [(["al-summer"], FRAME_PHOTOS_SCANNED)]
    assert (await service.photo("p-1")).id == "p-1"
    assert await service.photo("p-sys") is None


async def test_an_album_no_longer_open_to_everyone_drops_out(service, settings, repo):
    """Picked, then made private (it leaves the repo's open list)."""
    await _on(settings, ("al-summer", "sys"))
    repo.albums = [a for a in repo.albums if a.id != "al-summer"]
    assert [p.id for p in await service.photos("2026-11-02")] == ["p-sys"]
    assert await service.photo("p-1") is None


async def test_the_wall_names_the_system_and_default_album(service, settings, config):
    config.rows[DEFAULT_ALBUM_KEY] = "al-default"
    await _on(settings, ("sys", "al-default", "al-summer"))
    names = {"system": "Beiträge", "default": "Fotos (de)"}
    by_id = {p.id: p.album_name for p in await service.photos("d", album_names=names)}
    assert by_id == {
        "p-sys": "Beiträge",
        "p-def": "Fotos (de)",
        "p-1": "Summer",
        "p-2": "Summer",
    }
    # Without wall names, the stored ones.
    by_id = {p.id: p.album_name for p in await service.photos("d")}
    assert by_id["p-sys"] == "Posts"


async def test_a_big_library_is_sampled_the_same_all_day(service, settings, repo):
    await _on(settings)
    repo.photos = [
        _photo(f"p-{n:03}", "al-summer", f"2026-01-{n % 28 + 1:02}") for n in range(90)
    ]
    first = await service.photos("2026-11-02")
    assert len(first) == FRAME_PHOTOS_SHOWN
    assert await service.photos("2026-11-02") == first
    assert await service.photos("2026-11-03") != first
    assert len({p.id for p in first}) == FRAME_PHOTOS_SHOWN


async def test_read_gives_the_stored_file(service, tmp_path):
    (tmp_path / "p-1.webp").write_bytes(b"RIFF....WEBP")
    assert await service.read(_photo("p-1", "al-summer")) == b"RIFF....WEBP"


@pytest.mark.parametrize("name", ["", "../x.webp", "a/b.webp", "a\\b.webp", ".hidden"])
async def test_read_refuses_odd_file_names(service, name):
    photo = replace(_photo("p-1", "al-summer"), filename=name)
    assert await service.read(photo) is None


def _sparse(path, size: int) -> None:
    with open(path, "wb") as fh:
        fh.truncate(size)


async def test_read_missing_or_too_big(service, tmp_path):
    assert await service.read(_photo("gone", "al-summer")) is None
    _sparse(tmp_path / "big.webp", MAX_PHOTO_BYTES + 1)
    assert await service.read(replace(_photo("big", "al-summer"))) is None
