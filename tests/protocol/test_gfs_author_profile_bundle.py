# ruff: noqa: F401, F811 — ``env`` is the shared fixture of test_gfs_member_publish_items
"""Release-blocker protocol tests: a new follower's author-profile bundle
(v_61). Marked ``@pytest.mark.security``.

The host forwards the authors' own signed ``author_profile`` items; it must
not be able to vouch for anything the authors did not sign. Drives the REAL
:meth:`SpacePublicInbound.apply_bundled_profiles` (the follower side, after
the bundle was opened and authority-verified) over real SQLite repos with
real writer certs. Pins that a bundled item is kept only when it passes
every check a relayed item passes — author signature, self-cert, the
subject is its signer, a writer cert binding the author at the bundle's
epoch — and that nothing but a profile is ever applied from a bundle.
"""

from __future__ import annotations

import json

import pytest

from socialhome.repositories.space_author_profile_repo import (
    SqliteSpaceAuthorProfileRepo,
)
from socialhome.repositories.space_post_repo import SqliteSpacePostRepo
from tests.protocol.test_gfs_author_profile import _profile
from tests.protocol.test_gfs_member_publish_items import (
    BOB,
    EVE,
    SPACE,
    _cert,
    _generic,
    _post_inner,
    env,
)

pytestmark = pytest.mark.security


@pytest.fixture
async def profiles(env):
    repo = SqliteSpaceAuthorProfileRepo(env["db"])
    env["inbound"].attach_author_profiles(repo)
    return repo


async def _bundle(env, *items, epoch=None):
    epoch = epoch if epoch is not None else await env["crypto"].get_current_epoch(SPACE)
    return await env["inbound"].apply_bundled_profiles(
        SPACE, epoch=epoch, items=list(items)
    )


async def _held(profiles, user_id):
    rows = await profiles.list_for(SPACE, [user_id])
    return rows[0] if rows else None


async def test_signed_profiles_in_a_bundle_are_kept(env, profiles):
    bob = {**_profile(BOB), "writer_cert": await _cert(env, BOB, "comment")}
    eve = {
        **_profile(EVE, display_name="Eve Example"),
        "writer_cert": await _cert(env, EVE, "comment"),
    }
    assert await _bundle(env, bob, eve) == 2
    assert (await _held(profiles, BOB.user_id)).display_name == "Bob Builder"
    assert (await _held(profiles, EVE.user_id)).display_name == "Eve Example"


async def test_a_host_cannot_vouch_for_an_unsigned_or_edited_profile(env, profiles):
    """The host chooses which items to forward, never what they say."""
    inner = {**_profile(BOB), "writer_cert": await _cert(env, BOB, "comment")}
    body = json.loads(inner["content"])
    body["display_name"] = "Mallory"
    edited = {**inner, "content": json.dumps(body)}
    unsigned = {k: v for k, v in inner.items() if k != "author_sig"}
    assert await _bundle(env, edited, unsigned) == 0
    assert await _held(profiles, BOB.user_id) is None


async def test_a_bundled_profile_needs_a_cert_binding_its_author(env, profiles):
    no_cert = _profile(BOB)
    unbound = {
        **_profile(BOB),
        "writer_cert": await _cert(env, BOB, "comment", users=["someone-else"]),
    }
    assert await _bundle(env, no_cert, unbound) == 0
    assert await _held(profiles, BOB.user_id) is None


async def test_a_bundled_profile_about_someone_else_is_refused(env, profiles):
    item = {
        **_profile(EVE, about=BOB.user_id, display_name="Bob (real)"),
        "writer_cert": await _cert(
            env, EVE, "comment", users=[EVE.user_id, BOB.user_id]
        ),
    }
    assert await _bundle(env, item) == 0
    assert await _held(profiles, BOB.user_id) is None


async def test_a_bundle_at_a_stale_epoch_is_refused(env, profiles):
    """The certs are checked at the bundle's epoch, which must be open here."""
    old = await env["crypto"].get_current_epoch(SPACE)
    item = {**_profile(BOB), "writer_cert": await _cert(env, BOB, "comment")}
    await env["crypto"].rotate_epoch(SPACE)
    await env["crypto"].rotate_epoch(SPACE)
    assert await _bundle(env, item, epoch=old) == 0
    assert await _held(profiles, BOB.user_id) is None


async def test_nothing_but_a_profile_is_applied_from_a_bundle(env, profiles):
    """A bundle can never smuggle a post, comment or reaction in."""
    post = {
        **_post_inner(BOB, "post-1", "post", content="smuggled"),
        "writer_cert": await _cert(env, BOB, "write"),
    }
    comment = {
        **_generic(BOB, "comment", "c-1", "post-1", content="smuggled"),
        "writer_cert": await _cert(env, BOB, "comment"),
    }
    assert await _bundle(env, post, comment, "not-an-item", 7) == 0
    assert await SqliteSpacePostRepo(env["db"]).get("post-1") is None


async def test_a_bundle_for_an_unknown_space_is_ignored(env, profiles):
    item = {**_profile(BOB), "writer_cert": await _cert(env, BOB, "comment")}
    assert (
        await env["inbound"].apply_bundled_profiles("nope", epoch=0, items=[item]) == 0
    )
