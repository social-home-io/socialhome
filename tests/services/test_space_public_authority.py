"""Tests for the authority kinds of the host relay (approved posts)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from socialhome.crypto import (
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
)
from socialhome.domain.link_preview import LinkPreview
from socialhome.domain.post import LocationData, Post, PostType
from socialhome.domain.space_item import (
    AUTHORITY_KIND_APPROVED_POST,
    AUTHORITY_KIND_FIELD,
    AUTHORITY_KIND_REMOVAL,
)
from socialhome.federation.owner_bound_id import SPACE_POST_KIND, mint_owner_bound_id
from socialhome.services.space_public_author import (
    build_signed_author_inner,
    verify_signed_author_inner,
)
from socialhome.services.space_public_authority import (
    approved_relay_for,
    is_approved,
    is_authority_inner,
)

_KP = generate_identity_keypair()
_USER = derive_user_id(_KP.public_key, "bob")
_ORIGIN = derive_instance_id(_KP.public_key)


def _post(**over) -> Post:
    base = Post(
        id=mint_owner_bound_id(SPACE_POST_KIND, space_id="sp", owner_user_id=_USER),
        author=_USER,
        type=PostType.TEXT,
        content="approved words",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        location=LocationData(lat=52.1234, lon=13.4321, label="cafe"),
    )
    return dataclasses.replace(base, **over)


def _signed(post: Post, *, origin: str = _ORIGIN, space_id: str = "sp") -> dict:
    return build_signed_author_inner(
        post=post,
        space_id=space_id,
        author_username="bob",
        author_pk=_KP.public_key,
        author_identity_seed=_KP.private_key,
        origin_instance_id=origin,
    )


def test_an_approved_relay_keeps_the_author_signature_and_is_marked():
    post = _post()
    out = approved_relay_for(_signed(post), post=post, space_id="sp")
    assert out is not None
    assert out[AUTHORITY_KIND_FIELD] == AUTHORITY_KIND_APPROVED_POST
    assert is_approved(out)
    assert not is_authority_inner(out)
    # The mark is outside the author signature.
    assert verify_signed_author_inner(out)


def test_the_link_card_padding_and_cert_are_dropped():
    post = _post(link_preview=LinkPreview(url="https://example.org", title="t"))
    relay = _signed(post)
    relay["writer_cert"] = {"x": 1}
    relay["_pad"] = "000"
    out = approved_relay_for(relay, post=post, space_id="sp")
    assert out is not None
    for key in (
        "link_preview",
        "link_preview_sig",
        "link_preview_sig_suite",
        "writer_cert",
        "_pad",
    ):
        assert key not in out


@pytest.mark.parametrize(
    "case",
    [
        "not_a_dict",
        "already_marked",
        "bad_sig",
        "other_space",
        "other_post",
        "other_author",
        "foreign_origin",
        "bad_pk",
        "edited_content",
        "other_location",
        "hidden_flag",
    ],
)
def test_a_signed_copy_that_is_not_exactly_the_approved_post_is_refused(case):
    post = _post()
    relay: object = _signed(post)
    published = post
    if case == "not_a_dict":
        relay = ["x"]
    elif case == "already_marked":
        relay[AUTHORITY_KIND_FIELD] = AUTHORITY_KIND_REMOVAL  # type: ignore[index]
    elif case == "bad_sig":
        relay["content"] = "tampered"  # type: ignore[index]
        published = _post(content="tampered")
    elif case == "other_space":
        relay = _signed(post, space_id="sp-2")
    elif case == "other_post":
        published = _post(
            id=mint_owner_bound_id(SPACE_POST_KIND, space_id="sp", owner_user_id=_USER)
        )
    elif case == "other_author":
        published = _post(author=derive_user_id(_KP.public_key, "carol"))
    elif case == "foreign_origin":
        relay = _signed(post, origin="someone.else")
    elif case == "bad_pk":
        relay["author_pk"] = "zz"  # type: ignore[index]
    elif case == "edited_content":
        # The reviewers approved other words than the author signed.
        published = _post(content="what the moderators saw")
    elif case == "other_location":
        published = _post(location=None)
    else:
        published = _post(hidden_from_feed=True)
    assert approved_relay_for(relay, post=published, space_id="sp") is None


def test_an_unsigned_inner_is_an_authority_inner():
    assert is_authority_inner({AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL})
    assert not is_approved({AUTHORITY_KIND_FIELD: AUTHORITY_KIND_REMOVAL})
