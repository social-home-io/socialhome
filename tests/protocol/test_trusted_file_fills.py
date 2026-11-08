"""v_86 — trusted file fills, end to end on a real app.

A household that holds a space row whose hash map lists ``h`` takes the
missing file ``sc(S, h)`` from the row's AUTHOR household or from a
TRUSTED household (the host, a live admin or moderator seat — content
authority). The bytes must hash to ``h``; an existing file is never
replaced. Anyone else's push for a known row is refused, as is any push
for a deleted row. Unhashed (legacy) files keep the owner-only fill.
Rows themselves still come only from the host / a delegated admin.
"""

from __future__ import annotations

import base64
import hashlib
import pathlib

import pytest

from socialhome.domain.federation import FederationEventType as FET
from socialhome.domain.file_hashes import space_content_media_name
from socialhome.services.inbound_media_store import space_direct_media_name
from tests.protocol.test_content_file_hashes import _files, _media, _post, env
from tests.protocol.test_space_content_authorship import (
    ADMIN,
    AUTHOR,
    HOST,
    MOD,
    OTHER,
    SP,
    STRANGER,
    _deliver,
)

pytestmark = pytest.mark.security

__all__ = ["env"]

PIC = b"the author's picture"
H = hashlib.sha256(PIC).hexdigest()
SC = space_content_media_name(SP, H, "pic.webp")


def _blob(row_id: str, data: bytes = PIC, *, h: str | None = H, **extra) -> dict:
    p: dict = {
        "correlation_id": row_id,
        "filename": "pic.webp",
        "bytes_b64": base64.b64encode(data).decode(),
        **extra,
    }
    if h is not None:
        p.update({"h": h, "h_suite": "sha256"})
    return p


async def _hashed_post(app, post_id: str = "p-fill") -> None:
    """The author's live post, whose picture never arrived here."""
    await _deliver(
        app, FET.SPACE_POST_CREATED, _post(post_id, **_files("pic.webp")), sender=AUTHOR
    )


def _sc(tmp_dir: pathlib.Path) -> pathlib.Path:
    return _media(tmp_dir) / SC


@pytest.mark.parametrize(
    "sender",
    [
        pytest.param(MOD, id="moderator"),
        pytest.param(ADMIN, id="admin"),
        pytest.param(HOST, id="host"),
        pytest.param(AUTHOR, id="author"),
    ],
)
async def test_a_trusted_or_author_household_fills_a_hashed_file(env, tmp_dir, sender):
    app, _db = env
    await _hashed_post(app)
    assert not _sc(tmp_dir).exists()
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill"), sender=sender)
    assert _sc(tmp_dir).read_bytes() == PIC


@pytest.mark.parametrize(
    "sender",
    [
        pytest.param(OTHER, id="plain member"),
        pytest.param(STRANGER, id="no seat"),
    ],
)
async def test_a_plain_member_or_stranger_never_fills(env, tmp_dir, sender):
    """The bytes would even be right — but neither the author nor a
    trusted household: refused (the fill rule, not only the hash)."""
    app, _db = env
    await _hashed_post(app)
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill"), sender=sender)
    assert not _sc(tmp_dir).exists()
    assert not (
        _media(tmp_dir) / space_direct_media_name(SP, sender, "pic.webp")
    ).exists()


async def test_a_moderators_bad_bytes_are_refused(env, tmp_dir):
    app, _db = env
    await _hashed_post(app)
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill", b"planted"), sender=MOD)
    assert not _sc(tmp_dir).exists()


async def test_an_existing_file_is_never_replaced(env, tmp_dir):
    app, _db = env
    await _hashed_post(app)
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill"), sender=AUTHOR)
    before = _sc(tmp_dir).stat()
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill"), sender=MOD)
    after = _sc(tmp_dir).stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert _sc(tmp_dir).read_bytes() == PIC


async def test_an_unhashed_file_is_filled_by_the_owner_only(env, tmp_dir):
    """A legacy (unhashed) post keeps #1311's rule: a moderator's fill is
    refused, the host's lands under the name the row shows."""
    app, _db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-legacy"), sender=AUTHOR)
    shown = space_direct_media_name(SP, AUTHOR, "pic.webp")
    legacy = {
        "correlation_id": "p-legacy",
        "filename": shown,
        "bytes_b64": base64.b64encode(PIC).decode(),
    }
    await _deliver(app, FET.SPACE_MEDIA_BLOB, legacy, sender=MOD)
    assert not (_media(tmp_dir) / shown).exists()
    await _deliver(app, FET.SPACE_MEDIA_BLOB, legacy, sender=HOST)
    assert (_media(tmp_dir) / shown).read_bytes() == PIC


async def test_an_h_the_row_does_not_list_is_refused(env, tmp_dir):
    app, _db = env
    await _hashed_post(app)
    other = b"another picture"
    oh = hashlib.sha256(other).hexdigest()
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill", other, h=oh), sender=MOD)
    assert not (_media(tmp_dir) / space_content_media_name(SP, oh, "pic.webp")).exists()
    assert not _sc(tmp_dir).exists()


async def test_a_deleted_rows_file_is_never_filled(env, tmp_dir):
    """A soft-deleted post keeps its picture list on disk; nobody fills it,
    not even the host."""
    app, db = env
    await _hashed_post(app)
    await db.enqueue("UPDATE space_posts SET deleted=1 WHERE id='p-fill'", ())
    for sender in (MOD, HOST, AUTHOR):
        await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("p-fill"), sender=sender)
    assert not _sc(tmp_dir).exists()


async def test_a_fill_for_a_row_of_another_space_is_refused(env, tmp_dir):
    """The blob claims another space for a row of ``SP``: the row lives
    elsewhere — neither space's content name is written."""
    app, _db = env
    await _hashed_post(app)
    elsewhere = "sp-elsewhere"
    await _deliver(
        app,
        FET.SPACE_MEDIA_BLOB,
        _blob(
            "p-fill",
            filename=space_content_media_name(elsewhere, H, "pic.webp"),
            space_id=elsewhere,
        ),
        sender=MOD,
    )
    assert not _sc(tmp_dir).exists()
    assert not (
        _media(tmp_dir) / space_content_media_name(elsewhere, H, "pic.webp")
    ).exists()


async def test_a_comments_file_is_filled_by_its_author_or_a_moderator(env, tmp_dir):
    """The author check reads the comment's author, not its post's."""
    app, _db = env
    await _deliver(app, FET.SPACE_POST_CREATED, _post("p-c"), sender=AUTHOR)
    await _deliver(
        app,
        FET.SPACE_COMMENT_CREATED,
        {
            "space_id": SP,
            "post_id": "p-c",
            "comment_id": "c-fill",
            "author": "u-o",
            "type": "image",
            "content": "",
            "media_url": "api/media/pic.webp",
            **_files("pic.webp"),
        },
        sender=OTHER,
    )
    # The post's author household is not the comment's author.
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("c-fill"), sender=AUTHOR)
    assert not _sc(tmp_dir).exists()
    await _deliver(app, FET.SPACE_MEDIA_BLOB, _blob("c-fill"), sender=OTHER)
    assert _sc(tmp_dir).read_bytes() == PIC
