"""Gallery downloads: human file names and the streamed ZIP."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime

import pytest

from socialhome.domain.gallery import GalleryDownloadFile, GalleryItem
from socialhome.services.gallery_download import (
    content_disposition,
    download_names,
    safe_label,
    stream_zip,
    zip_size,
)


def _item(
    iid: str,
    *,
    taken_at: str | None = None,
    created_at: str | None = "2026-08-01T10:00:00+00:00",
    url: str = "api/media/abc.webp",
) -> GalleryItem:
    return GalleryItem(
        id=iid,
        album_id="al",
        uploaded_by="u",
        item_type="photo",
        url=url,
        thumbnail_url="api/media/t.webp",
        width=10,
        height=10,
        taken_at=taken_at,
        created_at=created_at,
    )


def test_a_lone_photo_is_named_after_its_album_and_day():
    assert download_names([(_item("1", taken_at="2026-07-12"), "Sommer 2026")]) == [
        "Sommer 2026 – 2026-07-12.webp"
    ]


def test_photos_of_one_day_are_numbered_and_others_are_not():
    names = download_names(
        [
            (_item("1", taken_at="2026-07-12"), "Sommer 2026"),
            (_item("2", taken_at="2026-07-13"), "Sommer 2026"),
            (_item("3", taken_at="2026-07-12", url="api/media/v.webm"), "Sommer 2026"),
        ]
    )
    assert names == [
        "Sommer 2026 – 2026-07-12 – 1.webp",
        "Sommer 2026 – 2026-07-13.webp",
        "Sommer 2026 – 2026-07-12 – 2.webm",
    ]


def test_no_day_taken_falls_back_to_the_day_added_then_to_none():
    names = download_names(
        [
            (_item("1"), "Lake"),
            (_item("2", created_at=None, url="api/media/x"), "Lake"),
        ]
    )
    assert names == ["Lake – 2026-08-01.webp", "Lake.bin"]


def test_names_stay_unique_even_against_a_tricky_album_name():
    names = download_names(
        [
            (_item("1", taken_at="2026-07-12"), "A – 2026-07-12 – 1"),
            (_item("2", taken_at="2026-07-12"), "A"),
            (_item("3", taken_at="2026-07-12"), "A"),
        ]
    )
    assert len({n.casefold() for n in names}) == 3


@pytest.mark.parametrize(
    ("raw", "safe"),
    [
        ("../../etc/passwd", "etc passwd"),
        ('Gifts: "Mum" <3', "Gifts Mum 3"),
        ("  ...  ", "Photo"),
        ("Tab\there", "Tab here"),
        ("x" * 200, "x" * 80),
        (None, "Photo"),
    ],
)
def test_album_names_are_made_file_safe(raw, safe):
    assert safe_label(raw) == safe


def test_content_disposition_carries_a_utf8_name_and_an_ascii_fallback():
    header = content_disposition('Über "Sommer" – 1.webp')
    assert header.startswith('attachment; filename="')
    assert '"' not in header.split('filename="', 1)[1].split('";', 1)[0]
    assert "filename*=UTF-8''%C3%9Cber%20%22Sommer%22%20%E2%80%93%201.webp" in header


async def test_the_zip_holds_the_stored_files_unchanged(tmp_path):
    files = []
    for n, payload in enumerate([b"RIFF0000WEBPdata", b"\x1aE\xdf\xa3webm" * 1000]):
        p = tmp_path / f"f{n}"
        p.write_bytes(payload)
        files.append(
            GalleryDownloadFile(path=str(p), name=f"Über – {n}.webp", size=len(payload))
        )
    out = bytearray()

    async def write(chunk: bytes) -> None:
        out.extend(chunk)

    written = await stream_zip(files, write, now=datetime(2026, 11, 2, 12, 30, 10))

    assert written == len(out) == zip_size(files)
    with zipfile.ZipFile(io.BytesIO(bytes(out))) as zf:
        assert zf.testzip() is None  # every CRC checks out
        assert zf.namelist() == ["Über – 0.webp", "Über – 1.webp"]
        assert zf.read("Über – 1.webp") == (tmp_path / "f1").read_bytes()
        info = zf.getinfo("Über – 0.webp")
        assert info.compress_type == zipfile.ZIP_STORED  # no second compression
        assert info.date_time == (2026, 11, 2, 12, 30, 10)


async def test_a_file_that_changed_size_ends_the_download(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"short")
    files = [GalleryDownloadFile(path=str(p), name="a.webp", size=99)]

    async def write(chunk: bytes) -> None:
        return None

    with pytest.raises(OSError):
        await stream_zip(files, write)


async def test_an_archive_past_the_classic_zip_limit_is_refused(tmp_path):
    files = [GalleryDownloadFile(path=str(tmp_path / "x"), name="a", size=2**32)]

    async def write(chunk: bytes) -> None:
        return None

    with pytest.raises(ValueError):
        await stream_zip(files, write)
