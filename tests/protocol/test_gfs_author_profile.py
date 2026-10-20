# ruff: noqa: F401, F811 — ``env`` is the shared fixture of test_gfs_member_publish_items
"""Release-blocker protocol tests: author profiles for a space's GFS
followers (v_60, ``author_profile``). Marked ``@pytest.mark.security``.

Drives the REAL :class:`SpacePublicInbound` over real SQLite repos with real
writer certs, as a follower household (no roster) receives ``space_item``
frames — the harness of ``test_gfs_member_publish_items``. Pins that a
follower keeps a profile only when:

* it is signed by the household whose identity key self-certifies the named
  author, under a writer cert that binds the author;
* it is about its own signer (``item_target == post_id == author``);
* its content decodes (name present, sizes capped, picture is WebP and
  matches its hash);
* it is newer than the one held.

And that a kept profile names the author for the SPA (``PersonSummaryService``
in the space scope) — and never creates a seat.
"""

from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from socialhome.domain.space_author_profile import encode_profile_content
from socialhome.repositories.space_author_profile_repo import (
    SqliteSpaceAuthorProfileRepo,
)
from socialhome.repositories.space_remote_member_repo import (
    SqliteSpaceRemoteMemberRepo,
)
from tests.protocol.test_gfs_member_publish_items import (
    BOB,
    EVE,
    SPACE,
    _cert,
    _generic,
    _now,
    _send,
    env,
)

pytestmark = pytest.mark.security


def _webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (200, 120, 60)).save(buf, "WEBP")
    return buf.getvalue()


def _profile(signer, *, about=None, ts=None, **content_kw) -> dict:
    content, _ = encode_profile_content(
        display_name=content_kw.pop("display_name", "Bob Builder"),
        bio=content_kw.pop("bio", "Builds boats."),
        picture_webp=content_kw.pop("picture_webp", _webp()),
    )
    target = about or signer.user_id
    return _generic(
        signer,
        "author_profile",
        target,
        target,
        content=content_kw.pop("content", content),
        **({"ts": ts} if ts else {}),
    )


@pytest.fixture
async def profiles(env):
    repo = SqliteSpaceAuthorProfileRepo(env["db"])
    env["inbound"].attach_author_profiles(repo)
    return repo


async def _held(profiles, user_id):
    rows = await profiles.list_for(SPACE, [user_id])
    return rows[0] if rows else None


async def test_a_signed_profile_is_kept(env, profiles):
    await _send(env, "author_profile", _profile(BOB), await _cert(env, BOB, "comment"))
    got = await _held(profiles, BOB.user_id)
    assert got is not None
    assert (got.display_name, got.bio) == ("Bob Builder", "Builds boats.")
    assert got.picture_hash and got.author_pk == BOB.kp.public_key.hex()
    raw, _ = await profiles.get_picture(SPACE, BOB.user_id)
    assert raw[:4] == b"RIFF"
    # Never a seat: a follower does not invent members.
    seats = await SqliteSpaceRemoteMemberRepo(env["db"]).list_for_space(SPACE)
    assert seats == []


async def test_a_profile_about_someone_else_is_refused(env, profiles):
    """Eve signs a profile naming Bob as its subject."""
    await _send(
        env,
        "author_profile",
        _profile(EVE, about=BOB.user_id, display_name="Bob (real)"),
        await _cert(env, EVE, "comment", users=[EVE.user_id, BOB.user_id]),
    )
    assert await _held(profiles, BOB.user_id) is None


async def test_a_profile_claiming_another_author_does_not_verify(env, profiles):
    """Eve's household signs an inner that names Bob as the author: the
    self-cert (``derive_user_id(author_pk, username)``) fails."""
    inner = _profile(EVE)
    inner = {**inner, "author_user_id": BOB.user_id, "item_target": BOB.user_id}
    await _send(env, "author_profile", inner, await _cert(env, EVE, "comment"))
    assert await _held(profiles, BOB.user_id) is None
    assert await _held(profiles, EVE.user_id) is None


async def test_a_cert_that_does_not_bind_the_author_is_refused(env, profiles):
    await _send(
        env,
        "author_profile",
        _profile(BOB),
        await _cert(env, BOB, "comment", users=["someone-else"]),
    )
    assert await _held(profiles, BOB.user_id) is None


async def test_an_edited_profile_after_signing_is_refused(env, profiles):
    """The profile is the signed ``content``: changing the name breaks the
    author signature."""
    inner = _profile(BOB)
    body = json.loads(inner["content"])
    body["display_name"] = "Mallory"
    inner = {**inner, "content": json.dumps(body)}
    await _send(env, "author_profile", inner, await _cert(env, BOB, "comment"))
    assert await _held(profiles, BOB.user_id) is None


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"display_name": "", "bio": None, "picture": None}),
        json.dumps({"display_name": "x" * 65, "bio": None, "picture": None}),
        json.dumps({"display_name": "Bob", "bio": "x" * 301, "picture": None}),
        json.dumps(
            {
                "display_name": "Bob",
                "bio": None,
                "picture": base64.b64encode(b"<svg onload=alert(1)>").decode(),
                "picture_hash": "0123456789abcdef",
            }
        ),
        json.dumps(
            {
                "display_name": "Bob",
                "bio": None,
                "picture": base64.b64encode(_webp()).decode(),
                "picture_hash": "0123456789abcdef",  # not the bytes' hash
            }
        ),
    ],
)
async def test_malformed_profile_content_is_refused(env, profiles, content):
    await _send(
        env,
        "author_profile",
        _profile(BOB, content=content),
        await _cert(env, BOB, "comment"),
    )
    assert await _held(profiles, BOB.user_id) is None


async def test_an_older_profile_never_replaces_a_newer_one(env, profiles):
    cert = await _cert(env, BOB, "comment")
    await _send(
        env, "author_profile", _profile(BOB, display_name="New", ts=_now(-5)), cert
    )
    await _send(
        env, "author_profile", _profile(BOB, display_name="Old", ts=_now(-60)), cert
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None and got.display_name == "New"
    await _send(
        env, "author_profile", _profile(BOB, display_name="Newer", ts=_now(-1)), cert
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None and got.display_name == "Newer"


async def test_a_protected_authors_name_only_profile_is_kept_as_such(env, profiles):
    await _send(
        env,
        "author_profile",
        _profile(BOB, bio=None, picture_webp=None),
        await _cert(env, BOB, "comment"),
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None
    assert (got.display_name, got.bio, got.picture_hash) == ("Bob Builder", None, None)
    assert await profiles.get_picture(SPACE, BOB.user_id) is None
