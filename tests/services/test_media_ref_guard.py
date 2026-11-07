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
