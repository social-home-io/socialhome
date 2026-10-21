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

from socialhome.domain.space_author_profile import (
    PROFILE_PICTURE_FIELD,
    encode_picture_sidecar,
    encode_profile_content,
    profile_picture_digest,
)
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


def _other_webp() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 200, 90)).save(buf, "WEBP")
    return buf.getvalue()


def _profile(
    signer, *, about=None, ts=None, sidecar: bytes | None | bool = True, **content_kw
) -> dict:
    """An author-signed profile item. The signed content names the picture
    by its hash; the bytes ride as the unsigned sidecar (``sidecar=True``:
    the picture itself; ``False``: none; bytes: these instead)."""
    picture = content_kw.pop("picture_webp", _webp())
    content, _ = encode_profile_content(
        display_name=content_kw.pop("display_name", "Bob Builder"),
        bio=content_kw.pop("bio", "Builds boats."),
        picture_hash=profile_picture_digest(picture) if picture else None,
    )
    target = about or signer.user_id
    inner = _generic(
        signer,
        "author_profile",
        target,
        target,
        content=content_kw.pop("content", content),
        **({"ts": ts} if ts else {}),
    )
    rider = picture if sidecar is True else (sidecar or None)
    if rider:
        inner[PROFILE_PICTURE_FIELD] = encode_picture_sidecar(rider)
    return inner


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
        json.dumps({"display_name": "Bob", "bio": None, "picture_hash": 7}),
        json.dumps({"display_name": "Bob", "bio": None, "picture_hash": "ab&sig=evil"}),
        json.dumps(
            {"display_name": "Bob", "bio": None, "picture_hash": "0123456789ABCDEF"}
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


async def test_a_relayed_picture_is_re_encoded_never_stored_as_sent(env, profiles):
    """A sidecar that is the signed picture is still re-encoded before it
    is stored and served to every viewer."""
    sent = _webp()
    await _send(
        env,
        "author_profile",
        _profile(BOB, picture_webp=sent),
        await _cert(env, BOB, "comment"),
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None and got.picture_hash == profile_picture_digest(sent)
    stored = await profiles.get_picture(SPACE, BOB.user_id)
    assert stored is not None
    raw, picture_hash = stored
    assert raw != sent and raw[:4] == b"RIFF"
    assert picture_hash == got.picture_hash


async def test_a_sidecar_that_is_not_the_signed_picture_is_dropped(env, profiles):
    """The sidecar is outside the author signature: a relay or host that
    swaps it (a polyglot, another picture) keeps the name, never the bytes."""
    swapped = _webp() + b"<script>alert(1)</script>"
    await _send(
        env,
        "author_profile",
        _profile(BOB, sidecar=swapped),
        await _cert(env, BOB, "comment"),
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None and got.display_name == "Bob Builder"
    assert await profiles.get_picture(SPACE, BOB.user_id) is None


async def test_a_name_change_without_the_picture_keeps_the_held_picture(env, profiles):
    """A name or bio change goes out without the picture bytes (a 1–4 KiB
    item instead of 64 KiB); the follower keeps the picture of the same
    signed hash."""
    cert = await _cert(env, BOB, "comment")
    await _send(env, "author_profile", _profile(BOB, ts=_now(-60)), cert)
    before = await profiles.get_picture(SPACE, BOB.user_id)
    assert before is not None
    await _send(
        env,
        "author_profile",
        _profile(BOB, display_name="Bob B.", sidecar=False),
        cert,
    )
    got = await _held(profiles, BOB.user_id)
    assert got is not None and got.display_name == "Bob B."
    assert await profiles.get_picture(SPACE, BOB.user_id) == before


async def test_a_follower_keeps_no_signed_item(env, profiles):
    """M2: only a household that can build a bundle (holds the space seed)
    keeps the author-signed item; a follower keeps the profile alone."""
    await _send(env, "author_profile", _profile(BOB), await _cert(env, BOB, "comment"))
    assert await _held(profiles, BOB.user_id) is not None
    assert await profiles.list_signed_items(SPACE, limit=5) == []


async def test_a_seed_holder_keeps_the_signed_item_with_its_picture(env, profiles):
    """A host (seed holder) forwards a relayed profile to a new follower
    author-signed, so it keeps the item — writer cert, epoch, and the
    original picture sidecar the follower will check against the hash."""
    await env["spaces"].set_space_seed(SPACE, env["space_kp"].private_key)
    inner = _profile(BOB)
    cert = await _cert(env, BOB, "comment")
    await _send(env, "author_profile", inner, cert)
    epoch = await env["crypto"].get_current_epoch(SPACE)
    [kept] = await profiles.list_signed_items(SPACE, limit=5)
    assert kept.epoch == epoch
    assert kept.item == {**inner, "writer_cert": cert}
    assert kept.picture_hash == profile_picture_digest(_webp())


async def test_a_seed_holder_stores_the_item_without_unsigned_junk(env, profiles):
    """Review I5: only the signed fields, the signature, the writer cert and
    the picture are kept for the bundle — never a member's padding."""
    await env["spaces"].set_space_seed(SPACE, env["space_kp"].private_key)
    inner = {**_profile(BOB), "junk": "x" * 50_000}
    await _send(env, "author_profile", inner, await _cert(env, BOB, "comment"))
    [kept] = await profiles.list_signed_items(SPACE, limit=5)
    assert "junk" not in kept.item


async def test_a_seed_holder_drops_the_old_picture_when_the_hash_changes(env, profiles):
    """Review: the held original picture rides on only while the signed
    hash is the same; a new hash without bytes leaves no picture to forward."""
    await env["spaces"].set_space_seed(SPACE, env["space_kp"].private_key)
    cert = await _cert(env, BOB, "comment")
    await _send(env, "author_profile", _profile(BOB, ts=_now(-60)), cert)
    [first] = await profiles.list_signed_items(SPACE, limit=5)
    assert PROFILE_PICTURE_FIELD in first.item
    newer = _profile(BOB, picture_webp=_other_webp(), sidecar=False)
    await _send(env, "author_profile", newer, cert)
    [kept] = await profiles.list_signed_items(SPACE, limit=5)
    assert PROFILE_PICTURE_FIELD not in kept.item
