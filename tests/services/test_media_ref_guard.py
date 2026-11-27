"""Tests for the media-reference write guard (``services/media_ref_guard``)."""

from __future__ import annotations

import pytest

from socialhome.domain.errors import InvalidMediaRefError
from socialhome.services.media_ref_guard import MediaRefGuard, MediaRefGuardedMixin


class _Refs:
    """In-memory stub: which ``(file, user)`` pairs may be named."""

    def __init__(self, allowed: set[tuple[str, str]]) -> None:
        self.allowed = allowed
        self.asked: list[tuple[str, str]] = []

    async def may_reference(self, basename: str, user_id: str) -> bool:
        self.asked.append((basename, user_id))
        return (basename, user_id) in self.allowed


async def test_every_reference_is_checked_and_empties_skipped():
    refs = _Refs({("a.webp", "u1"), ("b.webp", "u1")})
    guard = MediaRefGuard(refs)  # type: ignore[arg-type]
    await guard.require_openable(
        ["api/media/a.webp?exp=1&sig=x", None, "", "/api/media/b.webp"], user_id="u1"
    )
    assert refs.asked == [("a.webp", "u1"), ("b.webp", "u1")]


async def test_a_refusal_is_the_malformed_reference_answer():
    guard = MediaRefGuard(_Refs(set()))  # type: ignore[arg-type]
    with pytest.raises(InvalidMediaRefError) as err:
        await guard.require_openable(
            ["api/media/x.webp"], user_id="u1", field="cover_url"
        )
    assert err.value.params["field"] == "cover_url"


async def test_a_page_scoped_copy_is_never_named_by_a_write():
    """v_82: a space page's page-scoped copy (``sp-``) belongs to that page
    alone — no write may name it, whatever the reference rows say."""
    sp = "sp-" + "a" * 40 + ".webp"
    refs = _Refs({(sp, "u1")})
    guard = MediaRefGuard(refs)  # type: ignore[arg-type]
    with pytest.raises(InvalidMediaRefError):
        await guard.require_openable([f"api/media/{sp}"], user_id="u1")
    assert refs.asked == []


class _Svc(MediaRefGuardedMixin):
    __slots__ = ("_media_guard",)


async def test_the_mixin_checks_once_attached():
    svc = _Svc()
    # Not attached (a bare unit-test service): nothing is checked.
    await svc._require_openable_media(["api/media/x.webp"], user_id="u1")
    svc.attach_media_guard(MediaRefGuard(_Refs(set())))  # type: ignore[arg-type]
    with pytest.raises(InvalidMediaRefError):
        await svc._require_openable_media(["api/media/x.webp"], user_id="u1")


async def test_may_open_is_the_naming_rule():
    refs = _Refs({("a.webp", "u1")})
    guard = MediaRefGuard(refs)  # type: ignore[arg-type]
    assert await guard.may_open("a.webp", "u1")
    assert not await guard.may_open("a.webp", "u2")


class _SpaceRefs:
    """Which files rows reference, and which only this space's do."""

    def __init__(self, referenced=(), confined=()) -> None:
        self.referenced = set(referenced)
        self.confined = set(confined)

    async def is_referenced(self, basename):
        return basename in self.referenced

    async def confined_to_space(self, basename, space_id):
        return space_id == "sp" and basename in self.confined


async def test_a_file_named_elsewhere_gets_a_copy_local_to_the_space(tmp_path):
    """v_97 (coordinator 2A) — a write naming a file another place already
    references (another space, the household feed, a DM) gets a copy of
    its own for this space: a new name, the same bytes; the reference
    keeps its path and query. A fresh upload (no row yet) and a file only
    this space's rows name keep their names; a missing file keeps its."""
    (tmp_path / "shared.webp").write_bytes(b"pixels")
    (tmp_path / "fresh.webp").write_bytes(b"new")
    (tmp_path / "mine.webp").write_bytes(b"here")
    refs = _SpaceRefs(
        referenced={"shared.webp", "mine.webp", "gone.webp"}, confined={"mine.webp"}
    )
    guard = MediaRefGuard(refs, media_dir=tmp_path)  # type: ignore[arg-type]
    rename = await guard.space_copies(
        [
            "api/media/shared.webp?exp=1",
            "api/media/fresh.webp",
            "api/media/mine.webp",
            "api/media/gone.webp",
            None,
        ],
        space_id="sp",
    )
    copy = rename("api/media/shared.webp?exp=1")
    assert (
        copy is not None
        and copy.startswith("api/media/")
        and copy.endswith(".webp?exp=1")
    )
    name = copy.removeprefix("api/media/").split("?")[0]
    assert name != "shared.webp"
    assert (tmp_path / name).read_bytes() == b"pixels"
    assert (tmp_path / "shared.webp").read_bytes() == b"pixels"
    for kept in ("api/media/fresh.webp", "api/media/mine.webp", "api/media/gone.webp"):
        assert rename(kept) == kept
    assert rename(None) is None
    # The same file named twice in one write: one copy.
    twice = await guard.space_copies(
        ["api/media/shared.webp", "api/media/shared.webp"], space_id="sp"
    )
    assert twice("api/media/shared.webp") == twice("api/media/shared.webp")


async def test_without_a_media_dir_nothing_is_copied():
    guard = MediaRefGuard(_SpaceRefs(referenced={"a.webp"}))  # type: ignore[arg-type]
    rename = await guard.space_copies(["api/media/a.webp"], space_id="sp")
    assert rename("api/media/a.webp") == "api/media/a.webp"
