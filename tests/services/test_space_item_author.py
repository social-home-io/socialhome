"""Tests for the generic author-signed space item inner (v_49)."""

from __future__ import annotations

import pytest

from socialhome.crypto import (
    b64url_encode,
    derive_instance_id,
    derive_user_id,
    generate_identity_keypair,
    sign_ed25519,
)
from socialhome.services.space_item_author import (
    ITEM_AUTHOR_SIG_SUITE_ED25519,
    UnsupportedItemAuthorSigSuite,
    build_signed_item_inner,
    item_signing_bytes,
    verify_signed_item_inner,
)
from socialhome.services.space_public_author import author_signing_bytes

KP = generate_identity_keypair()
USER = derive_user_id(KP.public_key, "bob")
ORIGIN = derive_instance_id(KP.public_key)


def _inner(**over) -> dict:
    kw = dict(
        item_type="comment",
        item_target="c-1",
        space_id="sp-1",
        post_id="p-1",
        author_user_id=USER,
        author_username="bob",
        author_pk=KP.public_key,
        author_identity_seed=KP.private_key,
        origin_instance_id=ORIGIN,
        ts="2026-10-03T12:00:00+00:00",
        comment_type="text",
        content="hello",
    )
    kw.update(over)
    return build_signed_item_inner(**kw)


def test_a_built_inner_verifies():
    inner = _inner()
    assert inner["author_sig_suite"] == ITEM_AUTHOR_SIG_SUITE_ED25519
    assert verify_signed_item_inner(inner)


@pytest.mark.parametrize(
    "field",
    [
        "item_type",
        "item_target",
        "space_id",
        "post_id",
        "content",
        "ts",
        "emoji",
        "parent_id",
        "created_at",
        "media_url",
        "comment_type",
        "origin_instance_id",
    ],
)
def test_every_field_is_signed(field):
    inner = _inner()
    inner[field] = "tampered"
    assert not verify_signed_item_inner(inner)


def test_the_item_type_cannot_be_rewrapped():
    inner = _inner(item_type="comment_delete")
    inner["item_type"] = "post_delete"
    assert not verify_signed_item_inner(inner)


def test_a_forged_author_is_refused():
    other = generate_identity_keypair()
    inner = _inner(author_pk=other.public_key, author_identity_seed=other.private_key)
    # The user id derives from another key: the self-cert fails.
    assert not verify_signed_item_inner(inner)


def test_a_signature_from_another_key_is_refused():
    other = generate_identity_keypair()
    inner = _inner()
    inner["author_sig"] = b64url_encode(
        sign_ed25519(other.private_key, item_signing_bytes(inner))
    )
    assert not verify_signed_item_inner(inner)


@pytest.mark.parametrize("field", ["author_user_id", "author_pk", "author_username"])
def test_missing_identity_fields_are_refused(field):
    inner = _inner()
    inner.pop(field)
    assert not verify_signed_item_inner(inner)


@pytest.mark.parametrize("sig", [None, "", "!!not-b64!!"])
def test_a_missing_or_malformed_signature_is_refused(sig):
    inner = _inner()
    inner["author_sig"] = sig
    assert not verify_signed_item_inner(inner)


def test_an_unknown_suite_raises_never_falls_back():
    inner = _inner()
    inner["author_sig_suite"] = "mldsa65"
    with pytest.raises(UnsupportedItemAuthorSigSuite):
        verify_signed_item_inner(inner)


def test_an_anchor_derived_author_verifies_and_the_anchor_is_signed():
    anchor = "8b7d0d3e-uuid-anchor"
    user = derive_user_id(KP.public_key, anchor)
    inner = _inner(author_user_id=user, author_identity_anchor=anchor)
    assert inner["identity_anchor"] == anchor
    assert verify_signed_item_inner(inner)
    stripped = dict(inner)
    stripped.pop("identity_anchor")
    assert not verify_signed_item_inner(stripped)


def test_a_username_anchor_is_carried_as_absent():
    inner = _inner(author_identity_anchor="bob")
    assert "identity_anchor" not in inner
    assert verify_signed_item_inner(inner)


def test_the_domain_differs_from_the_post_author_signature():
    """A generic item signature never verifies as a post inner (and the
    reverse), so neither shape can be passed off as the other."""
    inner = _inner()
    assert item_signing_bytes(inner) != author_signing_bytes(inner)
    assert item_signing_bytes(inner).startswith(b"space-item-author:v1:")


def test_a_non_dict_is_refused():
    assert not verify_signed_item_inner("x")  # type: ignore[arg-type]
