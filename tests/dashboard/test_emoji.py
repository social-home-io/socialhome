"""Tests for the bundled Twemoji subset (:mod:`socialhome.dashboard.emoji`)."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from socialhome.dashboard import icons, map_view, messages, picture, presenter
from socialhome.dashboard.emoji import (
    EMOJI_DIR,
    FALLBACK_CODE,
    SOURCE_SIZE,
    EmojiImages,
    emoji_code,
    is_code,
    scale_png,
)
from socialhome.platform.ha.zones import DEFAULT_ZONE_EMOJI, MDI_ZONE_EMOJI


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("🏊", "1f3ca"),
        ("☀️", "2600"),
        ("🏋️", "1f3cb"),
        ("❤️‍🔥", "2764-fe0f-200d-1f525"),
        ("👨‍👩‍👧", "1f468-200d-1f469-200d-1f467"),
        ("🇩🇪", "1f1e9-1f1ea"),
        ("👍🏽", "1f44d-1f3fd"),
        ("W", None),
        ("", None),
        (None, None),
        ("🏊 Swim", None),
        ("a" * 20, None),
        ("️", None),
    ],
)
def test_emoji_code(text, code):
    assert emoji_code(text) == code


def test_is_code_refuses_traversal():
    assert is_code("1f3ca") and is_code("2764-fe0f-200d-1f525")
    for bad in ("../x", "1F3CA", "LICENSE", "1f3ca.png", "", "1f3ca/", "x" * 7):
        assert not is_code(bad)


def _wall_emoji() -> set[str]:
    """Every emoji the dashboard's own code can put on the wall."""
    found = {DEFAULT_ZONE_EMOJI, *MDI_ZONE_EMOJI.values()}
    found |= {e for e, _ in icons.GENERAL_KEYWORDS} | {
        e for e, _ in icons.LESSON_KEYWORDS
    }
    found |= {icons.FALLBACK_ICON, icons.BREAK_ICON, presenter.SCHOOL_ICON}
    found |= {p.icon for p in picture.DAY_PARTS}
    found |= {picture.FREE_ICON, picture.FREE_HOME_ICON}
    found |= {map_view.AWAY_ICON, map_view.HOME_ICON, map_view.PLACE_ICON}
    found |= {i for i in messages.KIND_ICON.values() if i}
    found |= {"🎈", "💬", "📝", "🌙", "🌅", "🔊", "🔒"}
    return found


def test_every_wall_emoji_is_bundled():
    missing = sorted(
        e for e in _wall_emoji() if not (EMOJI_DIR / f"{emoji_code(e)}.png").is_file()
    )
    assert missing == []


def test_bundle_has_its_licence_and_only_pngs():
    names = {p.name for p in EMOJI_DIR.iterdir()}
    assert "LICENSE.md" in names
    assert all(n.endswith(".png") and is_code(n[:-4]) for n in names - {"LICENSE.md"})
    assert (EMOJI_DIR / f"{FALLBACK_CODE}.png").is_file()
    assert "CC-BY 4.0" in (EMOJI_DIR / "LICENSE.md").read_text(encoding="utf-8")


def _png(size: int = 10) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (size, size), (255, 0, 0, 255)).save(out, format="PNG")
    return out.getvalue()


def test_scale_png():
    body = scale_png(_png(10), 48)
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "PNG" and img.size == (48, 48) and img.mode == "RGBA"


async def test_resolve_and_fallback():
    store = EmojiImages()
    assert await store.resolve("1f3ca") == "1f3ca"
    assert await store.resolve("2600-fe0f") == "2600"
    assert await store.resolve("10ffff") == FALLBACK_CODE


async def test_png_sizes_and_cache(tmp_path):
    (tmp_path / "1f3ca.png").write_bytes(_png(SOURCE_SIZE))
    store = EmojiImages(tmp_path, max_entries=1)
    raw = await store.png("1f3ca", SOURCE_SIZE)
    assert raw == (tmp_path / "1f3ca.png").read_bytes()
    small = await store.png("1f3ca", 24)
    with Image.open(io.BytesIO(small)) as img:
        assert img.size == (24, 24)
    # Cached (and the cache is bounded): the file can go away.
    (tmp_path / "1f3ca.png").unlink()
    assert await store.png("1f3ca", 24) == small
    with pytest.raises(FileNotFoundError):
        await store.png("1f3ca", SOURCE_SIZE)
